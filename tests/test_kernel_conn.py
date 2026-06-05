import threading
import time

from jp import fsrpc
from jp._sim import FakeKernelWS
from jp.kernel_conn import KernelConn


def test_call_returns_matching_reply_and_buffers(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"abcdef")
    ws = FakeKernelWS(root=str(tmp_path))
    conn = KernelConn(ws, comm_id="c1", session="s")
    ws.open_comm(comm_id="c1", target="jp.fs")

    resp, buffers = conn.call(fsrpc.OP_READ, path="f.txt", offset=0, length=6)
    assert resp["ok"] is True
    assert buffers[0] == b"abcdef"


def test_call_increments_rid(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"x")
    ws = FakeKernelWS(root=str(tmp_path))
    conn = KernelConn(ws, comm_id="c1", session="s")
    ws.open_comm(comm_id="c1", target="jp.fs")
    r1, _ = conn.call(fsrpc.OP_PING)
    r2, _ = conn.call(fsrpc.OP_PING)
    assert r1["rid"] != r2["rid"]


def test_call_sends_buffers(tmp_path):
    ws = FakeKernelWS(root=str(tmp_path), writable=True)
    conn = KernelConn(ws, comm_id="c1", session="s")
    ws.open_comm(comm_id="c1", target="jp.fs")

    resp, _ = conn.call(fsrpc.OP_WRITE, path="x.txt", buffers=[b"data"])
    assert resp["ok"] is True
    assert (tmp_path / "x.txt").read_bytes() == b"data"


def test_call_raises_on_timeout_with_no_reply(tmp_path):
    import pytest

    from jp.kernel_conn import RpcTimeout

    ws = FakeKernelWS(root=str(tmp_path))
    conn = KernelConn(ws, comm_id="WRONG", session="s")  # comm never opened -> dropped
    ws.open_comm(comm_id="c1", target="jp.fs")
    with pytest.raises(RpcTimeout):
        conn.call(fsrpc.OP_PING, _max_polls=3)


def _fresh_ws(tmp_path, *, writable=False):
    ws = FakeKernelWS(root=str(tmp_path), writable=writable)
    ws.open_comm(comm_id="c1", target="jp.fs")
    return ws


def test_reconnect_on_dropped_connection(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"abcdef")
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _fresh_ws(tmp_path), "c1"

    ws = _fresh_ws(tmp_path)
    conn = KernelConn(ws, comm_id="c1", session="s", reconnect=factory, sleep_fn=lambda _s: None)

    # Simulate a culled kernel: the live socket reports closed.
    ws.closed = True

    # The call should transparently reconnect via the factory and succeed.
    resp, buffers = conn.call(fsrpc.OP_READ, path="f.txt", offset=0, length=6)
    assert resp["ok"] is True
    assert buffers[0] == b"abcdef"
    assert calls["n"] == 1  # the factory was invoked exactly once


def test_connection_lost_after_max_reconnects(tmp_path):
    import pytest

    from jp.kernel_conn import ConnectionLost

    def dead_factory():
        ws = FakeKernelWS(root=str(tmp_path))
        ws.closed = True  # every reconnect returns a dead socket
        return ws, "c1"

    ws = FakeKernelWS(root=str(tmp_path))
    ws.closed = True
    conn = KernelConn(
        ws,
        comm_id="c1",
        session="s",
        reconnect=dead_factory,
        max_reconnects=3,
        sleep_fn=lambda _s: None,
    )
    with pytest.raises(ConnectionLost):
        conn.call(fsrpc.OP_PING)


def test_keepalive_true_when_alive(tmp_path):
    ws = _fresh_ws(tmp_path)
    conn = KernelConn(ws, comm_id="c1", session="s")
    assert conn.keepalive() is True


class _YieldingInt(int):
    """An ``int`` whose ``+`` yields the GIL mid-operation.

    Production ``KernelConn`` does ``self._rid += 1; rid = self._rid`` -- two
    adjacent statements with no I/O between them, so under CPython's GIL they
    are effectively atomic and a lost ``_rid`` update almost never surfaces
    against the in-memory simulator. (The production segfault is the same kind
    of data race, but on a real SSL socket, which an in-memory backend cannot
    reproduce.) Installing this as ``conn._rid`` makes ``+ 1`` read the current
    value, yield so a sibling thread can run the same increment, then return the
    new value -- opening the exact window where two unserialized callers obtain
    the SAME rid. The result of ``+`` is again a ``_YieldingInt``, so the next
    increment keeps yielding; it behaves as a plain int everywhere else
    (hashing, dict keys, comparisons), so ``_pending`` lookups are unaffected.

    With ``call()`` holding its lock, only one thread is ever inside the
    increment, so the yields are harmless: every caller gets a unique rid.
    """

    def __add__(self, n):  # type: ignore[override]
        cur = int(self)
        time.sleep(1e-5)  # yield: let a sibling interleave here
        return _YieldingInt(cur + int(n))

    __radd__ = __add__


class _LockedWS:
    """Wraps FakeKernelWS so its own (non-production) outbox/agent state is not
    itself raced by the test harness.

    The point of the concurrency test is to prove that *KernelConn* serializes
    access -- not to exercise FakeKernelWS's thread-safety. So we guard the
    simulator's send/read with a lock HERE (in the test, never in production
    ``jp._sim``), and let the ``_YieldingInt`` rid counter drive the actual
    ``call()`` race. Without ``call()``'s lock, two callers grab the same rid
    and one pops the other's reply (cross-talk / KeyError / timeout); with it,
    every caller gets exactly its own reply.
    """

    def __init__(self, inner: FakeKernelWS) -> None:
        self._inner = inner
        self._lock = threading.Lock()

    def send_binary(self, blob: bytes) -> None:
        with self._lock:
            self._inner.send_binary(blob)

    def read_messages(self):
        with self._lock:
            return self._inner.read_messages()

    @property
    def closed(self) -> bool:
        return self._inner.closed


def test_concurrent_calls_are_serialized_and_correct(tmp_path):
    # Several files with distinct, recognisable contents.
    n_files = 8
    contents = {}
    for i in range(n_files):
        body = (f"file-{i}-".encode() * 200)[: 700 + i * 13]
        (tmp_path / f"f{i}.txt").write_bytes(body)
        contents[f"f{i}.txt"] = body

    inner = FakeKernelWS(root=str(tmp_path))
    inner.open_comm(comm_id="c1", target="jp.fs")
    ws = _LockedWS(inner)
    # No inter-poll sleep so a starved rid (the unserialized-race failure mode)
    # surfaces fast as RpcTimeout instead of spinning for the full wall-clock
    # poll budget.
    conn = KernelConn(ws, comm_id="c1", session="s", sleep_fn=lambda _s: None)
    # Drive the unserialized rid race deterministically (see _YieldingInt).
    conn._rid = _YieldingInt(0)

    n_threads = 16
    iters = 80
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []
    errors_lock = threading.Lock()

    def worker(tid: int) -> None:
        # Each thread owns one file; deterministic expected bytes per op.
        name = f"f{tid % n_files}.txt"
        want = contents[name]
        try:
            barrier.wait()  # release everyone at once -> maximal contention
            for j in range(iters):
                kind = j % 3
                if kind == 0:
                    resp, buffers = conn.call(
                        fsrpc.OP_READ, path=name, offset=0, length=len(want), _max_polls=500
                    )
                    assert resp["ok"] is True, resp
                    assert buffers[0] == want, (name, buffers[0][:20], want[:20])
                elif kind == 1:
                    resp, _ = conn.call(fsrpc.OP_STAT, path=name, _max_polls=500)
                    assert resp["ok"] is True, resp
                    assert resp["size"] == len(want), resp
                else:
                    resp, _ = conn.call(fsrpc.OP_PING, _max_polls=500)
                    assert resp.get("ok") is True, resp
        except Exception as exc:  # noqa: BLE001 -- collect, assert in main thread
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors[:5]

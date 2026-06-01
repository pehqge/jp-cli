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

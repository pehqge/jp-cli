"""End-to-end validation of jp's kernel transport against a REAL jupyter_server.

Everything else in the suite exercises jp against its own in-process simulator
(``jp._sim``). That proves jp is self-consistent, but it can NOT catch a wire
format that disagrees with the actual server -- the simulator reuses jp's own
``pack_ws_v1``/``unpack_ws_v1``, so a framing bug shared by both ends is
invisible. This module closes that gap two ways:

  1. ``test_wire_format_interop_with_jupyter_server`` -- a deterministic,
     server-less cross-check that imports jupyter_server's OWN v1 serializer
     (``serialize_msg_to_ws_v1`` / ``deserialize_msg_from_ws_v1``) and asserts
     jp interoperates byte-for-byte in both directions, including a raw binary
     buffer. This is the core correctness proof.

  2. ``test_connect_live_against_real_jupyter`` -- a full round-trip against a
     throwaway ``jupyter server`` launched on 127.0.0.1: create a kernel, inject
     the agent, open the ``jp.fs`` comm, then stat/read/readdir/write over the
     REAL kernel and verify writes land on disk and the kernel is culled on
     cleanup.

Both are gated: the module skips entirely unless jupyter_server is importable,
and the e2e test additionally requires the throwaway venv's ``jupyter`` binary.
Normal CI (no jupyter installed) skips the whole file at collection time.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

# Module-level guard: without jupyter_server there is nothing to validate
# against, so skip the whole file rather than fail collection.
pytest.importorskip("jupyter_server", reason="jupyter_server not installed")

from jupyter_server.services.kernels.connection.base import (  # noqa: E402
    deserialize_msg_from_ws_v1,
    serialize_msg_to_ws_v1,
)

from jp import kernel_proto as kp  # noqa: E402
from jp.api import Api  # noqa: E402
from jp.live_session import connect_live  # noqa: E402

# The throwaway venv the task provisions. The e2e test launches THIS jupyter.
_JUPYTER_BIN = "/tmp/jpreal/bin/jupyter"


# --------------------------------------------------------------------------- #
# 1. Wire-format cross-check (no server) -- the key correctness proof.
# --------------------------------------------------------------------------- #
@pytest.mark.realjupyter
def test_wire_format_interop_with_jupyter_server():
    """jp's v1 binary framing is byte-compatible with jupyter_server's own.

    Builds a realistic 5-part message ``[header, parent, metadata, content,
    buffer]`` (json dicts + a raw bytes buffer) and asserts BOTH directions:

      * jupyter serializes  -> jp.unpack_ws_v1 recovers channel + identical parts
      * jp.pack_ws_v1       -> jupyter deserializes to identical parts

    A failure here means jp would silently misframe messages to the real kernel
    (e.g. the offset-table sentinel mismatch that this test was written to
    catch: jupyter reconstructs each part as ``data[offsets[i]:offsets[i+1]]``
    and so requires a trailing end-of-message offset that jp must also emit).
    """
    header = json.dumps({"msg_type": "comm_msg", "version": "5.3"}).encode("utf-8")
    parent = json.dumps({}).encode("utf-8")
    metadata = json.dumps({}).encode("utf-8")
    content = json.dumps({"comm_id": "c1", "data": {"rid": 7, "op": "read"}}).encode("utf-8")
    buffer = bytes(range(256))  # raw binary -- must survive without base64

    # --- direction A: jupyter serializes, jp must unpack ------------------- #
    # serialize_msg_to_ws_v1(msg_or_list, channel, pack=None): with pack=None it
    # takes a ready list of byte parts. (With a `pack` callable it instead packs
    # a {header,parent_header,metadata,content} dict -- tested separately below.)
    parts4 = [header, parent, metadata, content]
    jwire = serialize_msg_to_ws_v1(parts4, "shell")
    channel, recovered = kp.unpack_ws_v1(jwire)
    assert channel == "shell"
    assert recovered == parts4, "jp.unpack_ws_v1 did not recover jupyter's parts"

    # The same, but driving jupyter's `pack` path from dicts (the form the
    # server uses for outgoing kernel replies). Must land on the same parts.
    msg_dict = {
        "header": {"msg_type": "comm_msg", "version": "5.3"},
        "parent_header": {},
        "metadata": {},
        "content": {"comm_id": "c1", "data": {"rid": 7, "op": "read"}},
    }
    pack = lambda obj: json.dumps(obj).encode("utf-8")  # noqa: E731
    jwire2 = serialize_msg_to_ws_v1(msg_dict, "iopub", pack)
    channel2, recovered2 = kp.unpack_ws_v1(jwire2)
    assert channel2 == "iopub"
    keys = ("header", "parent_header", "metadata", "content")
    assert recovered2 == [pack(msg_dict[k]) for k in keys]

    # --- direction B: jp serializes (incl. a buffer), jupyter must parse --- #
    parts5 = [header, parent, metadata, content, buffer]
    jpwire = kp.pack_ws_v1("shell", parts5)
    channel3, msg_list = deserialize_msg_from_ws_v1(jpwire)
    assert channel3 == "shell"
    assert msg_list == parts5, "jupyter_server did not recover jp's parts (incl. binary buffer)"
    # The raw buffer survived verbatim -- no base64, no corruption.
    assert msg_list[4] == buffer

    # --- jp self-roundtrip with a buffer, for good measure ----------------- #
    assert kp.unpack_ws_v1(kp.pack_ws_v1("control", parts5)) == ("control", parts5)


# --------------------------------------------------------------------------- #
# 2. Full end-to-end against a REAL running jupyter server.
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server(port: int, token: str, timeout: float = 30.0) -> None:
    """Poll GET /api/status with the token until it returns 200 (or time out)."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/status"
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        req = urllib.request.Request(url, headers={"Authorization": f"token {token}"})
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
            time.sleep(0.25)
    raise RuntimeError(f"jupyter server did not come up on :{port}: {last_err}")


@pytest.fixture
def real_jupyter(tmp_path):
    """Launch a throwaway jupyter server on 127.0.0.1; yield (api, root, token, port).

    Seeds the server root with a ``work/`` subdir (the agent jail) containing a
    text file and a nested binary file. Tears the server down and prints its log
    on failure.
    """
    if not os.path.exists(_JUPYTER_BIN):
        pytest.skip(f"throwaway jupyter not present at {_JUPYTER_BIN}")

    root = tmp_path / "srvroot"
    work = root / "work"
    nested = work / "sub"
    nested.mkdir(parents=True)
    (work / "hello.txt").write_bytes(b"hello real jupyter")  # 18 bytes
    binary_blob = bytes(range(256)) * 4  # 1024 bytes, every byte value present
    (nested / "data.bin").write_bytes(binary_blob)

    token = "jp-test-" + os.urandom(8).hex()
    port = _free_port()
    log = open(tmp_path / "jupyter.log", "w+b")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            _JUPYTER_BIN,
            "server",
            f"--ServerApp.token={token}",
            f"--ServerApp.port={port}",
            "--ServerApp.ip=127.0.0.1",
            f"--ServerApp.root_dir={root}",
            "--ServerApp.open_browser=False",
            "--ServerApp.disable_check_xsrf=True",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        try:
            _wait_for_server(port, token)
        except Exception:
            proc.terminate()
            log.flush()
            log.seek(0)
            sys.stderr.write("\n----- jupyter server log -----\n")
            sys.stderr.write(log.read().decode("utf-8", "replace"))
            raise
        # base_url has NO /api and uses http -> opt into the loopback escape hatch.
        api = Api(f"http://127.0.0.1:{port}", token=token, _allow_http_localhost=True)
        yield api, root, token, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def _list_kernels(port: int, token: str) -> list:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/kernels",
        headers={"Authorization": f"token {token}"},
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.mark.realjupyter
def test_connect_live_against_real_jupyter(real_jupyter):
    api, root, token, port = real_jupyter
    work = root / "work"
    binary_blob = bytes(range(256)) * 4

    # connect_live forwards `prefix` to build_bootstrap(root=prefix); the agent
    # does os.path.realpath(root). The kernel's cwd is the server root_dir, but
    # we hand it an ABSOLUTE jail path so it is unambiguous.
    agent_root = str(work)

    rfs, cleanup = connect_live(api, prefix=agent_root, token=token, writable=True)
    captured_kids: list[str] = []
    try:
        # --- liveness over the real kernel + comm ------------------------- #
        assert rfs.ping() is True

        # --- stat / read a text file -------------------------------------- #
        st = rfs.stat("hello.txt")
        assert st.type == "file"
        assert st.size == 18
        assert rfs.read("hello.txt", 0, 18) == b"hello real jupyter"

        # --- binary read at an offset: proves raw buffers survive a REAL ---
        # kernel round-trip with no base64 corruption.
        got = rfs.read("sub/data.bin", 100, 200)
        assert got == binary_blob[100:300], "binary bytes corrupted over real kernel"
        # And a full read recovers every byte value exactly.
        assert rfs.read("sub/data.bin", 0, len(binary_blob)) == binary_blob

        # --- readdir lists seeded entries --------------------------------- #
        names = {e.name for e in rfs.listdir("")}
        assert "hello.txt" in names
        assert "sub" in names

        # --- writable: the write must land on disk ------------------------ #
        rfs.write("created.txt", b"written via jp")
        on_disk = work / "created.txt"
        assert on_disk.read_bytes() == b"written via jp"

        # Capture the live kernel id (via the server) so we can assert culling.
        live = _list_kernels(port, token)
        assert len(live) >= 1
        captured_kids = [k["id"] for k in live]
    finally:
        cleanup()

    # --- cleanup() deleted the kernel: server reports none remaining ------ #
    remaining = _list_kernels(port, token)
    remaining_ids = {k["id"] for k in remaining}
    for kid in captured_kids:
        assert kid not in remaining_ids, f"kernel {kid} was not culled by cleanup()"
        assert api.kernel_alive(kid) is False

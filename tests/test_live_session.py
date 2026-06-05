"""Tests for jp.live_session.connect_live -- fully mocked, NO network.

A FakeApi stands in for jp.api.Api's kernel lifecycle; a ws_connect stub returns
the real FakeKernelWS simulator (which boots the real Agent on a temp dir). The
comm_open that connect_live sends naturally registers the comm in the simulator
(see jp._sim's comm_open handling), so a stat/read over the returned RemoteFS
proves the bootstrap+comm path end to end.
"""

from __future__ import annotations

import pytest

from jp._sim import FakeKernelWS
from jp.live_session import connect_live


class FakeApi:
    def __init__(self):
        self.deleted: list[str] = []
        self.created = 0

    def create_kernel(self, name="python3"):
        self.created += 1
        return "KID"

    def kernel_ws_url(self, kernel_id):
        return f"wss://h/api/kernels/{kernel_id}/channels"

    def delete_kernel(self, kernel_id):
        self.deleted.append(kernel_id)


def _make_ws_connect(tmp_path, *, writable=True):
    def ws_connect(url, headers):
        # The simulator auto-registers the comm when connect_live sends comm_open.
        return FakeKernelWS(root=str(tmp_path), writable=writable)

    return ws_connect


def test_connect_live_builds_working_remote_fs(tmp_path):
    (tmp_path / "somefile").write_bytes(b"hello")
    api = FakeApi()
    rfs, cleanup = connect_live(
        api, prefix=str(tmp_path), token="t", ws_connect=_make_ws_connect(tmp_path)
    )

    st = rfs.stat("somefile")
    assert st.type == "file"
    assert st.size == 5

    cleanup()
    assert api.deleted == ["KID"]


def test_connect_live_injects_bootstrap_and_opens_comm(tmp_path):
    (tmp_path / "data.txt").write_bytes(b"abcdef")
    api = FakeApi()
    rfs, cleanup = connect_live(
        api, prefix=str(tmp_path), token="t", ws_connect=_make_ws_connect(tmp_path)
    )

    # A read working proves the comm_open registered the comm in the simulator.
    assert rfs.read("data.txt", 0, 6) == b"abcdef"
    assert api.created == 1

    cleanup()
    assert "KID" in api.deleted


def test_kernel_deleted_when_connect_fails(tmp_path):
    """Issue #3: if the websocket connect (or any post-create step) fails, the
    just-created kernel must be deleted so it does not leak on a GPU."""
    api = FakeApi()

    def ws_connect(url, headers):
        raise RuntimeError("connect boom")

    with pytest.raises(RuntimeError, match="connect boom"):
        connect_live(api, prefix=str(tmp_path), token="t", ws_connect=ws_connect)

    assert api.created == 1
    assert api.deleted == ["KID"]  # the leaked kernel was cleaned up

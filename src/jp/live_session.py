"""Establish a live kernel-backed RemoteFS against a REAL Jupyter server.

Used only by the gated `jp live --live` path (blocked until certification). The
flow: create a kernel, open the v1-binary kernel websocket, inject the agent
bootstrap (one execute_request), open the `jp.fs` comm, and return a
reconnect-capable KernelConn wrapped in a RemoteFS, plus a cleanup() that deletes
the kernel. This module performs real network I/O, so it is exercised with a
mocked Api + fake-ws factory in tests, never against a real server here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from . import agent_loader
from . import kernel_proto as kp
from ._ws import WebSocket
from .kernel_conn import KernelConn
from .remote_fs import RemoteFS

# The kernel websocket negotiates this binary subprotocol; the v1 framing
# (pack_ws_v1/unpack_ws_v1) depends on the server agreeing to it.
KERNEL_SUBPROTOCOL = "v1.kernel.websocket.jupyter.org"


def _default_ws_connect(url: str, headers: dict[str, str]) -> Any:
    """Open the kernel websocket requesting the v1 binary subprotocol."""
    return WebSocket.connect(url, headers, subprotocol=KERNEL_SUBPROTOCOL)


def connect_live(
    api: Any,
    *,
    prefix: str,
    token: str,
    writable: bool = False,
    ws_connect: Callable[[str, dict[str, str]], Any] | None = None,
    timeout: float = 30.0,
) -> tuple[RemoteFS, Callable[[], None]]:
    """Create a kernel, inject the agent, open the comm; return (RemoteFS, cleanup).

    ``ws_connect`` is injectable for tests; it defaults to a thin wrapper over
    :meth:`jp._ws.WebSocket.connect` that requests the kernel binary
    subprotocol. ``writable`` is threaded into the agent bootstrap (gated -- the
    agent refuses every write unless True). ``cleanup()`` deletes the current
    kernel and closes the websocket.
    """
    connect = ws_connect or _default_ws_connect
    session = kp.new_id()
    state: dict[str, Any] = {"kid": None, "ws": None}

    def _establish() -> tuple[Any, str, str]:
        kid = api.create_kernel()
        url = api.kernel_ws_url(kid)
        ws = connect(url, {"Authorization": f"token {token}"})

        # 1) Inject the agent bootstrap as a one-shot execute_request.
        code = agent_loader.build_bootstrap(root=prefix, writable=writable)
        exec_parts = kp.build_execute_request(code, session=session, msg_id=kp.new_id())
        ws.send_binary(kp.pack_ws_v1("shell", exec_parts))

        # 2) Open the jp.fs comm so the agent's registered target instantiates it.
        comm_id = kp.new_id()
        open_parts = kp.build_comm_open(comm_id, "jp.fs", session=session, msg_id=kp.new_id())
        ws.send_binary(kp.pack_ws_v1("shell", open_parts))

        return ws, comm_id, kid

    ws, comm_id, kid = _establish()
    state["ws"], state["kid"] = ws, kid

    def _reconnect() -> tuple[Any, str]:
        # Best-effort delete of the dead kernel before standing up a fresh one.
        old = state.get("kid")
        if old:
            with contextlib.suppress(Exception):
                api.delete_kernel(old)
        new_ws, new_comm_id, new_kid = _establish()
        state["ws"], state["kid"] = new_ws, new_kid
        return new_ws, new_comm_id

    conn = KernelConn(ws, comm_id=comm_id, session=session, reconnect=_reconnect)
    remote_fs = RemoteFS(conn)

    def cleanup() -> None:
        kid_now = state.get("kid")
        if kid_now:
            with contextlib.suppress(Exception):
                api.delete_kernel(kid_now)
        ws_now = state.get("ws")
        if ws_now is not None:
            with contextlib.suppress(Exception):
                ws_now.close()

    return remote_fs, cleanup

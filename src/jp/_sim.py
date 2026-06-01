"""In-process Jupyter simulator for offline, network-free, SAFE testing.

The whole jp-live stack is exercised against this. The kernel/comm path is NOT
mocked at the dict level -- it runs the REAL agent (jp._agent.agentd.Agent)
against a temp directory, so the agent's logic and the binary framing are tested
end to end without touching any real server (Safety Charter rule 1).

This lives in the shipped package (not under tests/) so that `jp live --dry-run`
can import it as a self-test without depending on the test tree. It performs no
I/O of its own beyond the agent's reads under the given root.
"""

from __future__ import annotations

import json

from . import kernel_proto as kp
from ._agent.agentd import Agent


class FakeKernelWS:
    """Quacks like jp._ws.WebSocket but routes comm_msgs to a real Agent.

    Only the surface the client uses is implemented: ``send_binary`` (client ->
    server) and ``read_messages`` (server -> client), plus ``open_comm`` to
    register the agent's comm handler (the simulator's stand-in for running the
    bootstrap execute_request).
    """

    def __init__(self, root: str, *, writable: bool = False) -> None:
        self._agent = Agent(root, writable=writable)
        self._comm_ids: set[str] = set()
        self._outbox: list[bytes] = []
        self.closed = False

    def open_comm(self, *, comm_id: str, target: str) -> None:
        assert target == "jp.fs"
        self._comm_ids.add(comm_id)

    def send_binary(self, blob: bytes) -> None:
        _channel, blobs = kp.unpack_ws_v1(blob)
        header = json.loads(blobs[0])
        content = json.loads(blobs[3])
        # A real kernel instantiates the registered comm target on comm_open.
        # Detect it by msg_type (faithful) or, defensively, by the presence of a
        # ``target_name`` in the content, and auto-register that comm_id.
        if header.get("msg_type") == "comm_open" or "target_name" in content:
            comm_id = content.get("comm_id")
            if comm_id:
                self.open_comm(comm_id=comm_id, target=content.get("target_name", "jp.fs"))
            return
        comm_id = content.get("comm_id")
        if comm_id not in self._comm_ids:
            return  # unknown comm: silently dropped, like a real kernel
        req = content["data"]
        in_buffers = list(blobs[4:])  # client-supplied comm buffers (e.g. write data)
        resp, buffers = self._agent.handle(req, in_buffers)
        reply_parts, _ = kp.build_comm_msg(
            comm_id, resp, buffers=buffers, session="agent", msg_id=kp.new_id()
        )
        self._outbox.append(kp.pack_ws_v1("iopub", [*reply_parts, *buffers]))

    def send_text(self, text: str) -> None:  # bootstrap execute_request: no-op reply
        self._outbox.append(b"")

    def read_messages(self) -> list[bytes]:
        out = [m for m in self._outbox if m]
        self._outbox.clear()
        return out

    def close(self) -> None:
        self.closed = True

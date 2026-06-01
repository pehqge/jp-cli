"""In-process Jupyter simulator for offline, network-free, SAFE testing.

The whole jp-live stack is exercised against this. The kernel/comm path is NOT
mocked at the dict level -- it runs the REAL agent (jp._agent.agentd.Agent)
against a temp directory, so the agent's logic and the binary framing are tested
end to end without touching any real server (Safety Charter rule 1).
"""

from __future__ import annotations

import json

from jp import kernel_proto as kp
from jp._agent.agentd import Agent


class FakeKernelWS:
    """Quacks like jp._ws.WebSocket but routes comm_msgs to a real Agent.

    Only the surface the client uses is implemented: ``send_binary`` (client ->
    server) and ``read_messages`` (server -> client), plus ``open_comm`` to
    register the agent's comm handler (the simulator's stand-in for running the
    bootstrap execute_request).
    """

    def __init__(self, root: str) -> None:
        self._agent = Agent(root)
        self._comm_ids: set[str] = set()
        self._outbox: list[bytes] = []
        self.closed = False

    # test/bootstrap helper -- equivalent to the agent registering its target
    def open_comm(self, *, comm_id: str, target: str) -> None:
        assert target == "jp.fs"
        self._comm_ids.add(comm_id)

    # client -> server
    def send_binary(self, blob: bytes) -> None:
        channel, blobs = kp.unpack_ws_v1(blob)
        content = json.loads(blobs[3])
        comm_id = content.get("comm_id")
        if comm_id not in self._comm_ids:
            return  # unknown comm: silently dropped, like a real kernel
        req = content["data"]
        resp, buffers = self._agent.handle(req)
        reply_parts, _ = kp.build_comm_msg(
            comm_id, resp, buffers=buffers, session="agent", msg_id=kp.new_id()
        )
        self._outbox.append(kp.pack_ws_v1("iopub", [*reply_parts, *buffers]))

    def send_text(self, text: str) -> None:  # bootstrap execute_request: no-op reply
        self._outbox.append(b"")

    # server -> client
    def read_messages(self) -> list[bytes]:
        out = [m for m in self._outbox if m]
        self._outbox.clear()
        return out

    def close(self) -> None:
        self.closed = True

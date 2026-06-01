"""Request/reply RPC over a kernel WebSocket using the comm channel.

A :class:`KernelConn` wraps any object exposing ``send_binary`` /
``read_messages`` (the real :class:`jp._ws.WebSocket` in production, the
simulator's ``FakeKernelWS`` in tests). It serializes an ``fsrpc`` request into a
``comm_msg`` (binary v1 framing), then drains messages until the reply whose
``rid`` matches arrives, returning ``(response_dict, buffers)``.
"""

from __future__ import annotations

import json
from typing import Any

from . import fsrpc
from . import kernel_proto as kp


class RpcTimeout(Exception):
    """No matching reply arrived within the poll budget."""


class KernelConn:
    def __init__(self, ws: Any, *, comm_id: str, session: str) -> None:
        self._ws = ws
        self._comm_id = comm_id
        self._session = session
        self._rid = 0
        self._pending: dict[int, tuple[dict, list[bytes]]] = {}

    def call(
        self,
        op: str,
        *,
        _max_polls: int = 10000,
        buffers: list[bytes] | None = None,
        **fields: Any,
    ) -> tuple[dict, list[bytes]]:
        self._rid += 1
        rid = self._rid
        req = fsrpc.request(op, rid=rid, **fields)
        parts, _ = kp.build_comm_msg(
            self._comm_id, req, buffers=buffers, session=self._session, msg_id=kp.new_id()
        )
        self._ws.send_binary(kp.pack_ws_v1("shell", [*parts, *(buffers or [])]))

        for _ in range(_max_polls):
            if rid in self._pending:
                return self._pending.pop(rid)
            for raw in self._ws.read_messages():
                self._absorb(raw)
            if rid in self._pending:
                return self._pending.pop(rid)
        raise RpcTimeout(f"no reply for rid={rid} op={op}")

    def _absorb(self, raw: bytes) -> None:
        try:
            _channel, blobs = kp.unpack_ws_v1(raw)
        except kp.ProtocolError:
            return
        if len(blobs) < 4:
            return
        try:
            content = json.loads(blobs[3])
        except (ValueError, IndexError):
            return
        data = content.get("data")
        if not isinstance(data, dict) or "rid" not in data:
            return
        buffers = blobs[4:]
        self._pending[int(data["rid"])] = (data, buffers)

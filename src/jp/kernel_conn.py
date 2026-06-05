"""Request/reply RPC over a kernel WebSocket using the comm channel.

A :class:`KernelConn` wraps any object exposing ``send_binary`` /
``read_messages`` (the real :class:`jp._ws.WebSocket` in production, the
simulator's ``FakeKernelWS`` in tests). It serializes an ``fsrpc`` request into a
``comm_msg`` (binary v1 framing), then drains messages until the reply whose
``rid`` matches arrives, returning ``(response_dict, buffers)``.

Resilience: a kernel can be culled/dropped under us (idle timeout, server
restart). When constructed with a ``reconnect`` factory -- a zero-arg callable
returning a fresh ``(ws, comm_id)`` (a freshly connected kernel with the agent
re-injected and the comm re-opened) -- :class:`KernelConn` transparently
reconnects on a dropped socket and re-issues the SAME request. If reconnection
is exhausted it raises :class:`ConnectionLost`; the caller then FREEZES rather
than guess. Without a factory the behaviour is unchanged (raises
:class:`RpcTimeout`).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

from . import fsrpc
from . import kernel_proto as kp
from ._ws import WebSocketError


class RpcTimeout(Exception):
    """No matching reply arrived within the poll budget."""


class ConnectionLost(Exception):
    """The kernel websocket dropped and could not be re-established."""


class KernelConn:
    def __init__(
        self,
        ws: Any,
        *,
        comm_id: str,
        session: str,
        reconnect: Callable[[], tuple[Any, str]] | None = None,
        max_reconnects: int = 3,
        sleep_fn: Callable[[float], None] = time.sleep,
        poll_interval: float = 0.01,
    ) -> None:
        self._ws = ws
        self._comm_id = comm_id
        self._session = session
        self._reconnect = reconnect
        self._max_reconnects = max_reconnects
        self._sleep_fn = sleep_fn
        # Wait between idle polls so the poll budget spans wall-clock seconds, not
        # microseconds. A real kernel takes time to import the agent + register
        # the comm target + execute an op, so a sleepless busy-poll would burn
        # through _max_polls long before the first reply lands (it never does
        # against the in-process simulator, which replies on the first drain).
        self._poll_interval = poll_interval
        self._rid = 0
        self._pending: dict[int, tuple[dict, list[bytes]]] = {}
        # One KernelConn is shared by every WebDAV worker thread (the server is
        # a ThreadingHTTPServer) AND the live keepalive loop. They all funnel
        # through a SINGLE underlying SSL socket. Concurrent read/write on one
        # OpenSSL socket from multiple threads is undefined behaviour and
        # segfaults CPython; it also races _rid/_pending. This lock serializes
        # the ENTIRE request/reply cycle so exactly one thread touches the
        # socket (and the shared rid/pending state) at a time.
        self._lock = threading.Lock()

    def call(
        self,
        op: str,
        *,
        _max_polls: int = 10000,
        buffers: list[bytes] | None = None,
        **fields: Any,
    ) -> tuple[dict, list[bytes]]:
        # Serialize the whole cycle: only one thread may hold the socket (and
        # mutate _rid/_pending) at a time. keepalive() reaches call() WITHOUT
        # holding the lock, so it is serialized here too -- no double-locking,
        # no deadlock; the lock is taken once and released on return/raise.
        with self._lock:
            self._rid += 1
            rid = self._rid

            attempts = 0
            while True:
                try:
                    return self._attempt(rid, op, _max_polls=_max_polls, buffers=buffers, **fields)
                except (RpcTimeout, OSError, WebSocketError) as exc:
                    # A bare RpcTimeout with no reconnect factory is the legacy
                    # behaviour -- propagate it unchanged.
                    if self._reconnect is None:
                        if isinstance(exc, RpcTimeout):
                            raise
                        raise RpcTimeout(str(exc)) from exc
                    if attempts >= self._max_reconnects:
                        raise ConnectionLost(
                            f"kernel connection lost after {attempts} reconnect attempt(s) "
                            f"(rid={rid} op={op})"
                        ) from exc
                    attempts += 1
                    self._do_reconnect(attempts)

    def _attempt(
        self,
        rid: int,
        op: str,
        *,
        _max_polls: int,
        buffers: list[bytes] | None,
        **fields: Any,
    ) -> tuple[dict, list[bytes]]:
        req = fsrpc.request(op, rid=rid, **fields)
        parts, _ = kp.build_comm_msg(
            self._comm_id, req, buffers=buffers, session=self._session, msg_id=kp.new_id()
        )
        self._ws.send_binary(kp.pack_ws_v1("shell", [*parts, *(buffers or [])]))

        for _ in range(_max_polls):
            if rid in self._pending:
                return self._pending.pop(rid)
            if getattr(self._ws, "closed", False):
                raise WebSocketError("kernel websocket closed before a reply arrived")
            messages = self._ws.read_messages()
            for raw in messages:
                self._absorb(raw)
            if getattr(self._ws, "closed", False) and rid not in self._pending:
                raise WebSocketError("kernel websocket closed before a reply arrived")
            if rid in self._pending:
                return self._pending.pop(rid)
            # Nothing arrived this pass: yield briefly so the budget measures
            # real time, letting a slow kernel catch up instead of spinning hot.
            if not messages:
                self._sleep_fn(self._poll_interval)
        raise RpcTimeout(f"no reply for rid={rid} op={op}")

    def _do_reconnect(self, attempt: int) -> None:
        """Swap in a fresh ws+comm via the factory, with a short backoff.

        Clears any pending replies (they belonged to the dead socket). Raises
        :class:`ConnectionLost` if the factory itself fails.
        """
        assert self._reconnect is not None
        # Short, bounded backoff so a flapping server does not spin us hot.
        self._sleep_fn(min(0.1 * attempt, 1.0))
        try:
            ws, comm_id = self._reconnect()
        except Exception as exc:  # factory failed -> unrecoverable
            raise ConnectionLost(f"reconnect factory failed: {exc}") from exc
        self._ws = ws
        self._comm_id = comm_id
        self._pending.clear()

    def keepalive(self) -> bool:
        """Ping the agent to defeat idle-culling.

        Returns True if the kernel answered. On a dropped socket this triggers
        the same reconnect path; returns False only if reconnection is
        impossible (no factory, or the factory exhausted/failed).
        """
        try:
            resp, _ = self.call(fsrpc.OP_PING)
        except (RpcTimeout, ConnectionLost):
            return False
        return bool(resp.get("ok"))

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

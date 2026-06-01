"""Pure helpers for the Jupyter v5 kernel messaging protocol and the
``v1.kernel.websocket.jupyter.org`` binary WebSocket framing.

No I/O lives here -- every function is a pure transform so it is unit-tested
without a kernel or a socket. The kernel WebSocket (``/api/kernels/<id>/channels``)
is the Jupyter Server endpoint that relays these messages to/from the kernel; the
server performs ZMQ HMAC signing on our behalf, so this client never needs the
kernel's signing key (confirmed empirically, research/02 §4).
"""

from __future__ import annotations

import json
import os
from typing import Any

PROTOCOL_VERSION = "5.3"


def new_id() -> str:
    """A random hex id for sessions / msg_ids / comm_ids (no PII)."""
    return os.urandom(16).hex()


def build_header(msg_type: str, *, session: str, msg_id: str) -> dict[str, Any]:
    # ``date`` is a fixed, content-free placeholder: we never need wall-clock
    # time and avoiding it keeps this function pure/deterministic for tests.
    return {
        "msg_id": msg_id,
        "session": session,
        "username": "jp",
        "msg_type": msg_type,
        "version": PROTOCOL_VERSION,
        "date": "1970-01-01T00:00:00.000000Z",
    }


def _parts(msg_type: str, content: dict[str, Any], *, session: str, msg_id: str) -> list[bytes]:
    header = build_header(msg_type, session=session, msg_id=msg_id)
    return [
        json.dumps(header).encode("utf-8"),
        json.dumps({}).encode("utf-8"),  # parent_header
        json.dumps({}).encode("utf-8"),  # metadata
        json.dumps(content).encode("utf-8"),  # content
    ]


def build_execute_request(code: str, *, session: str, msg_id: str) -> list[bytes]:
    """The 4 JSON dict-parts for an ``execute_request`` (used to bootstrap)."""
    content = {
        "code": code,
        "silent": False,
        "store_history": False,
        "user_expressions": {},
        "allow_stdin": False,
        "stop_on_error": True,
    }
    return _parts("execute_request", content, session=session, msg_id=msg_id)


def build_comm_open(comm_id: str, target: str, *, session: str, msg_id: str) -> list[bytes]:
    content = {"comm_id": comm_id, "target_name": target, "data": {}}
    return _parts("comm_open", content, session=session, msg_id=msg_id)


def build_comm_msg(
    comm_id: str,
    data: dict[str, Any],
    *,
    buffers: list[bytes] | None = None,
    session: str,
    msg_id: str,
) -> tuple[list[bytes], int]:
    """Return (4 dict-parts, n_buffers). The framing layer appends the buffers."""
    content = {"comm_id": comm_id, "data": data}
    return _parts("comm_msg", content, session=session, msg_id=msg_id), len(buffers or [])

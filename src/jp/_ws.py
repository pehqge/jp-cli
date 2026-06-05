"""Minimal RFC 6455 WebSocket client -- stdlib only (socket + ssl).

This is a deliberately tiny, dependency-free client built for ONE job: drive a
Jupyter ``terminado`` terminal websocket (see ``commands/terminal.py``). It is
intentionally NOT a general-purpose websocket library.

It implements only what that job needs:

  * the opening handshake (random ``Sec-WebSocket-Key``; verify the ``101`` and
    the ``Sec-WebSocket-Accept`` digest);
  * client->server frames, which RFC 6455 §5.3 REQUIRES to be masked;
  * server->client frame parsing (FIN/opcode/7-16-64-bit length), reassembly of
    fragmented messages, transparent PING->PONG, and CLOSE handling.

The transport is split from the protocol so it is unit-testable without a
network: the pure helpers (:func:`accept_key`, :func:`mask`, :func:`encode_frame`,
:func:`parse_frame`) and the framing logic in :class:`WebSocket` are exercised
through an injected fake socket; nothing here opens a real connection in tests.

Security: this module never logs and never holds the token; the caller passes the
``Authorization`` header in via :func:`WebSocket.connect` and error strings carry
no secret. The caller is still responsible for routing any printed error through
``ui.redact``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import socket
import ssl
import struct
from typing import Any
from urllib.parse import urlsplit

# Magic GUID from RFC 6455 §1.3 used to derive the handshake accept value.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Frame opcodes (RFC 6455 §5.2).
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_READ_CHUNK = 65536


class WebSocketError(Exception):
    """Any handshake/protocol failure in this client."""


# --------------------------------------------------------------------------- #
# Pure protocol helpers (no I/O -- directly unit-tested)
# --------------------------------------------------------------------------- #
def accept_key(client_key: str) -> str:
    """Compute the server's expected ``Sec-WebSocket-Accept`` for ``client_key``.

    ``base64(sha1(key + GUID))`` per RFC 6455 §4.2.2. We verify the server echoed
    this exact value before trusting the connection.
    """
    digest = hashlib.sha1((client_key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def mask(payload: bytes, key: bytes) -> bytes:
    """XOR ``payload`` with the 4-byte ``key`` (RFC 6455 §5.3). Self-inverse."""
    return bytes(b ^ key[i & 3] for i, b in enumerate(payload))


def encode_frame(opcode: int, payload: bytes, mask_key: bytes) -> bytes:
    """Encode a single, final (FIN=1), MASKED client frame.

    Client frames MUST be masked. ``mask_key`` must be exactly 4 bytes (the
    caller supplies a fresh random key per frame).
    """
    if len(mask_key) != 4:
        raise ValueError("mask key must be 4 bytes")
    b0 = 0x80 | (opcode & 0x0F)  # FIN=1
    out = bytearray([b0])
    n = len(payload)
    if n < 126:
        out.append(0x80 | n)  # MASK bit set
    elif n < 65536:
        out.append(0x80 | 126)
        out += struct.pack("!H", n)
    else:
        out.append(0x80 | 127)
        out += struct.pack("!Q", n)
    out += mask_key
    out += mask(payload, mask_key)
    return bytes(out)


def parse_frame(buf: bytes | bytearray) -> tuple[bool, int, bytes, int] | None:
    """Parse one frame from the front of ``buf``.

    Returns ``(fin, opcode, payload, consumed)`` or ``None`` if ``buf`` does not
    yet hold a complete frame. Handles a (server-side, normally absent) mask bit
    defensively so a masked server frame is still decoded correctly.
    """
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    idx = 2
    if length == 126:
        if len(buf) < 4:
            return None
        length = struct.unpack("!H", buf[2:4])[0]
        idx = 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length = struct.unpack("!Q", buf[2:10])[0]
        idx = 10
    mask_key = b""
    if masked:
        if len(buf) < idx + 4:
            return None
        mask_key = bytes(buf[idx : idx + 4])
        idx += 4
    if len(buf) < idx + length:
        return None
    payload = bytes(buf[idx : idx + length])
    if masked:
        payload = mask(payload, mask_key)
    return fin, opcode, payload, idx + length


def build_handshake_request(
    path: str,
    host_header: str,
    key: str,
    *,
    headers: dict[str, str] | None = None,
    subprotocol: str | None = None,
) -> str:
    """Build the raw HTTP upgrade request line block (no I/O -- unit-testable).

    When ``subprotocol`` is set, a ``Sec-WebSocket-Protocol`` line is emitted
    (the kernel websocket needs ``v1.kernel.websocket.jupyter.org``).
    """
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if subprotocol:
        lines.append(f"Sec-WebSocket-Protocol: {subprotocol}")
    for hkey, hval in (headers or {}).items():
        lines.append(f"{hkey}: {hval}")
    return "\r\n".join(lines) + "\r\n\r\n"


# --------------------------------------------------------------------------- #
# WebSocket connection
# --------------------------------------------------------------------------- #
class WebSocket:
    """A connected websocket. Construct via :meth:`connect`.

    ``__init__`` takes an already-open, handshaked socket so tests can inject a
    fake transport without any network.
    """

    def __init__(self, sock: Any) -> None:
        self._sock = sock
        self._buf = bytearray()
        self._frag_op: int | None = None
        self._frag = bytearray()
        self.closed = False
        self.subprotocol: str | None = None

    @classmethod
    def connect(
        cls,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float = 30.0,
        ssl_context: ssl.SSLContext | None = None,
        subprotocol: str | None = None,
    ) -> WebSocket:
        """Open a TCP/TLS connection to ``url`` and perform the WS handshake.

        ``headers`` are extra request headers (the caller passes the
        ``Authorization`` header here -- we never put a token in the URL). TLS is
        always verified for ``wss://`` (a default ``ssl`` context).

        ``subprotocol``, when given, is sent as ``Sec-WebSocket-Protocol`` (and
        retained on the connection). The kernel websocket REQUIRES
        ``v1.kernel.websocket.jupyter.org`` -- its binary framing depends on the
        server agreeing to that subprotocol. Terminal usage passes ``None`` and
        is unaffected.
        """
        parts = urlsplit(url)
        secure = parts.scheme == "wss"
        if parts.scheme not in ("ws", "wss"):
            raise WebSocketError(f"unsupported scheme: {parts.scheme!r}")
        host = parts.hostname
        if not host:
            raise WebSocketError(f"missing host in url: {url!r}")
        port = parts.port or (443 if secure else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        raw = socket.create_connection((host, port), timeout=timeout)
        sock: Any
        try:
            if secure:
                if ssl_context is None:
                    ssl_context = ssl.create_default_context()
                    # Refuse the broken TLS 1.0/1.1 a default context still
                    # permits -- require TLS 1.2+ (every modern hub has it).
                    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
                sock = ssl_context.wrap_socket(raw, server_hostname=host)
            else:
                sock = raw
        except Exception:
            raw.close()
            raise

        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_header = f"{host}:{port}" if parts.port else host
            request = build_handshake_request(
                path, host_header, key, headers=headers, subprotocol=subprotocol
            )
            sock.sendall(request.encode("latin1"))

            resp = bytearray()
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(_READ_CHUNK)
                if not chunk:
                    raise WebSocketError("connection closed during handshake")
                resp += chunk
                if len(resp) > 65536:
                    raise WebSocketError("handshake response too large")

            header_blob, _, rest = bytes(resp).partition(b"\r\n\r\n")
            cls._verify_handshake(header_blob, key)
            sock.settimeout(None)  # blocking from here; reads are drained explicitly
            ws = cls(sock)
            ws.subprotocol = subprotocol  # kept: binary framing depends on it
            ws._buf += rest  # bytes after the headers may already be frame data
            return ws
        except Exception:
            with contextlib.suppress(OSError):
                sock.close()
            raise

    @staticmethod
    def _verify_handshake(header_blob: bytes, client_key: str) -> None:
        lines = header_blob.split(b"\r\n")
        status_line = lines[0].decode("latin1") if lines else ""
        fields = status_line.split(" ", 2)
        if len(fields) < 2 or fields[1] != "101":
            raise WebSocketError(f"websocket handshake failed: {status_line!r}")
        accept = ""
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"sec-websocket-accept":
                accept = value.strip().decode("latin1")
                break
        if accept != accept_key(client_key):
            raise WebSocketError("websocket handshake failed: bad Sec-WebSocket-Accept")

    # --- I/O -----------------------------------------------------------------
    def fileno(self) -> int:
        """Underlying fd, so the caller can ``select()`` on this socket."""
        return self._sock.fileno()

    def send_text(self, text: str) -> None:
        """Send a TEXT message (masked, as required for client frames)."""
        self._sock.sendall(encode_frame(OP_TEXT, text.encode("utf-8"), os.urandom(4)))

    def send_binary(self, data: bytes) -> None:
        """Send a BINARY message (masked, as required for client frames)."""
        self._sock.sendall(encode_frame(OP_BINARY, bytes(data), os.urandom(4)))

    def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        try:
            self._sock.sendall(encode_frame(opcode, payload, os.urandom(4)))
        except OSError:
            self.closed = True

    def read_messages(self) -> list[bytes]:
        """Drain all currently-available bytes and return complete data messages.

        Returns the payloads of every complete TEXT/BINARY message now available
        (reassembling fragments). Control frames are handled inline: PING is
        answered with PONG; CLOSE sets :attr:`closed`. An empty list with
        ``closed`` True means the peer hung up.
        """
        self._drain()
        messages: list[bytes] = []
        while True:
            parsed = parse_frame(self._buf)
            if parsed is None:
                break
            fin, opcode, payload, consumed = parsed
            del self._buf[:consumed]

            if opcode == OP_PING:
                self._send_control(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.closed = True
                self._send_control(OP_CLOSE)
                break
            if opcode == OP_CONT:
                self._frag += payload
                if fin:
                    messages.append(bytes(self._frag))
                    self._frag = bytearray()
                    self._frag_op = None
                continue
            # TEXT or BINARY
            if fin:
                messages.append(payload)
            else:
                self._frag_op = opcode
                self._frag = bytearray(payload)
        return messages

    def _drain(self) -> None:
        """Pull every byte currently readable into the buffer (non-blocking).

        Reading until the socket would block avoids a TLS-buffering pitfall: one
        ``recv`` can decrypt several frames at once, and ``select`` would not fire
        again for bytes already sitting in the SSL buffer.
        """
        self._sock.setblocking(False)
        try:
            while True:
                try:
                    chunk = self._sock.recv(_READ_CHUNK)
                except (BlockingIOError, ssl.SSLWantReadError):
                    break
                except (ssl.SSLError, OSError):
                    break
                if not chunk:
                    self.closed = True
                    break
                self._buf += chunk
        finally:
            with contextlib.suppress(OSError):
                self._sock.setblocking(True)

    def close(self) -> None:
        """Best-effort clean close: send CLOSE, then shut the socket."""
        if not self.closed:
            self._send_control(OP_CLOSE)
        self.closed = True
        with contextlib.suppress(OSError):
            self._sock.close()

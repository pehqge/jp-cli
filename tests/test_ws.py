"""Tests for jp._ws -- the hand-rolled RFC 6455 client.

Everything here runs against an injected fake socket; no test opens a real
connection. We exercise the protocol invariants that matter:

  * the handshake accept digest matches the RFC 6455 §1.3 example vector;
  * client frames are masked and length-encoded correctly at 7/16/64-bit widths;
  * masking is self-inverse;
  * server frames (incl. fragmented messages and control frames) parse correctly,
    PING is answered with PONG, and CLOSE flips ``closed``;
  * SSL/TLS buffering does not strand a second frame (drain reads all available).
"""

from __future__ import annotations

import errno
import struct

import pytest

from jp._ws import (
    OP_BINARY,
    OP_CLOSE,
    OP_CONT,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    WebSocket,
    accept_key,
    encode_frame,
    mask,
    parse_frame,
)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_accept_key_matches_rfc6455_example():
    # RFC 6455 §1.3: key "dGhlIHNhbXBsZSBub25jZQ==" -> this exact accept value.
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_mask_is_self_inverse():
    key = b"\x01\x02\x03\x04"
    data = bytes(range(200))
    assert mask(mask(data, key), key) == data


def test_encode_frame_small_payload_sets_fin_and_mask():
    frame = encode_frame(OP_TEXT, b"hi", b"\x00\x00\x00\x00")
    assert frame[0] == 0x81  # FIN + opcode 0x1
    assert frame[1] == 0x80 | 2  # MASK bit + length 2
    # zero mask key -> payload unchanged after the 4-byte key
    assert frame[2:6] == b"\x00\x00\x00\x00"
    assert frame[6:] == b"hi"


def test_encode_frame_16bit_and_64bit_lengths():
    mid = encode_frame(OP_BINARY, b"x" * 200, b"\x00\x00\x00\x00")
    assert mid[1] == 0x80 | 126
    assert struct.unpack("!H", mid[2:4])[0] == 200

    big = encode_frame(OP_BINARY, b"y" * 70000, b"\x00\x00\x00\x00")
    assert big[1] == 0x80 | 127
    assert struct.unpack("!Q", big[2:10])[0] == 70000


def test_encode_frame_rejects_bad_mask_key():
    with pytest.raises(ValueError):
        encode_frame(OP_TEXT, b"x", b"\x00")


def test_encode_then_parse_roundtrip_unmasks():
    # A client frame is masked; parse_frame must recover the original payload.
    frame = encode_frame(OP_TEXT, b"hello world", b"\xab\xcd\xef\x01")
    fin, opcode, payload, consumed = parse_frame(frame)
    assert fin is True
    assert opcode == OP_TEXT
    assert payload == b"hello world"
    assert consumed == len(frame)


def test_parse_frame_incomplete_returns_none():
    assert parse_frame(b"") is None
    assert parse_frame(b"\x81") is None  # header started, no length/payload
    # 16-bit length announced but payload truncated.
    assert parse_frame(b"\x81\xfe\x00\x10short") is None


def _server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Build an UNMASKED server->client frame (server frames are never masked)."""
    b0 = (0x80 if fin else 0) | opcode
    out = bytearray([b0])
    n = len(payload)
    if n < 126:
        out.append(n)
    elif n < 65536:
        out.append(126)
        out += struct.pack("!H", n)
    else:
        out.append(127)
        out += struct.pack("!Q", n)
    out += payload
    return bytes(out)


# --------------------------------------------------------------------------- #
# Fake socket + WebSocket framing
# --------------------------------------------------------------------------- #
class FakeSocket:
    """Scripted, in-memory stand-in for a (TLS) socket.

    ``recv`` returns chunks from a queue, then raises BlockingIOError to mimic a
    drained non-blocking socket. ``sendall`` records bytes for assertions.
    """

    def __init__(self, chunks: list[bytes], eof: bool = False):
        self._chunks = list(chunks)
        self._eof = eof  # True -> recv returns b"" (peer closed) once drained
        self.sent = bytearray()
        self.blocking = True
        self.closed = False

    def setblocking(self, flag):
        self.blocking = flag

    def recv(self, _n):
        if self._chunks:
            return self._chunks.pop(0)
        if self._eof:
            return b""  # EOF: a real socket returns b"" even when non-blocking
        if not self.blocking:
            raise BlockingIOError(errno.EAGAIN, "would block")
        return b""

    def sendall(self, data):
        self.sent += data

    def fileno(self):
        return -1

    def close(self):
        self.closed = True


def test_read_single_text_message():
    sock = FakeSocket([_server_frame(OP_TEXT, b'["stdout","ok"]')])
    ws = WebSocket(sock)
    assert ws.read_messages() == [b'["stdout","ok"]']
    assert ws.closed is False


def test_read_multiple_frames_in_one_chunk_are_all_returned():
    # TLS can hand us two frames at once; drain+parse must yield both, not strand
    # the second waiting for another select() wakeup.
    blob = _server_frame(OP_TEXT, b"one") + _server_frame(OP_TEXT, b"two")
    ws = WebSocket(FakeSocket([blob]))
    assert ws.read_messages() == [b"one", b"two"]


def test_fragmented_message_is_reassembled():
    frames = (
        _server_frame(OP_TEXT, b"par", fin=False)
        + _server_frame(OP_CONT, b"t1", fin=False)
        + _server_frame(OP_CONT, b"-end", fin=True)
    )
    ws = WebSocket(FakeSocket([frames]))
    assert ws.read_messages() == [b"part1-end"]


def test_ping_is_answered_with_pong_and_not_returned():
    sock = FakeSocket([_server_frame(OP_PING, b"pingpayload")])
    ws = WebSocket(sock)
    assert ws.read_messages() == []  # PING is not a data message
    # A masked PONG frame must have been sent back carrying the same payload.
    fin, opcode, payload, _ = parse_frame(bytes(sock.sent))
    assert opcode == OP_PONG
    assert payload == b"pingpayload"


def test_close_frame_sets_closed():
    ws = WebSocket(FakeSocket([_server_frame(OP_CLOSE, b"")]))
    assert ws.read_messages() == []
    assert ws.closed is True


def test_eof_recv_marks_closed():
    # A drained socket that returns b"" (EOF) means the peer hung up.
    ws = WebSocket(FakeSocket([], eof=True))
    assert ws.read_messages() == []
    assert ws.closed is True


def test_send_text_masks_and_roundtrips():
    sock = FakeSocket([])
    ws = WebSocket(sock)
    ws.send_text('["stdin","ls\\n"]')
    fin, opcode, payload, _ = parse_frame(bytes(sock.sent))
    assert opcode == OP_TEXT
    assert payload.decode() == '["stdin","ls\\n"]'
    # Masked: the 4-byte key sits right after the 2-byte header.
    assert sock.sent[1] & 0x80  # MASK bit set on a client frame

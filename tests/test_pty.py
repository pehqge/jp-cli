"""Tests for jp.pty: protocol helpers, the run-output scanner (marker swallow +
exit code), and the output-pump. No test touches a real tty, socket, or server.
"""

from __future__ import annotations

import json
import os

from jp import pty


# --------------------------------------------------------------------------- #
# Pure protocol helpers
# --------------------------------------------------------------------------- #
def test_stdin_message_format():
    assert pty.stdin_message(b"ls\n") == json.dumps(["stdin", "ls\n"])


def test_setsize_message_format():
    assert pty.setsize_message(40, 120) == json.dumps(["set_size", 40, 120])


def test_parse_server_message():
    assert pty.parse_server_message('["stdout","hi"]') == ("stdout", "hi")
    assert pty.parse_server_message('["disconnect",1]') == ("disconnect", 1)
    assert pty.parse_server_message("not json") == ("", None)
    assert pty.parse_server_message("[]") == ("", None)


# --------------------------------------------------------------------------- #
# RunScanner
# --------------------------------------------------------------------------- #
TKN = "deadbeef"
START = pty.start_marker(TKN)


def _end(rc: int) -> bytes:
    return pty.end_prefix(TKN) + str(rc).encode() + b"\x1b\\"


def test_scanner_swallows_prompt_then_streams():
    s = pty.RunScanner(TKN, clear_seq=b"<clr>")
    # The shell prompt + echoed command come first and must be discarded.
    assert s.feed(b"user@host:~$ printf ...; python3 x.py\r\n") == b""
    # Start marker -> clear the status line, then stream real output.
    out = s.feed(START + b"hello\n")
    assert out == b"<clr>hello\n"
    assert not s.done


def test_scanner_captures_exit_code_and_stops():
    s = pty.RunScanner(TKN)
    s.feed(START)
    out = s.feed(b"line1\n" + _end(0))
    assert out == b"line1\n"
    assert s.exit_code == 0 and s.done is True


def test_scanner_nonzero_exit_code():
    s = pty.RunScanner(TKN)
    s.feed(START)
    s.feed(b"boom\n" + _end(7))
    assert s.exit_code == 7


def test_scanner_handles_split_start_marker():
    s = pty.RunScanner(TKN, clear_seq=b"")
    half = len(START) // 2
    assert s.feed(b"prompt$ " + START[:half]) == b""  # partial start held back
    assert s.feed(START[half:] + b"out") == b"out"


def test_scanner_handles_split_end_marker():
    s = pty.RunScanner(TKN)
    s.feed(START)
    e = _end(3)
    half = len(e) // 2
    assert s.feed(b"data" + e[:half]) == b"data"  # partial end held back
    assert s.feed(e[half:]) == b""
    assert s.exit_code == 3


def test_scanner_byte_by_byte_never_leaks_marker():
    # Regression: feeding one byte at a time must still detect both markers and
    # never write any marker byte to the output (the holdback bug).
    s = pty.RunScanner(TKN)
    stream = b"prompt$ cmd\r\n" + START + b"hello\n" + _end(5)
    out = bytearray()
    for i in range(len(stream)):
        out += s.feed(stream[i : i + 1])
    assert bytes(out) == b"hello\n"
    assert s.exit_code == 5 and s.done is True
    assert b"\x1b_jp" not in bytes(out)


def test_scanner_default_clear_seq_empty():
    s = pty.RunScanner(TKN)
    assert s.feed(START + b"x") == b"x"  # no clear sequence emitted


# --------------------------------------------------------------------------- #
# _pump: server stdout -> fd, filtered by the scanner
# --------------------------------------------------------------------------- #
class _FakeWS:
    def __init__(self, message_batches):
        self._batches = list(message_batches)
        self.closed = False
        self.sent: list[str] = []
        self.close_calls = 0

    def read_messages(self):
        if not self._batches:
            self.closed = True
            return []
        return self._batches.pop(0)

    def fileno(self):
        return 0

    def send_text(self, text):
        self.sent.append(text)

    def close(self):
        self.close_calls += 1
        self.closed = True


def _stdout_msg(data: bytes) -> bytes:
    return json.dumps(["stdout", data.decode("utf-8", "replace")]).encode("utf-8")


def test_pump_streams_then_reports_done_on_end_marker():
    ws = _FakeWS([[_stdout_msg(START + b"hi\n")], [_stdout_msg(b"done\n" + _end(0))]])
    scanner = pty.RunScanner(TKN)
    r, w = os.pipe()
    try:
        assert pty._pump(ws, w, scanner) is False
        assert os.read(r, 1024) == b"hi\n"
        assert pty._pump(ws, w, scanner) is True
        assert os.read(r, 1024) == b"done\n"
        assert scanner.exit_code == 0
    finally:
        os.close(r)
        os.close(w)


def test_pump_stops_on_disconnect():
    ws = _FakeWS([[_stdout_msg(b"x"), b'["disconnect",1]', _stdout_msg(b"y")]])
    r, w = os.pipe()
    try:
        assert pty._pump(ws, w, None) is True
        assert os.read(r, 1024) == b"x"  # only bytes before disconnect
    finally:
        os.close(r)
        os.close(w)


class _FakeStdout:
    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd


def test_pump_noninteractive_sends_command_and_returns_code(monkeypatch):
    ws = _FakeWS([[_stdout_msg(START + b"out\n" + _end(5))]])
    scanner = pty.RunScanner(TKN)
    r, w = os.pipe()
    try:
        monkeypatch.setattr(pty.sys, "stdout", _FakeStdout(w))
        rc = pty.pump_noninteractive(ws, initial=["cmd"], scanner=scanner)
        assert rc == 5
        assert os.read(r, 1024) == b"out\n"
        assert ws.sent == ["cmd"]
    finally:
        os.close(r)
        os.close(w)

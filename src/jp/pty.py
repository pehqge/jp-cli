"""Terminado (Jupyter terminal) protocol helpers + a raw-mode PTY driver.

Self-contained on purpose: ``jp terminal`` keeps its own copy of the trivial
protocol helpers so this module can evolve (and be edited for ``jp run``)
without touching ``terminal.py`` while it is being changed elsewhere.

``jp run`` runs a one-shot program inside the terminado shell but must look
exactly like a local run -- no shell prompt, no echoed command, no remote
chrome. It achieves that with the same technique terminal emulators use for
shell integration (FinalTerm / iTerm2 OSC markers): the remote command brackets
the program's output with two unique control-sequence markers, and the client
swallows everything before the start marker (the prompt + the echoed command)
and stops at the end marker (which also carries the exit code). The marker bytes
only ever appear in real output -- the echoed command shows them as literal
backslash text -- so they can never be matched in the wrong place.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import signal
import sys

ESCAPE = 0x1D  # Ctrl-]: force-disconnect even if the remote is wedged.

try:
    import termios
    import tty

    HAS_PTY = True
except ImportError:  # pragma: no cover - Windows
    HAS_PTY = False


# --------------------------------------------------------------------------- #
# Pure protocol helpers (terminado JSON message format)
# --------------------------------------------------------------------------- #
def stdin_message(data: bytes) -> str:
    return json.dumps(["stdin", data.decode("utf-8", "replace")])


def setsize_message(rows: int, cols: int) -> str:
    return json.dumps(["set_size", int(rows), int(cols)])


def parse_server_message(text: str) -> tuple[str, object]:
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return ("", None)
    if isinstance(msg, list) and msg:
        payload = msg[1] if len(msg) > 1 else None
        return (str(msg[0]), payload)
    return ("", None)


def write_all(fd: int, data: bytes) -> None:
    while data:
        written = os.write(fd, data)
        data = data[written:]


def send_winsize(ws, fd: int) -> None:
    try:
        size = os.get_terminal_size(fd)
        rows, cols = size.lines, size.columns
    except OSError:
        rows, cols = 24, 80
    ws.send_text(setsize_message(rows, cols))


# --------------------------------------------------------------------------- #
# Markers: the remote command emits these around the program's output.
# Built with a per-run random token so they cannot collide with real output.
# APC form (ESC _ ... ESC \) is ignored by terminals if one ever leaks.
# --------------------------------------------------------------------------- #
def start_marker(token: str) -> bytes:
    return b"\x1b_jp" + token.encode() + b"C\x1b\\"


def end_prefix(token: str) -> bytes:
    return b"\x1b_jp" + token.encode() + b"D"


def end_regex(token: str) -> re.Pattern[bytes]:
    return re.compile(rb"\x1b_jp" + re.escape(token.encode()) + rb"D(-?\d+)\x1b\\")


class RunScanner:
    """Filters the terminado output stream for a one-shot ``jp run``.

    Before the start marker (PRE): everything is discarded -- that is the shell
    prompt plus the echoed command line. From the start marker on (STREAM):
    bytes are passed through verbatim until the end marker, which carries the
    exit code. ``clear_seq`` (emitted once on transition) erases the transient
    connecting status line on an interactive terminal.
    """

    def __init__(self, token: str, *, clear_seq: bytes = b"") -> None:
        self._start = start_marker(token)
        self._end_prefix = end_prefix(token)
        self._end_re = end_regex(token)
        self._clear_seq = clear_seq
        self.exit_code: int | None = None
        self.done = False
        self._streaming = False
        self._buf = b""

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        out = bytearray()
        if not self._streaming:
            i = self._buf.find(self._start)
            if i < 0:
                # Discard everything but a tail that could be a partial start.
                self._buf = _suffix_prefix_of(self._buf, self._start)
                return b""
            self._buf = self._buf[i + len(self._start) :]
            self._streaming = True
            out += self._clear_seq
        m = self._end_re.search(self._buf)
        if m:
            out += self._buf[: m.start()]
            self.exit_code = int(m.group(1))
            self.done = True
            self._buf = b""
            return bytes(out)
        # Emit everything except a trailing partial end marker.
        hold = _end_holdback(self._buf, self._end_prefix)
        if hold:
            out += self._buf[:-hold]
            self._buf = self._buf[-hold:]
        else:
            out += self._buf
            self._buf = b""
        return bytes(out)


def _suffix_prefix_of(buf: bytes, marker: bytes) -> bytes:
    """Return the longest suffix of ``buf`` that is a prefix of ``marker``."""
    maxk = min(len(buf), len(marker) - 1)
    for k in range(maxk, 0, -1):
        if buf[-k:] == marker[:k]:
            return buf[-k:]
    return b""


_END_TAIL = re.compile(rb"-?[0-9]*\x1b?\Z")  # exit digits + the pending terminator ESC


def _end_holdback(buf: bytes, prefix: bytes) -> int:
    """How many trailing bytes could be an incomplete end marker.

    Either a suffix that is a prefix of ``ESC_jp<token>D`` (marker not started
    yet), or that fixed part already followed by the exit digits and the pending
    terminator ESC (waiting only for the final backslash). The whole marker is
    held back across feeds so a partial is never written to the screen.
    """
    hold = 0
    maxk = min(len(buf), len(prefix) - 1)
    for k in range(maxk, 0, -1):
        if buf[-k:] == prefix[:k]:
            hold = k
            break
    i = buf.rfind(prefix)
    if i != -1 and _END_TAIL.fullmatch(buf[i + len(prefix) :]):
        hold = max(hold, len(buf) - i)
    return hold


# --------------------------------------------------------------------------- #
# Output pump (shared) + drivers
# --------------------------------------------------------------------------- #
def _pump(ws, stdout_fd: int, scanner: RunScanner | None) -> bool:
    """Write pending server stdout to the fd. Returns True when the session
    should end (disconnect, socket closed, or the scanner saw the end marker)."""
    for raw in ws.read_messages():
        kind, payload = parse_server_message(raw.decode("utf-8", "replace"))
        if kind == "stdout" and isinstance(payload, str):
            data = payload.encode("utf-8")
            if scanner is not None:
                data = scanner.feed(data)
            write_all(stdout_fd, data)
            if scanner is not None and scanner.done:
                return True
        elif kind == "disconnect":
            return True
    return ws.closed


def pump_noninteractive(ws, *, initial: list[str], scanner: RunScanner) -> int | None:
    """Stream-only driver for a non-tty stdout (e.g. ``jp run x.py > out``):
    send the command, pump output through the scanner, no raw mode, no stdin."""
    for msg in initial:
        ws.send_text(msg)
    stdout_fd = sys.stdout.fileno()
    while not ws.closed:
        if _pump(ws, stdout_fd, scanner):
            break
    return scanner.exit_code


def drive_pty(
    ws, *, initial: list[str] | None = None, scanner: RunScanner | None = None
) -> int | None:
    """Raw-mode local proxy. Sends each message in ``initial`` after entering raw
    mode, then proxies stdin<->ws until exit. With a ``scanner`` the loop ends on
    the end marker and the captured exit code is returned; otherwise None."""
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    saved = termios.tcgetattr(stdin_fd)

    pipe_r, pipe_w = os.pipe()
    os.set_blocking(pipe_w, False)
    old_wakeup = signal.set_wakeup_fd(pipe_w)
    old_winch = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGWINCH, lambda *_: None)

    try:
        tty.setraw(stdin_fd)
        send_winsize(ws, stdout_fd)
        for msg in initial or []:
            ws.send_text(msg)
        if not _pump(ws, stdout_fd, scanner):
            _loop(ws, stdin_fd, stdout_fd, pipe_r, scanner)
    finally:
        signal.signal(signal.SIGWINCH, old_winch)
        signal.set_wakeup_fd(old_wakeup)
        os.close(pipe_r)
        os.close(pipe_w)
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
    return scanner.exit_code if scanner is not None else None


def _loop(ws, stdin_fd: int, stdout_fd: int, wakeup_fd: int, scanner: RunScanner | None) -> None:
    while not ws.closed:
        try:
            readable, _, _ = select.select([stdin_fd, ws.fileno(), wakeup_fd], [], [])
        except OSError:
            break
        if wakeup_fd in readable:
            with contextlib.suppress(OSError):
                os.read(wakeup_fd, 4096)
            send_winsize(ws, stdout_fd)
        if stdin_fd in readable:
            try:
                data = os.read(stdin_fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            ws.send_text(stdin_message(data))
        if ws.fileno() in readable and _pump(ws, stdout_fd, scanner):
            break

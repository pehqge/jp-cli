# `jp run` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `jp run <file> [args…]` that executes a local script on the remote JupyterHub in the user's current mapped folder, without pushing the script — streaming output live and propagating the exit code.

**Architecture:** Reuse the terminado PTY transport (`POST /api/terminals` → websocket) that `jp terminal` uses. The script source is delivered through the PTY as a here-doc that writes a hidden, random-named temp file *in the current mapped folder* (so `sys.path[0]`/`__file__`/relative `open()` match a local run), executes it, deletes only that file, and emits an exit sentinel. A new self-contained `src/jp/pty.py` holds the protocol helpers + an orchestrated PTY driver; `terminal.py` is deliberately left untouched to avoid merge conflicts with concurrent Windows work.

**Tech Stack:** Python 3.10+, stdlib only (`argparse`, `os`, `secrets`, `shlex`, `re`, `select`, `termios`/`tty`, `signal`), pytest.

---

## Design notes the engineer must respect

- **Never overwrite/delete user data.** The temp file name is `.<stem>.jp-run-<32 hex><ext>` (128 bits from `secrets.token_hex(16)`). Three independent guards: (1) 128-bit random, (2) remote `[ -e "$f" ]` pre-check → `__JP_COLLIDE__` sentinel → CLI retries with a new name, (3) `set -C` noclobber on the redirection. Cleanup is `rm -f -- "$f"` of exactly that one file. **No directory is ever created or removed.**
- **Portability:** emit POSIX `sh` only. Use a raw quoted here-doc with a random delimiter (NOT `base64 -d`, whose flag differs on BSD/macOS). Normalize the local source CRLF→LF before sending.
- **Parity:** the terminal is created with `cwd=<remote_cwd>` and the temp file is written relative to it, so it runs exactly as if launched locally in that folder.
- **Shebang honored** via `chmod +x` + `./"$f"`; files without a shebang use an extension→interpreter map; `--as` overrides both.
- **Do NOT edit `src/jp/commands/terminal.py`.** Duplicate the 4 trivial protocol helpers into `pty.py` instead (a concurrent agent is editing `terminal.py` for Windows).

## File structure

- **Create `src/jp/pty.py`** — terminado protocol helpers (`stdin_message`, `setsize_message`, `parse_server_message`, `write_all`, `send_winsize`), an `ExitScanner` that strips the exit/collide sentinels from the output stream and captures the exit code, and `drive_pty(ws, *, initial, scanner)` — a raw-mode local proxy that optionally sends an initial payload and stops when the scanner sees the exit sentinel.
- **Create `src/jp/commands/run.py`** — pure helpers (`resolve_runner`, `temp_name`, `normalize_source`, `build_remote_script`) plus `run(args)` orchestration.
- **Modify `src/jp/commands/__init__.py`** — register `run` in the imports and in `ALL`.
- **Create `tests/test_pty.py`** and **`tests/test_run.py`**.

---

## Task 1: Protocol helpers + ExitScanner in `pty.py`

**Files:**
- Create: `src/jp/pty.py`
- Test: `tests/test_pty.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pty.py
from __future__ import annotations

import json

from jp import pty


def test_stdin_message_format():
    assert pty.stdin_message(b"ls\n") == json.dumps(["stdin", "ls\n"])


def test_setsize_message_format():
    assert pty.setsize_message(40, 120) == json.dumps(["set_size", 40, 120])


def test_parse_server_message():
    assert pty.parse_server_message('["stdout","hi"]') == ("stdout", "hi")
    assert pty.parse_server_message('["disconnect",1]') == ("disconnect", 1)
    assert pty.parse_server_message("not json") == ("", None)
    assert pty.parse_server_message("[]") == ("", None)


def test_exit_scanner_strips_sentinel_and_captures_code():
    s = pty.ExitScanner()
    assert s.feed(b"hello\n") == b"hello\n"
    out = s.feed(b"world\n__JP_EXIT__0__\n")
    assert out == b"world\n"          # sentinel + trailing removed
    assert s.exit_code == 0
    assert s.done is True


def test_exit_scanner_nonzero_code():
    s = pty.ExitScanner()
    s.feed(b"boom\n__JP_EXIT__7__\n")
    assert s.exit_code == 7


def test_exit_scanner_handles_split_sentinel():
    s = pty.ExitScanner()
    assert s.feed(b"out__JP_EX") == b"out"     # holds back possible partial
    assert s.feed(b"IT__3__\n") == b""
    assert s.exit_code == 3


def test_exit_scanner_strips_collide_marker():
    s = pty.ExitScanner()
    out = s.feed(b"__JP_COLLIDE__\n")
    assert out == b""
    assert s.collided is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pty.py -q`
Expected: FAIL (`ModuleNotFoundError: jp.pty`).

- [ ] **Step 3: Implement `pty.py` helpers + ExitScanner**

```python
# src/jp/pty.py
"""Shared terminado (Jupyter terminal) protocol helpers + a raw-mode PTY driver.

Self-contained on purpose: ``jp terminal`` keeps its own copy of the trivial
protocol helpers so this module can evolve (and be edited for ``jp run``)
without touching ``terminal.py`` while it is being changed elsewhere.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import signal
import sys

from ._ws import WebSocket  # noqa: F401  (re-exported type hint convenience)

# Local escape: Ctrl-] forces a disconnect even if the remote is wedged.
ESCAPE = 0x1D

try:
    import termios
    import tty

    HAS_PTY = True
except ImportError:  # pragma: no cover - Windows
    HAS_PTY = False


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


_EXIT_RE = re.compile(rb"\n?__JP_EXIT__(\d+)__\n?")
_COLLIDE = b"__JP_COLLIDE__"
# Longest prefix we might need to hold back so a sentinel split across two
# feeds is not written half-way to the screen.
_MAX_HOLD = len(b"\n__JP_EXIT__") + 20


class ExitScanner:
    """Filters the output stream: removes the exit/collide sentinels and records
    the remote exit code. ``feed`` returns the bytes that are safe to display."""

    def __init__(self) -> None:
        self.exit_code: int | None = None
        self.collided = False
        self.done = False
        self._buf = b""

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        out = bytearray()
        # Drain complete COLLIDE markers first (they carry no trailing newline
        # guarantee, so match the bare token).
        while True:
            i = self._buf.find(_COLLIDE)
            if i < 0:
                break
            out += self._buf[:i]
            self.collided = True
            self._buf = self._buf[i + len(_COLLIDE) :]
        m = _EXIT_RE.search(self._buf)
        if m:
            out += self._buf[: m.start()]
            self.exit_code = int(m.group(1))
            self.done = True
            self._buf = b""
            return bytes(out)
        # Hold back a tail that could be the start of a sentinel.
        if len(self._buf) > _MAX_HOLD:
            cut = len(self._buf) - _MAX_HOLD
            out += self._buf[:cut]
            self._buf = self._buf[cut:]
        return bytes(out)

    def flush(self) -> bytes:
        out, self._buf = self._buf, b""
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_pty.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jp/pty.py tests/test_pty.py
git commit -m "feat(pty): shared terminado protocol helpers + exit-sentinel scanner"
```

---

## Task 2: `drive_pty` raw-mode driver in `pty.py`

**Files:**
- Modify: `src/jp/pty.py`
- Test: `tests/test_pty.py`

The driver mirrors `terminal._run_pty` but adds: an `initial` list of stdin messages sent after raw mode is set, and an optional `scanner` that filters output and ends the loop on the exit sentinel. We test the **output-pump** logic with a fake ws + a pipe (exactly like `test_terminal.py::test_pump_output_*`), not the raw tty.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_pty.py
import os as _os


class _FakeWS:
    def __init__(self, message_batches):
        self._batches = list(message_batches)
        self.closed = False
        self.sent: list[str] = []

    def read_messages(self):
        if not self._batches:
            self.closed = True
            return []
        return self._batches.pop(0)

    def fileno(self):
        return 0

    def send_text(self, text):
        self.sent.append(text)


def test_pump_filters_sentinel_to_fd_and_reports_done():
    # Two batches: a line, then a line ending with the exit sentinel.
    ws = _FakeWS([[b'["stdout","hi\\n"]'], [b'["stdout","done\\n__JP_EXIT__0__\\n"]']])
    scanner = pty.ExitScanner()
    r, w = _os.pipe()
    try:
        # First pump writes "hi\n", not done yet.
        assert pty._pump(ws, w, scanner) is False
        assert _os.read(r, 1024) == b"hi\n"
        # Second pump writes "done\n", strips sentinel, signals done.
        assert pty._pump(ws, w, scanner) is True
        assert _os.read(r, 1024) == b"done\n"
        assert scanner.exit_code == 0
    finally:
        _os.close(r)
        _os.close(w)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pty.py::test_pump_filters_sentinel_to_fd_and_reports_done -q`
Expected: FAIL (`AttributeError: module 'jp.pty' has no attribute '_pump'`).

- [ ] **Step 3: Implement `_pump` and `drive_pty`**

```python
# append to src/jp/pty.py

def _pump(ws, stdout_fd: int, scanner: "ExitScanner | None") -> bool:
    """Write pending server stdout to the fd. Returns True when the session
    should end (disconnect, socket closed, or scanner saw the exit sentinel)."""
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


def drive_pty(ws, *, initial: list[str] | None = None, scanner: "ExitScanner | None" = None) -> int | None:
    """Raw-mode local proxy. Sends each message in ``initial`` after entering raw
    mode, then proxies stdin<->ws until exit. With a ``scanner`` the loop ends on
    the exit sentinel and the captured exit code is returned; otherwise None."""
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


def _loop(ws, stdin_fd: int, stdout_fd: int, wakeup_fd: int, scanner: "ExitScanner | None") -> None:
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
            if not data or ESCAPE in data:
                break
            ws.send_text(stdin_message(data))
        if ws.fileno() in readable and _pump(ws, stdout_fd, scanner):
            break
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_pty.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jp/pty.py tests/test_pty.py
git commit -m "feat(pty): raw-mode drive_pty with initial payload + scanner stop"
```

---

## Task 3: Pure helpers in `run.py` (runner, temp name, normalize, script)

**Files:**
- Create: `src/jp/commands/run.py`
- Test: `tests/test_run.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_run.py
from __future__ import annotations

import shlex

import pytest

from jp.commands import run
from jp.errors import UsageError


def test_resolve_runner_as_override_wins():
    r = run.resolve_runner("x", "#!/usr/bin/env python3", as_override="bash")
    assert r == run.Runner(use_shebang=False, interp="bash")


def test_resolve_runner_shebang():
    r = run.resolve_runner("script", "#!/bin/sh\n", as_override="")
    assert r == run.Runner(use_shebang=True, interp="")


def test_resolve_runner_extension_map():
    assert run.resolve_runner("a.py", "import os", "").interp == "python3"
    assert run.resolve_runner("a.sh", "echo hi", "").interp == "bash"
    assert run.resolve_runner("a.js", "1", "").interp == "node"


def test_resolve_runner_unknown_extension_errors():
    with pytest.raises(UsageError, match="--as"):
        run.resolve_runner("data.bin", "x", "")


def test_temp_name_shape():
    name = run.temp_name("train.py", "a" * 32)
    assert name == ".train.jp-run-" + "a" * 32 + ".py"


def test_temp_name_no_extension():
    assert run.temp_name("script", "b" * 32) == ".script.jp-run-" + "b" * 32


def test_temp_name_random_differs():
    import os as _os

    n1 = run.temp_name("x.py", _os.urandom(16).hex())
    n2 = run.temp_name("x.py", _os.urandom(16).hex())
    assert n1 != n2 and n1.startswith(".x.jp-run-") and n1.endswith(".py")


def test_normalize_source_crlf():
    assert run.normalize_source("a\r\nb\rc") == "a\nb\nc\n"
    assert run.normalize_source("ok\n") == "ok\n"


def test_build_remote_script_interp_path():
    src = "print('hi')\n"
    s = run.build_remote_script(".x.jp-run-AA.py", src,
                                run.Runner(False, "python3"), ["--n", "5"], "JP_EOF_DEAD")
    assert "set -C" in s and "set +C" in s
    assert '[ -e "$f" ]' in s
    assert "__JP_COLLIDE__" in s
    assert "<<'JP_EOF_DEAD'" in s
    assert src in s
    assert "rm -f -- \"$f\"" in s
    assert "__JP_EXIT__%d__" in s
    # interpreter + shlex-quoted args
    assert 'python3 "$f" --n 5' in s
    assert "chmod +x" not in s


def test_build_remote_script_shebang_path():
    s = run.build_remote_script(".x.jp-run-BB", "#!/bin/sh\necho hi\n",
                                run.Runner(True, ""), [], "JP_EOF_LIVE")
    assert 'chmod +x -- "$f" && "./$f"' in s


def test_build_remote_script_quotes_args():
    s = run.build_remote_script(".x.jp-run-CC.py", "x\n",
                                run.Runner(False, "python3"), ["a b", "$X"], "JP_EOF_Q")
    assert shlex.quote("a b") in s and shlex.quote("$X") in s


def test_pick_delimiter_avoids_collision():
    # If the candidate appears in the source, a different one is returned.
    src = "JP_EOF_" + "0" * 16 + "\n"
    delim = run.pick_delimiter(src, seed_hex="0" * 16)
    assert delim not in src
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_run.py -q`
Expected: FAIL (`ModuleNotFoundError` / missing attrs).

- [ ] **Step 3: Implement the pure helpers**

```python
# src/jp/commands/run.py  (pure-helper portion; run() added in Task 4)
"""``jp run`` -- run a LOCAL script on the remote in the current mapped folder.

The source is delivered through an ephemeral terminado PTY (never the Contents
API): a hidden, random-named temp file is written *in the mapped folder* so the
script runs with exactly the same cwd/sys.path/__file__ as a local run, then it
is executed and removed. Only that one file is ever created or deleted.
"""

from __future__ import annotations

import argparse
import secrets
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from ..errors import UsageError

EXT_INTERP = {
    ".py": "python3",
    ".sh": "bash",
    ".bash": "bash",
    ".js": "node",
    ".mjs": "node",
    ".rb": "ruby",
    ".pl": "perl",
    ".r": "Rscript",
    ".lua": "lua",
    ".php": "php",
}


@dataclass(frozen=True)
class Runner:
    use_shebang: bool
    interp: str  # "" when use_shebang is True


def resolve_runner(filename: str, first_line: str, as_override: str) -> Runner:
    if as_override:
        return Runner(False, as_override)
    if first_line.startswith("#!"):
        return Runner(True, "")
    ext = PurePosixPath(filename).suffix.lower()
    interp = EXT_INTERP.get(ext)
    if not interp:
        raise UsageError(
            f"don't know how to run {filename!r}; pass --as <interpreter> "
            f"(e.g. --as python3)"
        )
    return Runner(False, interp)


def temp_name(real_name: str, rand_hex: str) -> str:
    p = PurePosixPath(real_name)
    return f".{p.stem}.jp-run-{rand_hex}{p.suffix}"


def normalize_source(text: str) -> str:
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    return out if out.endswith("\n") else out + "\n"


def pick_delimiter(source: str, seed_hex: str = "") -> str:
    cand = "JP_EOF_" + (seed_hex or secrets.token_hex(8))
    while cand in source:
        cand = "JP_EOF_" + secrets.token_hex(8)
    return cand


def build_remote_script(
    tmp_name: str, source: str, runner: Runner, args: list[str], delim: str
) -> str:
    q_args = " ".join(shlex.quote(a) for a in args)
    tail = f" {q_args}" if q_args else ""
    if runner.use_shebang:
        exec_line = f'chmod +x -- "$f" && "./$f"{tail}'
    else:
        exec_line = f'{shlex.quote(runner.interp)} "$f"{tail}'
    body = source if source.endswith("\n") else source + "\n"
    return (
        f"f={shlex.quote(tmp_name)}\n"
        f'if [ -e "$f" ]; then printf \'\\n__JP_COLLIDE__\\n\'; else\n'
        f"set -C\n"
        f'cat > "$f" <<\'{delim}\'\n'
        f"{body}"
        f"{delim}\n"
        f"set +C\n"
        f"{exec_line}\n"
        f"__rc=$?\n"
        f'rm -f -- "$f"\n'
        f"printf '\\n__JP_EXIT__%d__\\n' \"$__rc\"\n"
        f"fi\n"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_run.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jp/commands/run.py tests/test_run.py
git commit -m "feat(run): pure helpers for interpreter, temp name, remote script"
```

---

## Task 4: `run()` orchestration + cwd resolution

**Files:**
- Modify: `src/jp/commands/run.py`
- Test: `tests/test_run.py`

`run()` resolves the workspace + current subfolder, reads the local file, builds the script, opens a terminado terminal at the mapped cwd, and drives it with an `ExitScanner`. We test it with fakes (api + ws + a stubbed `pty.drive_pty`), exactly like `test_terminal.py`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_run.py
import argparse
from pathlib import Path

from jp import pty


class _FakeApi:
    def __init__(self, session, *, dir_exists=True):
        self._session = session
        self._dir_exists = dir_exists
        self.created_cwd = "UNSET"
        self.deleted: list[str] = []

    def stat(self, api_path):
        return object() if self._dir_exists else None

    def create_terminal(self, cwd=None):
        self.created_cwd = cwd
        return self._session

    def delete_terminal(self, name):
        self.deleted.append(name)

    def terminal_ws_url(self, name):
        return f"wss://h/terminals/websocket/{name}"


class _Ctx:
    def __init__(self, root):
        self.root = Path(root)

        class cfg:
            prefix = "me/proj"
            base_url = "https://h/user/a"
            timeout = 30.0

        self.cfg = cfg


def _args(file, **kw):
    ns = argparse.Namespace(file=file, args=[], as_interp="", dry_run=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _wire(monkeypatch, tmp_path, *, api, ws, cwd=None, drive_raises=False):
    from jp.api import TerminalSession  # noqa: F401

    monkeypatch.setattr(run, "load_repo", lambda: _Ctx(tmp_path))
    monkeypatch.setattr(run._context, "build_api", lambda cfg: api)
    monkeypatch.setattr(run.config_mod, "load_token", lambda cfg: "tok-123456")
    monkeypatch.setattr(run.WebSocket, "connect", staticmethod(lambda *a, **k: ws))
    monkeypatch.setattr(run.pty, "HAS_PTY", True)
    monkeypatch.setattr(run.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(run.sys.stdout, "isatty", lambda: True)

    calls = {}

    def fake_drive(ws_arg, *, initial, scanner):
        calls["initial"] = initial
        if drive_raises:
            raise RuntimeError("boom")
        scanner.exit_code = 0
        return 0

    monkeypatch.setattr(run.pty, "drive_pty", fake_drive)
    monkeypatch.chdir(cwd or tmp_path)
    return calls


def test_run_happy_path(monkeypatch, tmp_path):
    from jp.api import TerminalSession

    (tmp_path / "train.py").write_text("print('hi')\n")
    api = _FakeApi(TerminalSession(name="42", cwd_applied=True))
    ws = _FakeWS([])
    _wire(monkeypatch, tmp_path, api=api, ws=ws)

    rc = run.run(_args("train.py"))
    assert rc == 0
    assert api.created_cwd == "me/proj"     # root -> prefix
    assert api.deleted == ["42"]            # always cleaned up


def test_run_cwd_subfolder_maps_prefix_plus_rel(monkeypatch, tmp_path):
    from jp.api import TerminalSession

    sub = tmp_path / "pkg" / "sub"
    sub.mkdir(parents=True)
    (sub / "a.py").write_text("x=1\n")
    api = _FakeApi(TerminalSession(name="7", cwd_applied=True))
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS([]), cwd=sub)

    run.run(_args("a.py"))
    assert api.created_cwd == "me/proj/pkg/sub"


def test_run_missing_remote_folder_errors(monkeypatch, tmp_path):
    from jp.api import TerminalSession

    (tmp_path / "a.py").write_text("x\n")
    api = _FakeApi(TerminalSession(name="1", cwd_applied=True), dir_exists=False)
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS([]))

    with pytest.raises(UsageError, match="does not exist"):
        run.run(_args("a.py"))
    assert api.created_cwd == "UNSET"       # never opened a terminal


def test_run_deletes_session_even_when_drive_raises(monkeypatch, tmp_path):
    from jp.api import TerminalSession

    (tmp_path / "a.py").write_text("x\n")
    api = _FakeApi(TerminalSession(name="9", cwd_applied=True))
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS([]), drive_raises=True)

    with pytest.raises(RuntimeError, match="boom"):
        run.run(_args("a.py"))
    assert api.deleted == ["9"]


def test_run_missing_local_file_errors(monkeypatch, tmp_path):
    from jp.api import TerminalSession

    api = _FakeApi(TerminalSession(name="1", cwd_applied=True))
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS([]))
    with pytest.raises(UsageError, match="no such file"):
        run.run(_args("ghost.py"))


def test_run_dry_run_makes_no_terminal(monkeypatch, tmp_path, capsys):
    from jp.api import TerminalSession

    (tmp_path / "a.py").write_text("print(1)\n")
    api = _FakeApi(TerminalSession(name="1", cwd_applied=True))
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS([]))
    rc = run.run(_args("a.py", dry_run=True))
    assert rc == 0
    assert api.created_cwd == "UNSET"


def test_run_windows_errors(monkeypatch, tmp_path):
    from jp.api import TerminalSession

    (tmp_path / "a.py").write_text("x\n")
    api = _FakeApi(TerminalSession(name="1", cwd_applied=True))
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS([]))
    monkeypatch.setattr(run.pty, "HAS_PTY", False)
    with pytest.raises(UsageError, match="POSIX"):
        run.run(_args("a.py"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_run.py -q`
Expected: FAIL (no `add_parser`/`run`).

- [ ] **Step 3: Implement `run()` + `add_parser`**

```python
# append to src/jp/commands/run.py

import sys

from .. import config as config_mod
from .. import paths, ui
from .._ws import WebSocket, WebSocketError
from .. import pty
from ..errors import EXIT_OK, NetworkError
from . import _context
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "run",
        help="run a local script on the remote in the current mapped folder",
    )
    p.add_argument("file", help="local file to run (relative to the current folder)")
    p.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="arguments passed to the script",
    )
    p.add_argument(
        "--as",
        dest="as_interp",
        default="",
        metavar="INTERP",
        help="force the interpreter (e.g. python3, bash, node)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show the target folder, interpreter and source without running",
    )
    p.set_defaults(func=run)


def _remote_cwd(root, prefix: str) -> str:
    """prefix + (current dir relative to the workspace root), POSIX, validated."""
    rel = paths.to_pureposix(str(Path.cwd().resolve().relative_to(Path(root).resolve())))
    rel_str = "" if str(rel) == "." else str(rel)
    cwd = prefix if not rel_str else f"{prefix}/{rel_str}"
    return paths.validate_prefix(prefix) and cwd  # validate_prefix raises on bad prefix


def run(args: argparse.Namespace) -> int:
    from pathlib import Path  # local import keeps the module import list tidy

    ctx = load_repo()
    prefix = paths.validate_prefix(ctx.cfg.prefix)

    local = Path(args.file)
    if not local.is_file():
        raise UsageError(f"no such file: {args.file!r}")
    raw = local.read_text(encoding="utf-8", errors="replace")
    source = normalize_source(raw)
    first_line = source.split("\n", 1)[0]
    runner = resolve_runner(local.name, first_line, args.as_interp)

    remote_cwd = _remote_cwd(ctx.root, prefix)

    if args.dry_run:
        how = "shebang" if runner.use_shebang else runner.interp
        ui.heading(f"jp run (dry-run): {args.file}")
        ui.info(f"remote folder : {remote_cwd}")
        ui.info(f"interpreter   : {how}")
        ui.info(f"args          : {args.args}")
        ui.out(source)
        return EXIT_OK

    if not pty.HAS_PTY:
        raise UsageError(
            "jp run needs a POSIX terminal; Windows support arrives with the "
            "jp terminal fix."
        )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise UsageError("jp run needs an interactive terminal (a tty).")

    api = _context.build_api(ctx.cfg)
    if api.stat(remote_cwd) is None:
        raise UsageError(
            f"the remote folder {remote_cwd!r} does not exist -- run 'jp push' first."
        )

    delim = pick_delimiter(source)
    tmp = temp_name(local.name, secrets.token_hex(16))
    script = build_remote_script(tmp, source, runner, args.args, delim)

    session = api.create_terminal(cwd=remote_cwd)
    try:
        token = config_mod.load_token(ctx.cfg)
        url = api.terminal_ws_url(session.name)
        try:
            ws = WebSocket.connect(
                url, headers={"Authorization": f"token {token}"}, timeout=ctx.cfg.timeout
            )
        except WebSocketError as exc:
            raise NetworkError(f"could not open the run websocket: {exc}") from exc
        try:
            scanner = pty.ExitScanner()
            # Hide our orchestration: disable echo, run the block, re-enable.
            initial = [
                pty.stdin_message(b"stty -echo 2>/dev/null; clear\n"),
                pty.stdin_message((script).encode("utf-8")),
            ]
            if not session.cwd_applied:
                initial.insert(0, pty.stdin_message(_cd(remote_cwd).encode("utf-8")))
            pty.drive_pty(ws, initial=initial, scanner=scanner)
            return scanner.exit_code if scanner.exit_code is not None else EXIT_OK
        finally:
            ws.close()
    finally:
        api.delete_terminal(session.name)


def _cd(prefix: str) -> str:
    return f"cd -- {shlex.quote(prefix)} 2>/dev/null\n"
```

> NOTE for the implementer: move the `from pathlib import Path` to the module
> top with the other imports (the inline import above is illustrative). Ensure
> `_remote_cwd` uses the module-level `Path`. Keep `_remote_cwd` returning just
> the composed string — simplify the `validate_prefix and cwd` trick into:
> `paths.validate_prefix(prefix); return cwd`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_run.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jp/commands/run.py tests/test_run.py
git commit -m "feat(run): orchestrate terminado run with cwd mapping + exit code"
```

---

## Task 5: Register the command + full suite + lint

**Files:**
- Modify: `src/jp/commands/__init__.py`

- [ ] **Step 1: Add `run` to the registry**

In `src/jp/commands/__init__.py`, add `run` to the alphabetized import block and to `ALL` (place it after `rm`, before `status` to mirror grouping — exact position is cosmetic):

```python
from . import (
    clone,
    config_cmd,
    diff,
    doctor,
    ignore_cmd,
    init,
    kernel,
    login,
    ls,
    pull,
    push,
    rm,
    run,
    status,
    terminal,
    update,
    version,
)

ALL = [
    clone,
    init,
    login,
    pull,
    push,
    status,
    ls,
    diff,
    config_cmd,
    ignore_cmd,
    rm,
    run,
    kernel,
    terminal,
    doctor,
    update,
    version,
]
```

- [ ] **Step 2: Smoke-test the CLI wiring**

Run: `python -m jp run --help`
Expected: help text for `jp run` prints (exit 0).

- [ ] **Step 3: Run the whole suite + lint gate** (per the project memory: ruff + format + pytest before commit)

Run:
```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```
Expected: all green. Fix any lint/format issues (`ruff format .`) and re-run.

- [ ] **Step 4: Commit**

```bash
git add src/jp/commands/__init__.py
git commit -m "feat(run): register the jp run command"
```

---

## Task 6: Docs + README mention

**Files:**
- Modify: `README.md` (add `jp run` to the command list/examples, next to `jp terminal`)
- Create/Modify: `docs/` page if the repo documents commands there (check `docs/` for a commands reference; if none, README only).

- [ ] **Step 1: Add a short section**

Document: purpose (run a local script remotely without push), the parity guarantee, the "data files it opens must already exist remotely" caveat, and examples:

```
jp run train.py --epochs 5
jp run analyze.py            # from a subfolder -> runs in <prefix>/<subfolder>
jp run cleanup.sh
jp run --as python3 script   # extensionless file
jp run --dry-run train.py    # preview target + source, run nothing
```

- [ ] **Step 2: Commit**

```bash
git add README.md docs/
git commit -m "docs(run): document jp run"
```

---

## Self-review (completed during planning)

- **Spec coverage:** workspace-only (Task 4 `load_repo`), cwd parity (Task 4 `_remote_cwd`), interpreter agnostic (Task 3 `resolve_runner`), triple anti-overwrite (Task 3 `build_remote_script` + Task 4 `secrets.token_hex(16)`), concurrency=per-run unique file/no dir (Task 3, no mkdir/rmdir anywhere), portability raw heredoc + CRLF norm (Task 3), exit code (Task 1 `ExitScanner` + Task 4 return), `--dry-run`/`--as` (Task 4), Windows clear error (Task 4), terminal.py untouched (all PTY logic in `pty.py`). ✅
- **Placeholder scan:** the only prose-only step is the Task 4 NOTE which explicitly instructs the cleanup of the illustrative inline `Path` import — fix it during implementation. No `TBD`/`TODO`. ✅
- **Type consistency:** `Runner(use_shebang, interp)`, `ExitScanner.feed/flush/exit_code/done/collided`, `drive_pty(ws, *, initial, scanner)`, `build_remote_script(tmp_name, source, runner, args, delim)` are used identically across tasks. ✅

"""Tests for jp.commands.run.

Pure helpers (interpreter resolution, temp name, source normalization, remote
command assembly) are tested directly. ``run()`` is tested with fakes for the
Api and the websocket, and a stubbed pty driver -- so no test touches a real
tty, socket, or server. Key invariants: the terminal we create and the temp
file we upload are ALWAYS cleaned up.
"""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path

import pytest

from jp.api import TerminalSession
from jp.commands import run
from jp.errors import UsageError


# --------------------------------------------------------------------------- #
# resolve_runner
# --------------------------------------------------------------------------- #
def test_resolve_runner_as_override_wins():
    r = run.resolve_runner("x", "#!/usr/bin/env python3", as_override="bash")
    assert r == run.Runner(use_shebang=False, interp="bash")


def test_resolve_runner_shebang():
    assert run.resolve_runner("script", "#!/bin/sh\n", "") == run.Runner(True, "")


def test_resolve_runner_extension_map():
    assert run.resolve_runner("a.py", "import os", "").interp == "python3"
    assert run.resolve_runner("a.sh", "echo hi", "").interp == "bash"
    assert run.resolve_runner("a.js", "1", "").interp == "node"


def test_resolve_runner_unknown_extension_errors():
    with pytest.raises(UsageError, match="--as"):
        run.resolve_runner("data.bin", "x", "")


# --------------------------------------------------------------------------- #
# temp_name / normalize_source
# --------------------------------------------------------------------------- #
def test_temp_name_shape():
    assert run.temp_name("train.py", "a" * 32) == "__temp__.train." + "a" * 32 + ".py"


def test_temp_name_not_hidden():
    # Must NOT start with a dot: the Contents API rejects hidden files.
    assert not run.temp_name("train.py", "a" * 32).startswith(".")


def test_temp_name_no_extension():
    assert run.temp_name("script", "b" * 32) == "__temp__.script." + "b" * 32


def test_temp_name_random_differs():
    n1 = run.temp_name("x.py", os.urandom(16).hex())
    n2 = run.temp_name("x.py", os.urandom(16).hex())
    assert n1 != n2 and n1.startswith("__temp__.x.") and n1.endswith(".py")


def test_normalize_source_crlf():
    assert run.normalize_source("a\r\nb\rc") == "a\nb\nc\n"
    assert run.normalize_source("ok\n") == "ok\n"


# --------------------------------------------------------------------------- #
# build_remote_command
# --------------------------------------------------------------------------- #
def test_build_remote_command_python_uses_real_name_bootstrap():
    s = run.build_remote_command(
        "__temp__.x.AA.py",
        run.Runner(False, "python3"),
        ["--n", "5"],
        "tok123",
        remote_cwd="me/proj",
        cwd_applied=True,
        real_name="train.py",
    )
    assert s.startswith("f=__temp__.x.AA.py;")  # no cd when cwd already applied
    assert "tok123" in s
    # Bootstrap: real name passed via env, temp path via $f, args after -c.
    assert 'JPF="$f" JPNAME=train.py python3 -c ' in s
    assert "sys.argv[0]=real" in s and 'compile(src,real,"exec")' in s
    assert s.rstrip("; exit").endswith("--n 5") or "--n 5" in s
    assert 'rm -f -- "$f"' in s
    assert "D%d" in s  # exit-code marker
    assert s.endswith("; exit")


def test_build_remote_command_non_python_runs_temp_directly():
    s = run.build_remote_command(
        "__temp__.x.AA.js",
        run.Runner(False, "node"),
        [],
        "tok",
        remote_cwd="me/proj",
        cwd_applied=True,
        real_name="x.js",
    )
    assert 'node "$f"' in s
    assert "JPNAME" not in s  # no bootstrap for non-python


def test_build_remote_command_shebang_and_cd():
    s = run.build_remote_command(
        "__temp__.x.BB",
        run.Runner(True, ""),
        [],
        "tok",
        remote_cwd="me/proj/sub",
        cwd_applied=False,
        real_name="x",
    )
    assert s.startswith("cd -- me/proj/sub 2>/dev/null; ")
    assert 'chmod +x "$f"; "./$f"' in s
    assert "JPNAME" not in s  # shebang path is not bootstrapped


def test_build_remote_command_quotes_args():
    s = run.build_remote_command(
        "__temp__.x.CC.py",
        run.Runner(False, "python3"),
        ["a b", "$X"],
        "tok",
        remote_cwd="me/proj",
        cwd_applied=True,
        real_name="x.py",
    )
    assert shlex.quote("a b") in s and shlex.quote("$X") in s


# --------------------------------------------------------------------------- #
# run() orchestration
# --------------------------------------------------------------------------- #
class _FakeWS:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed = True


class _FakeApi:
    def __init__(self, session, *, dir_exists=True):
        self._session = session
        self._dir_exists = dir_exists
        self.created_cwd = "UNSET"
        self.deleted_terminals: list[str] = []
        self.put: list[str] = []
        self.deleted_files: list[str] = []

    def stat(self, api_path):
        # Temp files never pre-exist (free name); the mapped dir exists iff configured.
        if "__temp__." in api_path:
            return None
        return object() if self._dir_exists else None

    def put_file_bytes(self, api_path, data):
        self.put.append(api_path)
        return object()

    def delete(self, api_path):
        self.deleted_files.append(api_path)

    def create_terminal(self, cwd=None):
        self.created_cwd = cwd
        return self._session

    def delete_terminal(self, name):
        self.deleted_terminals.append(name)

    def terminal_ws_url(self, name):
        return f"wss://h/terminals/websocket/{name}"


class _Ctx:
    def __init__(self, root):
        self.root = Path(root)

        class cfg:  # noqa: N801 - mimic a real Config's attribute shape
            prefix = "me/proj"
            base_url = "https://h/user/a"
            timeout = 30.0

        self.cfg = cfg


def _args(file, **kw):
    ns = argparse.Namespace(file=file, args=[], as_interp="", dry_run=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _wire(monkeypatch, tmp_path, *, api, ws, cwd=None, drive_raises=False, isatty=True):
    monkeypatch.setattr(run, "load_repo", lambda: _Ctx(tmp_path))
    monkeypatch.setattr(run._context, "build_api", lambda cfg: api)
    monkeypatch.setattr(run.config_mod, "load_token", lambda cfg: "tok-123456")
    monkeypatch.setattr(run.WebSocket, "connect", staticmethod(lambda *a, **k: ws))
    monkeypatch.setattr(run.pty, "HAS_PTY", True)
    monkeypatch.setattr(run.sys.stdin, "isatty", lambda: isatty)
    monkeypatch.setattr(run.sys.stdout, "isatty", lambda: isatty)

    calls = {}

    def fake_drive(ws_arg, *, initial, scanner):
        calls["initial"] = initial
        if drive_raises:
            raise RuntimeError("boom")
        scanner.exit_code = 0
        return 0

    monkeypatch.setattr(run.pty, "drive_pty", fake_drive)
    monkeypatch.setattr(
        run.pty,
        "pump_noninteractive",
        lambda ws_arg, **k: k["scanner"].__setattr__("exit_code", 0) or 0,
    )
    monkeypatch.chdir(cwd or tmp_path)
    return calls


def test_run_happy_path(monkeypatch, tmp_path):
    (tmp_path / "train.py").write_text("print('hi')\n")
    api = _FakeApi(TerminalSession(name="42", cwd_applied=True))
    ws = _FakeWS()
    _wire(monkeypatch, tmp_path, api=api, ws=ws)

    rc = run.run(_args("train.py"))
    assert rc == 0
    assert api.created_cwd == "me/proj"
    assert len(api.put) == 1 and api.put[0].startswith("me/proj/__temp__.train.")
    assert api.deleted_terminals == ["42"]
    assert api.deleted_files == api.put  # temp file cleaned up
    assert ws.close_calls == 1


def test_run_cwd_subfolder_maps_prefix_plus_rel(monkeypatch, tmp_path):
    sub = tmp_path / "pkg" / "sub"
    sub.mkdir(parents=True)
    (sub / "a.py").write_text("x=1\n")
    api = _FakeApi(TerminalSession(name="7", cwd_applied=True))
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS(), cwd=sub)

    run.run(_args("a.py"))
    assert api.created_cwd == "me/proj/pkg/sub"
    assert api.put[0].startswith("me/proj/pkg/sub/__temp__.a.")


def test_run_missing_remote_folder_errors(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    api = _FakeApi(TerminalSession(name="1", cwd_applied=True), dir_exists=False)
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS())

    with pytest.raises(UsageError, match="does not exist"):
        run.run(_args("a.py"))
    assert api.created_cwd == "UNSET"  # never opened a terminal
    assert api.put == []  # never uploaded


def test_run_deletes_terminal_and_temp_when_drive_raises(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    api = _FakeApi(TerminalSession(name="9", cwd_applied=True))
    ws = _FakeWS()
    _wire(monkeypatch, tmp_path, api=api, ws=ws, drive_raises=True)

    with pytest.raises(RuntimeError, match="boom"):
        run.run(_args("a.py"))
    assert api.deleted_terminals == ["9"]
    assert api.deleted_files == api.put  # temp still cleaned up
    assert ws.close_calls == 1


def test_run_missing_local_file_errors(monkeypatch, tmp_path):
    api = _FakeApi(TerminalSession(name="1", cwd_applied=True))
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS())
    with pytest.raises(UsageError, match="no such file"):
        run.run(_args("ghost.py"))


def test_run_dry_run_uploads_nothing(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n")
    api = _FakeApi(TerminalSession(name="1", cwd_applied=True))
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS())
    rc = run.run(_args("a.py", dry_run=True))
    assert rc == 0
    assert api.created_cwd == "UNSET" and api.put == []


def test_run_noninteractive_streams(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n")
    api = _FakeApi(TerminalSession(name="3", cwd_applied=True))
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS(), isatty=False)
    rc = run.run(_args("a.py"))
    assert rc == 0
    assert api.deleted_terminals == ["3"] and api.deleted_files == api.put


def test_run_windows_errors(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    api = _FakeApi(TerminalSession(name="1", cwd_applied=True))
    _wire(monkeypatch, tmp_path, api=api, ws=_FakeWS())
    monkeypatch.setattr(run.pty, "HAS_PTY", False)
    with pytest.raises(UsageError, match="POSIX"):
        run.run(_args("a.py"))
    assert api.put == []

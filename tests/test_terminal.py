"""Tests for jp.commands.terminal.

The pure protocol helpers are tested directly. The orchestration (`run`) is
tested with fakes for the Api and the websocket; the raw-PTY proxy itself is
stubbed so no test touches a real tty, a real socket, or a real server. The
single most important behaviour asserted here is that the terminal session we
create is ALWAYS deleted -- even when the proxy raises.
"""

from __future__ import annotations

import argparse
import json
import os

import pytest

from jp.api import TerminalSession
from jp.commands import terminal
from jp.errors import UsageError


# --------------------------------------------------------------------------- #
# Pure protocol helpers
# --------------------------------------------------------------------------- #
def test_stdin_message_format():
    assert terminal.stdin_message(b"ls\n") == json.dumps(["stdin", "ls\n"])


def test_setsize_message_format():
    assert terminal.setsize_message(40, 120) == json.dumps(["set_size", 40, 120])


def test_parse_server_message_stdout():
    assert terminal.parse_server_message('["stdout","hello"]') == ("stdout", "hello")


def test_parse_server_message_setup_and_disconnect():
    assert terminal.parse_server_message('["setup",{}]') == ("setup", {})
    assert terminal.parse_server_message('["disconnect",1]') == ("disconnect", 1)


def test_parse_server_message_garbage_is_ignored():
    assert terminal.parse_server_message("not json") == ("", None)
    assert terminal.parse_server_message("[]") == ("", None)


def test_cd_command_quotes_prefix_and_clears():
    cmd = terminal.cd_command("me/projetos x")
    assert cmd.startswith("cd -- ")
    assert "'me/projetos x'" in cmd  # shlex-quoted
    assert cmd.endswith("&& clear\n")


# --------------------------------------------------------------------------- #
# _pump_output: server stdout -> terminal; disconnect ends the loop
# --------------------------------------------------------------------------- #
class _FakeWS:
    def __init__(self, message_batches):
        self._batches = list(message_batches)
        self.closed = False
        self.sent: list[str] = []
        self.close_calls = 0

    def read_messages(self):
        if self._batches:
            return self._batches.pop(0)
        self.closed = True
        return []

    def send_text(self, text):
        self.sent.append(text)

    def fileno(self):
        return -1

    def close(self):
        self.close_calls += 1
        self.closed = True


def test_pump_output_writes_stdout_to_fd():
    r, w = os.pipe()
    try:
        ws = _FakeWS([[b'["stdout","abc"]', b'["stdout","def"]']])
        stop = terminal._pump_output(ws, w)
        assert stop is False
        assert os.read(r, 1024) == b"abcdef"
    finally:
        os.close(r)
        os.close(w)


def test_pump_output_stops_on_disconnect():
    r, w = os.pipe()
    try:
        ws = _FakeWS([[b'["stdout","x"]', b'["disconnect",1]', b'["stdout","never"]']])
        stop = terminal._pump_output(ws, w)
        assert stop is True
        # Only bytes before the disconnect were written.
        assert os.read(r, 1024) == b"x"
    finally:
        os.close(r)
        os.close(w)


# --------------------------------------------------------------------------- #
# run(): lifecycle + the critical "always delete" guarantee
# --------------------------------------------------------------------------- #
class _FakeApi:
    def __init__(self, session):
        self._session = session
        self.created_cwd = "UNSET"
        self.deleted: list[str] = []

    def create_terminal(self, cwd=None):
        self.created_cwd = cwd
        return self._session

    def delete_terminal(self, name):
        self.deleted.append(name)

    def terminal_ws_url(self, name):
        return f"wss://h/terminals/websocket/{name}"


class _Ctx:
    class cfg:  # noqa: N801 - mimic the attribute shape of a real Config
        prefix = "me/proj"
        base_url = "https://h/user/a"
        timeout = 30.0


def _wire(monkeypatch, *, api, ws, pty_raises=False, has_pty=True, isatty=True):
    monkeypatch.setattr(terminal, "_HAS_PTY", has_pty)
    monkeypatch.setattr(terminal, "load_repo", lambda: _Ctx())
    monkeypatch.setattr(terminal._context, "build_api", lambda cfg: api)
    monkeypatch.setattr(terminal.config_mod, "load_token", lambda cfg: "tok-secret-123456")
    monkeypatch.setattr(terminal.WebSocket, "connect", staticmethod(lambda *a, **k: ws))
    monkeypatch.setattr(terminal.sys.stdin, "isatty", lambda: isatty)
    monkeypatch.setattr(terminal.sys.stdout, "isatty", lambda: isatty)

    def fake_pty(ws_arg, *, prefix, do_cd):
        fake_pty.calls.append({"prefix": prefix, "do_cd": do_cd})
        if pty_raises:
            raise RuntimeError("boom inside proxy")

    fake_pty.calls = []
    monkeypatch.setattr(terminal, "_run_pty", fake_pty)
    return fake_pty


def _args(**kw):
    ns = argparse.Namespace(no_cd=False, yes=True)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_run_happy_path_creates_connects_and_deletes(monkeypatch):
    api = _FakeApi(TerminalSession(name="42", cwd_applied=True))
    ws = _FakeWS([])
    fake_pty = _wire(monkeypatch, api=api, ws=ws)

    rc = terminal.run(_args())

    assert rc == 0
    assert api.created_cwd == "me/proj"  # cwd requested natively
    assert api.deleted == ["42"]  # session cleaned up
    assert ws.close_calls == 1
    # cwd applied natively -> no manual cd.
    assert fake_pty.calls == [{"prefix": "me/proj", "do_cd": False}]


def test_run_deletes_session_even_when_proxy_raises(monkeypatch):
    api = _FakeApi(TerminalSession(name="99", cwd_applied=True))
    ws = _FakeWS([])
    _wire(monkeypatch, api=api, ws=ws, pty_raises=True)

    with pytest.raises(RuntimeError, match="boom"):
        terminal.run(_args())

    # THE invariant: the terminal we created is deleted no matter what, and the
    # websocket is closed.
    assert api.deleted == ["99"]
    assert ws.close_calls == 1


def test_run_manual_cd_when_server_ignores_cwd(monkeypatch):
    api = _FakeApi(TerminalSession(name="5", cwd_applied=False))
    ws = _FakeWS([])
    fake_pty = _wire(monkeypatch, api=api, ws=ws)

    terminal.run(_args())
    assert fake_pty.calls == [{"prefix": "me/proj", "do_cd": True}]


def test_run_no_cd_flag_skips_cwd(monkeypatch):
    api = _FakeApi(TerminalSession(name="6", cwd_applied=False))
    ws = _FakeWS([])
    fake_pty = _wire(monkeypatch, api=api, ws=ws)

    terminal.run(_args(no_cd=True))
    assert api.created_cwd is None  # never requested a cwd
    assert fake_pty.calls == [{"prefix": "me/proj", "do_cd": False}]


def test_run_aborts_on_declined_confirmation(monkeypatch):
    api = _FakeApi(TerminalSession(name="1", cwd_applied=True))
    ws = _FakeWS([])
    _wire(monkeypatch, api=api, ws=ws)
    monkeypatch.setattr(terminal, "_confirm", lambda: False)

    rc = terminal.run(_args(yes=False))
    assert rc == 0
    assert api.created_cwd == "UNSET"  # create_terminal never called
    assert api.deleted == []  # nothing created, nothing deleted


def test_run_refuses_without_tty(monkeypatch):
    api = _FakeApi(TerminalSession(name="1", cwd_applied=True))
    _wire(monkeypatch, api=api, ws=_FakeWS([]), isatty=False)
    with pytest.raises(UsageError, match="tty"):
        terminal.run(_args())
    assert api.deleted == []


def test_run_windows_uses_browser_fallback(monkeypatch):
    api = _FakeApi(TerminalSession(name="1", cwd_applied=True))
    _wire(monkeypatch, api=api, ws=_FakeWS([]), has_pty=False)
    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    rc = terminal.run(_args())
    assert rc == 0
    # Fell back to the browser; never created a terminal session.
    assert api.created_cwd == "UNSET"
    assert api.deleted == []
    assert opened and opened[0].startswith("https://h/user/a")

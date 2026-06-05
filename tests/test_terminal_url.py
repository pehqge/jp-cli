"""Tests for the standalone ``jp terminal <URL>`` form (spec C6).

These cover the purely-additive URL path: an optional positional ``url`` plus
``--credential``. With a URL, ``run()`` must build its Config via
``_context.config_from_url`` (workspace-free) and must NOT call ``load_repo``;
with no URL it must keep the exact prior behavior and call ``load_repo``.

Everything is fully offline: ``config_from_url``, ``build_api``, the websocket
connect, the token load and the PTY proxy are all stubbed at the boundary, so no
network, no real tty and no saved credential are ever touched.
"""

from __future__ import annotations

import argparse

from jp.api import TerminalSession
from jp.commands import terminal
from jp.config import Config


# --------------------------------------------------------------------------- #
# add_parser: the URL is optional and --credential is accepted, without
# breaking the original no-URL form.
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    terminal.add_parser(sub)
    return parser


def test_add_parser_accepts_no_url():
    ns = _build_parser().parse_args(["terminal"])
    assert ns.url == ""
    assert ns.credential == ""
    assert ns.no_cd is False
    assert ns.yes is False
    assert ns.func is terminal.run


def test_add_parser_accepts_positional_url_and_credential():
    ns = _build_parser().parse_args(
        ["terminal", "https://h/user/a/lab/tree/me/proj", "--credential", "work"]
    )
    assert ns.url == "https://h/user/a/lab/tree/me/proj"
    assert ns.credential == "work"


def test_add_parser_keeps_existing_flags_with_url():
    ns = _build_parser().parse_args(
        ["terminal", "https://h/user/a/lab/tree/me/proj", "--no-cd", "-y"]
    )
    assert ns.url == "https://h/user/a/lab/tree/me/proj"
    assert ns.no_cd is True
    assert ns.yes is True


# --------------------------------------------------------------------------- #
# Offline fakes for the run() boundary.
# --------------------------------------------------------------------------- #
class _FakeWS:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed = True


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


def _args(**kw):
    ns = argparse.Namespace(url="", credential="", no_cd=False, yes=True)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _wire(monkeypatch, *, api, ws):
    """Stub the whole run() boundary so nothing touches the network or a tty."""
    monkeypatch.setattr(terminal, "_HAS_PTY", True)
    monkeypatch.setattr(terminal._context, "build_api", lambda cfg: api)
    monkeypatch.setattr(terminal.config_mod, "load_token", lambda cfg: "tok-secret-123456")
    monkeypatch.setattr(terminal.WebSocket, "connect", staticmethod(lambda *a, **k: ws))
    monkeypatch.setattr(terminal.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(terminal.sys.stdout, "isatty", lambda: True)

    def fake_pty(ws_arg, *, prefix, do_cd):
        fake_pty.calls.append({"prefix": prefix, "do_cd": do_cd})

    fake_pty.calls = []
    monkeypatch.setattr(terminal, "_run_pty", fake_pty)
    return fake_pty


# --------------------------------------------------------------------------- #
# With a URL: config_from_url is used and load_repo is NOT called.
# --------------------------------------------------------------------------- #
def test_run_with_url_uses_config_from_url_not_load_repo(monkeypatch):
    api = _FakeApi(TerminalSession(name="77", cwd_applied=True))
    ws = _FakeWS()
    _wire(monkeypatch, api=api, ws=ws)

    cfg = Config(base_url="https://h/user/a", prefix="me/proj")
    seen: dict[str, object] = {}

    def fake_config_from_url(url, *, credential="", token_path=""):
        seen["url"] = url
        seen["credential"] = credential
        return cfg

    monkeypatch.setattr(terminal._context, "config_from_url", fake_config_from_url)

    def boom_load_repo():
        raise AssertionError("load_repo must NOT be called when a URL is given")

    monkeypatch.setattr(terminal, "load_repo", boom_load_repo)

    rc = terminal.run(_args(url="https://h/user/a/lab/tree/me/proj", credential="work"))

    assert rc == 0
    # config_from_url received the URL and credential verbatim.
    assert seen == {"url": "https://h/user/a/lab/tree/me/proj", "credential": "work"}
    # Downstream used the Config from config_from_url (prefix flows through).
    assert api.created_cwd == "me/proj"
    assert api.deleted == ["77"]  # session always cleaned up
    assert ws.close_calls == 1


def test_run_with_url_windows_fallback_uses_config_from_url(monkeypatch):
    # Even on the no-PTY (web fallback) path, a URL must resolve via
    # config_from_url and never load a workspace.
    cfg = Config(base_url="https://h/user/a", prefix="me/proj")
    seen: dict[str, object] = {}

    def fake_config_from_url(url, *, credential="", token_path=""):
        seen["url"] = url
        seen["credential"] = credential
        return cfg

    monkeypatch.setattr(terminal._context, "config_from_url", fake_config_from_url)
    monkeypatch.setattr(terminal, "_HAS_PTY", False)
    monkeypatch.setattr(terminal.config_mod, "load_token", lambda cfg: "tok-secret-123456")

    def boom_load_repo():
        raise AssertionError("load_repo must NOT be called when a URL is given")

    monkeypatch.setattr(terminal, "load_repo", boom_load_repo)

    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    rc = terminal.run(_args(url="https://h/user/a/lab/tree/me/proj"))

    assert rc == 0
    assert seen["url"] == "https://h/user/a/lab/tree/me/proj"
    assert opened and opened[0].startswith("https://h/user/a")


# --------------------------------------------------------------------------- #
# With no URL: load_repo is still used (and config_from_url is NOT).
# --------------------------------------------------------------------------- #
class _Ctx:
    def __init__(self, cfg):
        self.cfg = cfg


def test_run_without_url_calls_load_repo(monkeypatch):
    api = _FakeApi(TerminalSession(name="42", cwd_applied=True))
    ws = _FakeWS()
    _wire(monkeypatch, api=api, ws=ws)

    cfg = Config(base_url="https://h/user/a", prefix="me/proj")
    load_repo_calls: list[bool] = []

    def fake_load_repo():
        load_repo_calls.append(True)
        return _Ctx(cfg)

    monkeypatch.setattr(terminal, "load_repo", fake_load_repo)

    def boom_config_from_url(*a, **k):
        raise AssertionError("config_from_url must NOT be called without a URL")

    monkeypatch.setattr(terminal._context, "config_from_url", boom_config_from_url)

    rc = terminal.run(_args())

    assert rc == 0
    assert load_repo_calls == [True]  # workspace path taken
    assert api.created_cwd == "me/proj"
    assert api.deleted == ["42"]

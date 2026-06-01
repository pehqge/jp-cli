"""Tests for the gated ``jp live`` dispatch: --print-agent and --live gating.

These are network-free: --print-agent never connects, and the --live gating
tests stop at the confirmation/prefix gate, BEFORE any call to connect_live.
We monkeypatch the command module's references (build_api / connect_live) so a
test can never reach a real server even if a gate were to (wrongly) fall through.
"""

from __future__ import annotations

import argparse

import pytest

from jp import config as config_mod
from jp.commands import live
from jp.config import Config
from jp.errors import EXIT_OK, SafetyError, UsageError
from jp.index import Index


def _make_workspace(tmp_path, prefix: str = "users/alice"):
    root = tmp_path / "work"
    root.mkdir()
    cfg = Config(base_url="https://hub.example/api", prefix=prefix)
    config_mod.save(root, cfg)
    Index(root).save()
    # A token file so config_mod.load_token would succeed if reached.
    tok = tmp_path / "tok"
    tok.write_text("secret-token")
    return root, tok


def _args(**over):
    base = {
        "print_agent": False,
        "dry_run": False,
        "live": False,
        "writable": False,
        "yes": False,
        "mount": None,
        "root": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- #
# --print-agent: prints the exact bootstrap, no network
# --------------------------------------------------------------------------- #
def test_print_agent_outputs_bootstrap(tmp_path, monkeypatch, capsys):
    root, _ = _make_workspace(tmp_path)
    monkeypatch.chdir(root)

    rc = live.run(_args(print_agent=True))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "class Agent" in out
    assert "register_target" in out


def test_print_agent_writable_reflected(tmp_path, monkeypatch, capsys):
    root, _ = _make_workspace(tmp_path)
    monkeypatch.chdir(root)

    rc = live.run(_args(print_agent=True, writable=True))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "writable=True" in out


# --------------------------------------------------------------------------- #
# Mode dispatch
# --------------------------------------------------------------------------- #
def test_live_requires_mode(tmp_path, monkeypatch):
    root, _ = _make_workspace(tmp_path)
    monkeypatch.chdir(root)
    with pytest.raises(UsageError):
        live.run(_args())


def test_dry_run_still_works(tmp_path, capsys):
    (tmp_path / "a.txt").write_bytes(b"hi")
    rc = live.run(_args(dry_run=True, root=str(tmp_path)))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "ping: ok" in out


# --------------------------------------------------------------------------- #
# --live gating (network-free)
# --------------------------------------------------------------------------- #
def _guard_no_network(monkeypatch):
    """Make connect_live explode if ever reached -- proves the gate fires first."""

    def _boom(*a, **k):
        raise AssertionError("connect_live must not be called in a gated test")

    monkeypatch.setattr(live, "connect_live", _boom)


def test_live_non_tty_without_yes_refuses(tmp_path, monkeypatch):
    root, _ = _make_workspace(tmp_path)
    monkeypatch.chdir(root)
    _guard_no_network(monkeypatch)

    # status probe says "up" so we reach the confirmation gate.
    monkeypatch.setattr(live._context, "build_api", lambda cfg: _FakeApi("up"))
    # Non-interactive stdin.
    monkeypatch.setattr(live.sys.stdin, "isatty", lambda: False)

    with pytest.raises(SafetyError):
        live.run(_args(live=True))


def test_live_validates_prefix_blocks_shared(tmp_path, monkeypatch):
    root, _ = _make_workspace(tmp_path, prefix="users/alice")
    # Tamper the on-disk config to a forbidden prefix AFTER save (save validates).
    cfgfile = config_mod.config_path(root)
    import json

    data = json.loads(cfgfile.read_text())
    data["prefix"] = "compartilhado"
    cfgfile.write_text(json.dumps(data))
    monkeypatch.chdir(root)
    _guard_no_network(monkeypatch)

    with pytest.raises(SafetyError):
        live.run(_args(live=True))


def test_live_yes_reaches_connect(tmp_path, monkeypatch):
    """With --yes the gate passes and we DO call connect_live (mocked)."""
    root, _ = _make_workspace(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("JP_TOKEN", "secret-token")

    monkeypatch.setattr(live._context, "build_api", lambda cfg: _FakeApi("up"))

    called = {}

    def _fake_connect(api, *, prefix, token, writable):
        called["prefix"] = prefix
        called["writable"] = writable
        return _FakeRFS(), lambda: called.setdefault("cleanup", True)

    monkeypatch.setattr(live, "connect_live", _fake_connect)
    # Stop the serving loop immediately (return the OK exit code _live propagates).
    monkeypatch.setattr(live, "_serve", lambda *a, **k: EXIT_OK)

    rc = live.run(_args(live=True, yes=True))
    assert rc == EXIT_OK
    assert called["prefix"] == "users/alice"
    assert called["writable"] is False
    assert called.get("cleanup") is True


# --------------------------------------------------------------------------- #
# Tiny fakes
# --------------------------------------------------------------------------- #
class _FakeApi:
    def __init__(self, mode: str) -> None:
        self._mode = mode

    def status_probe(self):
        from jp.api import StatusResult

        if self._mode == "up":
            return StatusResult(up=True, detail="ok")
        from jp.errors import ServerDownError

        raise ServerDownError()


class _FakeRFS:
    def ping(self) -> bool:
        return True

    def listdir(self, path: str):
        from jp.remote_fs import Entry

        return [Entry(name="project", type="directory", size=0)]

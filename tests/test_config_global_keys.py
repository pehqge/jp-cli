from __future__ import annotations

import argparse

import pytest

from jp import credentials
from jp import global_prefs as gp
from jp.commands import config_cmd
from jp.errors import ConfigError


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "global_dir", lambda: tmp_path)


def _no_workspace(monkeypatch):
    # config resolves the workspace via paths.find_root; force "outside a workspace".
    import jp.paths

    monkeypatch.setattr(jp.paths, "find_root", lambda start=None: None)
    monkeypatch.setattr(
        config_cmd,
        "load_repo",
        lambda: (_ for _ in ()).throw(AssertionError("should not load repo")),
    )


def test_config_no_action_outside_workspace_shows_global(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _no_workspace(monkeypatch)
    rc = config_cmd.run(argparse.Namespace(action=None, key=None, value=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "machine settings" in out
    assert "auto_update" in out and "update_notifier" in out


def test_config_list_outside_workspace_shows_global(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _no_workspace(monkeypatch)
    rc = config_cmd.run(argparse.Namespace(action="list", key=None, value=None))
    assert rc == 0
    assert "machine settings" in capsys.readouterr().out


def test_config_workspace_key_outside_workspace_errors(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _no_workspace(monkeypatch)
    with pytest.raises(ConfigError):
        config_cmd.run(argparse.Namespace(action="get", key="base_url", value=None))


def test_set_global_key_without_workspace(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        config_cmd,
        "load_repo",
        lambda: (_ for _ in ()).throw(AssertionError("should not load repo")),
    )
    rc = config_cmd.run(argparse.Namespace(action="set", key="auto_update", value="true"))
    assert rc == 0
    assert gp.get("auto_update") is True
    assert "auto_update" in capsys.readouterr().out


def test_get_global_key_without_workspace(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        config_cmd,
        "load_repo",
        lambda: (_ for _ in ()).throw(AssertionError("should not load repo")),
    )
    gp.set("update_notifier", False)
    rc = config_cmd.run(argparse.Namespace(action="get", key="update_notifier", value=None))
    assert rc == 0
    assert "False" in capsys.readouterr().out

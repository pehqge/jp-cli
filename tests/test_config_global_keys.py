from __future__ import annotations

import argparse

from jp import credentials
from jp import global_prefs as gp
from jp.commands import config_cmd


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "global_dir", lambda: tmp_path)


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

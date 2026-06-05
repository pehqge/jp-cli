"""Global user settings: XDG-aware path, defaults, round-trip, 0600, merge."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jp import user_settings


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Point the settings file at a temp dir via HOME and XDG_CONFIG_HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_settings_path_honors_xdg(cfg_home):
    assert user_settings.settings_path() == cfg_home / "xdg" / "jp" / "settings.json"


def test_settings_path_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert user_settings.settings_path() == tmp_path / ".config" / "jp" / "settings.json"


def test_defaults_when_no_file(cfg_home):
    assert not user_settings.settings_path().exists()
    assert user_settings.get_live_access() == "ask"
    assert user_settings.get_live_code_terminal() is True


def test_access_round_trip(cfg_home):
    for value in ("writable", "read-only", "ask"):
        user_settings.set_live_access(value)
        assert user_settings.get_live_access() == value


def test_code_terminal_round_trip(cfg_home):
    user_settings.set_live_code_terminal(False)
    assert user_settings.get_live_code_terminal() is False
    user_settings.set_live_code_terminal(True)
    assert user_settings.get_live_code_terminal() is True


def test_set_live_access_rejects_bad_value(cfg_home):
    with pytest.raises(ValueError):
        user_settings.set_live_access("bogus")
    # Nothing was written for the bad value.
    assert user_settings.get_live_access() == "ask"


def test_corrupt_file_yields_defaults(cfg_home):
    path = user_settings.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert user_settings.get_live_access() == "ask"
    assert user_settings.get_live_code_terminal() is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes do not apply on Windows")
def test_file_mode_is_0600(cfg_home):
    user_settings.set_live_access("writable")
    assert _mode(user_settings.settings_path()) == 0o600


def test_setting_one_key_preserves_the_other(cfg_home):
    user_settings.set_live_access("read-only")
    user_settings.set_live_code_terminal(False)
    # Both survive independently.
    assert user_settings.get_live_access() == "read-only"
    assert user_settings.get_live_code_terminal() is False
    # And re-writing one does not clobber the other.
    user_settings.set_live_access("writable")
    assert user_settings.get_live_code_terminal() is False
    assert user_settings.get_live_access() == "writable"

"""Tests for the ``jp credentials`` command."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jp import credentials
from jp.commands import credentials_cmd
from jp.errors import EXIT_OK, UsageError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows: expanduser() uses USERPROFILE, not HOME.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("JP_NO_TUI", "1")  # force non-interactive paths
    return tmp_path


def _args(**kw) -> SimpleNamespace:
    base = {"list": False, "rm": "", "rename": None, "set_site": None, "force": False}
    base.update(kw)
    return SimpleNamespace(**base)


def _names() -> list[str]:
    return [c.name for c in credentials.list_credentials()]


def _site(name: str) -> str:
    for c in credentials.list_credentials():
        if c.name == name:
            return c.site
    raise AssertionError(f"no credential {name!r}")


def test_list_prints_name_scope_site_no_token(home, capsys):
    credentials.add("myserver", "SECRETTOKENVALUE12345", scope="global", site="https://h")
    assert credentials_cmd.run(_args(list=True)) == EXIT_OK
    out = capsys.readouterr().out
    assert "myserver" in out
    assert "global" in out
    assert "https://h" in out
    assert "SECRETTOKENVALUE12345" not in out


def test_set_site_stores_origin(home):
    credentials.add("myserver", "SECRETTOKENVALUE12345", scope="global")
    rc = credentials_cmd.run(_args(set_site=["myserver", "https://host/user/x"]))
    assert rc == EXIT_OK
    assert _site("myserver") == "https://host"


def test_set_site_invalid_url_errors(home):
    credentials.add("myserver", "SECRETTOKENVALUE12345", scope="global")
    with pytest.raises(UsageError):
        credentials_cmd.run(_args(set_site=["myserver", "not-a-url"]))


def test_rename_moves_credential(home):
    credentials.add("old", "SECRETTOKENVALUE12345", scope="global")
    rc = credentials_cmd.run(_args(rename=["old", "new"]))
    assert rc == EXIT_OK
    assert "old" not in _names()
    assert "new" in _names()


def test_rm_force_removes(home):
    credentials.add("myserver", "SECRETTOKENVALUE12345", scope="global")
    rc = credentials_cmd.run(_args(rm="myserver", force=True))
    assert rc == EXIT_OK
    assert "myserver" not in _names()


def test_rm_non_tty_without_force_errors(home, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    credentials.add("myserver", "SECRETTOKENVALUE12345", scope="global")
    with pytest.raises(UsageError):
        credentials_cmd.run(_args(rm="myserver", force=False))
    assert "myserver" in _names()


def test_rm_missing_errors(home):
    with pytest.raises(UsageError):
        credentials_cmd.run(_args(rm="missing", force=True))


def test_no_flags_non_interactive_prints_table(home, capsys):
    credentials.add("myserver", "SECRETTOKENVALUE12345", scope="global", site="https://h")
    rc = credentials_cmd.run(_args())
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "NAME" in out
    assert "myserver" in out


def test_no_flags_empty_prints_hint(home, capsys):
    rc = credentials_cmd.run(_args())
    assert rc == EXIT_OK
    assert "jp login" in capsys.readouterr().out

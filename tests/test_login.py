"""`jp login`: saves named credentials, never echoes the token, records scope."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from jp import config as config_mod
from jp import credentials
from jp.commands import login
from jp.config import Config
from jp.errors import UsageError


def _args(**kw) -> SimpleNamespace:
    base = {
        "name": "",
        "url": "",
        "no_browser": False,
        "scope_global": False,
        "scope_local": False,
        "token_path": "",
        "token_stdin": False,
        "force": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JP_TOKEN", raising=False)
    monkeypatch.delenv("JP_TOKEN_FILE", raising=False)
    return tmp_path


def test_login_global_via_stdin(home, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)  # not inside a workspace
    monkeypatch.setattr("sys.stdin", io.StringIO("MYTOKENVALUE1234567890\n"))
    rc = login.run(_args(name="myserver", scope_global=True, token_stdin=True))
    assert rc == 0
    cred = credentials.resolve("myserver")
    assert cred is not None and cred.scope == "global"
    assert credentials.read_token(cred) == "MYTOKENVALUE1234567890"
    out = capsys.readouterr()
    assert "MYTOKENVALUE1234567890" not in (out.out + out.err)


def test_login_interactive_prompts_for_name_and_scope(home, monkeypatch, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    config_mod.save(root, Config(base_url="https://h/api", prefix="users/alice"))
    from jp.index import Index

    Index(root).save()
    monkeypatch.chdir(root)

    class _Stdin:
        def isatty(self):
            return True

        def readline(self):
            return ""

    monkeypatch.setattr("sys.stdin", _Stdin())
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "HIDDENTOKEN1234567890")
    # site URL (blank), then name, then scope = local
    answers = iter(["", "mylab", "l"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

    rc = login.run(_args())
    assert rc == 0
    cred = credentials.resolve("mylab", root=root)
    assert cred is not None and cred.scope == "local"
    assert config_mod.load(root).credential == "mylab"


def test_login_token_path_registers_existing_file(home, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "tok"
    src.write_text("FILELOADEDTOKEN1234567890\n")
    rc = login.run(_args(name="myserver", scope_global=True, token_path=str(src)))
    assert rc == 0
    reg = json.loads((home / ".config" / "jp" / "credentials.json").read_text())
    assert reg["credentials"]["myserver"]["token_path"] == str(src)


def test_login_local_outside_workspace_errors(home, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("TOKENVALUE1234567890\n"))
    with pytest.raises(UsageError):
        login.run(_args(name="myserver", scope_local=True, token_stdin=True))


def test_login_noninteractive_requires_name(home, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("TOKENVALUE1234567890\n"))
    with pytest.raises(UsageError):
        login.run(_args(scope_global=True, token_stdin=True))


# --------------------------------------------------------------------------- #
# Site linking, default name, and the browser step (new flow)
# --------------------------------------------------------------------------- #
def _no_browser(monkeypatch) -> list[str]:
    """Record webbrowser.open calls instead of actually opening a browser."""
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url))
    return opened


def test_login_url_records_site_and_default_name(home, monkeypatch, tmp_path):
    """--url links the credential to its origin and seeds a sensible name."""
    monkeypatch.chdir(tmp_path)
    opened = _no_browser(monkeypatch)
    src = tmp_path / "tok"
    src.write_text("FILELOADEDTOKEN1234567890\n")
    rc = login.run(
        _args(
            url="https://host/user/alice/lab/tree/x",
            token_path=str(src),
            scope_global=True,
        )
    )
    assert rc == 0
    # Exactly one credential was saved; it carries the right origin as its site.
    creds = credentials.list_credentials()
    assert len(creds) == 1
    cred = creds[0]
    assert cred.site == "https://host"
    # Default name was derived from username + host.
    assert "alice" in cred.name and "host" in cred.name
    # list_for_site finds it by origin.
    assert [c.name for c in credentials.list_for_site("https://host")] == [cred.name]
    # No tty -> no browser opened.
    assert opened == []


def test_login_no_browser_does_not_open(home, monkeypatch, tmp_path):
    """--no-browser suppresses webbrowser.open even on an interactive tty."""
    monkeypatch.chdir(tmp_path)
    opened = _no_browser(monkeypatch)

    class _Stdin:
        def isatty(self):
            return True

        def readline(self):
            return ""

    monkeypatch.setattr("sys.stdin", _Stdin())
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "HIDDENTOKEN1234567890")
    # name prompt only (url comes from --url, scope is --global).
    answers = iter(["myserver"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

    rc = login.run(_args(url="https://host/user/alice/lab", scope_global=True, no_browser=True))
    assert rc == 0
    cred = credentials.resolve("myserver")
    assert cred is not None and cred.site == "https://host"
    assert opened == []


def test_login_blank_url_saves_without_site(home, monkeypatch, tmp_path):
    """No URL -> credential saved with an empty site (acts as wildcard)."""
    monkeypatch.chdir(tmp_path)
    _no_browser(monkeypatch)
    src = tmp_path / "tok"
    src.write_text("FILELOADEDTOKEN1234567890\n")
    rc = login.run(_args(name="myserver", token_path=str(src), scope_global=True))
    assert rc == 0
    cred = credentials.resolve("myserver")
    assert cred is not None and cred.site == ""
    # Legacy (no-site) credential is a wildcard for any site query.
    assert "myserver" in [c.name for c in credentials.list_for_site("https://x")]


def test_login_default_name_applied_when_name_omitted(home, monkeypatch, tmp_path):
    """Non-tty path uses the derived default when --name is omitted."""
    monkeypatch.chdir(tmp_path)
    _no_browser(monkeypatch)
    src = tmp_path / "tok"
    src.write_text("FILELOADEDTOKEN1234567890\n")
    rc = login.run(
        _args(url="https://hub.example/user/bob/lab", token_path=str(src), scope_global=True)
    )
    assert rc == 0
    creds = credentials.list_credentials()
    assert len(creds) == 1
    assert creds[0].name == "bob-hub.example"
    assert creds[0].site == "https://hub.example"


def test_login_bad_url_ignored_no_site(home, monkeypatch, tmp_path, capsys):
    """A non-http URL is warned about and ignored; credential gets no site."""
    monkeypatch.chdir(tmp_path)
    _no_browser(monkeypatch)
    src = tmp_path / "tok"
    src.write_text("FILELOADEDTOKEN1234567890\n")
    rc = login.run(_args(name="myserver", url="ftp://nope", token_path=str(src), scope_global=True))
    assert rc == 0
    cred = credentials.resolve("myserver")
    assert cred is not None and cred.site == ""


def test_login_tty_opens_browser_at_token_page(home, monkeypatch, tmp_path):
    """Interactive tty offers to open <origin>/hub/token (default yes)."""
    monkeypatch.chdir(tmp_path)
    opened = _no_browser(monkeypatch)

    class _Stdin:
        def isatty(self):
            return True

        def readline(self):
            return ""

    monkeypatch.setattr("sys.stdin", _Stdin())
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "HIDDENTOKEN1234567890")
    # name prompt, then the "open browser?" confirm (empty -> default yes).
    answers = iter(["myserver", ""])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

    rc = login.run(_args(url="https://host/user/alice/lab", scope_global=True))
    assert rc == 0
    assert opened == ["https://host/hub/token"]

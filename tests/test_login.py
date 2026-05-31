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
    answers = iter(["mylab", "l"])  # name, then scope = local
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

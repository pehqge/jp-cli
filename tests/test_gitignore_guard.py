"""The .jp/.gitignore guard: a workspace can never commit its metadata/tokens."""

from __future__ import annotations

import pytest

from jp import config as config_mod
from jp import credentials
from jp.config import Config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JP_TOKEN", raising=False)
    monkeypatch.delenv("JP_TOKEN_FILE", raising=False)
    return tmp_path


def test_config_save_writes_dot_gitignore(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    config_mod.save(root, Config(base_url="https://h/api", prefix="users/alice"))
    gi = root / ".jp" / ".gitignore"
    assert gi.is_file()
    # '*' makes git ignore everything under .jp/ (config, index, tokens).
    assert "*" in gi.read_text().splitlines()


def test_ensure_dot_gitignore_is_idempotent_and_preserves_content(tmp_path):
    root = tmp_path / "ws"
    config_mod.ensure_dot_gitignore(root)
    gi = root / ".jp" / ".gitignore"
    gi.write_text("custom\n")  # user customized it
    config_mod.ensure_dot_gitignore(root)  # must NOT overwrite an existing file
    assert gi.read_text() == "custom\n"


def test_local_credential_dir_is_gitignored(home, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    cred = credentials.add("myserver", "LOCALTOKENVALUE1234567890", scope="local", root=root)
    # token file exists locally...
    assert (root / ".jp" / "credentials.d" / "myserver.token").is_file()
    assert cred.scope == "local"
    # ...and .jp/ is git-ignored, so the token can't be committed.
    gi = root / ".jp" / ".gitignore"
    assert gi.is_file()
    assert "*" in gi.read_text().splitlines()

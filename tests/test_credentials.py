"""Named credential store: scopes, 0600 perms, name validation, resolution."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from jp import credentials, ui
from jp.errors import AuthError, UsageError


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point ~/.config/jp at a temp dir for the global scope."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_validate_name_accepts_safe_names():
    assert credentials.validate_name("ufsc") == "ufsc"
    assert credentials.validate_name("  lab-gpu_1.2  ") == "lab-gpu_1.2"


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "-x", ".hidden", "x y", "a" * 65])
def test_validate_name_rejects_bad(bad):
    with pytest.raises(UsageError):
        credentials.validate_name(bad)


def test_add_global_writes_private_file_and_registry(home):
    cred = credentials.add("ufsc", "SECRET-TOKEN-abcdef1234", scope="global")
    tok = Path(cred.token_path)
    assert tok.is_file()
    assert _mode(tok) == 0o600
    assert tok.read_text().strip() == "SECRET-TOKEN-abcdef1234"
    reg = json.loads((home / ".config" / "jp" / "credentials.json").read_text())
    assert reg["credentials"]["ufsc"]["token_path"] == str(tok)


def test_read_token_registers_redaction(home):
    cred = credentials.add("ufsc", "TOPSECRETVALUE1234567890", scope="global")
    assert credentials.read_token(cred) == "TOPSECRETVALUE1234567890"
    assert "TOPSECRETVALUE1234567890" not in ui.redact("token TOPSECRETVALUE1234567890")


def test_local_requires_root(home):
    with pytest.raises(UsageError):
        credentials.add("x", "toktoktoktoktok123456", scope="local", root=None)


def test_local_shadows_global(home, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    credentials.add("srv", "GLOBALTOKEN1234567890", scope="global")
    credentials.add("srv", "LOCALTOKEN1234567890", scope="local", root=root)
    assert (root / ".jp" / "credentials.d" / "srv.token").is_file()
    resolved = credentials.resolve("srv", root=root)
    assert resolved is not None and resolved.scope == "local"
    assert credentials.read_token(resolved) == "LOCALTOKEN1234567890"
    assert credentials.resolve("srv", root=None).scope == "global"


def test_duplicate_without_force_raises(home):
    credentials.add("ufsc", "FIRSTTOKEN1234567890", scope="global")
    with pytest.raises(UsageError):
        credentials.add("ufsc", "SECONDTOKEN1234567890", scope="global")
    cred = credentials.add("ufsc", "SECONDTOKEN1234567890", scope="global", overwrite=True)
    assert credentials.read_token(cred) == "SECONDTOKEN1234567890"


def test_add_path_registers_existing_file_without_copy(home, tmp_path):
    src = tmp_path / "mytoken"
    src.write_text("EXISTINGTOKEN1234567890\n")
    cred = credentials.add_path("ufsc", str(src), scope="global")
    assert cred.token_path == str(src)
    assert credentials.read_token(cred) == "EXISTINGTOKEN1234567890"


def test_list_credentials_merges_local_and_global(home, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    credentials.add("ufsc", "TOKAAAAAAAAAAAAAAAAA1", scope="global")
    credentials.add("other", "TOKBBBBBBBBBBBBBBBBB2", scope="global")
    credentials.add("ufsc", "LOCALTOKCCCCCCCCCCC3", scope="local", root=root)
    names = {(c.name, c.scope) for c in credentials.list_credentials(root=root)}
    assert names == {("ufsc", "local"), ("other", "global")}


def test_empty_token_refused(home):
    with pytest.raises(AuthError):
        credentials.add("ufsc", "   ", scope="global")

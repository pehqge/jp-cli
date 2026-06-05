"""Named credential store: scopes, 0600 perms, name validation, resolution."""

from __future__ import annotations

import json
import stat
import sys
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
    assert credentials.validate_name("myserver") == "myserver"
    assert credentials.validate_name("  lab-gpu_1.2  ") == "lab-gpu_1.2"


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "-x", ".hidden", "x y", "a" * 65])
def test_validate_name_rejects_bad(bad):
    with pytest.raises(UsageError):
        credentials.validate_name(bad)


def test_add_global_writes_private_file_and_registry(home):
    cred = credentials.add("myserver", "SECRET-TOKEN-abcdef1234", scope="global")
    tok = Path(cred.token_path)
    assert tok.is_file()
    if sys.platform != "win32":
        # Windows has no POSIX mode bits, so chmod(0o600) can't be enforced there.
        assert _mode(tok) == 0o600
    assert tok.read_text().strip() == "SECRET-TOKEN-abcdef1234"
    reg = json.loads((home / ".config" / "jp" / "credentials.json").read_text())
    assert reg["credentials"]["myserver"]["token_path"] == str(tok)


def test_read_token_registers_redaction(home):
    cred = credentials.add("myserver", "TOPSECRETVALUE1234567890", scope="global")
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
    credentials.add("myserver", "FIRSTTOKEN1234567890", scope="global")
    with pytest.raises(UsageError):
        credentials.add("myserver", "SECONDTOKEN1234567890", scope="global")
    cred = credentials.add("myserver", "SECONDTOKEN1234567890", scope="global", overwrite=True)
    assert credentials.read_token(cred) == "SECONDTOKEN1234567890"


def test_add_path_registers_existing_file_without_copy(home, tmp_path):
    src = tmp_path / "mytoken"
    src.write_text("EXISTINGTOKEN1234567890\n")
    cred = credentials.add_path("myserver", str(src), scope="global")
    assert cred.token_path == str(src)
    assert credentials.read_token(cred) == "EXISTINGTOKEN1234567890"


def test_list_credentials_merges_local_and_global(home, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    credentials.add("myserver", "TOKAAAAAAAAAAAAAAAAA1", scope="global")
    credentials.add("other", "TOKBBBBBBBBBBBBBBBBB2", scope="global")
    credentials.add("myserver", "LOCALTOKCCCCCCCCCCC3", scope="local", root=root)
    names = {(c.name, c.scope) for c in credentials.list_credentials(root=root)}
    assert names == {("myserver", "local"), ("other", "global")}


def test_empty_token_refused(home):
    with pytest.raises(AuthError):
        credentials.add("myserver", "   ", scope="global")


def test_add_with_site_persists_and_round_trips(home):
    cred = credentials.add(
        "myserver", "SITETOKEN1234567890", scope="global", site="https://hub.example.com"
    )
    assert cred.site == "https://hub.example.com"
    reg = json.loads((home / ".config" / "jp" / "credentials.json").read_text())
    assert reg["credentials"]["myserver"]["site"] == "https://hub.example.com"
    (loaded,) = [c for c in credentials.list_credentials() if c.name == "myserver"]
    assert loaded.site == "https://hub.example.com"


def test_add_without_site_stores_no_key(home):
    credentials.add("nosite", "NOSITETOKEN1234567890", scope="global")
    reg = json.loads((home / ".config" / "jp" / "credentials.json").read_text())
    assert "site" not in reg["credentials"]["nosite"]
    (loaded,) = [c for c in credentials.list_credentials() if c.name == "nosite"]
    assert loaded.site == ""


def test_add_path_with_site_persists(home, tmp_path):
    src = tmp_path / "mytoken"
    src.write_text("EXISTINGTOKEN1234567890\n")
    cred = credentials.add_path("bypath", str(src), scope="global", site="https://hub.example.com")
    assert cred.site == "https://hub.example.com"
    reg = json.loads((home / ".config" / "jp" / "credentials.json").read_text())
    assert reg["credentials"]["bypath"]["site"] == "https://hub.example.com"


def test_set_site_updates_membership(home):
    credentials.add("srv", "SETSITETOKEN1234567890", scope="global")
    # No site yet -> wildcard, matches any site filter.
    assert {c.name for c in credentials.list_for_site("https://a.example")} == {"srv"}
    updated = credentials.set_site("srv", "https://a.example", scope="global")
    assert updated.site == "https://a.example"
    # Now it matches its own site...
    assert {c.name for c in credentials.list_for_site("https://a.example")} == {"srv"}
    # ...but no longer a different one.
    assert {c.name for c in credentials.list_for_site("https://b.example")} == set()


def test_set_site_missing_name_raises(home):
    with pytest.raises(UsageError):
        credentials.set_site("ghost", "https://a.example", scope="global")


def test_list_for_site_filters_and_wildcards(home):
    credentials.add("a", "AAAATOKEN1234567890", scope="global", site="https://a.example")
    credentials.add("b", "BBBBTOKEN1234567890", scope="global", site="https://b.example")
    credentials.add("legacy", "LEGTOKEN1234567890", scope="global")  # no site

    # Same origin (case-insensitive) plus the siteless wildcard; excludes other site.
    names = {c.name for c in credentials.list_for_site("HTTPS://A.EXAMPLE")}
    assert names == {"a", "legacy"}

    # Empty site -> everything.
    assert {c.name for c in credentials.list_for_site("")} == {"a", "b", "legacy"}

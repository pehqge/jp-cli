"""Offline tests for ``_context.config_from_url`` (C1).

No network, no real server, no real credential: the ``_isolate_user_config``
autouse fixture in conftest points HOME at a tmp dir, so ``list_credentials``
only ever sees what each test writes. Nothing is written to disk by the helper.
"""

from __future__ import annotations

import pytest

from jp import credentials
from jp.commands import _context
from jp.config import Config
from jp.errors import AuthError, SafetyError, UsageError

URL = "https://jupyter.example.com/user/alice/lab/tree/privado/jp-live-test"


# ---------------------------------------------------------------------------
# URL -> base_url + prefix
# ---------------------------------------------------------------------------


def test_parses_base_url_and_prefix(home):
    credentials.add("myserver", "TOKENAAAAAAAAAAAAAA12", scope="global")
    cfg = _context.config_from_url(URL)
    assert isinstance(cfg, Config)
    assert cfg.base_url == "https://jupyter.example.com/user/alice"
    assert cfg.prefix == "privado/jp-live-test"


def test_standalone_server_url(home):
    credentials.add("myserver", "TOKENAAAAAAAAAAAAAA12", scope="global")
    cfg = _context.config_from_url("https://host/lab/tree/proj/sub")
    assert cfg.base_url == "https://host"
    assert cfg.prefix == "proj/sub"


def test_not_a_url_raises_usage():
    with pytest.raises(UsageError):
        _context.config_from_url("not-a-url", token_path="/x/tok")


def test_empty_prefix_refused(home):
    # No path component -> empty prefix -> validate_prefix refuses.
    credentials.add("myserver", "TOKENAAAAAAAAAAAAAA12", scope="global")
    with pytest.raises(SafetyError):
        _context.config_from_url("https://host/user/alice/lab/tree/")


def test_shared_prefix_refused(home):
    credentials.add("myserver", "TOKENAAAAAAAAAAAAAA12", scope="global")
    with pytest.raises(SafetyError):
        _context.config_from_url("https://host/user/alice/lab/tree/shared")


# ---------------------------------------------------------------------------
# Credential resolution (mirrors choose_credential)
# ---------------------------------------------------------------------------


def test_single_credential_auto_selected(home):
    credentials.add("only", "TOKENAAAAAAAAAAAAAA12", scope="global")
    cfg = _context.config_from_url(URL)
    assert cfg.credential == "only"


def test_explicit_credential_validated(home):
    credentials.add("a", "TOKENAAAAAAAAAAAAAA12", scope="global")
    credentials.add("b", "TOKENBBBBBBBBBBBBBB34", scope="global")
    cfg = _context.config_from_url(URL, credential="b")
    assert cfg.credential == "b"


def test_unknown_credential_errors(home):
    credentials.add("a", "TOKENAAAAAAAAAAAAAA12", scope="global")
    with pytest.raises(UsageError):
        _context.config_from_url(URL, credential="nope")


def test_zero_credentials_errors(home):
    with pytest.raises(AuthError):
        _context.config_from_url(URL)


def test_multiple_credentials_noninteractive_errors(home, monkeypatch):
    monkeypatch.setenv("JP_NO_TUI", "1")
    credentials.add("a", "TOKENAAAAAAAAAAAAAA12", scope="global")
    credentials.add("b", "TOKENBBBBBBBBBBBBBB34", scope="global")
    with pytest.raises(UsageError):
        _context.config_from_url(URL)


def test_token_path_bypasses_credentials(home):
    # No credentials saved, but an explicit token path -> no error, no name.
    cfg = _context.config_from_url(URL, token_path="/some/tok")
    assert cfg.credential == ""
    assert cfg.token_path == "/some/tok"


# ---------------------------------------------------------------------------
# No disk writes
# ---------------------------------------------------------------------------


def test_writes_nothing_to_disk(home, tmp_path, monkeypatch):
    credentials.add("only", "TOKENAAAAAAAAAAAAAA12", scope="global")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    _context.config_from_url(URL)
    # No .jp/ workspace created in the cwd by the helper.
    assert not (work / ".jp").exists()
    assert list(work.iterdir()) == []


@pytest.fixture
def home(tmp_path, monkeypatch):
    # JP_NO_TUI forces the non-interactive credential-picker path (error, not a
    # blocking prompt) for the multi-credential case.
    monkeypatch.setenv("JP_NO_TUI", "1")
    return tmp_path

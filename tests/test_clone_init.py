"""Credential selection logic used by `jp clone` / `jp init`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jp import credentials
from jp.commands import _context
from jp.errors import AuthError, UsageError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows: expanduser() uses USERPROFILE, not HOME
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("JP_TOKEN", raising=False)
    monkeypatch.delenv("JP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("JP_NO_TUI", "1")  # force non-interactive selection paths
    return tmp_path


def _args(**kw) -> SimpleNamespace:
    base = {"token_path": "", "credential": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def _add_with_site(name: str, token: str, site: str) -> None:
    credentials.add(name, token, scope="global", site=site)


def test_zero_credentials_errors(home):
    with pytest.raises(AuthError):
        _context.choose_credential(_args(), root=None)


def test_zero_credentials_ok_with_env_token(home, monkeypatch):
    monkeypatch.setenv("JP_TOKEN", "ENVTOKEN1234567890abcd")
    assert _context.choose_credential(_args(), root=None) == ""


def test_token_path_bypasses_selection(home):
    assert _context.choose_credential(_args(token_path="/x/tok"), root=None) == ""


def test_single_credential_auto_selected(home):
    credentials.add("myserver", "TOKENAAAAAAAAAAAAAA12", scope="global")
    assert _context.choose_credential(_args(), root=None) == "myserver"


def test_multiple_credentials_noninteractive_errors(home):
    credentials.add("myserver", "TOKENAAAAAAAAAAAAAA12", scope="global")
    credentials.add("lab", "TOKENBBBBBBBBBBBBBB34", scope="global")
    with pytest.raises(UsageError):
        _context.choose_credential(_args(), root=None)


def test_multiple_credentials_explicit_choice(home):
    credentials.add("myserver", "TOKENAAAAAAAAAAAAAA12", scope="global")
    credentials.add("lab", "TOKENBBBBBBBBBBBBBB34", scope="global")
    assert _context.choose_credential(_args(credential="lab"), root=None) == "lab"


def test_unknown_credential_errors(home):
    credentials.add("myserver", "TOKENAAAAAAAAAAAAAA12", scope="global")
    with pytest.raises(UsageError):
        _context.choose_credential(_args(credential="nope"), root=None)


# --- choose_credential threads args.url through to site filtering ----------- #
def test_choose_credential_url_filters_to_single_site_match(home):
    # Two creds, each pinned to a different site; the URL's origin matches only
    # one -> the pool narrows to a single credential and is auto-selected even
    # in a non-interactive shell.
    _add_with_site("alpha", "TOKENAAAAAAAAAAAAAA12", "https://a.example.com")
    _add_with_site("beta", "TOKENBBBBBBBBBBBBBB34", "https://b.example.com")
    args = _args(url="https://a.example.com/user/me/lab/tree/proj")
    assert _context.choose_credential(args, root=None) == "alpha"


def test_choose_credential_url_no_match_falls_back_to_all(home):
    # The URL origin matches no credential's site; pool falls back to ALL, and
    # with more than one credential a non-interactive shell errors.
    _add_with_site("alpha", "TOKENAAAAAAAAAAAAAA12", "https://a.example.com")
    _add_with_site("beta", "TOKENBBBBBBBBBBBBBB34", "https://b.example.com")
    args = _args(url="https://z.example.com/user/me/lab/tree/proj")
    with pytest.raises(UsageError):
        _context.choose_credential(args, root=None)


def test_choose_credential_without_url_behaves_as_before(home):
    # No .url attribute at all: getattr default kicks in and behavior matches the
    # legacy single-credential auto-select.
    credentials.add("only", "TOKENAAAAAAAAAAAAAA12", scope="global")
    assert _context.choose_credential(_args(), root=None) == "only"

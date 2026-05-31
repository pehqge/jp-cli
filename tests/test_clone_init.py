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
    monkeypatch.delenv("JP_TOKEN", raising=False)
    monkeypatch.delenv("JP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("JP_NO_TUI", "1")  # force non-interactive selection paths
    return tmp_path


def _args(**kw) -> SimpleNamespace:
    base = {"token_path": "", "credential": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def test_zero_credentials_errors(home):
    with pytest.raises(AuthError):
        _context.choose_credential(_args(), root=None)


def test_zero_credentials_ok_with_env_token(home, monkeypatch):
    monkeypatch.setenv("JP_TOKEN", "ENVTOKEN1234567890abcd")
    assert _context.choose_credential(_args(), root=None) == ""


def test_token_path_bypasses_selection(home):
    assert _context.choose_credential(_args(token_path="/x/tok"), root=None) == ""


def test_single_credential_auto_selected(home):
    credentials.add("ufsc", "TOKENAAAAAAAAAAAAAA12", scope="global")
    assert _context.choose_credential(_args(), root=None) == "ufsc"


def test_multiple_credentials_noninteractive_errors(home):
    credentials.add("ufsc", "TOKENAAAAAAAAAAAAAA12", scope="global")
    credentials.add("lab", "TOKENBBBBBBBBBBBBBB34", scope="global")
    with pytest.raises(UsageError):
        _context.choose_credential(_args(), root=None)


def test_multiple_credentials_explicit_choice(home):
    credentials.add("ufsc", "TOKENAAAAAAAAAAAAAA12", scope="global")
    credentials.add("lab", "TOKENBBBBBBBBBBBBBB34", scope="global")
    assert _context.choose_credential(_args(credential="lab"), root=None) == "lab"


def test_unknown_credential_errors(home):
    credentials.add("ufsc", "TOKENAAAAAAAAAAAAAA12", scope="global")
    with pytest.raises(UsageError):
        _context.choose_credential(_args(credential="nope"), root=None)

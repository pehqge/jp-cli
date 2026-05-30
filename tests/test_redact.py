"""Token-never-in-output invariant: ui.redact and the print helpers scrub secrets."""

from __future__ import annotations

import pytest

from jp import ui


@pytest.fixture(autouse=True)
def _reset_secrets():
    # Snapshot and restore the module-global secret set between tests.
    saved = set(ui._KNOWN_SECRETS)
    yield
    ui._KNOWN_SECRETS.clear()
    ui._KNOWN_SECRETS.update(saved)


def test_registered_secret_is_redacted():
    secret = "supersecrettoken1234567890abcdef"
    ui.register_secret(secret)
    out = ui.redact(f"Authorization failed for {secret} on host")
    assert secret not in out
    assert "REDACTED" in out


def test_authorization_header_pattern_redacted():
    # Even an UNregistered token in an Authorization header gets masked.
    out = ui.redact("Authorization: token abcDEF1234567890abcDEF12")
    assert "abcDEF1234567890abcDEF12" not in out
    assert out.lower().startswith("authorization: token")


def test_bearer_pattern_redacted():
    out = ui.redact("Authorization: Bearer abcDEF1234567890abcDEF12")
    assert "abcDEF1234567890abcDEF12" not in out


def test_query_token_redacted():
    out = ui.redact("GET https://h/api?token=abcDEF1234567890abcDEF12 HTTP/1.1")
    assert "abcDEF1234567890abcDEF12" not in out
    assert "token=" in out


def test_long_hex_blob_redacted():
    blob = "deadbeef" * 8  # 64 hex chars
    out = ui.redact(f"the token is {blob} ok")
    assert blob not in out


def test_error_helper_redacts(capsys):
    secret = "zzzzz1234567890SECRETtoken000"
    ui.register_secret(secret)
    ui.error(f"failed with {secret}")
    captured = capsys.readouterr()
    assert secret not in captured.err
    assert secret not in captured.out


def test_out_helper_redacts(capsys):
    secret = "anothersecret1234567890abcdef00"
    ui.register_secret(secret)
    ui.out(f"value {secret} here")
    captured = capsys.readouterr()
    assert secret not in captured.out


def test_redact_accepts_non_str():
    assert ui.redact(12345) == "12345"


# --- absolute server path leakage (/lapix/...) ------------------------------
def test_redact_masks_leaked_lapix_path():
    # A real server error body leaks the absolute on-disk path.
    msg = "Encoding error saving /lapix/privado/secret/data.txt: Incorrect padding"
    out = ui.redact(msg)
    assert "/lapix/privado/secret" not in out
    assert "/lapix" not in out
    # The useful basename is preserved so the message stays actionable.
    assert "data.txt" in out


def test_redact_masks_directory_not_empty_path():
    msg = "Directory /lapix/privado/sub not empty"
    out = ui.redact(msg)
    assert "/lapix/privado" not in out
    assert "sub" in out


def test_redact_leaves_ordinary_relative_paths_alone():
    # Relative paths the user actually has should NOT be mangled.
    msg = "could not read users/alice/notes.txt"
    out = ui.redact(msg)
    assert out == msg


def test_redact_does_not_touch_https_urls():
    msg = "reached https://hub.example/user/alice/api/status"
    out = ui.redact(msg)
    assert "hub.example" in out  # host URL is fine to show

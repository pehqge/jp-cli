"""Config + credential invariants: token path only, no token in config, http refusal."""

from __future__ import annotations

import json

import pytest

from jp import config as config_mod
from jp import ui
from jp.config import Config
from jp.errors import AuthError, ConfigError, SafetyError


def test_config_roundtrip_stores_no_token(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    cfg = Config(base_url="https://h/api", prefix="users/alice", token_path="/x/token")
    config_mod.save(root, cfg)
    raw = json.loads((root / ".jp" / "config.json").read_text())
    # The config stores the PATH, never a token value field.
    assert raw["token_path"] == "/x/token"
    assert "token" not in raw
    loaded = config_mod.load(root)
    assert loaded.prefix == "users/alice"


def test_config_load_revalidates_prefix(tmp_path):
    root = tmp_path / "r"
    (root / ".jp").mkdir(parents=True)
    # A tampered config pointing at a shared prefix must be rejected on load.
    (root / ".jp" / "config.json").write_text(
        json.dumps({"base_url": "https://h/api", "prefix": "shared"})
    )
    with pytest.raises(SafetyError):
        config_mod.load(root)


def test_config_missing_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        config_mod.load(tmp_path)


def test_load_token_from_file_registers_redaction(tmp_path, monkeypatch):
    monkeypatch.delenv("JP_TOKEN", raising=False)
    monkeypatch.delenv("JP_TOKEN_FILE", raising=False)
    tokfile = tmp_path / "tok"
    tokfile.write_text("MY-SECRET-TOKEN-1234567890\n")
    cfg = Config(base_url="https://h/api", prefix="users/alice", token_path=str(tokfile))
    token = config_mod.load_token(cfg)
    assert token == "MY-SECRET-TOKEN-1234567890"
    # After loading, the value is scrubbed from output.
    assert "MY-SECRET-TOKEN-1234567890" not in ui.redact(f"using {token}")


def test_load_token_env_value(monkeypatch):
    monkeypatch.setenv("JP_TOKEN", "ENVTOKEN1234567890abcdef")
    cfg = Config(base_url="https://h/api", prefix="users/alice")
    assert config_mod.load_token(cfg) == "ENVTOKEN1234567890abcdef"


def test_load_token_missing_raises_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("JP_TOKEN", raising=False)
    monkeypatch.delenv("JP_TOKEN_FILE", raising=False)
    monkeypatch.setattr(config_mod, "_default_token_candidates", lambda cfg: [tmp_path / "nope"])
    cfg = Config(base_url="https://h/api", prefix="users/alice")
    with pytest.raises(AuthError):
        config_mod.load_token(cfg)


def test_api_refuses_token_over_http():
    from jp.api import Api

    with pytest.raises(AuthError):
        Api("http://insecure/api", "sometoken")

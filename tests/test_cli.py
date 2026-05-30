"""CLI dispatch, exit codes (DESIGN §9), and the read-only guarantee of status."""

from __future__ import annotations

import json

import conftest
from jp import cli
from jp.commands import _context
from jp.errors import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_SAFETY,
)


def test_version_command(capsys):
    rc = cli.main(["version"])
    assert rc == EXIT_OK
    assert "jp" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    rc = cli.main([])
    assert rc == EXIT_OK
    assert "usage" in capsys.readouterr().out.lower()


def test_status_outside_repo_returns_config_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["status"])
    assert rc == EXIT_CONFIG


def test_init_refuses_shared_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["init", "--base-url", "https://h/api", "--prefix", "shared"])
    assert rc == EXIT_SAFETY


def test_init_creates_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["init", "--base-url", "https://h/api", "--prefix", "users/alice"])
    assert rc == EXIT_OK
    assert (tmp_path / ".jp" / "config.json").is_file()
    raw = json.loads((tmp_path / ".jp" / "config.json").read_text())
    assert raw["prefix"] == "users/alice"


def _patch_api(monkeypatch, fake_api):
    monkeypatch.setattr(_context, "build_api", lambda cfg: fake_api)


def test_status_is_read_only(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    fake = conftest.FakeApi()
    fake.seed("users/alice/remote.txt", b"r")
    _patch_api(monkeypatch, fake)
    conftest.write_file(repo, "local.txt", b"l")

    rc = cli.main(["status"])
    assert rc == EXIT_OK
    # status must not mutate the remote NOR the index.
    assert fake.calls == []
    idx = json.loads((repo / ".jp" / "index.json").read_text())
    assert idx["entries"] == {}


def test_push_conflict_returns_safety_exit(repo, monkeypatch):
    monkeypatch.chdir(repo)
    from jp import sync
    from jp.index import Entry, Index

    fake = conftest.FakeApi()
    fake.seed("users/alice/c.txt", b"remote-edit")
    idx = Index.load(repo)
    idx.set("c.txt", Entry(sha256=sync.sha256_bytes(b"base"), size=4))
    idx.save()
    conftest.write_file(repo, "c.txt", b"local-edit")
    _patch_api(monkeypatch, fake)

    rc = cli.main(["push"])
    assert rc == EXIT_SAFETY
    # Remote untouched.
    assert fake.files["users/alice/c.txt"] == b"remote-edit"


def test_error_output_never_contains_token(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    secret = "LEAKYTOKEN1234567890abcdefSECRET"

    from jp import ui

    ui.register_secret(secret)

    def boom(cfg):
        raise RuntimeError(f"connection failed using {secret}")

    monkeypatch.setattr(_context, "build_api", boom)
    rc = cli.main(["pull"])
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert rc != EXIT_OK

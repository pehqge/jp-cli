"""Tests for ``jp unversion`` -- the opt-out that removes the LOCAL version store.

Invariants under test:
* removes objects/refs/HEAD/staged/format but leaves config.json + index.json +
  working files intact;
* a non-tty without --yes REFUSES (SafetyError) and removes NOTHING;
* --yes proceeds and removes the store;
* prints the remote note (history may still be on the remote);
* a second run says "nothing to remove" and exits 0;
* index.json is never touched.
"""

from __future__ import annotations

import argparse

import pytest

import jp.commands.unversion as unversion_cmd
from jp.commands._context import RepoContext
from jp.config import Config
from jp.errors import EXIT_OK, SafetyError
from jp.ignore import IgnoreSet
from jp.index import Index
from jp.versioning import repo as repo_module


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ctx(root) -> RepoContext:
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    return RepoContext(
        root=root,
        cfg=cfg,
        index=Index.load(root),
        ignore=IgnoreSet.from_root(root),
    )


def _write(root, rel, data: bytes):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _commit(root, rel, data: bytes, message="m") -> str:
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _write(root, rel, data)
    repo_module.stage_paths(
        root, cfg, IgnoreSet.from_root(root), [rel], all_files=False, dry_run=False
    )
    return repo_module.create_commit(
        root, cfg, message=message, stage_all=False, allow_empty=False, dry_run=False
    )["sha"]


def _args(yes=False) -> argparse.Namespace:
    return argparse.Namespace(yes=yes)


def _setup(repo, monkeypatch):
    monkeypatch.setattr(unversion_cmd, "load_repo_root", lambda: repo)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_unversion_yes_removes_store_keeps_config_and_working(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"hello")
    # Also stage something so staged.json exists.
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _write(repo, "b.txt", b"world")
    repo_module.stage_paths(
        repo, cfg, IgnoreSet.from_root(repo), ["b.txt"], all_files=False, dry_run=False
    )

    dot = repo / ".jp"
    assert (dot / "objects").is_dir()
    assert (dot / "refs").is_dir()
    assert (dot / "HEAD").is_file()
    assert (dot / "staged.json").is_file()
    assert (dot / "format").is_file()

    _setup(repo, monkeypatch)
    rc = unversion_cmd.run(_args(yes=True))
    out = capsys.readouterr().out
    assert rc == EXIT_OK

    # Versioning store gone.
    assert not (dot / "objects").exists()
    assert not (dot / "refs").exists()
    assert not (dot / "HEAD").exists()
    assert not (dot / "staged.json").exists()
    assert not (dot / "format").exists()

    # Config, sync base, gitignore, working files untouched.
    assert (dot / "config.json").is_file()
    assert (dot / "index.json").is_file()
    assert (repo / "a.txt").read_bytes() == b"hello"
    assert (repo / "b.txt").read_bytes() == b"world"
    assert "removed local version history" in out.lower()


def test_unversion_non_tty_without_yes_refuses_and_removes_nothing(repo, monkeypatch):
    _commit(repo, "a.txt", b"hello")
    dot = repo / ".jp"

    _setup(repo, monkeypatch)
    # Force a non-interactive stdin so ui.confirm returns the default (False).
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(SafetyError):
        unversion_cmd.run(_args(yes=False))

    # Nothing removed.
    assert (dot / "objects").is_dir()
    assert (dot / "HEAD").is_file()


def test_unversion_tty_decline_removes_nothing(repo, monkeypatch):
    _commit(repo, "a.txt", b"hello")
    dot = repo / ".jp"

    _setup(repo, monkeypatch)
    # Simulate an interactive "no" answer.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")

    with pytest.raises(SafetyError):
        unversion_cmd.run(_args(yes=False))

    assert (dot / "objects").is_dir()
    assert (dot / "HEAD").is_file()


def test_unversion_prints_remote_note(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"hello")
    _setup(repo, monkeypatch)
    unversion_cmd.run(_args(yes=True))
    err = capsys.readouterr().err
    # The remote note goes to stderr (ui.warn) and references __jp under the prefix.
    assert "__jp" in err
    assert "users/alice" in err


def test_unversion_second_run_nothing_to_remove(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"hello")
    _setup(repo, monkeypatch)

    unversion_cmd.run(_args(yes=True))
    capsys.readouterr()

    rc = unversion_cmd.run(_args(yes=True))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "no local version history to remove" in out.lower()


def test_unversion_does_not_touch_index_json(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"hello")
    idx = repo / ".jp" / "index.json"
    before = idx.read_bytes()

    _setup(repo, monkeypatch)
    unversion_cmd.run(_args(yes=True))
    capsys.readouterr()
    assert idx.read_bytes() == before


def test_unversion_removes_packs_dir_if_present(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"hello")
    packs = repo / ".jp" / "packs"
    packs.mkdir()
    (packs / "p1").write_bytes(b"packdata")

    _setup(repo, monkeypatch)
    unversion_cmd.run(_args(yes=True))
    capsys.readouterr()
    assert not packs.exists()


def test_unversion_refuses_to_follow_symlinked_dir_out_of_dot(repo, monkeypatch, capsys, tmp_path):
    """A symlinked versioning dir must NOT have its target's contents removed."""
    _commit(repo, "a.txt", b"hello")
    dot = repo / ".jp"

    # Replace .jp/refs with a symlink pointing at an outside directory holding a
    # precious file. unversion must remove the LINK, never the target's contents.
    import os
    import shutil

    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"DO NOT DELETE")
    shutil.rmtree(dot / "refs")
    os.symlink(str(outside), str(dot / "refs"))

    _setup(repo, monkeypatch)
    unversion_cmd.run(_args(yes=True))
    capsys.readouterr()

    # The symlink is gone but the target dir + its file survive.
    assert not (dot / "refs").exists()
    assert (outside / "keep.txt").read_bytes() == b"DO NOT DELETE"

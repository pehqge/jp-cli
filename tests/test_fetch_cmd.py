"""Command-layer wiring for Task 9: ``jp fetch``, ``jp restore``, ``clone --history``.

These exercise the argparse commands end-to-end against the FakeApi, including the
non-zero exit on a verification failure and the byte-identical behavior of
``clone`` WITHOUT ``--history``.
"""

from __future__ import annotations

import argparse
import shutil

import pytest

import conftest
from jp.commands import clone as clone_cmd
from jp.commands import fetch as fetch_cmd
from jp.commands import restore as restore_cmd
from jp.errors import EXIT_OK, EXIT_PARTIAL
from jp.versioning import mirror as mirror_mod
from jp.versioning import refs
from jp.versioning.objects import ObjectStore, VersioningError


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _commit(repo, cfg, files: dict[str, bytes], message: str = "c") -> str:
    from jp.versioning import repo as vrepo

    for rel, data in files.items():
        conftest.write_file(repo, rel, data)
    return vrepo.create_commit(
        repo, cfg, message=message, stage_all=True, allow_empty=False, dry_run=False
    )["sha"]


def _wipe_local_history(repo) -> None:
    jp = repo / ".jp"
    shutil.rmtree(jp / "objects", ignore_errors=True)
    shutil.rmtree(jp / "refs", ignore_errors=True)
    (jp / "HEAD").unlink(missing_ok=True)
    (jp / "staged.json").unlink(missing_ok=True)


def _ref_puts(fake_api):
    return [p for (m, p) in fake_api.calls if m == "PUT" and "/__jp/refs/" in p]


def _wire(monkeypatch, module, repo, cfg, fake_api):
    """Point a command module's load_repo + build_api at the fixtures."""
    from jp.commands._context import RepoContext
    from jp.ignore import IgnoreSet
    from jp.index import Index

    ctx = RepoContext(root=repo, cfg=cfg, index=Index.load(repo), ignore=IgnoreSet.from_root(repo))
    monkeypatch.setattr(module, "load_repo", lambda: ctx)
    monkeypatch.setattr(module._context, "build_api", lambda c: fake_api)
    return ctx


# --------------------------------------------------------------------------- #
# jp fetch
# --------------------------------------------------------------------------- #
def test_fetch_cmd_round_trip(repo, cfg, fake_api, monkeypatch):
    _commit(repo, cfg, {"a.txt": b"alpha"})
    tip = refs.resolve_head(repo)
    mirror_mod.mirror_history(repo, cfg, fake_api)
    _wipe_local_history(repo)

    _wire(monkeypatch, fetch_cmd, repo, cfg, fake_api)
    rc = fetch_cmd.run(argparse.Namespace(branches=[]))
    assert rc == EXIT_OK
    assert refs.read_ref(repo, "main") == tip
    assert ObjectStore(repo).has(tip)


def test_fetch_cmd_tampered_object_exits_nonzero(repo, cfg, fake_api, monkeypatch):
    _commit(repo, cfg, {"a.txt": b"x" * 4096})
    from jp.versioning import repo as vrepo

    store = ObjectStore(repo)
    commit = vrepo.read_commit(store, refs.resolve_head(repo))
    blob_sha = next(iter(vrepo.read_tree(store, commit["tree"]).values()))["sha256"]
    mirror_mod.mirror_history(repo, cfg, fake_api)
    rp = f"users/alice/__jp/objects/{blob_sha[:2]}/{blob_sha[2:]}"
    tampered = bytearray(fake_api.files[rp])
    tampered[-1] ^= 0xFF
    fake_api.files[rp] = bytes(tampered)
    _wipe_local_history(repo)

    _wire(monkeypatch, fetch_cmd, repo, cfg, fake_api)
    # A verification failure RAISES -> the CLI dispatcher maps it to a non-zero
    # exit. The command itself does not swallow it.
    with pytest.raises(VersioningError):
        fetch_cmd.run(argparse.Namespace(branches=[]))


def test_fetch_cmd_diverged_exits_partial(repo, cfg, fake_api, monkeypatch):
    base = _commit(repo, cfg, {"a.txt": b"alpha"})
    _commit(repo, cfg, {"a.txt": b"remote"}, message="remote")
    mirror_mod.mirror_history(repo, cfg, fake_api)

    # Diverge locally.
    refs.write_ref(repo, "main", base)
    conftest.write_file(repo, "a.txt", b"local")
    from jp.versioning import repo as vrepo

    vrepo.create_commit(
        repo, cfg, message="local", stage_all=True, allow_empty=False, dry_run=False
    )

    _wire(monkeypatch, fetch_cmd, repo, cfg, fake_api)
    rc = fetch_cmd.run(argparse.Namespace(branches=[]))
    assert rc == EXIT_PARTIAL


# --------------------------------------------------------------------------- #
# jp restore
# --------------------------------------------------------------------------- #
def test_restore_cmd_rebuilds_working_tree(repo, cfg, fake_api, monkeypatch):
    _commit(repo, cfg, {"a.txt": b"alpha", "sub/b.txt": b"beta"})
    mirror_mod.mirror_history(repo, cfg, fake_api)
    _wipe_local_history(repo)
    (repo / "a.txt").unlink()
    shutil.rmtree(repo / "sub", ignore_errors=True)

    _wire(monkeypatch, restore_cmd, repo, cfg, fake_api)
    rc = restore_cmd.run(argparse.Namespace(force=False))
    assert rc == EXIT_OK
    assert (repo / "a.txt").read_bytes() == b"alpha"
    assert (repo / "sub" / "b.txt").read_bytes() == b"beta"


def test_restore_cmd_no_history_is_ok(repo, cfg, fake_api, monkeypatch):
    _wipe_local_history(repo)
    _wire(monkeypatch, restore_cmd, repo, cfg, fake_api)
    rc = restore_cmd.run(argparse.Namespace(force=False))
    assert rc == EXIT_OK  # nothing to restore is not an error


# --------------------------------------------------------------------------- #
# clone --history  (and the byte-identical no-flag behavior)
# --------------------------------------------------------------------------- #
def _clone_args(target, **kw):
    base = {
        "url": "",
        "dir": str(target),
        "base_url": "https://hub.example/api",
        "prefix": "users/alice",
        "token_path": "/tmp/tok",  # bypasses credential selection
        "credential": "",
        "dry_run": False,
        "history": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _prepare_remote_with_history(src_repo, cfg, fake_api):
    """Commit + mirror history AND seed the user files on the fake remote."""
    _commit(src_repo, cfg, {"a.txt": b"alpha", "notes/x.txt": b"hi"})
    mirror_mod.mirror_history(src_repo, cfg, fake_api)
    # Seed the WORKING files on the remote too so the normal pull has content.
    fake_api.seed("users/alice/a.txt", b"alpha")
    fake_api.seed("users/alice/notes/x.txt", b"hi")
    return refs.resolve_head(src_repo)


def test_clone_with_history_brings_history(repo, cfg, fake_api, monkeypatch, tmp_path):
    tip = _prepare_remote_with_history(repo, cfg, fake_api)

    target = tmp_path / "cloned"
    monkeypatch.setattr(clone_cmd._context, "build_api", lambda c: fake_api)

    rc = clone_cmd.run(_clone_args(target, history=True))
    assert rc == EXIT_OK
    # The files were pulled AND the history backup was fetched + verified.
    assert (target / "a.txt").read_bytes() == b"alpha"
    store = ObjectStore(target)
    assert store.has(tip)
    assert refs.read_ref(target, "main") == tip


def test_clone_without_history_pulls_files_only(repo, cfg, fake_api, monkeypatch, tmp_path):
    _prepare_remote_with_history(repo, cfg, fake_api)

    target = tmp_path / "cloned_nohist"
    monkeypatch.setattr(clone_cmd._context, "build_api", lambda c: fake_api)

    rc = clone_cmd.run(_clone_args(target, history=False))
    assert rc == EXIT_OK
    # Files pulled, but NO local history was created (byte-identical to before T9).
    assert (target / "a.txt").read_bytes() == b"alpha"
    assert not (target / ".jp" / "objects").exists()
    assert not (target / ".jp" / "refs" / "heads" / "main").exists()


def test_clone_history_tampered_object_exits_nonzero(repo, cfg, fake_api, monkeypatch, tmp_path):
    """clone --history of a backup with a corrupt object fails loudly (raises)."""
    _commit(repo, cfg, {"a.txt": b"y" * 4096})
    from jp.versioning import repo as vrepo

    store = ObjectStore(repo)
    commit = vrepo.read_commit(store, refs.resolve_head(repo))
    blob_sha = next(iter(vrepo.read_tree(store, commit["tree"]).values()))["sha256"]
    mirror_mod.mirror_history(repo, cfg, fake_api)
    fake_api.seed("users/alice/a.txt", b"y" * 4096)
    rp = f"users/alice/__jp/objects/{blob_sha[:2]}/{blob_sha[2:]}"
    tampered = bytearray(fake_api.files[rp])
    tampered[-1] ^= 0xFF
    fake_api.files[rp] = bytes(tampered)

    target = tmp_path / "cloned_bad"
    monkeypatch.setattr(clone_cmd._context, "build_api", lambda c: fake_api)

    with pytest.raises(VersioningError):
        clone_cmd.run(_clone_args(target, history=True))

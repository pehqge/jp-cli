"""Tests for reachability (jp.versioning.fsck.reachable_objects/walk_reachable) and
the ``jp fsck`` integrity check.

Covers:
* reachability collects commit+tree+blob shas across multiple commits + parents,
  includes staged blobs only when asked, does not hang on a forged cycle, and
  RECORDS (not crashes on) a missing object;
* fsck: clean repo -> exit 0; a deleted/corrupted reachable object -> MISSING/
  CORRUPT + non-zero exit; a dangling ref -> reported; --full finds an unreachable
  corrupt object; uninitialized repo -> exit 0 "no history";
* index.json is never touched.
"""

from __future__ import annotations

import argparse

import jp.commands.fsck as fsck_cmd
from jp.commands._context import RepoContext
from jp.config import Config
from jp.errors import EXIT_GENERIC, EXIT_OK
from jp.ignore import IgnoreSet
from jp.index import Index
from jp.versioning import fsck as fsck_mod
from jp.versioning import refs
from jp.versioning import repo as repo_module
from jp.versioning.objects import ObjectStore
from jp.versioning.staging import Staging


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
    """Stage one file and commit it; return the new commit sha."""
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _write(root, rel, data)
    repo_module.stage_paths(
        root, cfg, IgnoreSet.from_root(root), [rel], all_files=False, dry_run=False
    )
    res = repo_module.create_commit(
        root, cfg, message=message, stage_all=False, allow_empty=False, dry_run=False
    )
    return res["sha"]


def _fsck_args(full=False) -> argparse.Namespace:
    return argparse.Namespace(full=full)


# --------------------------------------------------------------------------- #
# Reachability
# --------------------------------------------------------------------------- #
def test_reachable_collects_commit_tree_blob_across_commits(repo):
    c1 = _commit(repo, "a.txt", b"one")
    c2 = _commit(repo, "b.txt", b"two")
    store = ObjectStore(repo)

    reach = fsck_mod.reachable_objects(repo, store, include_staged=False)

    # Both commits, both trees, both blobs are reachable.
    assert c1 in reach
    assert c2 in reach
    # The blob shas (idempotent write returns the existing sha).
    blob1 = store.write(b"one")
    blob2 = store.write(b"two")
    assert blob1 in reach
    assert blob2 in reach
    # The tree of each commit is reachable too.
    c1_obj = repo_module.read_commit(store, c1)
    c2_obj = repo_module.read_commit(store, c2)
    assert c1_obj["tree"] in reach
    assert c2_obj["tree"] in reach


def test_reachable_includes_staged_only_when_asked(repo):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)

    # Stage a brand-new blob that is NOT in any commit.
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _write(repo, "staged_only.txt", b"STAGED")
    repo_module.stage_paths(
        repo, cfg, IgnoreSet.from_root(repo), ["staged_only.txt"], all_files=False, dry_run=False
    )
    staged_entry = Staging.load(repo).get("staged_only.txt")
    assert staged_entry is not None
    staged_sha = staged_entry.sha256

    without = fsck_mod.reachable_objects(repo, store, include_staged=False)
    assert staged_sha not in without

    with_staged = fsck_mod.reachable_objects(repo, store, include_staged=True)
    assert staged_sha in with_staged


def test_reachable_does_not_hang_on_cycle(repo, monkeypatch):
    """A forged parent cycle must terminate (visited-set guard), not loop forever.

    Commit shas are immutable so a natural cycle cannot exist; we forge one by
    monkeypatching read_commit to make commit A claim B as a parent and B claim A
    as a parent. Without the visited guard the DAG walk would loop forever.
    """
    store = ObjectStore(repo)
    refs.init_versioning(repo)
    blob = store.write(b"x")
    tree = repo_module.write_tree(store, {"a.txt": {"sha256": blob, "size": 1, "mode": "file"}})
    a = repo_module.write_commit(
        store, tree=tree, parents=[], message="a", author="t", timestamp_epoch=1, jp_version="0"
    )
    b = repo_module.write_commit(
        store, tree=tree, parents=[a], message="b", author="t", timestamp_epoch=2, jp_version="0"
    )
    refs.write_ref(repo, refs.DEFAULT_BRANCH, b)

    real_read_commit = repo_module.read_commit

    def _cyclic_read(s, sha):
        commit = dict(real_read_commit(s, sha))
        if sha == a:
            commit["parents"] = [b]  # forge the back-edge a -> b -> a
        return commit

    monkeypatch.setattr("jp.versioning.fsck.read_commit", _cyclic_read)

    reach = fsck_mod.reachable_objects(repo, store, include_staged=False)
    # Terminates and still collects both commits + the tree + the blob.
    assert {a, b, tree, blob} <= reach


def test_reachable_records_missing_object_not_crash(repo):
    c1 = _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    # Delete the blob object so the tree references a now-missing object.
    blob = store.write(b"one")
    store.path_for(blob).unlink()

    report = fsck_mod.walk_reachable(repo, store, include_staged=False)
    # The commit + tree are still reachable; the missing blob is recorded.
    assert c1 in report.reachable
    assert blob in report.missing


# --------------------------------------------------------------------------- #
# jp fsck
# --------------------------------------------------------------------------- #
def test_fsck_clean_repo_exits_zero(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")
    monkeypatch.setattr(fsck_cmd, "load_repo", lambda: _ctx(repo))

    rc = fsck_cmd.run(_fsck_args())
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "clean" in out.lower()


def test_fsck_uninitialized_repo_exits_zero(repo, monkeypatch, capsys):
    monkeypatch.setattr(fsck_cmd, "load_repo", lambda: _ctx(repo))
    rc = fsck_cmd.run(_fsck_args())
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "no versioning history" in out.lower()


def test_fsck_missing_reachable_object_reported(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    blob = store.write(b"one")
    store.path_for(blob).unlink()

    monkeypatch.setattr(fsck_cmd, "load_repo", lambda: _ctx(repo))
    rc = fsck_cmd.run(_fsck_args())
    err = capsys.readouterr().err
    assert rc == EXIT_GENERIC
    assert "missing" in err.lower()
    assert blob[:12] in err


def test_fsck_corrupt_reachable_object_reported(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    blob = store.write(b"one")
    # Corrupt the blob bytes in place (overwrite the on-disk payload).
    p = store.path_for(blob)
    p.write_bytes(b"\x00CORRUPTED")

    monkeypatch.setattr(fsck_cmd, "load_repo", lambda: _ctx(repo))
    rc = fsck_cmd.run(_fsck_args())
    err = capsys.readouterr().err
    assert rc == EXIT_GENERIC
    assert "corrupt" in err.lower()
    assert blob[:12] in err


def test_fsck_dangling_ref_reported(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")
    # Point a second branch at a sha that has no commit object.
    bogus = "a" * 64
    refs.write_ref(repo, "feature", bogus)

    monkeypatch.setattr(fsck_cmd, "load_repo", lambda: _ctx(repo))
    rc = fsck_cmd.run(_fsck_args())
    err = capsys.readouterr().err
    assert rc == EXIT_GENERIC
    assert "dangling" in err.lower()
    assert "feature" in err


def test_fsck_dangling_head_reported(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")
    # Detach HEAD onto a non-existent commit.
    refs.set_head_detached(repo, "b" * 64)

    monkeypatch.setattr(fsck_cmd, "load_repo", lambda: _ctx(repo))
    rc = fsck_cmd.run(_fsck_args())
    err = capsys.readouterr().err
    assert rc == EXIT_GENERIC
    assert "head" in err.lower()


def test_fsck_full_finds_unreachable_corrupt(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    # Write an UNREACHABLE object, then corrupt it so its content no longer hashes
    # to its filename. It is not referenced by any commit.
    orphan = store.write(b"orphan-content")
    store.path_for(orphan).write_bytes(b"\x00garbage")

    monkeypatch.setattr(fsck_cmd, "load_repo", lambda: _ctx(repo))

    # Without --full the corrupt unreachable object is NOT seen (clean).
    rc = fsck_cmd.run(_fsck_args(full=False))
    capsys.readouterr()
    assert rc == EXIT_OK

    # With --full it is detected.
    rc = fsck_cmd.run(_fsck_args(full=True))
    err = capsys.readouterr().err
    assert rc == EXIT_GENERIC
    assert orphan[:12] in err


def test_fsck_does_not_touch_index_json(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")
    idx = repo / ".jp" / "index.json"
    before = idx.read_bytes()

    monkeypatch.setattr(fsck_cmd, "load_repo", lambda: _ctx(repo))
    fsck_cmd.run(_fsck_args(full=True))
    capsys.readouterr()
    assert idx.read_bytes() == before


def test_fsck_writes_nothing(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    objects = set(store.iter_objects())

    monkeypatch.setattr(fsck_cmd, "load_repo", lambda: _ctx(repo))
    fsck_cmd.run(_fsck_args(full=True))
    capsys.readouterr()
    assert set(store.iter_objects()) == objects

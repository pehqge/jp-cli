"""Tests for ``jp gc`` and its core (jp.versioning.gc.run_gc).

Safety invariants under test:
* an unreachable OLD object is pruned ONLY with --prune (default is dry-run);
* a reachable object is NEVER pruned even if old;
* a STAGED-but-uncommitted blob is NEVER pruned (include_staged reachability);
* an unreachable but TOO-RECENT object (within grace) is KEPT;
* dry-run writes nothing;
* the reachable set includes ALL branches + HEAD;
* index.json is never touched.
"""

from __future__ import annotations

import argparse
import os
import time

import jp.commands.gc as gc_cmd
from jp.commands._context import RepoContext
from jp.config import Config
from jp.errors import EXIT_OK
from jp.ignore import IgnoreSet
from jp.index import Index
from jp.versioning import gc as gc_mod
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
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _write(root, rel, data)
    repo_module.stage_paths(
        root, cfg, IgnoreSet.from_root(root), [rel], all_files=False, dry_run=False
    )
    return repo_module.create_commit(
        root, cfg, message=message, stage_all=False, allow_empty=False, dry_run=False
    )["sha"]


def _age_object(store: ObjectStore, sha: str, days: float) -> None:
    """Backdate an object file's mtime by ``days`` so it is older than the grace."""
    p = store.path_for(sha)
    old = time.time() - days * 86400
    os.utime(str(p), (old, old))


def _gc_args(prune=False, grace=gc_mod.DEFAULT_GRACE_DAYS) -> argparse.Namespace:
    return argparse.Namespace(prune=prune, grace=grace)


# --------------------------------------------------------------------------- #
# run_gc core
# --------------------------------------------------------------------------- #
def test_unreachable_old_object_pruned_only_with_prune(repo):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    orphan = store.write(b"orphan")
    _age_object(store, orphan, days=30)

    # Dry run: reports a candidate but deletes nothing.
    dry = gc_mod.run_gc(repo, prune=False)
    assert orphan in [sha for sha, _ in dry.candidates]
    assert dry.pruned == []
    assert store.has(orphan)

    # --prune: actually deletes it.
    res = gc_mod.run_gc(repo, prune=True)
    assert orphan in res.pruned
    assert not store.has(orphan)


def test_reachable_object_never_pruned_even_if_old(repo):
    _commit(repo, "a.txt", b"keepme")
    store = ObjectStore(repo)
    reachable_blob = store.write(b"keepme")  # idempotent -> the committed blob's sha
    _age_object(store, reachable_blob, days=999)

    res = gc_mod.run_gc(repo, prune=True)
    assert reachable_blob not in res.pruned
    assert store.has(reachable_blob)


def test_staged_uncommitted_blob_never_pruned(repo):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)

    # Stage a brand-new file but DO NOT commit it.
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _write(repo, "staged.txt", b"STAGED-ONLY")
    repo_module.stage_paths(
        repo, cfg, IgnoreSet.from_root(repo), ["staged.txt"], all_files=False, dry_run=False
    )
    entry = Staging.load(repo).get("staged.txt")
    assert entry is not None
    staged_blob = entry.sha256
    _age_object(store, staged_blob, days=999)  # old enough to be a candidate by age

    res = gc_mod.run_gc(repo, prune=True)
    assert staged_blob not in res.pruned
    assert store.has(staged_blob)


def test_unreachable_recent_object_kept_within_grace(repo):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    fresh_orphan = store.write(b"fresh-orphan")
    # Brand new (mtime = now) -> within the default 14-day grace -> kept.

    res = gc_mod.run_gc(repo, prune=True, grace_days=14)
    assert fresh_orphan not in res.pruned
    assert store.has(fresh_orphan)
    # It is not even a candidate.
    assert fresh_orphan not in [sha for sha, _ in res.candidates]


def test_dry_run_writes_nothing(repo):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    orphan = store.write(b"orphan")
    _age_object(store, orphan, days=30)

    before = set(store.iter_objects())
    gc_mod.run_gc(repo, prune=False)
    after = set(store.iter_objects())
    assert before == after


def test_reachable_set_includes_all_branches(repo):
    """An object reachable only via a NON-HEAD branch is never pruned."""
    _commit(repo, "a.txt", b"main-content")
    store = ObjectStore(repo)

    # Create a second branch with its own unique blob, then move HEAD away from it.
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _write(repo, "feature.txt", b"feature-content")
    repo_module.stage_paths(
        repo, cfg, IgnoreSet.from_root(repo), ["feature.txt"], all_files=False, dry_run=False
    )
    feat_commit = repo_module.create_commit(
        repo, cfg, message="feat", stage_all=False, allow_empty=False, dry_run=False
    )["sha"]
    feature_blob = store.write(b"feature-content")
    # Record the feature commit under a feature branch, then detach HEAD elsewhere
    # so the feature blob is reachable ONLY through refs/heads/feature.
    refs.write_ref(repo, "feature", feat_commit)
    # Move HEAD's branch (main) back to the first commit so HEAD no longer reaches
    # the feature blob.
    first_commit = repo_module.read_commit(store, feat_commit)["parents"][0]
    refs.write_ref(repo, refs.DEFAULT_BRANCH, first_commit)
    _age_object(store, feature_blob, days=999)
    _age_object(store, feat_commit, days=999)

    res = gc_mod.run_gc(repo, prune=True)
    assert feature_blob not in res.pruned
    assert feat_commit not in res.pruned
    assert store.has(feature_blob)


def test_gc_does_not_touch_index_json(repo):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    orphan = store.write(b"orphan")
    _age_object(store, orphan, days=30)
    idx = repo / ".jp" / "index.json"
    before = idx.read_bytes()

    gc_mod.run_gc(repo, prune=True)
    assert idx.read_bytes() == before


def test_gc_does_not_delete_outside_objects(repo):
    """gc must only ever delete under .jp/objects -- nothing else is removed."""
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    orphan = store.write(b"orphan")
    _age_object(store, orphan, days=30)

    # A sentinel file elsewhere under .jp must survive a prune.
    sentinel = repo / ".jp" / "config.json"
    sentinel_before = sentinel.read_bytes()
    gc_mod.run_gc(repo, prune=True)
    assert sentinel.read_bytes() == sentinel_before
    # The working file too.
    assert (repo / "a.txt").read_bytes() == b"one"


# --------------------------------------------------------------------------- #
# jp gc command
# --------------------------------------------------------------------------- #
def test_gc_command_dry_run_default(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    orphan = store.write(b"orphan")
    _age_object(store, orphan, days=30)

    monkeypatch.setattr(gc_cmd, "load_repo", lambda: _ctx(repo))
    rc = gc_cmd.run(_gc_args(prune=False))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "would reclaim" in out.lower()
    assert store.has(orphan)  # nothing deleted on a dry run
    # Remote note is always printed.
    assert "local only" in out.lower()


def test_gc_command_prune_deletes(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")
    store = ObjectStore(repo)
    orphan = store.write(b"orphan")
    _age_object(store, orphan, days=30)

    monkeypatch.setattr(gc_cmd, "load_repo", lambda: _ctx(repo))
    rc = gc_cmd.run(_gc_args(prune=True))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "pruned" in out.lower()
    assert not store.has(orphan)


def test_gc_command_nothing_to_prune(repo, monkeypatch, capsys):
    _commit(repo, "a.txt", b"one")  # everything reachable, nothing old/orphaned
    monkeypatch.setattr(gc_cmd, "load_repo", lambda: _ctx(repo))
    rc = gc_cmd.run(_gc_args(prune=False))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "nothing to prune" in out.lower()

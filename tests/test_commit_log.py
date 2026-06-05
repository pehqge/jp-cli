"""Tests for the offline versioning commands: ``jp add``, ``jp commit``, ``jp log``.

Exercises the documented behavior end-to-end against a real on-disk repo (the
``repo`` fixture's working tree + ``.jp`` config). Key invariants under test:

* ``add`` writes blob(s) to the object store and updates ``staged.json``; ``.git/``
  is hard-skipped in both ``-A`` and explicit-path modes; ``-A`` stages deletions;
  a dry-run/preview writes NOTHING; an explicit unknown path errors.
* ``commit`` creates and advances the branch ref on the first (root) commit, chains
  the parent on the second, refuses an empty commit without ``--allow-empty``,
  allows it with it; is crash-safe (a ref-write failure AFTER objects are written
  leaves HEAD on the OLD commit and the new objects as harmless orphans); and uses
  a compare-and-swap that refuses if the branch moved underneath it.
* ``log`` prints "no commits yet" (exit 0) on an unborn repo, supports ``--oneline``
  and ``--stat``, and resolves a REF by branch name and by sha.
* INVARIANT: a full add->commit->log cycle never creates or modifies
  ``.jp/index.json`` (the sync base).
"""

from __future__ import annotations

import argparse

import pytest

import jp.commands.add as add_cmd
import jp.commands.commit as commit_cmd
import jp.commands.log as log_cmd
from jp.commands._context import RepoContext
from jp.config import Config
from jp.errors import EXIT_OK, EXIT_USAGE, UsageError
from jp.ignore import IgnoreSet
from jp.index import Index
from jp.versioning import refs, repo
from jp.versioning.objects import ObjectStore, VersioningError
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


def _patch_load(monkeypatch, mod, root) -> RepoContext:
    ctx = _ctx(root)
    monkeypatch.setattr(mod, "load_repo", lambda: ctx)
    return ctx


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _add_args(paths=None, all=False, dry_run=False):
    return _args(paths=list(paths or []), all=all, dry_run=dry_run)


def _commit_args(message="m", all=False, allow_empty=False, dry_run=False):
    return _args(message=message, all=all, allow_empty=allow_empty, dry_run=dry_run)


def _log_args(ref="", max_count=0, oneline=False, stat=False):
    return _args(ref=ref, max_count=max_count, oneline=oneline, stat=stat)


def _write(root, rel, data: bytes):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# --------------------------------------------------------------------------- #
# jp add
# --------------------------------------------------------------------------- #
def test_add_all_writes_blobs_and_staging(repo, monkeypatch):
    _patch_load(monkeypatch, add_cmd, repo)
    _write(repo, "a.txt", b"hello")
    _write(repo, "dir/b.txt", b"world")

    rc = add_cmd.run(_add_args(all=True))
    assert rc == EXIT_OK

    store = ObjectStore(repo)
    staging = Staging.load(repo)
    assert set(staging.entries) == {"a.txt", "dir/b.txt"}
    for e in staging.entries.values():
        assert store.has(e.sha256)
    # staged.json exists on disk.
    assert (repo / ".jp" / "staged.json").is_file()


def test_add_explicit_path(repo, monkeypatch):
    _patch_load(monkeypatch, add_cmd, repo)
    _write(repo, "a.txt", b"hello")
    _write(repo, "b.txt", b"other")

    rc = add_cmd.run(_add_args(paths=["a.txt"]))
    assert rc == EXIT_OK
    staging = Staging.load(repo)
    assert set(staging.entries) == {"a.txt"}


def test_add_skips_git_dir_under_all(repo, monkeypatch):
    _patch_load(monkeypatch, add_cmd, repo)
    _write(repo, "a.txt", b"keep")
    _write(repo, ".git/objects/deadbeef", b"GITOBJECT")
    _write(repo, ".git/config", b"[core]")

    add_cmd.run(_add_args(all=True))
    staging = Staging.load(repo)
    assert set(staging.entries) == {"a.txt"}
    assert not any(k.startswith(".git/") for k in staging.entries)


def test_add_skips_git_dir_explicit(repo, monkeypatch):
    _patch_load(monkeypatch, add_cmd, repo)
    _write(repo, ".git/config", b"[core]")
    rc = add_cmd.run(_add_args(paths=[".git/config"]))
    # Nothing staged + an error -> non-zero exit.
    assert rc == EXIT_USAGE
    assert Staging.load(repo).entries == {}


def test_add_all_stages_deletion(repo, monkeypatch):
    _patch_load(monkeypatch, add_cmd, repo)
    _write(repo, "a.txt", b"x")
    _write(repo, "b.txt", b"y")
    add_cmd.run(_add_args(all=True))
    assert set(Staging.load(repo).entries) == {"a.txt", "b.txt"}

    # Remove b.txt from the working tree, re-run -A: deletion is staged.
    (repo / "b.txt").unlink()
    add_cmd.run(_add_args(all=True))
    assert set(Staging.load(repo).entries) == {"a.txt"}


def test_add_explicit_deletion_of_tracked(repo, monkeypatch):
    _patch_load(monkeypatch, add_cmd, repo)
    _write(repo, "a.txt", b"x")
    add_cmd.run(_add_args(paths=["a.txt"]))
    (repo / "a.txt").unlink()
    rc = add_cmd.run(_add_args(paths=["a.txt"]))
    assert rc == EXIT_OK
    assert Staging.load(repo).entries == {}


def test_add_unknown_path_errors(repo, monkeypatch):
    _patch_load(monkeypatch, add_cmd, repo)
    rc = add_cmd.run(_add_args(paths=["nope.txt"]))
    assert rc == EXIT_USAGE


def test_add_preview_writes_nothing(repo, monkeypatch):
    _patch_load(monkeypatch, add_cmd, repo)
    _write(repo, "a.txt", b"hello")

    rc = add_cmd.run(_add_args())  # no paths, no -A -> preview
    assert rc == EXIT_OK
    # No staging file, no objects materialized.
    assert not (repo / ".jp" / "staged.json").exists()
    assert list(ObjectStore(repo).iter_objects()) == []


def test_add_dry_run_writes_nothing(repo, monkeypatch):
    _patch_load(monkeypatch, add_cmd, repo)
    _write(repo, "a.txt", b"hello")

    rc = add_cmd.run(_add_args(all=True, dry_run=True))
    assert rc == EXIT_OK
    assert not (repo / ".jp" / "staged.json").exists()
    assert list(ObjectStore(repo).iter_objects()) == []


# --------------------------------------------------------------------------- #
# jp commit
# --------------------------------------------------------------------------- #
def test_commit_requires_message(repo, monkeypatch):
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"x")
    with pytest.raises(UsageError):
        commit_cmd.run(_commit_args(message=""))
    with pytest.raises(UsageError):
        commit_cmd.run(_commit_args(message="   "))


def test_first_commit_creates_and_advances_ref(repo, monkeypatch):
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"hello")

    rc = commit_cmd.run(_commit_args(message="root", all=True))
    assert rc == EXIT_OK

    head_sha = refs.resolve_head(repo)
    assert head_sha is not None
    # The branch ref now points at the commit.
    assert refs.read_ref(repo, refs.DEFAULT_BRANCH) == head_sha
    store = ObjectStore(repo)
    commit = repo_read_commit(store, head_sha)
    assert commit["parents"] == []
    assert commit["message"] == "root"


def test_second_commit_chains_parent(repo, monkeypatch):
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"v1")
    commit_cmd.run(_commit_args(message="c1", all=True))
    first = refs.resolve_head(repo)

    _write(repo, "a.txt", b"v2")
    commit_cmd.run(_commit_args(message="c2", all=True))
    second = refs.resolve_head(repo)

    assert second != first
    store = ObjectStore(repo)
    assert repo_read_commit(store, second)["parents"] == [first]


def test_empty_commit_refused_without_allow_empty(repo, monkeypatch):
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"x")
    commit_cmd.run(_commit_args(message="c1", all=True))
    # Nothing changed -> a second -A commit is empty -> refused.
    with pytest.raises(VersioningError):
        commit_cmd.run(_commit_args(message="c2", all=True))


def test_empty_commit_allowed_with_flag(repo, monkeypatch):
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"x")
    commit_cmd.run(_commit_args(message="c1", all=True))
    first = refs.resolve_head(repo)
    rc = commit_cmd.run(_commit_args(message="empty", all=True, allow_empty=True))
    assert rc == EXIT_OK
    second = refs.resolve_head(repo)
    assert second != first
    store = ObjectStore(repo)
    assert repo_read_commit(store, second)["parents"] == [first]


def test_commit_dry_run_writes_nothing(repo, monkeypatch):
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"hello")
    rc = commit_cmd.run(_commit_args(message="m", all=True, dry_run=True))
    assert rc == EXIT_OK
    # No HEAD advance, no ref, no objects.
    assert refs.resolve_head(repo) is None
    assert list(ObjectStore(repo).iter_objects()) == []


def test_commit_crash_safety_ref_write_fails(repo, monkeypatch):
    """If the ref write raises AFTER objects are written, HEAD must stay put.

    We make a first real commit, then monkeypatch refs.update_ref to raise during a
    SECOND commit (after the new tree+commit objects are written). HEAD must still
    resolve to the OLD commit and the new objects are harmless orphans.
    """
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"v1")
    commit_cmd.run(_commit_args(message="c1", all=True))
    old_head = refs.resolve_head(repo)
    objs_before = {str(p) for p in ObjectStore(repo).iter_objects()}

    _write(repo, "a.txt", b"v2")

    def boom(*a, **k):
        raise RuntimeError("simulated crash writing the ref")

    monkeypatch.setattr(repo_mod_refs(), "update_ref", boom)
    with pytest.raises(RuntimeError):
        commit_cmd.run(_commit_args(message="c2", all=True))

    # HEAD unchanged; new objects exist but are unreferenced orphans.
    assert refs.resolve_head(repo) == old_head
    objs_after = {str(p) for p in ObjectStore(repo).iter_objects()}
    assert objs_after >= objs_before  # new orphans may exist, none removed


def test_commit_cas_refuses_if_branch_moved(repo, monkeypatch):
    """A compare-and-swap must refuse if the branch tip moved out from under us."""
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"v1")
    commit_cmd.run(_commit_args(message="c1", all=True))

    # Simulate a concurrent advance: move the branch to a different sha right before
    # our commit's CAS by wrapping resolve_head to report a stale parent.
    real_resolve = refs.resolve_head
    stale = real_resolve(repo)

    def stale_then_move(root):
        # Report the stale parent to create_commit, but actually move the ref so the
        # CAS (expected=stale) fails against the real, newer value.
        refs.write_ref(root, refs.DEFAULT_BRANCH, "f" * 64)
        return stale

    monkeypatch.setattr(repo_mod_refs(), "resolve_head", stale_then_move)
    _write(repo, "a.txt", b"v2")
    with pytest.raises(VersioningError):
        commit_cmd.run(_commit_args(message="c2", all=True))


# --------------------------------------------------------------------------- #
# jp log
# --------------------------------------------------------------------------- #
def test_log_no_commits(repo, monkeypatch, capsys):
    _patch_load(monkeypatch, log_cmd, repo)
    rc = log_cmd.run(_log_args())
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "no commits yet" in out


def test_log_oneline(repo, monkeypatch, capsys):
    _patch_load(monkeypatch, commit_cmd, repo)
    _patch_load(monkeypatch, log_cmd, repo)
    _write(repo, "a.txt", b"v1")
    commit_cmd.run(_commit_args(message="first", all=True))
    _write(repo, "a.txt", b"v2")
    commit_cmd.run(_commit_args(message="second", all=True))
    capsys.readouterr()  # drain the commit commands' own output

    log_cmd.run(_log_args(oneline=True))
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert lines[0].endswith("second")
    assert lines[1].endswith("first")
    # Each line begins with a 12-char short hash.
    assert all(len(ln.split(" ", 1)[0]) == 12 for ln in lines)


def test_log_stat_counts(repo, monkeypatch, capsys):
    _patch_load(monkeypatch, commit_cmd, repo)
    _patch_load(monkeypatch, log_cmd, repo)
    _write(repo, "a.txt", b"x")
    _write(repo, "b.txt", b"y")
    commit_cmd.run(_commit_args(message="root", all=True))
    capsys.readouterr()  # drain the commit command's own output

    log_cmd.run(_log_args(stat=True))
    out = capsys.readouterr().out
    # Root commit added two files.
    assert "2 file(s) changed (+2 ~0 -0)" in out
    assert "+ a.txt" in out
    assert "+ b.txt" in out


def test_log_ref_by_branch_and_sha(repo, monkeypatch, capsys):
    _patch_load(monkeypatch, commit_cmd, repo)
    _patch_load(monkeypatch, log_cmd, repo)
    _write(repo, "a.txt", b"x")
    commit_cmd.run(_commit_args(message="root", all=True))
    head = refs.resolve_head(repo)
    capsys.readouterr()  # drain the commit command's own output

    # By branch name.
    log_cmd.run(_log_args(ref=refs.DEFAULT_BRANCH, oneline=True))
    out_branch = capsys.readouterr().out
    assert head[:12] in out_branch

    # By full sha.
    log_cmd.run(_log_args(ref=head, oneline=True))
    out_sha = capsys.readouterr().out
    assert head[:12] in out_sha


def test_log_unknown_ref_raises(repo, monkeypatch):
    _patch_load(monkeypatch, log_cmd, repo)
    with pytest.raises(VersioningError):
        log_cmd.run(_log_args(ref="nonexistent-branch"))


def test_log_max_count(repo, monkeypatch, capsys):
    _patch_load(monkeypatch, commit_cmd, repo)
    _patch_load(monkeypatch, log_cmd, repo)
    for i in range(3):
        _write(repo, "a.txt", f"v{i}".encode())
        commit_cmd.run(_commit_args(message=f"c{i}", all=True))
    capsys.readouterr()  # drain the commit commands' own output

    log_cmd.run(_log_args(oneline=True, max_count=2))
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 2


def test_log_non_oneline_uses_short_hash_not_redacted(repo, monkeypatch, capsys):
    """The non-oneline ``commit`` line must show the 12-char short hash.

    A full 64-hex sha would trip ui.redact's bare-hex (>=32) heuristic and print
    as ``***REDACTED***``, breaking the feature's main output. The short hash is
    below that threshold and must appear verbatim.
    """
    _patch_load(monkeypatch, commit_cmd, repo)
    _patch_load(monkeypatch, log_cmd, repo)
    _write(repo, "a.txt", b"x")
    commit_cmd.run(_commit_args(message="root", all=True))
    head = refs.resolve_head(repo)
    capsys.readouterr()  # drain the commit command's own output

    log_cmd.run(_log_args())  # non-oneline
    out = capsys.readouterr().out
    assert head[:12] in out  # short hash shown verbatim
    assert "***REDACTED***" not in out  # the full sha was NOT emitted/masked
    assert head not in out  # the full 64-hex sha never reaches the output


def test_commit_on_detached_head(repo, monkeypatch):
    """A commit while HEAD is detached advances the detached HEAD (no branch ref)."""
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"v1")
    commit_cmd.run(_commit_args(message="c1", all=True))
    first = refs.resolve_head(repo)

    # Detach HEAD at the first commit, then commit again.
    refs.set_head_detached(repo, first)
    assert refs.current_branch(repo) is None  # detached
    _write(repo, "a.txt", b"v2")
    rc = commit_cmd.run(_commit_args(message="c2", all=True))
    assert rc == EXIT_OK

    head = refs.read_head(repo)
    assert head is not None and not head.symbolic  # still detached
    second = refs.resolve_head(repo)
    assert second != first
    store = ObjectStore(repo)
    assert repo_read_commit(store, second)["parents"] == [first]
    # The branch ref must NOT have moved (we were detached).
    assert refs.read_ref(repo, refs.DEFAULT_BRANCH) == first


def test_commit_recovers_missing_staged_blob_from_working_file(repo, monkeypatch):
    """If a staged blob is gone from the store, _ensure_blob re-writes it on commit.

    Stage a file (snapshotting its blob), delete the object from the store, then
    commit: the blob must be re-written from the still-present working file and the
    commit must succeed with the same content sha.
    """
    _patch_load(monkeypatch, add_cmd, repo)
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"recover me")
    add_cmd.run(_add_args(all=True))

    staging = Staging.load(repo)
    blob_sha = staging.entries["a.txt"].sha256
    store = ObjectStore(repo)
    assert store.has(blob_sha)
    # Simulate the object being lost from the store.
    store.path_for(blob_sha).unlink()
    assert not store.has(blob_sha)

    rc = commit_cmd.run(_commit_args(message="root"))  # no -A; commit from staging
    assert rc == EXIT_OK
    # The blob was re-written and the commit references it.
    assert store.has(blob_sha)
    head = refs.resolve_head(repo)
    tree = repo_read_tree(store, repo_read_commit(store, head)["tree"])
    assert tree["a.txt"]["sha256"] == blob_sha


def test_commit_missing_blob_and_working_file_gone_raises(repo, monkeypatch):
    """If the staged blob is gone AND the working file is gone, commit must raise."""
    _patch_load(monkeypatch, add_cmd, repo)
    _patch_load(monkeypatch, commit_cmd, repo)
    _write(repo, "a.txt", b"data")
    add_cmd.run(_add_args(all=True))

    staging = Staging.load(repo)
    blob_sha = staging.entries["a.txt"].sha256
    store = ObjectStore(repo)
    store.path_for(blob_sha).unlink()
    (repo / "a.txt").unlink()  # working file gone too

    with pytest.raises(VersioningError):
        commit_cmd.run(_commit_args(message="root"))


# --------------------------------------------------------------------------- #
# INVARIANT: versioning never perturbs the sync base (.jp/index.json)
# --------------------------------------------------------------------------- #
def test_add_commit_log_never_touch_index_json(repo, monkeypatch):
    _patch_load(monkeypatch, add_cmd, repo)
    _patch_load(monkeypatch, commit_cmd, repo)
    _patch_load(monkeypatch, log_cmd, repo)

    index_path = repo / ".jp" / "index.json"
    before = index_path.read_bytes() if index_path.exists() else None

    _write(repo, "a.txt", b"hello")
    add_cmd.run(_add_args(all=True))
    commit_cmd.run(_commit_args(message="root", all=True))
    log_cmd.run(_log_args())

    after = index_path.read_bytes() if index_path.exists() else None
    assert after == before


# --------------------------------------------------------------------------- #
# Small indirections so the crash/CAS tests patch the SAME refs module the
# command code uses (commit -> repo.create_commit -> refs.*).
# --------------------------------------------------------------------------- #
def repo_read_commit(store, sha):
    return repo.read_commit(store, sha)


def repo_read_tree(store, sha):
    return repo.read_tree(store, sha)


def repo_mod_refs():
    return refs

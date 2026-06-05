"""Tests for the tree/commit data model (jp.versioning.repo).

TDD-first against the documented contract: canonical, deterministic tree bytes
(identical entries -> identical bytes -> identical sha, so equal trees dedup to a
single object), strict structural validation on read, a history walk that follows
``parents[0]`` and refuses a cycle, and a ``diff_trees`` that classifies
added/modified/deleted by blob sha.
"""

from __future__ import annotations

import json

import pytest

from jp.versioning import repo
from jp.versioning.objects import ObjectStore, VersioningError


def _store(tmp_path) -> ObjectStore:
    return ObjectStore(tmp_path / "work")


def _entry(sha_seed: int, size: int = 3) -> dict:
    return {"sha256": f"{sha_seed:064x}", "size": size, "mode": "file"}


# --- build_tree_bytes -------------------------------------------------------
def test_build_tree_bytes_is_canonical_and_deterministic():
    a = {"b.txt": _entry(2), "a.txt": _entry(1)}
    # Same logical entries, different insertion order -> identical bytes.
    b = {"a.txt": _entry(1), "b.txt": _entry(2)}
    assert repo.build_tree_bytes(a) == repo.build_tree_bytes(b)
    # Canonical JSON: sorted keys, no spaces, no trailing newline.
    raw = repo.build_tree_bytes(a)
    assert raw == raw.strip()
    assert b" " not in raw
    parsed = json.loads(raw)
    assert parsed["version"] == 1
    assert set(parsed["entries"]) == {"a.txt", "b.txt"}


def test_build_tree_bytes_normalizes_keys():
    raw = repo.build_tree_bytes({"./a//b.txt": _entry(1)})
    parsed = json.loads(raw)
    assert list(parsed["entries"]) == ["a/b.txt"]


def test_build_tree_bytes_rejects_traversal_key():
    with pytest.raises(VersioningError):
        repo.build_tree_bytes({"../escape.txt": _entry(1)})


# --- write_tree / read_tree -------------------------------------------------
def test_identical_trees_dedup_to_one_object(tmp_path):
    store = _store(tmp_path)
    entries = {"a.txt": _entry(1), "b.txt": _entry(2)}
    sha1 = repo.write_tree(store, entries)
    sha2 = repo.write_tree(store, dict(reversed(list(entries.items()))))
    assert sha1 == sha2
    # Only ONE object materialized for the two identical writes.
    assert len(list(store.iter_objects())) == 1


def test_read_tree_round_trip(tmp_path):
    store = _store(tmp_path)
    entries = {"a.txt": _entry(1), "dir/c.txt": _entry(3, size=9)}
    sha = repo.write_tree(store, entries)
    got = repo.read_tree(store, sha)
    assert got == {
        "a.txt": {"sha256": f"{1:064x}", "size": 3, "mode": "file"},
        "dir/c.txt": {"sha256": f"{3:064x}", "size": 9, "mode": "file"},
    }


def test_read_tree_preserves_optional_nb_key(tmp_path):
    store = _store(tmp_path)
    entry = {"sha256": f"{5:064x}", "size": 10, "mode": "file", "nb": {"k": "v"}}
    sha = repo.write_tree(store, {"n.ipynb": entry})
    got = repo.read_tree(store, sha)
    assert got["n.ipynb"]["nb"] == {"k": "v"}


def test_read_tree_rejects_malformed_object(tmp_path):
    store = _store(tmp_path)
    # A perfectly valid object that is NOT a tree.
    sha = store.write(b'{"not":"a tree"}')
    with pytest.raises(VersioningError):
        repo.read_tree(store, sha)


def test_read_tree_rejects_bad_entry_sha(tmp_path):
    store = _store(tmp_path)
    bad = json.dumps(
        {"version": 1, "entries": {"a.txt": {"sha256": "nothex", "size": 1, "mode": "file"}}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sha = store.write(bad)
    with pytest.raises(VersioningError):
        repo.read_tree(store, sha)


# --- write_commit / read_commit ---------------------------------------------
def test_write_read_commit_round_trip(tmp_path):
    store = _store(tmp_path)
    tree = repo.write_tree(store, {"a.txt": _entry(1)})
    sha = repo.write_commit(
        store,
        tree=tree,
        parents=[],
        message="root",
        author="alice",
        timestamp_epoch=1700000000,
        jp_version="9.9.9",
    )
    commit = repo.read_commit(store, sha)
    assert commit["tree"] == tree
    assert commit["parents"] == []
    assert commit["message"] == "root"
    assert commit["author"] == "alice"
    assert commit["epoch"] == 1700000000
    assert commit["jp"] == "9.9.9"
    assert isinstance(commit["time"], str) and commit["time"]


def test_root_commit_has_empty_parents(tmp_path):
    store = _store(tmp_path)
    tree = repo.write_tree(store, {})
    sha = repo.write_commit(
        store,
        tree=tree,
        parents=[],
        message="m",
        author="a",
        timestamp_epoch=1,
        jp_version="1",
    )
    assert repo.read_commit(store, sha)["parents"] == []


def test_read_commit_rejects_bad_tree_sha(tmp_path):
    store = _store(tmp_path)
    bad = json.dumps(
        {
            "version": 1,
            "tree": "nothex",
            "parents": [],
            "message": "m",
            "author": "a",
            "time": "t",
            "epoch": 1,
            "jp": "1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sha = store.write(bad)
    with pytest.raises(VersioningError):
        repo.read_commit(store, sha)


def test_read_commit_rejects_bad_parent_sha(tmp_path):
    store = _store(tmp_path)
    tree = repo.write_tree(store, {})
    bad = json.dumps(
        {
            "version": 1,
            "tree": tree,
            "parents": ["nothex"],
            "message": "m",
            "author": "a",
            "time": "t",
            "epoch": 1,
            "jp": "1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sha = store.write(bad)
    with pytest.raises(VersioningError):
        repo.read_commit(store, sha)


# --- iter_history -----------------------------------------------------------
def _chain(store, n: int) -> list[str]:
    """Build a linear chain of n commits; return shas oldest->newest."""
    shas: list[str] = []
    parents: list[str] = []
    for i in range(n):
        tree = repo.write_tree(store, {f"f{i}.txt": _entry(i + 1)})
        sha = repo.write_commit(
            store,
            tree=tree,
            parents=parents,
            message=f"c{i}",
            author="a",
            timestamp_epoch=i,
            jp_version="1",
        )
        shas.append(sha)
        parents = [sha]
    return shas


def test_iter_history_walks_in_order_and_stops(tmp_path):
    store = _store(tmp_path)
    shas = _chain(store, 3)
    walked = list(repo.iter_history(store, shas[-1]))
    assert [s for s, _ in walked] == list(reversed(shas))
    assert [c["message"] for _, c in walked] == ["c2", "c1", "c0"]


def test_iter_history_single_root(tmp_path):
    store = _store(tmp_path)
    shas = _chain(store, 1)
    walked = list(repo.iter_history(store, shas[0]))
    assert len(walked) == 1
    assert walked[0][0] == shas[0]


def test_iter_history_cycle_via_corrupt_parent(tmp_path, monkeypatch):
    """A corrupted object that re-points to an already-visited sha must raise.

    sha256 makes a real self/2-cycle infeasible to forge, so we simulate the
    on-disk corruption by intercepting read_commit to always return a commit whose
    parent equals the sha just read -- a 1-cycle. iter_history must detect the
    repeat and raise rather than loop forever.
    """
    store = _store(tmp_path)
    tree = repo.write_tree(store, {})
    start = repo.write_commit(
        store, tree=tree, parents=[], message="m", author="a", timestamp_epoch=0, jp_version="1"
    )

    def fake_read_commit(_store, sha):
        return {
            "version": 1,
            "tree": tree,
            "parents": [sha],  # points back at itself -> 1-cycle
            "message": "loop",
            "author": "a",
            "time": "t",
            "epoch": 0,
            "jp": "1",
        }

    monkeypatch.setattr(repo, "read_commit", fake_read_commit)
    with pytest.raises(VersioningError):
        list(repo.iter_history(store, start))


# --- diff_trees -------------------------------------------------------------
def test_diff_trees_added_modified_deleted():
    a = {"keep.txt": _entry(1), "mod.txt": _entry(2), "gone.txt": _entry(3)}
    b = {"keep.txt": _entry(1), "mod.txt": _entry(99), "new.txt": _entry(4)}
    d = repo.diff_trees(a, b)
    assert d == {
        "added": ["new.txt"],
        "modified": ["mod.txt"],
        "deleted": ["gone.txt"],
    }


def test_diff_trees_sorted_and_empty():
    assert repo.diff_trees({}, {}) == {"added": [], "modified": [], "deleted": []}
    a = {}
    b = {"z.txt": _entry(1), "a.txt": _entry(2)}
    assert repo.diff_trees(a, b)["added"] == ["a.txt", "z.txt"]


# --- resolve_author ---------------------------------------------------------
def test_resolve_author_prefers_config_attr():
    class Cfg:
        versioning_author = "Dr. Alice"
        extra = {}

    assert repo.resolve_author(Cfg()) == "Dr. Alice"


def test_resolve_author_reads_extra_key():
    class Cfg:
        extra = {"versioning.author": "Bob"}

    assert repo.resolve_author(Cfg()) == "Bob"


def test_resolve_author_falls_back_to_env(monkeypatch):
    class Cfg:
        extra = {}

    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setenv("USER", "carol")
    assert repo.resolve_author(Cfg()) == "carol"


def test_resolve_author_unknown_last_resort(monkeypatch):
    class Cfg:
        extra = {}

    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    assert repo.resolve_author(Cfg()) == "unknown"


# --------------------------------------------------------------------------- #
# Notebook hybrid staging (uses the conftest ``repo`` working-tree fixture)
# --------------------------------------------------------------------------- #
import hashlib as _hashlib  # noqa: E402
import json as _json  # noqa: E402

from jp.ignore import IgnoreSet  # noqa: E402
from jp.versioning import notebooks  # noqa: E402
from jp.versioning import refs as _refs  # noqa: E402
from jp.versioning import repo as _repo  # noqa: E402
from jp.versioning.staging import Staging  # noqa: E402


def _nb_bytes(code: str, *, outputs=None, execution_count=None) -> bytes:
    return _json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "source": [code],
                    "outputs": outputs if outputs is not None else [],
                    "execution_count": execution_count,
                    "metadata": {},
                }
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    ).encode("utf-8")


def _stage_all(root):
    return _repo.stage_paths(
        root, object(), IgnoreSet.from_root(root), [], all_files=True, dry_run=False
    )


def test_staging_a_notebook_records_norm_sha_and_stores_original_blob(repo):
    nb = _nb_bytes("x = 1\n", outputs=[{"text": "1"}], execution_count=3)
    (repo / "a.ipynb").write_bytes(nb)

    _stage_all(repo)

    staging = Staging.load(repo)
    entry = staging.get("a.ipynb")
    assert entry is not None
    # The stored blob is the ORIGINAL bytes (faithful), not the normalized form.
    assert entry.sha256 == _hashlib.sha256(nb).hexdigest()
    # The hybrid key is the NORMALIZED sha.
    assert entry.nb_norm_sha == notebooks.normalized_sha(nb)
    assert entry.nb_norm_sha != entry.sha256

    # The original blob round-trips out of the store byte-for-byte.
    store = ObjectStore(repo)
    assert store.read(entry.sha256, max_size=entry.size) == nb


def test_add_all_does_not_restage_after_pure_rerun(repo):
    nb = _nb_bytes("print('hi')\n", outputs=[], execution_count=None)
    (repo / "a.ipynb").write_bytes(nb)
    _stage_all(repo)
    before = Staging.load(repo).get("a.ipynb")

    # Pure re-run: same code, fresh outputs + bumped execution_count.
    rerun = _nb_bytes(
        "print('hi')\n",
        outputs=[{"output_type": "stream", "text": "hi\n"}],
        execution_count=17,
    )
    assert rerun != nb
    (repo / "a.ipynb").write_bytes(rerun)

    summary = _stage_all(repo)
    assert "a.ipynb" not in summary["staged"]  # NOT re-staged

    after = Staging.load(repo).get("a.ipynb")
    # The staged entry is unchanged: same original blob, same norm sha.
    assert after.sha256 == before.sha256
    assert after.nb_norm_sha == before.nb_norm_sha
    # And no new blob for the re-run bytes was written.
    store = ObjectStore(repo)
    assert not store.has(_hashlib.sha256(rerun).hexdigest())


def test_add_all_restages_after_a_code_change(repo):
    nb = _nb_bytes("x = 1\n")
    (repo / "a.ipynb").write_bytes(nb)
    _stage_all(repo)
    before = Staging.load(repo).get("a.ipynb")

    changed = _nb_bytes("x = 2\n")  # a real code edit
    (repo / "a.ipynb").write_bytes(changed)
    summary = _stage_all(repo)
    assert "a.ipynb" in summary["staged"]  # re-staged

    after = Staging.load(repo).get("a.ipynb")
    assert after.sha256 != before.sha256  # new ORIGINAL blob
    assert after.nb_norm_sha != before.nb_norm_sha  # new normalized key
    # The new original blob is stored and equals the edited bytes.
    store = ObjectStore(repo)
    assert store.read(after.sha256, max_size=after.size) == changed


def test_committed_notebook_blob_equals_original_bytes(repo, monkeypatch):
    """Round-trip: commit a notebook, read its committed blob == original bytes."""
    monkeypatch.setenv("USER", "tester")
    nb = _nb_bytes("a = 1\n", outputs=[{"text": "noise"}], execution_count=9)
    (repo / "a.ipynb").write_bytes(nb)

    result = _repo.create_commit(
        repo, object(), message="add nb", stage_all=True, allow_empty=False, dry_run=False
    )
    store = ObjectStore(repo)
    commit = _repo.read_commit(store, result["sha"])
    tree = _repo.read_tree(store, commit["tree"])
    entry = tree["a.ipynb"]
    # Tree entry carries the nb sub-key with the normalized sha.
    assert entry["nb"] == {"norm_sha": notebooks.normalized_sha(nb)}
    # The committed blob is the FAITHFUL original.
    assert store.read(entry["sha256"], max_size=entry["size"]) == nb


def test_tree_entry_carries_nb_subkey(repo):
    nb = _nb_bytes("y = 5\n")
    (repo / "n.ipynb").write_bytes(nb)
    _stage_all(repo)
    staging = Staging.load(repo)
    # Simulate the entry-building create_commit does and assert the nb sub-key.
    entry = staging.get("n.ipynb")
    tree_entry = _repo._tree_entry_for(entry)
    assert tree_entry["nb"] == {"norm_sha": notebooks.normalized_sha(nb)}
    assert tree_entry["sha256"] == entry.sha256


def test_preview_stage_suppresses_pure_rerun(repo):
    nb = _nb_bytes("z = 0\n", outputs=[], execution_count=None)
    (repo / "a.ipynb").write_bytes(nb)
    _stage_all(repo)

    rerun = _nb_bytes("z = 0\n", outputs=[{"text": "x"}], execution_count=5)
    (repo / "a.ipynb").write_bytes(rerun)
    delta = _repo.preview_stage(repo, object(), IgnoreSet.from_root(repo))
    # A pure re-run is NOT reported as modified (hybrid suppression).
    assert "a.ipynb" not in delta["modified"]
    assert "a.ipynb" not in delta["added"]


def test_preview_stage_reports_code_change(repo):
    nb = _nb_bytes("z = 0\n")
    (repo / "a.ipynb").write_bytes(nb)
    _stage_all(repo)
    (repo / "a.ipynb").write_bytes(_nb_bytes("z = 9\n"))
    delta = _repo.preview_stage(repo, object(), IgnoreSet.from_root(repo))
    assert "a.ipynb" in delta["modified"]


# --------------------------------------------------------------------------- #
# stage_paths concurrency: the public wrapper holds the versioning lock, the
# internal stage assumes it (so create_commit's stage_all does not self-deadlock).
# --------------------------------------------------------------------------- #
from jp.versioning.lock import versioning_lock  # noqa: E402


def test_public_stage_paths_takes_the_lock(repo):
    """A real (non-dry-run) public stage_paths must acquire versioning_lock.

    We hold the lock in-process (it is NON-REENTRANT -- a second acquire on a fresh
    fd raises immediately), so the public wrapper's own acquire must raise.
    """
    (repo / "f.txt").write_text("hi", encoding="utf-8")
    with versioning_lock(repo):  # noqa: SIM117
        with pytest.raises(VersioningError, match="in progress"):
            _repo.stage_paths(
                repo, object(), IgnoreSet.from_root(repo), ["f.txt"], all_files=False, dry_run=False
            )


def test_dry_run_stage_paths_is_lock_free(repo):
    """A dry-run public stage_paths writes nothing, so it must NOT take the lock."""
    (repo / "f.txt").write_text("hi", encoding="utf-8")
    with versioning_lock(repo):
        # Must not raise even though the lock is held: dry_run is lock-free.
        summary = _repo.stage_paths(
            repo, object(), IgnoreSet.from_root(repo), ["f.txt"], all_files=False, dry_run=True
        )
    assert summary["staged"] == ["f.txt"]
    assert summary["dry_run"] is True
    # Wrote nothing: no staged.json materialized.
    assert not (repo / ".jp" / "staged.json").exists()


def test_create_commit_stage_all_does_not_self_deadlock(repo, monkeypatch):
    """create_commit(stage_all=True) completes a real commit without re-locking.

    create_commit already holds versioning_lock; its internal -A stage must use the
    LOCK-FREE _stage_paths_locked, NOT the public stage_paths (which would
    re-acquire the non-reentrant lock and raise "in progress").
    """
    monkeypatch.setenv("USER", "tester")
    (repo / "f.txt").write_text("hello", encoding="utf-8")

    result = _repo.create_commit(
        repo, object(), message="c", stage_all=True, allow_empty=False, dry_run=False
    )
    # A real commit was produced (not aborted by a self-deadlock).
    assert result["sha"] and len(result["sha"]) == 64
    assert "f.txt" in result["added"]
    store = ObjectStore(repo)
    commit = _repo.read_commit(store, result["sha"])
    tree = _repo.read_tree(store, commit["tree"])
    assert "f.txt" in tree


def test_standalone_add_writes_staged_json(repo):
    """A standalone public stage_paths writes staged.json (happy path unchanged)."""
    (repo / "f.txt").write_text("hello", encoding="utf-8")
    summary = _repo.stage_paths(
        repo, object(), IgnoreSet.from_root(repo), ["f.txt"], all_files=False, dry_run=False
    )
    assert summary["staged"] == ["f.txt"]
    staged_path = repo / ".jp" / "staged.json"
    assert staged_path.is_file()
    reloaded = Staging.load(repo)
    assert reloaded.get("f.txt") is not None


# --------------------------------------------------------------------------- #
# resolve_commitish
# --------------------------------------------------------------------------- #
def _commit_chain_with_refs(root, n: int):
    """Create n real commits on the default branch; return shas oldest->newest."""
    store = ObjectStore(root)
    _refs.init_versioning(root)
    shas: list[str] = []
    parents: list[str] = []
    for i in range(n):
        tree = _repo.write_tree(store, {f"f{i}.txt": _entry(i + 1)})
        sha = _repo.write_commit(
            store,
            tree=tree,
            parents=parents,
            message=f"c{i}",
            author="a",
            timestamp_epoch=i,
            jp_version="1",
        )
        _refs.update_ref(
            root, _refs.DEFAULT_BRANCH, sha, expected=(parents[0] if parents else None)
        )
        shas.append(sha)
        parents = [sha]
    return shas


def test_resolve_commitish_head(repo):
    shas = _commit_chain_with_refs(repo, 2)
    store = ObjectStore(repo)
    assert _repo.resolve_commitish(repo, store, "HEAD") == shas[-1]


def test_resolve_commitish_branch_name(repo):
    shas = _commit_chain_with_refs(repo, 1)
    store = ObjectStore(repo)
    assert _repo.resolve_commitish(repo, store, _refs.DEFAULT_BRANCH) == shas[0]


def test_resolve_commitish_full_sha(repo):
    shas = _commit_chain_with_refs(repo, 2)
    store = ObjectStore(repo)
    assert _repo.resolve_commitish(repo, store, shas[0]) == shas[0]


def test_resolve_commitish_unique_prefix(repo):
    shas = _commit_chain_with_refs(repo, 3)
    store = ObjectStore(repo)
    target = shas[1]
    assert _repo.resolve_commitish(repo, store, target[:12]) == target


def test_resolve_commitish_ambiguous_prefix_raises(repo, monkeypatch):
    # Two real commits, then craft a guaranteed prefix collision by stubbing the
    # reachable-sha set so two distinct shas share the queried prefix. (A natural
    # sha256 collision on a short prefix is astronomically unlikely, so we make the
    # ambiguity deterministic rather than skip the branch.)
    _commit_chain_with_refs(repo, 2)
    store = ObjectStore(repo)
    prefix = "abcd"
    a = prefix + "0" * 60
    b = prefix + "1" * 60
    monkeypatch.setattr(_repo, "_all_commit_shas", lambda root, store: {a, b})
    with pytest.raises(VersioningError, match="ambiguous"):
        _repo.resolve_commitish(repo, store, prefix)


def test_resolve_commitish_unknown_raises(repo):
    _commit_chain_with_refs(repo, 1)
    store = ObjectStore(repo)
    with pytest.raises(VersioningError):
        _repo.resolve_commitish(repo, store, "ffffffffdeadbeef")  # no such prefix


def test_resolve_commitish_unborn_head_raises(repo):
    _refs.init_versioning(repo)  # HEAD points at an unborn branch (no commits)
    store = ObjectStore(repo)
    with pytest.raises(VersioningError):
        _repo.resolve_commitish(repo, store, "HEAD")

"""Tests for the checkout planning + apply logic (jp.versioning.checkout).

The checkout path is the most destructive in the whole feature, so these tests
nail down the SAFETY CONTRACT exhaustively:

* the per-file truth table (absent / equal / clean-overwrite / dirty-block /
  dirty-force / untracked-collision), with notebooks compared via the normalized
  sha (a pure re-run is NOT dirty) while the BYTES written are the faithful
  original;
* path-scoped restore does not move HEAD nor delete extras; a named path missing
  from the commit errors;
* full checkout moves HEAD -- attaching to a branch when checking out a branch /
  HEAD, detaching on a raw sha;
* extras: default keeps + reports; --remove-extra deletes tracked-deleted; an
  untracked extra requires confirmation (tty yes/no; non-tty SafetyError, NOT
  deleted);
* a blocked dirty file ABORTS the whole checkout with ZERO writes;
* a symlink at a target path -> per-file SafetyError, that file skipped, OTHERS
  still checked out, and the symlink target untouched;
* a corrupted object -> that path fails, no garbage written;
* dry-run writes nothing;
* a malicious tree key (``../x``) is refused by safe_local_dest;
* staging is rewritten to match the checked-out tree; ``.jp/index.json`` is
  byte-unchanged across a full checkout.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from jp.errors import SafetyError
from jp.versioning import checkout as co
from jp.versioning import refs, repo
from jp.versioning.checkout import Action
from jp.versioning.objects import ObjectStore, VersioningError
from jp.versioning.staging import Staging


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write(root, rel, data: bytes):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _commit_all(root, message="c", env_user="tester"):
    """Stage the whole working tree and commit it; return the commit sha."""
    os.environ.setdefault("USER", env_user)
    result = repo.create_commit(
        root,
        object(),
        message=message,
        stage_all=True,
        allow_empty=False,
        dry_run=False,
    )
    return result["sha"]


def _nb_bytes(code: str, *, outputs=None, execution_count=None) -> bytes:
    return json.dumps(
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


def _no_confirm(rels):
    raise AssertionError("confirm should not have been called")


def _yes_confirm(rels):
    return True


# --------------------------------------------------------------------------- #
# Per-file truth table -- via classify()
# --------------------------------------------------------------------------- #
def _classify(root, rel, target_bytes, head_bytes=None, *, force=False):
    """Classify a single path with explicit target + (optional) HEAD content."""
    t_sha = hashlib.sha256(target_bytes).hexdigest()
    target_entry = {"sha256": t_sha, "size": len(target_bytes), "mode": "file"}
    head_entries = {}
    if head_bytes is not None:
        h_sha = hashlib.sha256(head_bytes).hexdigest()
        head_entries[rel] = {"sha256": h_sha, "size": len(head_bytes), "mode": "file"}
    return co.classify(rel, target_entry, head_entries, root, ObjectStore(root), force=force)


def test_truth_table_absent_writes(repo):
    fp = _classify(repo, "a.txt", b"TARGET", head_bytes=b"TARGET")
    assert fp.action is Action.WRITE  # no working file -> additive restore


def test_truth_table_equal_skips(repo):
    _write(repo, "a.txt", b"TARGET")
    fp = _classify(repo, "a.txt", b"TARGET", head_bytes=b"HEAD")
    assert fp.action is Action.SKIP


def test_truth_table_clean_overwrites(repo):
    # Working == HEAD, target differs -> clean, free overwrite.
    _write(repo, "a.txt", b"HEAD")
    fp = _classify(repo, "a.txt", b"TARGET", head_bytes=b"HEAD")
    assert fp.action is Action.WRITE


def test_truth_table_dirty_blocks(repo):
    # Working differs from BOTH target and HEAD -> uncommitted edits -> blocked.
    _write(repo, "a.txt", b"DIRTY")
    fp = _classify(repo, "a.txt", b"TARGET", head_bytes=b"HEAD")
    assert fp.action is Action.BLOCKED
    assert "uncommitted" in fp.reason


def test_truth_table_dirty_force_writes(repo):
    _write(repo, "a.txt", b"DIRTY")
    fp = _classify(repo, "a.txt", b"TARGET", head_bytes=b"HEAD", force=True)
    assert fp.action is Action.WRITE


def test_truth_table_untracked_collision_blocks(repo):
    # No HEAD entry (H is None), but a same-named working file exists and differs.
    _write(repo, "a.txt", b"UNTRACKED")
    fp = _classify(repo, "a.txt", b"TARGET", head_bytes=None)
    assert fp.action is Action.BLOCKED
    assert "untracked" in fp.reason


def test_truth_table_untracked_collision_force_writes(repo):
    _write(repo, "a.txt", b"UNTRACKED")
    fp = _classify(repo, "a.txt", b"TARGET", head_bytes=None, force=True)
    assert fp.action is Action.WRITE


# --- notebooks: normalized comparison, faithful bytes -----------------------
def test_notebook_pure_rerun_is_not_dirty(repo):
    """A re-run (same code, new outputs / exec count) must classify as SKIP."""
    target = _nb_bytes("x = 1\n", outputs=[], execution_count=None)
    head = target
    # Working tree = same code but executed (outputs + exec count present).
    rerun = _nb_bytes("x = 1\n", outputs=[{"text": "1"}], execution_count=7)
    _write(repo, "nb.ipynb", rerun)

    # Build a target entry carrying the nb.norm_sha, like a real commit does.
    from jp.versioning.notebooks import normalized_sha

    t_sha = hashlib.sha256(target).hexdigest()
    target_entry = {
        "sha256": t_sha,
        "size": len(target),
        "mode": "file",
        "nb": {"norm_sha": normalized_sha(target)},
    }
    h_sha = hashlib.sha256(head).hexdigest()
    head_entries = {
        "nb.ipynb": {
            "sha256": h_sha,
            "size": len(head),
            "mode": "file",
            "nb": {"norm_sha": normalized_sha(head)},
        }
    }
    fp = co.classify("nb.ipynb", target_entry, head_entries, repo, ObjectStore(repo), force=False)
    # Normalized shas match -> SKIP (a re-run is not a change).
    assert fp.action is Action.SKIP


def test_notebook_code_edit_is_dirty(repo):
    """A real code edit (different normalized sha) is dirty and blocks."""
    from jp.versioning.notebooks import normalized_sha

    target = _nb_bytes("x = 1\n")
    head = target
    edited = _nb_bytes("x = 999\n", outputs=[{"text": "x"}], execution_count=2)
    _write(repo, "nb.ipynb", edited)

    target_entry = {
        "sha256": hashlib.sha256(target).hexdigest(),
        "size": len(target),
        "mode": "file",
        "nb": {"norm_sha": normalized_sha(target)},
    }
    head_entries = {
        "nb.ipynb": {
            "sha256": hashlib.sha256(head).hexdigest(),
            "size": len(head),
            "mode": "file",
            "nb": {"norm_sha": normalized_sha(head)},
        }
    }
    fp = co.classify("nb.ipynb", target_entry, head_entries, repo, ObjectStore(repo), force=False)
    assert fp.action is Action.BLOCKED


def test_notebook_checkout_writes_faithful_original(repo, monkeypatch):
    """Checkout of a notebook restores the ORIGINAL bytes (outputs included)."""
    monkeypatch.setenv("USER", "tester")
    # Commit a notebook WITH outputs (the faithful original).
    original = _nb_bytes("x = 1\n", outputs=[{"text": "hi"}], execution_count=5)
    _write(repo, "nb.ipynb", original)
    c1 = _commit_all(repo)

    # Re-run the notebook locally (same code, different outputs) -> not dirty.
    rerun = _nb_bytes("x = 1\n", outputs=[{"text": "bye"}], execution_count=9)
    _write(repo, "nb.ipynb", rerun)

    co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=False,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    # A pure re-run is SKIPPED (not dirty), so the working file is untouched.
    assert (repo / "nb.ipynb").read_bytes() == rerun

    # Now actually diverge the code so checkout must restore -- force overwrite.
    _write(repo, "nb.ipynb", _nb_bytes("x = 2\n"))
    co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=True,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    # The restored bytes are the faithful ORIGINAL (outputs included), not normalized.
    assert (repo / "nb.ipynb").read_bytes() == original


# --------------------------------------------------------------------------- #
# Path-scoped mode
# --------------------------------------------------------------------------- #
def test_path_scoped_restores_only_named_and_keeps_head(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _write(repo, "b.txt", b"B1")
    c1 = _commit_all(repo)

    # Advance: change both files + add an extra, commit again.
    _write(repo, "a.txt", b"A2")
    _write(repo, "b.txt", b"B2")
    _write(repo, "c.txt", b"C2")
    c2 = _commit_all(repo)
    head_before = refs.resolve_head(repo)
    assert head_before == c2

    # Restore ONLY a.txt from c1; b.txt and HEAD must be untouched; c.txt kept.
    result = co.apply_checkout(
        repo,
        object(),
        c1,
        ["a.txt"],
        force=False,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    assert (repo / "a.txt").read_bytes() == b"A1"  # restored
    assert (repo / "b.txt").read_bytes() == b"B2"  # untouched
    assert (repo / "c.txt").read_bytes() == b"C2"  # extras never deleted
    assert refs.resolve_head(repo) == head_before  # HEAD did NOT move
    assert result.head_moved is False


def test_path_scoped_missing_path_errors(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    with pytest.raises(VersioningError):
        co.apply_checkout(
            repo,
            object(),
            c1,
            ["nope.txt"],
            force=False,
            remove_extra=False,
            dry_run=False,
            confirm_delete_untracked=_no_confirm,
        )


# --------------------------------------------------------------------------- #
# Full checkout: HEAD attach vs detach
# --------------------------------------------------------------------------- #
def test_full_checkout_branch_attaches_head(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _commit_all(repo)
    # Checking out a branch name attaches HEAD to it.
    co.apply_checkout(
        repo,
        object(),
        "main",
        [],
        force=False,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    head = refs.read_head(repo)
    assert head is not None and head.symbolic and head.branch == "main"


def test_full_checkout_raw_sha_detaches_head(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    # Checking out a raw sha detaches HEAD.
    co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=False,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    head = refs.read_head(repo)
    assert head is not None and not head.symbolic and head.target == c1


def test_head_keyword_attaches_when_symbolic(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _commit_all(repo)
    co.apply_checkout(
        repo,
        object(),
        "HEAD",
        [],
        force=False,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    head = refs.read_head(repo)
    assert head is not None and head.symbolic and head.branch == "main"


# --------------------------------------------------------------------------- #
# Extras
# --------------------------------------------------------------------------- #
def test_extras_default_keeps_and_reports(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    # An extra not present in c1.
    _write(repo, "extra.txt", b"EXTRA")
    result = co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=False,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    assert (repo / "extra.txt").exists()  # kept
    assert "extra.txt" in result.extras_kept
    assert result.deleted == []


def test_remove_extra_deletes_tracked_deleted(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _write(repo, "gone.txt", b"GONE")
    c1 = _commit_all(repo)  # both tracked
    # New commit drops gone.txt from the tree.
    (repo / "gone.txt").unlink()
    c2 = _commit_all(repo)
    # Recreate gone.txt in the working tree (so it is an extra vs c2, but tracked
    # in HEAD=c2? No -- it's deleted in c2's tree). Bring it back on disk:
    _write(repo, "gone.txt", b"GONE")
    # HEAD is c2; gone.txt is NOT in c2's tree, so it's an untracked-vs-HEAD extra?
    # It IS recoverable (it lives in c1's history) but the rule keys on HEAD's tree.
    # To test the TRACKED-deleted branch, check out c2 while HEAD is at c2 with the
    # file present: it is absent from HEAD's tree -> treated as untracked. So craft
    # the tracked case directly: HEAD has the file, target does not.
    assert refs.resolve_head(repo) == c2
    # Make HEAD c1 (file tracked there) then check out c2 (file absent in target).
    refs.set_head_branch(repo, "main")
    refs.write_ref(repo, "main", c1)
    result = co.apply_checkout(
        repo,
        object(),
        c2,
        [],
        force=False,
        remove_extra=True,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    # gone.txt IS in HEAD(c1)'s tree -> tracked-deleted -> removed without a prompt.
    assert not (repo / "gone.txt").exists()
    assert "gone.txt" in result.deleted


def test_remove_extra_untracked_requires_confirm_tty_yes(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    _write(repo, "new.txt", b"NEW")  # never committed -> untracked
    calls = []

    def confirm(rels):
        calls.append(list(rels))
        return True

    result = co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=False,
        remove_extra=True,
        dry_run=False,
        confirm_delete_untracked=confirm,
    )
    assert calls == [["new.txt"]]
    assert not (repo / "new.txt").exists()
    assert "new.txt" in result.deleted


def test_remove_extra_untracked_confirm_no_keeps(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    _write(repo, "new.txt", b"NEW")
    result = co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=False,
        remove_extra=True,
        dry_run=False,
        confirm_delete_untracked=lambda rels: False,
    )
    assert (repo / "new.txt").exists()  # declined -> kept
    assert "new.txt" in result.extras_kept


def test_remove_extra_untracked_non_tty_raises_and_keeps(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    _write(repo, "new.txt", b"NEW")

    def refusing(rels):
        raise SafetyError("non-tty refuses to delete untracked work")

    with pytest.raises(SafetyError):
        co.apply_checkout(
            repo,
            object(),
            c1,
            [],
            force=False,
            remove_extra=True,
            dry_run=False,
            confirm_delete_untracked=refusing,
        )
    # The untracked file must still be present (never deleted before refusal).
    assert (repo / "new.txt").exists()


# --------------------------------------------------------------------------- #
# Blocked dirty file aborts with ZERO writes
# --------------------------------------------------------------------------- #
def test_blocked_dirty_aborts_with_zero_writes(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _write(repo, "b.txt", b"B1")
    c1 = _commit_all(repo)
    # New commit changes both.
    _write(repo, "a.txt", b"A2")
    _write(repo, "b.txt", b"B2")
    _commit_all(repo)  # HEAD = c2 (a.txt=A2, b.txt=B2)
    # Make a.txt dirty (differs from HEAD=c2 and from target=c1) and b.txt clean-ish.
    _write(repo, "a.txt", b"DIRTY")  # != c1, != c2 -> dirty
    _write(repo, "b.txt", b"B2")  # == HEAD c2

    before_a = (repo / "a.txt").read_bytes()
    before_b = (repo / "b.txt").read_bytes()

    with pytest.raises(SafetyError):
        co.apply_checkout(
            repo,
            object(),
            c1,
            [],
            force=False,
            remove_extra=False,
            dry_run=False,
            confirm_delete_untracked=_no_confirm,
        )
    # ZERO writes: nothing changed, not even the clean b.txt that WOULD have been
    # overwritten -- the abort happens before any write.
    assert (repo / "a.txt").read_bytes() == before_a
    assert (repo / "b.txt").read_bytes() == before_b


# --------------------------------------------------------------------------- #
# Symlink resilience
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_symlink_at_target_skips_that_file_others_proceed(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _write(repo, "b.txt", b"B1")
    c1 = _commit_all(repo)
    # Diverge both with --force so they would all be written.
    _write(repo, "a.txt", b"A2")
    _write(repo, "b.txt", b"B2")

    # Replace a.txt with a symlink to an outside secret file.
    outside = repo.parent / "secret.txt"
    outside.write_bytes(b"SECRET")
    (repo / "a.txt").unlink()
    try:
        os.symlink(str(outside), str(repo / "a.txt"))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")

    result = co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=True,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    # a.txt failed (symlink), b.txt still restored.
    failed = [rel for rel, _ in result.failures]
    assert "a.txt" in failed
    assert "b.txt" in result.written
    assert (repo / "b.txt").read_bytes() == b"B1"
    # The symlink TARGET (outside secret) was never written through.
    assert outside.read_bytes() == b"SECRET"


# --------------------------------------------------------------------------- #
# Integrity: a corrupt object fails the path, no garbage written
# --------------------------------------------------------------------------- #
def test_corrupt_object_fails_path_no_garbage(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    _write(repo, "a.txt", b"DIRTY")  # will be force-overwritten

    # Corrupt the stored blob for a.txt (flip the body so the re-hash fails).
    store = ObjectStore(repo)
    blob_sha = hashlib.sha256(b"A1").hexdigest()
    blob_path = store.path_for(blob_sha)
    raw = blob_path.read_bytes()
    blob_path.write_bytes(raw[:1] + b"\xff" + raw[2:] if len(raw) > 2 else raw + b"\xff")

    result = co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=True,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    failed = [rel for rel, _ in result.failures]
    assert "a.txt" in failed
    # No garbage: a.txt still holds the DIRTY content (never overwritten with junk).
    assert (repo / "a.txt").read_bytes() == b"DIRTY"


# --------------------------------------------------------------------------- #
# Dry run writes nothing
# --------------------------------------------------------------------------- #
def test_dry_run_writes_nothing(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    _write(repo, "a.txt", b"A2")  # clean? No -- != HEAD. Make it clean:
    # Recommit so HEAD == A2, then create a target divergence cleanly.
    _commit_all(repo)  # HEAD = c2 (a.txt = A2)
    _write(repo, "a.txt", b"A2")  # == HEAD c2 -> clean overwrite candidate
    _write(repo, "extra.txt", b"EXTRA")
    head_before = refs.resolve_head(repo)

    result = co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=False,
        remove_extra=True,
        dry_run=True,
        confirm_delete_untracked=_no_confirm,
    )
    assert result.dry_run is True
    # Nothing written / deleted / moved.
    assert (repo / "a.txt").read_bytes() == b"A2"
    assert (repo / "extra.txt").exists()
    assert refs.resolve_head(repo) == head_before
    # The plan still PREVIEWS the work. extra.txt is untracked (never committed),
    # so the dry-run split predicts it as an untracked delete (needs confirmation /
    # refused in non-tty) -- and the confirm callable is NEVER invoked in a dry run.
    assert "a.txt" in result.written
    assert result.deleted == []
    assert result.would_delete_untracked == ["extra.txt"]
    assert result.would_delete_tracked == []


# --------------------------------------------------------------------------- #
# Malicious tree key refused by safe_local_dest
# --------------------------------------------------------------------------- #
def test_malicious_tree_key_refused(repo, monkeypatch):
    """A tree object built with a ``../x`` key must never escape the working tree."""
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _commit_all(repo)
    store = ObjectStore(repo)

    # Craft a malicious tree object directly (bypassing build_tree_bytes' guard) so
    # we exercise checkout's OWN defense via safe_local_dest.
    evil_payload = b"EVIL"
    evil_sha = store.write(evil_payload)
    tree_obj = {
        "version": 1,
        "entries": {
            "../escape.txt": {
                "sha256": evil_sha,
                "size": len(evil_payload),
                "mode": "file",
            }
        },
    }
    tree_bytes = json.dumps(tree_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tree_sha = store.write(tree_bytes)

    # read_tree would reject the unsafe key first -- assert checkout refuses it
    # (whether via read_tree's normalize_rel or safe_local_dest, the escape never
    # writes). We drive plan_checkout directly against the crafted tree's commit.
    from jp.versioning.repo import write_commit

    commit_sha = write_commit(
        store,
        tree=tree_sha,
        parents=[],
        message="evil",
        author="x",
        timestamp_epoch=0,
        jp_version="test",
    )
    outside = repo.parent / "escape.txt"
    with pytest.raises(VersioningError):
        co.apply_checkout(
            repo,
            object(),
            commit_sha,
            [],
            force=True,
            remove_extra=False,
            dry_run=False,
            confirm_delete_untracked=_no_confirm,
        )
    assert not outside.exists()  # the escape never materialized


# --------------------------------------------------------------------------- #
# Staging sync + index.json untouched
# --------------------------------------------------------------------------- #
def test_full_checkout_syncs_staging_and_leaves_index_unchanged(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _write(repo, "b.txt", b"B1")
    c1 = _commit_all(repo)
    # Advance: drop b.txt, add c.txt.
    (repo / "b.txt").unlink()
    _write(repo, "c.txt", b"C2")
    _commit_all(repo)

    # Snapshot .jp/index.json bytes (the sync base must be byte-unchanged).
    index_path = repo / ".jp" / "index.json"
    index_before = index_path.read_bytes() if index_path.exists() else None

    co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=True,
        remove_extra=True,
        dry_run=False,
        confirm_delete_untracked=_yes_confirm,
    )

    # Staging now EQUALS c1's tree: a.txt + b.txt, no c.txt.
    staging = Staging.load(repo)
    assert set(staging.entries) == {"a.txt", "b.txt"}
    assert staging.get("a.txt").sha256 == hashlib.sha256(b"A1").hexdigest()

    # .jp/index.json byte-unchanged (versioning never touches the sync base).
    index_after = index_path.read_bytes() if index_path.exists() else None
    assert index_after == index_before


def test_full_checkout_into_clean_tree_writes_all_and_warns(repo, monkeypatch):
    """Checking out a commit into an empty tree restores every file additively."""
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _write(repo, "dir/b.txt", b"B1")
    c1 = _commit_all(repo)
    # Remove all working files.
    (repo / "a.txt").unlink()
    (repo / "dir" / "b.txt").unlink()

    result = co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=False,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    assert (repo / "a.txt").read_bytes() == b"A1"
    assert (repo / "dir" / "b.txt").read_bytes() == b"B1"
    assert set(result.written) == {"a.txt", "dir/b.txt"}
    assert result.head_moved is True


# --------------------------------------------------------------------------- #
# FIX 1: a partial write must not let HEAD / staging lie
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_partial_failure_leaves_head_and_staging_unchanged(repo, monkeypatch):
    """A symlink-conflict in FULL mode -> HEAD UNCHANGED, staging UNCHANGED, the
    good files written, result is incomplete (the command maps this to PARTIAL)."""
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _write(repo, "b.txt", b"B1")
    c1 = _commit_all(repo)
    # Advance to c2 so HEAD != c1; record HEAD + staging snapshots.
    _write(repo, "a.txt", b"A2")
    _write(repo, "b.txt", b"B2")
    c2 = _commit_all(repo)
    head_before = refs.resolve_head(repo)
    assert head_before == c2
    staged_before = repo / ".jp" / "staged.json"
    staged_bytes_before = staged_before.read_bytes() if staged_before.exists() else None

    # Plant a symlink at a.txt so its write fails; b.txt should still be restored.
    outside = repo.parent / "secret.txt"
    outside.write_bytes(b"SECRET")
    (repo / "a.txt").unlink()
    try:
        os.symlink(str(outside), str(repo / "a.txt"))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")

    result = co.apply_checkout(
        repo,
        object(),
        c1,
        [],
        force=True,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )

    # a.txt failed; b.txt was still restored (per-file resilience).
    failed = [rel for rel, _ in result.failures]
    assert "a.txt" in failed
    assert "b.txt" in result.written
    assert (repo / "b.txt").read_bytes() == b"B1"
    # The symlink target was never written through.
    assert outside.read_bytes() == b"SECRET"

    # FIX 1: HEAD did NOT move and staging was NOT rewritten -- they don't lie about
    # a tree that was only half-materialized.
    assert result.incomplete is True
    assert result.head_moved is False
    assert refs.resolve_head(repo) == head_before
    staged_bytes_after = staged_before.read_bytes() if staged_before.exists() else None
    assert staged_bytes_after == staged_bytes_before


# --------------------------------------------------------------------------- #
# FIX 2: untracked-extra confirmation resolved BEFORE any write (zero side effects)
# --------------------------------------------------------------------------- #
def test_untracked_extra_refusal_aborts_with_zero_writes(repo, monkeypatch):
    """A non-tty --remove-extra refusal must abort BEFORE any write, so even a
    --force overwrite that the same run would have applied is NOT performed."""
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    # Advance so a.txt diverges -> with --force the full run WOULD overwrite it.
    _write(repo, "a.txt", b"A2")
    _commit_all(repo)
    _write(repo, "a.txt", b"DIRTY")  # would be force-overwritten back to A1
    _write(repo, "new.txt", b"NEW")  # untracked extra -> triggers the gate

    def refusing(rels):
        raise SafetyError("non-tty refuses to delete untracked work")

    with pytest.raises(SafetyError):
        co.apply_checkout(
            repo,
            object(),
            c1,
            [],
            force=True,
            remove_extra=True,
            dry_run=False,
            confirm_delete_untracked=refusing,
        )
    # ZERO side effects: the would-be force overwrite did NOT happen, the untracked
    # file is intact, and HEAD did not move.
    assert (repo / "a.txt").read_bytes() == b"DIRTY"
    assert (repo / "new.txt").read_bytes() == b"NEW"


# --------------------------------------------------------------------------- #
# FIX 3: symmetric notebook comparison -- identical content never false-BLOCKs
# --------------------------------------------------------------------------- #
def test_notebook_without_nb_subkey_equal_content_skips(repo):
    """A notebook target entry that LACKS nb.norm_sha but whose content equals the
    working file must SKIP (derive the target's normalized sha on the fly), not
    BLOCK on an asymmetric normalized-vs-raw compare."""
    from jp.versioning.notebooks import normalized_sha

    # Working file: a re-run of the same code (outputs present).
    working = _nb_bytes("x = 1\n", outputs=[{"text": "1"}], execution_count=4)
    _write(repo, "nb.ipynb", working)

    # Target blob = the SAME normalized notebook but stored WITHOUT an nb sub-key
    # (e.g. legacy-committed as a plain blob). Put the blob in the store so the
    # on-the-fly normalization can read it.
    store = ObjectStore(repo)
    target_bytes = _nb_bytes("x = 1\n", outputs=[], execution_count=None)
    t_sha = store.write(target_bytes)
    target_entry = {"sha256": t_sha, "size": len(target_bytes), "mode": "file"}  # NO "nb"

    # HEAD entry also lacks nb; same code.
    head_bytes = _nb_bytes("x = 1\n", outputs=[{"text": "old"}], execution_count=1)
    h_sha = store.write(head_bytes)
    head_entries = {"nb.ipynb": {"sha256": h_sha, "size": len(head_bytes), "mode": "file"}}

    # Sanity: the working + target normalize to the same value (so SKIP is correct).
    assert normalized_sha(working) == normalized_sha(target_bytes)

    fp = co.classify("nb.ipynb", target_entry, head_entries, repo, store, force=False)
    assert fp.action is Action.SKIP


def test_opaque_notebook_falls_back_to_raw_compare(repo):
    """A truly opaque .ipynb (normalization returns None, e.g. v3) compares RAW on
    both sides: identical raw content SKIPs, different raw content BLOCKs."""
    store = ObjectStore(repo)
    # nbformat v3 shape: cells live under worksheets -> normalize_notebook -> None.
    opaque = json.dumps({"worksheets": [{"cells": []}], "nbformat": 3}).encode("utf-8")
    _write(repo, "old.ipynb", opaque)
    sha = store.write(opaque)
    target_entry = {"sha256": sha, "size": len(opaque), "mode": "file"}
    # Working content == target raw content -> SKIP.
    fp = co.classify("old.ipynb", target_entry, {}, repo, store, force=False)
    assert fp.action is Action.SKIP

    # Different working content -> dirty -> BLOCK (no HEAD entry).
    _write(repo, "old.ipynb", opaque + b"\n")
    fp2 = co.classify("old.ipynb", target_entry, {}, repo, store, force=False)
    assert fp2.action is Action.BLOCKED


# --------------------------------------------------------------------------- #
# FIX 4c: --remove-extra delete-FAILURE path (recorded as a failure, not a crash)
# --------------------------------------------------------------------------- #
def test_remove_extra_delete_failure_is_recorded_not_crash(repo, monkeypatch):
    """A tracked-extra whose unlink FAILS (e.g. an OS error) is recorded as a
    per-file failure -- the run does not crash -- and that makes the full checkout
    incomplete (HEAD not moved, staging not rewritten)."""
    monkeypatch.setenv("USER", "tester")
    _write(repo, "a.txt", b"A1")
    _write(repo, "gone.txt", b"GONE")
    c1 = _commit_all(repo)  # both tracked in c1
    # c2 drops gone.txt from the tree.
    (repo / "gone.txt").unlink()
    c2 = _commit_all(repo)
    assert refs.resolve_head(repo) == c2

    # Recreate gone.txt on disk and point HEAD at c1, so checking out c2 sees it as
    # a TRACKED-deleted extra (in HEAD=c1's tree) -> deleted without a prompt.
    _write(repo, "gone.txt", b"GONE")
    refs.set_head_branch(repo, "main")
    refs.write_ref(repo, "main", c1)
    head_before = refs.resolve_head(repo)

    # Make the unlink of gone.txt fail (simulating e.g. a permission error). Other
    # unlinks pass through untouched.
    real_unlink = os.unlink

    def flaky_unlink(path, *a, **k):
        if str(path).endswith("gone.txt"):
            raise OSError("simulated unlink failure")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", flaky_unlink)

    result = co.apply_checkout(
        repo,
        object(),
        c2,
        [],
        force=False,
        remove_extra=True,
        dry_run=False,
        confirm_delete_untracked=_no_confirm,
    )
    # The delete failed (recorded), did NOT crash; gone.txt still present.
    failed = [rel for rel, _ in result.failures]
    assert "gone.txt" in failed
    assert "gone.txt" not in result.deleted
    assert (repo / "gone.txt").exists()
    # A per-file failure makes the full run incomplete (HEAD not moved).
    assert result.incomplete is True
    assert result.head_moved is False
    assert refs.resolve_head(repo) == head_before

"""Task 7: ``jp push`` PATH-scoped push (Feature A) + the commit-gate (Feature B).

Both are OPT-IN. The two load-bearing invariants under test:

* A repo with NO versioning (no HEAD) and a no-PATH push behaves EXACTLY as
  today -- the gate is never reached and ``sync.push`` is called with
  ``only_paths=None`` (the engine's tests in ``test_sync_push.py`` cover that
  default byte-for-byte; here we prove the command layer never perturbs it).
* The gate is OFFLINE and CI-safe: a non-tty run with uncommitted changes prints
  ONE warning and proceeds; it never blocks and never prompts.

These drive the command layer (``jp.commands.push.run``) against the real
on-disk ``repo`` fixture + the in-memory FakeApi, mirroring the patterns in
``test_sync_push.py`` and ``test_commit_log.py``.
"""

from __future__ import annotations

import argparse

import pytest

import jp.commands.push as push_cmd
from conftest import write_file
from jp import config as config_mod
from jp import sync
from jp.commands._context import RepoContext
from jp.errors import EXIT_OK, EXIT_USAGE
from jp.ignore import IgnoreSet
from jp.index import Index
from jp.versioning import refs
from jp.versioning import repo as vrepo


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ctx(root, cfg) -> RepoContext:
    return RepoContext(
        root=root,
        cfg=cfg,
        index=Index.load(root),
        ignore=IgnoreSet.from_root(root),
    )


def _patch(monkeypatch, root, cfg, fake_api):
    """Wire ``push_cmd`` to a fixed ctx + FakeApi; return the ctx."""
    ctx = _ctx(root, cfg)
    monkeypatch.setattr(push_cmd, "load_repo", lambda: ctx)
    monkeypatch.setattr(push_cmd._context, "build_api", lambda c: fake_api)
    return ctx


def _args(path=None, dry_run=False, mirror=None, yes=False, raw=False) -> argparse.Namespace:
    return argparse.Namespace(
        path=list(path or []), dry_run=dry_run, mirror=mirror, yes=yes, raw=raw
    )


def _commit_all(root, cfg):
    """Stage the whole tree and commit it (so HEAD resolves -> versioning active)."""
    return vrepo.create_commit(
        root, cfg, message="c", stage_all=True, allow_empty=False, dry_run=False
    )["sha"]


def _tty(monkeypatch, value: bool):
    """Force ``sys.stdin.isatty()`` (used by both ui.ask_line and the gate)."""
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: value)


def _no_mirror(monkeypatch):
    """Disable the Task 8 post-push history mirror for commit-gate-only tests.

    The mirror's ask-once prompt also reads ``ui.ask_line``; these tests assert
    the COMMIT-GATE prompt behavior in isolation, so we neutralize the orthogonal
    mirror hook to keep the ``ask_line`` sentinel meaningful."""
    monkeypatch.setattr(push_cmd, "_maybe_mirror_history", lambda *a, **k: None)


# =========================================================================== #
# FEATURE A: PATH-scoped push
# =========================================================================== #
def test_scoped_push_uploads_only_named_file(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"aaa")
    write_file(repo, "b.txt", b"bbb")

    rc = push_cmd.run(_args(path=["a.txt"]))
    assert rc == EXIT_OK
    # Only a.txt was PUT; b.txt was never touched.
    puts = [p for (m, p) in fake_api.calls if m == "PUT"]
    assert "users/alice/a.txt" in puts
    assert "users/alice/b.txt" not in puts


def test_scoped_push_directory_includes_children(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "sub/one.txt", b"1")
    write_file(repo, "sub/deep/two.txt", b"2")
    write_file(repo, "other.txt", b"x")

    rc = push_cmd.run(_args(path=["sub"]))
    assert rc == EXIT_OK
    puts = {p for (m, p) in fake_api.calls if m == "PUT"}
    assert "users/alice/sub/one.txt" in puts
    assert "users/alice/sub/deep/two.txt" in puts
    assert "users/alice/other.txt" not in puts


def test_scoped_push_unknown_path_errors_nonzero(repo, cfg, fake_api, monkeypatch, capsys):
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"a")

    rc = push_cmd.run(_args(path=["nope.txt"]))
    assert rc == EXIT_USAGE
    err = capsys.readouterr().err
    assert "nope.txt" in err
    # Nothing was uploaded.
    assert [m for (m, _) in fake_api.calls if m == "PUT"] == []


def test_scoped_push_partial_match_still_pushes_the_good_one(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"a")

    # One good path + one bad path: the good one pushes, exit is OK (something matched).
    rc = push_cmd.run(_args(path=["a.txt", "nope.txt"]))
    assert rc == EXIT_OK
    assert "users/alice/a.txt" in [p for (m, p) in fake_api.calls if m == "PUT"]


def test_scoped_push_never_triggers_mirror_delete(repo, fake_api, monkeypatch):
    # Mirror is ON, and the remote has an extra file. A scoped push must NOT offer
    # to delete it (additive-only), so nothing is ever DELETEd.
    from jp.config import Config

    cfg = Config(base_url="https://hub.example/api", prefix="users/alice", mirror=True)
    _patch(monkeypatch, repo, cfg, fake_api)
    fake_api.seed("users/alice/remote_only.txt", b"keep me")
    write_file(repo, "a.txt", b"a")

    rc = push_cmd.run(_args(path=["a.txt"], yes=True))
    assert rc == EXIT_OK
    assert fake_api.deletes == []
    assert "users/alice/remote_only.txt" in fake_api.files


def test_scoped_push_dry_run_shows_scope_writes_nothing(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"a")
    write_file(repo, "b.txt", b"b")

    rc = push_cmd.run(_args(path=["a.txt"], dry_run=True))
    assert rc == EXIT_OK
    assert fake_api.calls == []  # no remote mutation at all


def test_no_path_push_passes_only_paths_none(repo, cfg, fake_api, monkeypatch):
    """The opt-in invariant for Feature A: a no-PATH push calls sync.push with
    only_paths=None, i.e. byte-identical to the pre-Task-7 default."""
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"a")
    seen = {}

    real_push = sync.push

    def spy(*a, **kw):
        seen["only_paths"] = kw.get("only_paths", "MISSING")
        return real_push(*a, **kw)

    monkeypatch.setattr(push_cmd.sync, "push", spy)
    rc = push_cmd.run(_args())
    assert rc == EXIT_OK
    assert seen["only_paths"] is None


# =========================================================================== #
# FEATURE B: commit-gate opt-in invariant (no HEAD -> never prompts)
# =========================================================================== #
def test_no_versioning_never_prompts(repo, cfg, fake_api, monkeypatch):
    """A repo with no HEAD must never reach the gate's prompt path."""
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"a")

    # If anything tried to prompt, ask_line would be called -> fail loudly.
    called = {"asked": False}
    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda p: called.__setitem__("asked", True) or "")
    # Even pretending to be a tty must not trigger a prompt without a HEAD.
    _tty(monkeypatch, True)

    rc = push_cmd.run(_args())
    assert rc == EXIT_OK
    assert called["asked"] is False
    assert "users/alice/a.txt" in fake_api.files


# =========================================================================== #
# FEATURE B: commit-gate, interactive (tty) -- the four choices
# =========================================================================== #
def test_gate_choice_c_commits_then_pushes(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    _no_mirror(monkeypatch)
    write_file(repo, "a.txt", b"v1")
    _commit_all(repo, cfg)  # HEAD now exists
    write_file(repo, "a.txt", b"v2 uncommitted")  # working differs from HEAD
    _tty(monkeypatch, True)

    answers = iter(["c", "my message"])
    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda prompt: next(answers))

    head_before = refs.resolve_head(repo)
    rc = push_cmd.run(_args())
    assert rc == EXIT_OK
    # A new commit was created (HEAD advanced) AND the file was pushed.
    assert refs.resolve_head(repo) != head_before
    assert fake_api.files["users/alice/a.txt"] == b"v2 uncommitted"


def test_gate_choice_p_pushes_raw_no_commit(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    _no_mirror(monkeypatch)
    write_file(repo, "a.txt", b"v1")
    _commit_all(repo, cfg)
    write_file(repo, "a.txt", b"v2")
    _tty(monkeypatch, True)

    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda prompt: "p")
    head_before = refs.resolve_head(repo)
    rc = push_cmd.run(_args())
    assert rc == EXIT_OK
    assert refs.resolve_head(repo) == head_before  # NO new commit
    assert fake_api.files["users/alice/a.txt"] == b"v2"  # pushed raw


def test_gate_choice_a_persists_never_and_pushes(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    _no_mirror(monkeypatch)
    write_file(repo, "a.txt", b"v1")
    _commit_all(repo, cfg)
    write_file(repo, "a.txt", b"v2")
    _tty(monkeypatch, True)

    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda prompt: "a")
    rc = push_cmd.run(_args())
    assert rc == EXIT_OK
    # Config persisted to disk: push_prompt is now "never".
    reloaded = config_mod.load(repo)
    assert reloaded.versioning_push_prompt == "never"
    assert fake_api.files["users/alice/a.txt"] == b"v2"


def test_gate_choice_x_cancels_pushes_nothing(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"v1")
    _commit_all(repo, cfg)
    write_file(repo, "a.txt", b"v2")
    _tty(monkeypatch, True)

    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda prompt: "x")
    rc = push_cmd.run(_args())
    assert rc == EXIT_OK  # cancel is a clean exit
    # NOTHING was sent: build_api was patched but no PUT ever happened.
    assert [m for (m, _) in fake_api.calls if m == "PUT"] == []


def test_gate_unrecognized_then_cancel(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"v1")
    _commit_all(repo, cfg)
    write_file(repo, "a.txt", b"v2")
    _tty(monkeypatch, True)

    answers = iter(["huh?", "x"])
    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda prompt: next(answers))
    rc = push_cmd.run(_args())
    assert rc == EXIT_OK
    assert [m for (m, _) in fake_api.calls if m == "PUT"] == []


def test_gate_empty_answer_is_cancel(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"v1")
    _commit_all(repo, cfg)
    write_file(repo, "a.txt", b"v2")
    _tty(monkeypatch, True)

    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda prompt: "")
    rc = push_cmd.run(_args())
    assert rc == EXIT_OK
    assert [m for (m, _) in fake_api.calls if m == "PUT"] == []


# =========================================================================== #
# FEATURE B: non-tty (CI) -- warn + proceed, never block, never prompt
# =========================================================================== #
def test_gate_non_tty_warns_and_pushes(repo, cfg, fake_api, monkeypatch, capsys):
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"v1")
    _commit_all(repo, cfg)
    write_file(repo, "a.txt", b"v2")
    _tty(monkeypatch, False)  # CI / pipe

    # ask_line must never be reached in non-tty.
    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda p: pytest.fail("gate prompted in non-tty"))
    rc = push_cmd.run(_args())
    assert rc == EXIT_OK
    err = capsys.readouterr().err
    assert "uncommitted changes are not versioned" in err
    assert fake_api.files["users/alice/a.txt"] == b"v2"  # pushed anyway


# =========================================================================== #
# FEATURE B: skip paths -- --raw and push_prompt=never
# =========================================================================== #
def test_raw_skips_gate(repo, cfg, fake_api, monkeypatch):
    _patch(monkeypatch, repo, cfg, fake_api)
    _no_mirror(monkeypatch)
    write_file(repo, "a.txt", b"v1")
    _commit_all(repo, cfg)
    write_file(repo, "a.txt", b"v2")
    _tty(monkeypatch, True)

    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda p: pytest.fail("gate prompted under --raw"))
    rc = push_cmd.run(_args(raw=True))
    assert rc == EXIT_OK
    assert fake_api.files["users/alice/a.txt"] == b"v2"


def test_push_prompt_never_skips_gate(repo, fake_api, monkeypatch):
    from jp.config import Config

    cfg = Config(
        base_url="https://hub.example/api",
        prefix="users/alice",
        versioning_push_prompt="never",
    )
    _patch(monkeypatch, repo, cfg, fake_api)
    _no_mirror(monkeypatch)
    write_file(repo, "a.txt", b"v1")
    _commit_all(repo, cfg)
    write_file(repo, "a.txt", b"v2")
    _tty(monkeypatch, True)

    monkeypatch.setattr(
        push_cmd.ui, "ask_line", lambda p: pytest.fail("gate prompted with push_prompt=never")
    )
    rc = push_cmd.run(_args())
    assert rc == EXIT_OK
    assert fake_api.files["users/alice/a.txt"] == b"v2"


# =========================================================================== #
# FEATURE B: gate scoping (§5.1) -- the gate considers only the pushed scope
# =========================================================================== #
def test_gate_scope_clean_no_prompt(repo, cfg, fake_api, monkeypatch):
    """push a.txt while only b.txt is uncommitted -> the scope is clean -> no prompt."""
    _patch(monkeypatch, repo, cfg, fake_api)
    _no_mirror(monkeypatch)
    write_file(repo, "a.txt", b"a1")
    write_file(repo, "b.txt", b"b1")
    _commit_all(repo, cfg)  # both committed
    write_file(repo, "b.txt", b"b2 uncommitted")  # ONLY b changed
    _tty(monkeypatch, True)

    monkeypatch.setattr(
        push_cmd.ui, "ask_line", lambda p: pytest.fail("gate prompted for a clean scope")
    )
    rc = push_cmd.run(_args(path=["a.txt"]))
    assert rc == EXIT_OK


def test_gate_scope_dirty_prompts(repo, cfg, fake_api, monkeypatch):
    """push a.txt while a.txt IS uncommitted -> the scope is dirty -> prompt."""
    _patch(monkeypatch, repo, cfg, fake_api)
    _no_mirror(monkeypatch)
    write_file(repo, "a.txt", b"a1")
    _commit_all(repo, cfg)
    write_file(repo, "a.txt", b"a2 uncommitted")
    _tty(monkeypatch, True)

    asked = {"n": 0}

    def fake_ask(prompt):
        asked["n"] += 1
        return "p"  # push raw once

    monkeypatch.setattr(push_cmd.ui, "ask_line", fake_ask)
    rc = push_cmd.run(_args(path=["a.txt"]))
    assert rc == EXIT_OK
    assert asked["n"] >= 1  # the gate DID prompt for the dirty scope


# =========================================================================== #
# FEATURE B: --dry-run does NOT persist config and does NOT commit
# =========================================================================== #
def test_dry_run_does_not_prompt_commit_or_persist(repo, cfg, fake_api, monkeypatch, capsys):
    _patch(monkeypatch, repo, cfg, fake_api)
    write_file(repo, "a.txt", b"v1")
    head_sha = _commit_all(repo, cfg)
    write_file(repo, "a.txt", b"v2")
    _tty(monkeypatch, True)

    monkeypatch.setattr(
        push_cmd.ui, "ask_line", lambda p: pytest.fail("gate prompted during --dry-run")
    )
    rc = push_cmd.run(_args(dry_run=True))
    assert rc == EXIT_OK
    # No commit (HEAD unchanged), no config write (still "ask"), no remote write.
    assert refs.resolve_head(repo) == head_sha
    assert config_mod.load(repo).versioning_push_prompt == "ask"
    assert fake_api.calls == []
    out = capsys.readouterr().out
    assert "uncommitted change" in out  # the informational note


# =========================================================================== #
# FEATURE B: notebook hybrid -- a pure re-run is NOT an uncommitted change
# =========================================================================== #
def _nb_bytes(*, output_text, execution_count) -> bytes:
    import json

    nb = {
        "cells": [
            {
                "cell_type": "code",
                "source": ["print('hi')\n"],
                "outputs": (
                    []
                    if output_text is None
                    else [{"output_type": "stream", "name": "stdout", "text": [output_text]}]
                ),
                "execution_count": execution_count,
                "metadata": {},
            }
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(nb).encode("utf-8")


def test_gate_notebook_rerun_not_a_change_under_hybrid(repo, cfg, fake_api, monkeypatch):
    """A staged+committed notebook re-run (same code, new outputs) is NOT an
    uncommitted change under the default hybrid policy -> the gate does not fire."""
    _patch(monkeypatch, repo, cfg, fake_api)
    _no_mirror(monkeypatch)
    write_file(repo, "nb.ipynb", _nb_bytes(output_text=None, execution_count=None))
    _commit_all(repo, cfg)
    # A pure re-run: same code, new outputs + execution count.
    write_file(repo, "nb.ipynb", _nb_bytes(output_text="hi\n", execution_count=1))
    _tty(monkeypatch, True)

    monkeypatch.setattr(
        push_cmd.ui, "ask_line", lambda p: pytest.fail("gate prompted for a pure notebook re-run")
    )
    # working_vs_head must report NO change for the re-run under hybrid.
    changes = vrepo.working_vs_head(repo, cfg, IgnoreSet.from_root(repo))
    assert "nb.ipynb" not in changes

    rc = push_cmd.run(_args())
    assert rc == EXIT_OK


def test_gate_notebook_rerun_is_a_change_under_full(repo, fake_api, monkeypatch):
    """Under notebook_outputs=full a re-run DOES count as a change -> the gate fires."""
    from jp.config import Config

    cfg = Config(
        base_url="https://hub.example/api",
        prefix="users/alice",
        versioning_notebook_outputs="full",
    )
    _patch(monkeypatch, repo, cfg, fake_api)
    _no_mirror(monkeypatch)
    write_file(repo, "nb.ipynb", _nb_bytes(output_text=None, execution_count=None))
    _commit_all(repo, cfg)
    write_file(repo, "nb.ipynb", _nb_bytes(output_text="hi\n", execution_count=1))
    _tty(monkeypatch, True)

    asked = {"n": 0}

    def fake_ask(prompt):
        asked["n"] += 1
        return "p"  # push raw once

    monkeypatch.setattr(push_cmd.ui, "ask_line", fake_ask)
    rc = push_cmd.run(_args())
    assert rc == EXIT_OK
    assert asked["n"] >= 1  # full mode treats the re-run as a real change -> gate fires

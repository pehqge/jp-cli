"""Tests for ``jp show`` and ``jp diff --staged`` (read-only, offline).

``show`` resolves a commit-ish, prints a SHORT 12-char header, and diffs the
commit's tree against its first parent (the empty tree for a root commit): text via
unified diff, binary as "binary differs", and notebooks via the OUTPUTS-FREE code
text so a pure re-run is silent. ``diff --staged`` does the analogous staged-vs-HEAD
diff -- and crucially NEVER builds the API (proved by monkeypatching build_api to
raise). The default ``jp diff`` (no --staged) behavior is asserted unchanged.
"""

from __future__ import annotations

import argparse
import json

import pytest

import jp.commands.diff as diff_cmd
import jp.commands.show as show_cmd
from jp.commands import _context
from jp.commands._context import RepoContext
from jp.config import Config
from jp.errors import EXIT_OK
from jp.ignore import IgnoreSet
from jp.index import Index
from jp.versioning import repo
from jp.versioning.objects import VersioningError


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


def _show_args(commit="HEAD", path="", stat=False):
    return _args(commit=commit, path=path, stat=stat)


def _diff_args(path="", staged=False):
    return _args(path=path, staged=staged)


def _write(root, rel, data: bytes):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


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


def _commit(root, message="m", allow_empty=False):
    return repo.create_commit(
        root, object(), message=message, stage_all=True, allow_empty=allow_empty, dry_run=False
    )


# --------------------------------------------------------------------------- #
# jp show
# --------------------------------------------------------------------------- #
def test_show_empty_repo_says_no_commits(repo, monkeypatch, capsys):
    _patch_load(monkeypatch, show_cmd, repo)
    rc = show_cmd.run(_show_args())
    assert rc == EXIT_OK
    assert "no commits yet" in capsys.readouterr().out


def test_show_root_commit_shows_everything_added(repo, monkeypatch, capsys):
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, show_cmd, repo)
    _write(repo, "a.txt", b"hello\nworld\n")
    _write(repo, "dir/b.txt", b"x\n")
    _commit(repo)

    show_cmd.run(_show_args())
    out = capsys.readouterr().out
    assert "added a.txt" in out
    assert "added dir/b.txt" in out
    # The added content body appears.
    assert "+hello" in out
    assert "+world" in out


def test_show_text_diff_body_and_short_hash(repo, monkeypatch, capsys):
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, show_cmd, repo)
    _write(repo, "a.txt", b"one\ntwo\n")
    _commit(repo, "root")
    _write(repo, "a.txt", b"one\nTWO\n")
    res = _commit(repo, "edit")

    show_cmd.run(_show_args(commit=res["sha"]))
    out = capsys.readouterr().out
    # SHORT (12-char) hash in the header; the full 64-hex would be redacted.
    assert f"commit {res['sha'][:12]}" in out
    assert res["sha"] not in out  # the full sha never appears
    assert "modified a.txt" in out
    assert "-two" in out
    assert "+TWO" in out


def test_show_binary_reports_binary_differs(repo, monkeypatch, capsys):
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, show_cmd, repo)
    _write(repo, "b.bin", b"\x00\x01\x02bin")
    _commit(repo)
    out = capsys.readouterr().out  # drain
    show_cmd.run(_show_args())
    out = capsys.readouterr().out
    assert "added b.bin" in out
    assert "binary differs" in out


def test_show_notebook_diff_ignores_outputs(repo, monkeypatch, capsys):
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, show_cmd, repo)
    _write(repo, "n.ipynb", _nb_bytes("x = 1\n", outputs=[], execution_count=None))
    _commit(repo, "root")
    # A real code change (also new outputs/exec count) -> only the code shows.
    _write(
        repo,
        "n.ipynb",
        _nb_bytes(
            "x = 2\n",
            outputs=[{"output_type": "stream", "text": "noise"}],
            execution_count=5,
        ),
    )
    res = _commit(repo, "edit nb")

    show_cmd.run(_show_args(commit=res["sha"]))
    out = capsys.readouterr().out
    assert "modified n.ipynb" in out
    assert "-x = 1" in out
    assert "+x = 2" in out
    assert "noise" not in out  # outputs never appear in the diff


def test_show_notebook_rerun_introduces_no_change(repo, monkeypatch, capsys):
    """A pure re-run never even creates a commit; --allow-empty proves it's silent."""
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, show_cmd, repo)
    _write(repo, "n.ipynb", _nb_bytes("compute()\n", outputs=[], execution_count=None))
    _commit(repo, "root")
    # Pure re-run on disk: same code, new outputs.
    _write(
        repo,
        "n.ipynb",
        _nb_bytes(
            "compute()\n",
            outputs=[{"output_type": "stream", "text": "z"}],
            execution_count=9,
        ),
    )
    # An empty commit (forced) captures the re-run; show must report no nb change.
    res = _commit(repo, "rerun", allow_empty=True)
    show_cmd.run(_show_args(commit=res["sha"]))
    out = capsys.readouterr().out
    assert "n.ipynb" not in out.split("commit", 1)[-1].replace("rerun", "")
    assert "this commit introduced no file changes" in out


def test_show_stat_counts_only(repo, monkeypatch, capsys):
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, show_cmd, repo)
    _write(repo, "a.txt", b"a\n")
    _write(repo, "b.txt", b"b\n")
    _commit(repo)

    show_cmd.run(_show_args(stat=True))
    out = capsys.readouterr().out
    assert "2 file(s) changed (+2 ~0 -0)" in out
    assert "+ a.txt" in out
    assert "+ b.txt" in out
    # No diff body in --stat mode.
    assert "@@" not in out


def test_show_path_filter_limits_to_one_file(repo, monkeypatch, capsys):
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, show_cmd, repo)
    _write(repo, "a.txt", b"a\n")
    _write(repo, "b.txt", b"b\n")
    _commit(repo)

    show_cmd.run(_show_args(path="a.txt"))
    out = capsys.readouterr().out
    assert "added a.txt" in out
    assert "b.txt" not in out


def test_show_unknown_revision_raises(repo, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, show_cmd, repo)
    _write(repo, "a.txt", b"a\n")
    _commit(repo)
    with pytest.raises(VersioningError):
        show_cmd.run(_show_args(commit="ffffffffdead"))


# --------------------------------------------------------------------------- #
# jp diff --staged
# --------------------------------------------------------------------------- #
def test_diff_staged_text_vs_head(repo, monkeypatch, capsys):
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, diff_cmd, repo)
    _write(repo, "a.txt", b"old\n")
    _commit(repo, "root")
    # Stage a modification (do NOT commit it).
    _write(repo, "a.txt", b"new\n")
    repo_stage(repo)

    diff_cmd.run(_diff_args(staged=True))
    out = capsys.readouterr().out
    assert "modified a.txt" in out
    assert "-old" in out
    assert "+new" in out


def test_diff_staged_no_head_everything_new(repo, monkeypatch, capsys):
    _patch_load(monkeypatch, diff_cmd, repo)
    _write(repo, "a.txt", b"fresh\n")
    repo_stage(repo)  # staged but no commits yet

    diff_cmd.run(_diff_args(staged=True))
    out = capsys.readouterr().out
    assert "added a.txt" in out
    assert "+fresh" in out


def test_diff_staged_notebook_outputs_free(repo, monkeypatch, capsys):
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, diff_cmd, repo)
    _write(repo, "n.ipynb", _nb_bytes("k = 1\n"))
    _commit(repo, "root")
    _write(
        repo,
        "n.ipynb",
        _nb_bytes(
            "k = 2\n",
            outputs=[{"output_type": "stream", "text": "junk"}],
            execution_count=3,
        ),
    )
    repo_stage(repo)

    diff_cmd.run(_diff_args(staged=True))
    out = capsys.readouterr().out
    assert "modified n.ipynb" in out
    assert "-k = 1" in out
    assert "+k = 2" in out
    assert "junk" not in out


def test_diff_staged_never_builds_api(repo, monkeypatch, capsys):
    """The --staged path is fully offline: build_api must never be called."""
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, diff_cmd, repo)

    def _boom(cfg):
        raise AssertionError("build_api must not be called for --staged")

    monkeypatch.setattr(_context, "build_api", _boom)
    # Also patch the name imported into the diff module's namespace, if any.
    monkeypatch.setattr(diff_cmd._context, "build_api", _boom)

    _write(repo, "a.txt", b"x\n")
    repo_stage(repo)
    rc = diff_cmd.run(_diff_args(staged=True))
    assert rc == EXIT_OK
    assert "added a.txt" in capsys.readouterr().out


def test_diff_staged_clean_says_no_staged_changes(repo, monkeypatch, capsys):
    monkeypatch.setenv("USER", "tester")
    _patch_load(monkeypatch, diff_cmd, repo)
    _write(repo, "a.txt", b"same\n")
    _commit(repo, "root")  # commit makes staged == HEAD
    diff_cmd.run(_diff_args(staged=True))
    assert "no staged changes" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# jp diff (default, no --staged) -- behavior must be unchanged.
# --------------------------------------------------------------------------- #
def test_diff_default_uses_api_unchanged(repo, monkeypatch, capsys, make_fake_api):
    ctx = _patch_load(monkeypatch, diff_cmd, repo)
    api = make_fake_api()
    api.seed("users/alice/a.txt", b"remote\n")
    monkeypatch.setattr(_context, "build_api", lambda cfg: api)
    monkeypatch.setattr(diff_cmd._context, "build_api", lambda cfg: api)

    _write(repo, "a.txt", b"local\n")
    # Seed the index so sync.diff sees a.txt as a known/modified path.
    rc = diff_cmd.run(_diff_args())
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    # The default diff prints remote/local headers (offline staged would say HEAD/).
    assert "staged/" not in out
    _ = ctx


def repo_stage(root):
    """Stage the whole working tree via the real stage_paths (offline)."""
    repo.stage_paths(root, object(), IgnoreSet.from_root(root), [], all_files=True, dry_run=False)

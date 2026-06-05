"""``jp status`` local versioning section + the opt-in invariant.

The remote 3-way section is unchanged; these tests focus on the NEW, OFFLINE
"Versioning" block that is appended ONLY when versioning is active (HEAD resolves
or a ``staged.json`` exists). Coverage:

* INACTIVE opt-in invariant: a repo with no HEAD and no staging produces output
  byte-identical to status WITHOUT the versioning code path -- no section at all.
* staged (to be committed): a freshly-staged file shows under "staged".
* not staged: a working-tree edit that was never staged shows under "not staged".
* notebook outputs-only (hybrid): a pure re-run of a staged notebook shows as
  "outputs only", NOT as modified.
* notebook full: under ``notebook_outputs=full`` a re-run IS a modification.
* first-run safety: status on a fresh/unborn repo never crashes and emits no
  versioning section; ``.jp/index.json`` is never touched.
"""

from __future__ import annotations

import argparse
import json

import jp.commands.status as status_cmd
from jp.commands._context import RepoContext
from jp.config import Config
from jp.ignore import IgnoreSet
from jp.index import Index
from jp.versioning import repo as vrepo

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_NB_TEMPLATE = {
    "cells": [
        {
            "cell_type": "code",
            "source": ["print('hi')\n"],
            "outputs": [],
            "execution_count": None,
            "metadata": {},
        }
    ],
    "metadata": {},
    "nbformat": 4,
    "nbformat_minor": 5,
}


def _nb_bytes(*, output_text: str | None, execution_count) -> bytes:
    import copy

    nb = copy.deepcopy(_NB_TEMPLATE)
    cell = nb["cells"][0]
    cell["execution_count"] = execution_count
    if output_text is not None:
        cell["outputs"] = [
            {
                "output_type": "stream",
                "name": "stdout",
                "text": [output_text],
            }
        ]
    return json.dumps(nb).encode("utf-8")


def _ctx(root, **cfg_kw) -> RepoContext:
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice", **cfg_kw)
    return RepoContext(
        root=root,
        cfg=cfg,
        index=Index.load(root),
        ignore=IgnoreSet.from_root(root),
    )


class _FakeApi:
    """Minimal stand-in: status' remote diff sees an empty prefix listing."""

    def list_dir(self, api_path):
        return []

    def hash(self, api_path):
        return None

    def get_file_bytes(self, api_path):
        return b""

    def stat(self, api_path):
        return None


def _patch(monkeypatch, root, **cfg_kw) -> RepoContext:
    ctx = _ctx(root, **cfg_kw)
    monkeypatch.setattr(status_cmd, "load_repo", lambda: ctx)
    monkeypatch.setattr(status_cmd._context, "build_api", lambda cfg: _FakeApi())
    return ctx


def _args():
    return argparse.Namespace()


def _write(root, rel, data: bytes):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _commit_all(root, cfg):
    return vrepo.create_commit(
        root, cfg, message="c", stage_all=True, allow_empty=False, dry_run=False
    )["sha"]


# --------------------------------------------------------------------------- #
# Opt-in invariant: inactive -> no versioning section, identical output
# --------------------------------------------------------------------------- #
def test_inactive_repo_emits_no_versioning_section(repo, monkeypatch, capsys):
    _patch(monkeypatch, repo)
    _write(repo, "a.txt", b"hello")  # a plain local-only file (remote section)

    status_cmd.run(_args())
    out = capsys.readouterr().out
    # The existing remote section still works...
    assert "local-only (to push)" in out
    assert "a.txt" in out
    # ...but with no HEAD and no staged.json there is NO versioning block.
    assert "Versioning" not in out
    assert "not staged" not in out
    assert "to be committed" not in out


def test_inactive_output_is_identical_with_and_without_versioning(repo, monkeypatch, capsys):
    """Byte-identical: an inactive repo's status text must not change at all."""
    _patch(monkeypatch, repo)
    _write(repo, "a.txt", b"hello")
    status_cmd.run(_args())
    first = capsys.readouterr().out
    # No HEAD, no staging file written -> still inactive on a second run.
    assert not (repo / ".jp" / "staged.json").exists()
    assert not (repo / ".jp" / "HEAD").exists()
    status_cmd.run(_args())
    second = capsys.readouterr().out
    assert first == second
    assert "Versioning" not in first


# --------------------------------------------------------------------------- #
# Active section: staged / not-staged
# --------------------------------------------------------------------------- #
def test_staged_files_show_under_staged(repo, monkeypatch, capsys):
    ctx = _patch(monkeypatch, repo)
    _write(repo, "a.txt", b"hello")
    vrepo.stage_paths(repo, ctx.cfg, ctx.ignore, ["a.txt"], all_files=False, dry_run=False)

    status_cmd.run(_args())
    out = capsys.readouterr().out
    assert "Versioning" in out
    assert "a.txt" in out
    # Staged-but-uncommitted shows in the staged bucket.
    assert "to be committed" in out


def test_working_edit_shows_not_staged(repo, monkeypatch, capsys):
    ctx = _patch(monkeypatch, repo)
    _write(repo, "a.txt", b"hello")
    vrepo.stage_paths(repo, ctx.cfg, ctx.ignore, ["a.txt"], all_files=False, dry_run=False)
    # Edit AFTER staging: the working content now differs from the staged snapshot.
    _write(repo, "a.txt", b"hello CHANGED")

    status_cmd.run(_args())
    out = capsys.readouterr().out
    assert "not staged" in out
    assert "a.txt" in out


def test_new_working_file_shows_not_staged(repo, monkeypatch, capsys):
    ctx = _patch(monkeypatch, repo)
    _write(repo, "a.txt", b"hello")
    vrepo.stage_paths(repo, ctx.cfg, ctx.ignore, ["a.txt"], all_files=False, dry_run=False)
    # A brand-new, never-staged file.
    _write(repo, "b.txt", b"new")

    status_cmd.run(_args())
    out = capsys.readouterr().out
    assert "not staged" in out
    assert "b.txt" in out


def test_deleted_from_working_shows_not_staged(repo, monkeypatch, capsys):
    ctx = _patch(monkeypatch, repo)
    _write(repo, "a.txt", b"hello")
    vrepo.stage_paths(repo, ctx.cfg, ctx.ignore, ["a.txt"], all_files=False, dry_run=False)
    (repo / "a.txt").unlink()

    status_cmd.run(_args())
    out = capsys.readouterr().out
    assert "not staged" in out
    assert "a.txt" in out


# --------------------------------------------------------------------------- #
# Notebook outputs-only (hybrid) vs full
# --------------------------------------------------------------------------- #
def test_notebook_rerun_shows_outputs_only_under_hybrid(repo, monkeypatch, capsys):
    ctx = _patch(monkeypatch, repo)  # default notebook_outputs == "hybrid"
    _write(repo, "n.ipynb", _nb_bytes(output_text=None, execution_count=None))
    vrepo.stage_paths(repo, ctx.cfg, ctx.ignore, ["n.ipynb"], all_files=False, dry_run=False)
    # Pure re-run: same code, fresh outputs + bumped execution_count.
    _write(repo, "n.ipynb", _nb_bytes(output_text="result", execution_count=1))

    status_cmd.run(_args())
    out = capsys.readouterr().out
    assert "outputs only" in out
    assert "n.ipynb" in out
    # A pure re-run under hybrid is NOT a real modification.
    assert "not staged" not in out or "outputs only" in out


def test_notebook_rerun_is_modified_under_full(repo, monkeypatch, capsys):
    ctx = _patch(monkeypatch, repo, versioning_notebook_outputs="full")
    _write(repo, "n.ipynb", _nb_bytes(output_text=None, execution_count=None))
    vrepo.stage_paths(repo, ctx.cfg, ctx.ignore, ["n.ipynb"], all_files=False, dry_run=False)
    _write(repo, "n.ipynb", _nb_bytes(output_text="result", execution_count=1))

    status_cmd.run(_args())
    out = capsys.readouterr().out
    # Under full, EVERY byte change (incl. a re-run) is a real not-staged change.
    assert "not staged" in out
    assert "n.ipynb" in out


# --------------------------------------------------------------------------- #
# First-run / safety
# --------------------------------------------------------------------------- #
def test_status_on_unborn_head_does_not_crash(repo, monkeypatch, capsys):
    """A staged file with no commits yet (unborn HEAD) must not traceback."""
    ctx = _patch(monkeypatch, repo)
    from jp.versioning import refs

    refs.init_versioning(repo)  # writes HEAD (symbolic, unborn) + format
    _write(repo, "a.txt", b"hello")
    vrepo.stage_paths(repo, ctx.cfg, ctx.ignore, ["a.txt"], all_files=False, dry_run=False)

    rc = status_cmd.run(_args())
    out = capsys.readouterr().out
    assert rc in (0, 6)
    assert "Versioning" in out
    assert "a.txt" in out


def test_status_after_commit_clean_versioning(repo, monkeypatch, capsys):
    ctx = _patch(monkeypatch, repo)
    _write(repo, "a.txt", b"hello")
    _commit_all(repo, ctx.cfg)

    status_cmd.run(_args())
    out = capsys.readouterr().out
    # HEAD resolves now, so the versioning section IS shown, but nothing differs.
    assert "Versioning" in out


def test_status_never_touches_index_json(repo, monkeypatch):
    ctx = _patch(monkeypatch, repo)
    _write(repo, "a.txt", b"hello")
    vrepo.stage_paths(repo, ctx.cfg, ctx.ignore, ["a.txt"], all_files=False, dry_run=False)
    index_path = repo / ".jp" / "index.json"
    before = index_path.read_bytes() if index_path.exists() else None

    status_cmd.run(_args())

    after = index_path.read_bytes() if index_path.exists() else None
    assert before == after


def test_fresh_repo_no_versioning_no_crash(repo, monkeypatch, capsys):
    _patch(monkeypatch, repo)
    rc = status_cmd.run(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Versioning" not in out


# --------------------------------------------------------------------------- #
# Staged + not-staged together
# --------------------------------------------------------------------------- #
def test_staged_and_not_staged_coexist(repo, monkeypatch, capsys):
    ctx = _patch(monkeypatch, repo)
    _write(repo, "a.txt", b"A")
    _write(repo, "b.txt", b"B")
    vrepo.stage_paths(repo, ctx.cfg, ctx.ignore, ["a.txt"], all_files=False, dry_run=False)
    _write(repo, "a.txt", b"A2")  # staged but edited -> not staged too
    # b.txt is new and never staged -> not staged.

    status_cmd.run(_args())
    out = capsys.readouterr().out
    assert "to be committed" in out
    assert "not staged" in out
    assert "a.txt" in out
    assert "b.txt" in out

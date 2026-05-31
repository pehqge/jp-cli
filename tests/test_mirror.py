"""Mirror-mode safety: push/pull never delete unless mirror is on AND confirmed."""

from __future__ import annotations

import conftest
from jp import sync
from jp.commands import _mirror
from jp.commands._context import RepoContext
from jp.config import Config
from jp.ignore import IgnoreSet
from jp.index import Entry, Index


def _ctx(repo, cfg):
    return RepoContext(root=repo, cfg=cfg, index=Index.load(repo), ignore=IgnoreSet.from_root(repo))


def test_push_marks_remote_only_files_as_deletable(repo, cfg, fake_api, index, ignore):
    # Remote has a file with no local counterpart -> it's a deletion candidate,
    # but the engine itself NEVER deletes.
    fake_api.seed("users/alice/remote_only.txt", b"keep me")
    out = sync.push(repo, cfg, fake_api, index, ignore)
    assert "remote_only.txt" in out.deletable
    assert fake_api.deletes == []  # engine issued zero DELETEs
    assert "users/alice/remote_only.txt" in fake_api.files


def test_pull_marks_local_only_files_as_deletable(repo, cfg, fake_api, index, ignore):
    conftest.write_file(repo, "local_only.txt", b"mine")
    out = sync.pull(repo, cfg, fake_api, index, ignore)
    assert "local_only.txt" in out.deletable
    # engine never removed the local file
    assert (repo / "local_only.txt").exists()


def test_mirror_handle_deletes_only_confirmed_remote(repo, cfg, fake_api, monkeypatch):
    cfg.mirror = True
    fake_api.seed("users/alice/old.txt", b"x")
    fake_api.seed("users/alice/keep.txt", b"y")
    ctx = _ctx(repo, cfg)
    out = sync.Outcome(deletable=["old.txt", "keep.txt"])

    # Simulate the user choosing to delete only "old.txt" (monkeypatch auto-restores).
    import jp.tui as tui

    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    monkeypatch.setattr(tui, "confirm_deletions", lambda paths, where, _reader=None: ["old.txt"])
    _mirror.handle("remote", ctx, fake_api, out, yes=False, dry_run=False)

    assert "users/alice/old.txt" not in fake_api.files  # deleted
    assert "users/alice/keep.txt" in fake_api.files  # kept
    assert out.deleted == ["old.txt"]


def test_mirror_handle_noninteractive_without_yes_deletes_nothing(repo, cfg, fake_api, monkeypatch):
    cfg.mirror = True
    fake_api.seed("users/alice/old.txt", b"x")
    ctx = _ctx(repo, cfg)
    out = sync.Outcome(deletable=["old.txt"])

    import jp.tui as tui

    monkeypatch.setattr(tui, "interactive", lambda *a, **k: False)
    _mirror.handle("remote", ctx, fake_api, out, yes=False, dry_run=False)

    assert "users/alice/old.txt" in fake_api.files  # NOT deleted
    assert out.deleted == []


def test_mirror_handle_yes_deletes_all_remote(repo, cfg, fake_api):
    cfg.mirror = True
    fake_api.seed("users/alice/a.txt", b"x")
    fake_api.seed("users/alice/b.txt", b"y")
    ctx = _ctx(repo, cfg)
    out = sync.Outcome(deletable=["a.txt", "b.txt"])
    _mirror.handle("remote", ctx, fake_api, out, yes=True, dry_run=False)
    assert "users/alice/a.txt" not in fake_api.files
    assert "users/alice/b.txt" not in fake_api.files
    assert sorted(out.deleted) == ["a.txt", "b.txt"]


def test_mirror_handle_dry_run_deletes_nothing(repo, cfg, fake_api):
    cfg.mirror = True
    fake_api.seed("users/alice/a.txt", b"x")
    ctx = _ctx(repo, cfg)
    out = sync.Outcome(deletable=["a.txt"])
    _mirror.handle("remote", ctx, fake_api, out, yes=True, dry_run=True)
    assert "users/alice/a.txt" in fake_api.files  # dry-run never deletes
    assert out.deleted == []


def test_mirror_local_delete_after_confirm(repo, cfg, fake_api, monkeypatch):
    cfg.mirror = True
    conftest.write_file(repo, "scratch.txt", b"junk")
    # record it in the index so removal also drops the index entry
    idx = Index.load(repo)
    idx.set("scratch.txt", Entry(sha256=sync.sha256_bytes(b"junk"), size=4))
    idx.save()
    ctx = _ctx(repo, cfg)
    out = sync.Outcome(deletable=["scratch.txt"])

    import jp.tui as tui

    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    monkeypatch.setattr(tui, "confirm_deletions", lambda paths, where, _reader=None: ["scratch.txt"])
    _mirror.handle("local", ctx, fake_api, out, yes=False, dry_run=False)

    assert not (repo / "scratch.txt").exists()  # local file removed
    assert out.deleted == ["scratch.txt"]
    assert "scratch.txt" not in Index.load(repo)  # index entry dropped

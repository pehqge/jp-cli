"""Tests for ``jp rm`` -- the gated remote deleter (single file + recursive)."""

from __future__ import annotations

import io

import pytest

import jp.commands.rm as rm_cmd
from jp.commands._context import RepoContext
from jp.errors import SafetyError
from jp.index import Index


class _Args:
    def __init__(self, **kw):
        # Sensible defaults so each test only sets what it cares about.
        self.path = ""
        self.recursive = False
        self.dry_run = False
        self.local = False
        self.keep_local = False
        self.yes = False
        self.__dict__.update(kw)


def _ctx(repo, fake_api, monkeypatch):
    from jp.config import Config
    from jp.ignore import IgnoreSet

    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    index = Index.load(repo)
    ctx = RepoContext(root=repo, cfg=cfg, index=index, ignore=IgnoreSet.from_root(repo))
    monkeypatch.setattr(rm_cmd, "load_repo", lambda: ctx)
    monkeypatch.setattr(rm_cmd._context, "build_api", lambda c: fake_api)
    return ctx


def test_rm_requires_confirmation_when_not_tty(repo, fake_api, monkeypatch):
    _ctx(repo, fake_api, monkeypatch)
    fake_api.seed("users/alice/secret.txt", b"data")
    args = _Args(path="secret.txt", yes=False)

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SafetyError):
        rm_cmd.run(args)
    assert fake_api.deletes == []


def test_rm_deletes_after_confirmation(repo, fake_api, monkeypatch):
    _ctx(repo, fake_api, monkeypatch)
    fake_api.seed("users/alice/secret.txt", b"data")
    args = _Args(path="secret.txt", yes=True)
    rm_cmd.run(args)
    assert fake_api.deletes == ["users/alice/secret.txt"]


def test_rm_path_jail_blocks_escape(repo, fake_api, monkeypatch):
    _ctx(repo, fake_api, monkeypatch)
    args = _Args(path="../../etc/passwd", yes=True)
    with pytest.raises(SafetyError):
        rm_cmd.run(args)
    assert fake_api.deletes == []


def test_rm_nonempty_dir_without_recursive_errors_clearly(repo, fake_api, monkeypatch):
    """A non-empty directory without --recursive must refuse with a clear error,
    and must NOT issue any DELETE (no ugly server 400)."""
    _ctx(repo, fake_api, monkeypatch)
    fake_api.seed("users/alice/sub/a.txt", b"a")
    fake_api.seed("users/alice/sub/b.txt", b"b")
    args = _Args(path="sub", recursive=False, yes=True)
    with pytest.raises(SafetyError) as ei:
        rm_cmd.run(args)
    assert "recursive" in str(ei.value).lower()
    assert fake_api.deletes == []


def test_rm_recursive_deletes_bottom_up(repo, fake_api, monkeypatch):
    """--recursive deletes the whole tree bottom-up: every file/subdir before
    the directory it lives in, each validated by the path-jail."""
    _ctx(repo, fake_api, monkeypatch)
    fake_api.seed("users/alice/sub/a.txt", b"a")
    fake_api.seed("users/alice/sub/deep/b.txt", b"b")
    args = _Args(path="sub", recursive=True, yes=True)
    rm_cmd.run(args)

    deletes = fake_api.deletes
    # All leaves removed, plus the deep dir and the named dir.
    assert "users/alice/sub/a.txt" in deletes
    assert "users/alice/sub/deep/b.txt" in deletes
    assert "users/alice/sub/deep" in deletes
    assert "users/alice/sub" in deletes

    # Bottom-up: each file precedes its parent dir; deep dir precedes sub.
    assert deletes.index("users/alice/sub/deep/b.txt") < deletes.index("users/alice/sub/deep")
    assert deletes.index("users/alice/sub/deep") < deletes.index("users/alice/sub")
    assert deletes.index("users/alice/sub/a.txt") < deletes.index("users/alice/sub")
    # The named directory is the very last thing deleted.
    assert deletes[-1] == "users/alice/sub"
    # Nothing remains.
    assert fake_api.files == {}


def test_rm_recursive_dry_run_deletes_nothing(repo, fake_api, monkeypatch):
    _ctx(repo, fake_api, monkeypatch)
    fake_api.seed("users/alice/sub/a.txt", b"a")
    args = _Args(path="sub", recursive=True, dry_run=True, yes=True)
    rm_cmd.run(args)
    assert fake_api.deletes == []
    assert fake_api.files == {"users/alice/sub/a.txt": b"a"}


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_rm_recursive_dir_confirmation_uses_dir_name(repo, fake_api, monkeypatch):
    """Interactive dir delete is gated by typing the directory NAME."""
    _ctx(repo, fake_api, monkeypatch)
    fake_api.seed("users/alice/sub/a.txt", b"a")
    args = _Args(path="sub", recursive=True, yes=False)

    # Pretend stdin is an interactive terminal so the prompt path is taken.
    monkeypatch.setattr("sys.stdin", _TTY())

    # Wrong answer aborts without deleting.
    monkeypatch.setattr("builtins.input", lambda *a, **k: "nope")
    rm_cmd.run(args)
    assert fake_api.deletes == []

    # Correct dir name proceeds.
    monkeypatch.setattr("builtins.input", lambda *a, **k: "sub")
    rm_cmd.run(args)
    assert "users/alice/sub" in fake_api.deletes

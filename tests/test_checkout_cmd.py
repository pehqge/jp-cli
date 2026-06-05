"""Tests for the ``jp checkout`` CLI command and the new ui prompt helpers.

Covers the command wiring (exit codes, dry-run output, the non-tty untracked-extra
refusal, the divergence warning) and the two TTY-aware ui helpers added for this
task (``ui.confirm`` / ``ui.ask_line``): the ``assume_yes`` short-circuit, the
non-tty default/"" returns (never blocking CI), and the mockable tty path.
"""

from __future__ import annotations

import argparse
import os

import pytest

import jp.commands.checkout as checkout_cmd
from jp import ui
from jp.commands._context import RepoContext
from jp.config import Config
from jp.errors import EXIT_OK, EXIT_PARTIAL, SafetyError
from jp.ignore import IgnoreSet
from jp.index import Index
from jp.versioning import refs, repo


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _FakeStdin:
    """A minimal stdin stand-in whose ``isatty()`` is scriptable."""

    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _ctx(root) -> RepoContext:
    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    return RepoContext(
        root=root,
        cfg=cfg,
        index=Index.load(root),
        ignore=IgnoreSet.from_root(root),
    )


def _patch_load(monkeypatch, root) -> RepoContext:
    ctx = _ctx(root)
    monkeypatch.setattr(checkout_cmd, "load_repo", lambda: ctx)
    return ctx


def _args(commit, paths=None, force=False, remove_extra=False, dry_run=False, yes=False):
    return argparse.Namespace(
        commit=commit,
        paths=list(paths or []),
        force=force,
        remove_extra=remove_extra,
        dry_run=dry_run,
        yes=yes,
    )


def _write(root, rel, data: bytes):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _commit_all(root):
    os.environ.setdefault("USER", "tester")
    return repo.create_commit(
        root, object(), message="c", stage_all=True, allow_empty=False, dry_run=False
    )["sha"]


# --------------------------------------------------------------------------- #
# Command happy paths
# --------------------------------------------------------------------------- #
def test_cmd_full_checkout_attaches_and_warns(repo, monkeypatch, capsys):
    _patch_load(monkeypatch, repo)
    _write(repo, "a.txt", b"A1")
    _commit_all(repo)
    _write(repo, "a.txt", b"A2")  # dirty vs HEAD... actually clean? a.txt==HEAD? no.
    # Make a.txt clean (== HEAD) by recommitting:
    _commit_all(repo)
    _write(repo, "a.txt", b"A2")  # now == HEAD content -> clean

    rc = checkout_cmd.run(_args("main"))
    assert rc == EXIT_OK
    err = capsys.readouterr().err
    # Divergence warning is emitted on stderr.
    assert "next 'jp push'" in err


def test_cmd_path_scoped_no_head_move(repo, monkeypatch):
    ctx = _patch_load(monkeypatch, repo)
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    _write(repo, "a.txt", b"A2")
    _commit_all(repo)
    head_before = refs.resolve_head(ctx.root)

    rc = checkout_cmd.run(_args(c1, paths=["a.txt"], force=True))
    assert rc == EXIT_OK
    assert (repo / "a.txt").read_bytes() == b"A1"
    assert refs.resolve_head(ctx.root) == head_before


def test_cmd_dry_run_writes_nothing(repo, monkeypatch, capsys):
    _patch_load(monkeypatch, repo)
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    _write(repo, "a.txt", b"A1")  # == HEAD -> clean; target c1 same anyway -> skip
    _write(repo, "extra.txt", b"X")

    rc = checkout_cmd.run(_args(c1, remove_extra=True, dry_run=True))
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "dry run" in out
    # Nothing actually changed.
    assert (repo / "extra.txt").exists()


def test_cmd_symlink_conflict_returns_partial(repo, monkeypatch, capsys):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unsupported")
    ctx = _patch_load(monkeypatch, repo)
    _write(repo, "a.txt", b"A1")
    _write(repo, "b.txt", b"B1")
    c1 = _commit_all(repo)
    _write(repo, "a.txt", b"A2")
    _write(repo, "b.txt", b"B2")
    c2 = _commit_all(repo)
    head_before = refs.resolve_head(ctx.root)
    assert head_before == c2
    staged_before = repo / ".jp" / "staged.json"
    staged_bytes_before = staged_before.read_bytes() if staged_before.exists() else None

    outside = repo.parent / "secret.txt"
    outside.write_bytes(b"SECRET")
    (repo / "a.txt").unlink()
    try:
        os.symlink(str(outside), str(repo / "a.txt"))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")

    rc = checkout_cmd.run(_args(c1, force=True))
    assert rc == EXIT_PARTIAL  # a.txt failed -> partial
    assert (repo / "b.txt").read_bytes() == b"B1"
    assert outside.read_bytes() == b"SECRET"

    # FIX 1: HEAD UNCHANGED, staging UNCHANGED on a partial run; the INCOMPLETE
    # warning names the failed path.
    assert refs.resolve_head(ctx.root) == head_before
    staged_bytes_after = staged_before.read_bytes() if staged_before.exists() else None
    assert staged_bytes_after == staged_bytes_before
    err = capsys.readouterr().err
    assert "INCOMPLETE" in err
    assert "a.txt" in err
    # The divergence warning must NOT be printed (HEAD did not move).
    assert "next 'jp push'" not in err


# --------------------------------------------------------------------------- #
# The untracked-extra confirm callable (non-tty refusal)
# --------------------------------------------------------------------------- #
def test_make_confirm_yes_short_circuits(monkeypatch):
    confirm = checkout_cmd._make_confirm(assume_yes=True)
    assert confirm(["x.txt"]) is True


def test_make_confirm_non_tty_raises(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=False))
    confirm = checkout_cmd._make_confirm(assume_yes=False)
    with pytest.raises(SafetyError):
        confirm(["x.txt"])


def test_make_confirm_tty_delegates_to_ui(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))
    monkeypatch.setattr(ui, "confirm", lambda prompt, default=False: True)
    confirm = checkout_cmd._make_confirm(assume_yes=False)
    assert confirm(["x.txt"]) is True


def test_cmd_remove_extra_untracked_non_tty_refuses(repo, monkeypatch):
    _patch_load(monkeypatch, repo)
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=False))
    _write(repo, "a.txt", b"A1")
    c1 = _commit_all(repo)
    _write(repo, "new.txt", b"NEW")  # untracked extra

    with pytest.raises(SafetyError):
        checkout_cmd.run(_args(c1, remove_extra=True))
    assert (repo / "new.txt").exists()  # never deleted


# --------------------------------------------------------------------------- #
# ui.confirm / ui.ask_line
# --------------------------------------------------------------------------- #
def test_ui_confirm_assume_yes(monkeypatch):
    # assume_yes wins regardless of tty.
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=False))
    assert ui.confirm("ok?", assume_yes=True) is True


def test_ui_confirm_non_tty_returns_default(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=False))
    # Must NOT block: returns the default without reading input.
    assert ui.confirm("ok?", default=False) is False
    assert ui.confirm("ok?", default=True) is True


def test_ui_confirm_tty_reads_input(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))
    monkeypatch.setattr("builtins.input", lambda: "y")
    assert ui.confirm("ok?", default=False) is True
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert ui.confirm("ok?", default=True) is False
    # Empty -> default.
    monkeypatch.setattr("builtins.input", lambda: "")
    assert ui.confirm("ok?", default=True) is True
    assert ui.confirm("ok?", default=False) is False


def test_ui_confirm_tty_eof_returns_default(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))

    def _raise():
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    assert ui.confirm("ok?", default=True) is True


def test_ui_ask_line_non_tty_returns_empty(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=False))
    assert ui.ask_line("name? ") == ""


def test_ui_ask_line_tty_reads(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))
    monkeypatch.setattr("builtins.input", lambda: "  hello  ")
    assert ui.ask_line("name? ") == "hello"


def test_ui_confirm_redacts_prompt(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty=True))
    monkeypatch.setattr("builtins.input", lambda: "y")
    # A bare long hex blob in the prompt must be redacted on stderr.
    secret = "a" * 40
    ui.confirm(f"token {secret} ok?", default=False)
    err = capsys.readouterr().err
    assert secret not in err
    assert "REDACTED" in err

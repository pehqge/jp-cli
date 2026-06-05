import argparse

from jp.commands import live
from jp.errors import EXIT_OK


def _dry_args(**over):
    base = {
        "url": None,
        "read_only": False,
        "credential": None,
        "code": False,
        "yes": False,
        "mount": None,
        "dry_run": True,
        "root": None,
        "stats": False,
        "print_agent": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_dry_run_reports_success(tmp_path, capsys):
    (tmp_path / "notebook.ipynb").write_bytes(b'{"cells": []}')
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.csv").write_bytes(b"a,b\n1,2\n")

    rc = live.run(_dry_args(root=str(tmp_path)))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "ping: ok" in out
    assert "notebook.ipynb" in out and "data" in out
    assert "x.csv" in out
    assert "bytes verified" in out


def test_no_url_outside_workspace_raises_usage_error(tmp_path, monkeypatch):
    import pytest

    from jp.errors import UsageError

    # Neither --dry-run nor --print-agent, and no URL -> usage error (after the
    # workspace guard passes because cwd is a plain dir).
    monkeypatch.chdir(tmp_path)
    args = _dry_args(dry_run=False)
    with pytest.raises(UsageError):
        live.run(args)


def test_dry_run_stats_prints_machine(tmp_path, capsys):
    rc = live.run(_dry_args(root=str(tmp_path), stats=True))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "CPU" in out or "cpu" in out


def test_dry_run_writable_is_default_and_does_not_block(tmp_path, capsys):
    (tmp_path / "f.txt").write_bytes(b"hi")
    # Writable is now the default in dry-run too; --read-only opts out. With a
    # non-tty stdin the writable dry-run proceeds without blocking on input().
    rc = live.run(_dry_args(root=str(tmp_path)))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    # Warns that writes will reach the (simulated) remote, and does NOT block.
    assert "writ" in out.lower()


def test_dry_run_read_only_skips_writable_warning(tmp_path, capsys):
    (tmp_path / "f.txt").write_bytes(b"hi")
    rc = live.run(_dry_args(root=str(tmp_path), read_only=True))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "WRITABLE mode" not in out


def test_live_writable_warning_makes_no_checkpoint_promise():
    """Issue #4: the writable warning must NOT claim a server-side checkpoint
    (it is never wired) and SHOULD honestly say there is no automatic undo."""
    import inspect

    src = inspect.getsource(live)
    assert "checkpoint" not in src.lower()
    lowered = src.lower()
    assert "no automatic" in lowered and "undo" in lowered
    assert "backup" in lowered


def test_dry_run_with_mount_prints_mount_command(tmp_path, capsys, monkeypatch):
    (tmp_path / "f.txt").write_bytes(b"hi")
    from jp.mount import os_mount

    # Force the manual-instructions fallback so the test never performs a real
    # mount and is platform-agnostic (the gio/net-use/mount_webdav argv differ).
    def _boom(*a, **k):
        raise os_mount.MountError("no mount tool in CI")

    monkeypatch.setattr(os_mount, "mount", _boom)

    args = _dry_args(root=str(tmp_path), mount="/tmp/jpmnt")
    rc = live.run(args)
    cap = capsys.readouterr()
    combined = cap.out + cap.err  # the warn goes to stderr
    assert rc == 0
    assert "http://127.0.0.1:" in cap.out  # the WebDAV url was printed
    assert "mount it manually" in combined  # the fallback printed the manual command

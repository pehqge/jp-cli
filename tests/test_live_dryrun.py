import argparse

from jp.commands import live
from jp.errors import EXIT_OK


def test_dry_run_reports_success(tmp_path, capsys):
    (tmp_path / "notebook.ipynb").write_bytes(b'{"cells": []}')
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.csv").write_bytes(b"a,b\n1,2\n")

    args = argparse.Namespace(dry_run=True, root=str(tmp_path), live=False)
    rc = live.run(args)
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "ping: ok" in out
    assert "notebook.ipynb" in out and "data" in out
    assert "x.csv" in out
    assert "bytes verified" in out


def test_no_mode_raises_usage_error(tmp_path):
    import pytest

    from jp.errors import UsageError

    # Neither --dry-run nor --live nor --print-agent -> usage error.
    args = argparse.Namespace(dry_run=False, root=None, live=False, print_agent=False)
    with pytest.raises(UsageError):
        live.run(args)


def test_dry_run_stats_prints_machine(tmp_path, capsys):
    args = argparse.Namespace(dry_run=True, root=str(tmp_path), live=False, stats=True)
    rc = live.run(args)
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "CPU" in out or "cpu" in out


def test_dry_run_writable_flag_does_not_block(tmp_path, capsys):
    (tmp_path / "f.txt").write_bytes(b"hi")
    args = argparse.Namespace(dry_run=True, root=str(tmp_path), live=False, writable=True)
    rc = live.run(args)
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    # Warns that writes will reach the (simulated) remote, and does NOT block.
    assert "writ" in out.lower()


def test_live_writable_warning_makes_no_checkpoint_promise():
    """Issue #4: the writable warning must NOT claim a server-side checkpoint
    (it is never wired) and SHOULD honestly say there is no automatic undo."""
    import inspect

    src = inspect.getsource(live)
    assert "checkpoint" not in src.lower()
    lowered = src.lower()
    assert "no automatic" in lowered and "undo" in lowered
    assert "backup" in lowered


def test_dry_run_with_mount_prints_mount_command(tmp_path, capsys):
    (tmp_path / "f.txt").write_bytes(b"hi")
    args = argparse.Namespace(dry_run=True, root=str(tmp_path), live=False, mount="/tmp/jpmnt")
    rc = live.run(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "http://127.0.0.1:" in out  # the WebDAV url was printed
    assert "/tmp/jpmnt" in out  # the mount command references the point

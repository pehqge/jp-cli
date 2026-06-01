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


def test_live_path_is_blocked_in_phase1(tmp_path):
    import pytest

    from jp.errors import SafetyError

    args = argparse.Namespace(dry_run=False, root=None, live=True)
    with pytest.raises(SafetyError):
        live.run(args)

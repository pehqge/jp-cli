from __future__ import annotations

import argparse

from jp import changelog as cl
from jp.commands import version as ver
from jp.errors import EXIT_OK


def test_version_plain(capsys):
    assert ver.run(argparse.Namespace(changelog=False)) == EXIT_OK
    assert "jp " in capsys.readouterr().out


def test_version_with_changelog(monkeypatch, capsys):
    monkeypatch.setattr(ver, "__version__", "1.2.0")
    monkeypatch.setattr(cl, "release_for", lambda t: cl.Release("v1.2.0", "1.2.0", "release body"))
    assert ver.run(argparse.Namespace(changelog=True)) == EXIT_OK
    out = capsys.readouterr().out
    assert "jp 1.2.0" in out
    assert "release body" in out

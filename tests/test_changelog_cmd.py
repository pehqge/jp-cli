from __future__ import annotations

import argparse

from jp import changelog as cl
from jp.commands import changelog as cmd
from jp.errors import EXIT_NETWORK, EXIT_OK


def _ns(version=None, all=False):
    return argparse.Namespace(version=version, all=all)


def test_changelog_specific_version(monkeypatch, capsys):
    monkeypatch.setattr(cl, "release_for", lambda t: cl.Release("v1.2.0", "1.2.0", "notes here"))
    assert cmd.run(_ns(version="1.2.0")) == EXIT_OK
    assert "notes here" in capsys.readouterr().out


def test_changelog_specific_version_network_error(monkeypatch):
    monkeypatch.setattr(cl, "release_for", lambda t: None)
    assert cmd.run(_ns(version="9.9.9")) == EXIT_NETWORK


def test_changelog_up_to_date(monkeypatch, capsys):
    monkeypatch.setattr(cmd, "__version__", "1.2.0")
    monkeypatch.setattr(cl, "releases_since", lambda v: [])
    monkeypatch.setattr(cl, "latest_release", lambda: cl.Release("v1.2.0", "1.2.0", "current"))
    assert cmd.run(_ns()) == EXIT_OK
    assert "up to date" in capsys.readouterr().out


def test_changelog_newer_available(monkeypatch, capsys):
    monkeypatch.setattr(cmd, "__version__", "1.1.0")
    monkeypatch.setattr(
        cl, "releases_since", lambda v: [cl.Release("v1.2.0", "1.2.0", "new stuff")]
    )
    assert cmd.run(_ns()) == EXIT_OK
    assert "new stuff" in capsys.readouterr().out

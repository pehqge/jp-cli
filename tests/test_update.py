"""`jp update`: editable-install detection so it never reports a false success."""

from __future__ import annotations

from jp import cli
from jp.commands import update
from jp.errors import EXIT_OK


def test_editable_path_from_direct_url_detects_editable():
    raw = '{"url":"file:///Users/me/Downloads/jp","dir_info":{"editable":true}}'
    assert update._editable_path_from_direct_url(raw) == "/Users/me/Downloads/jp"


def test_editable_path_from_direct_url_ignores_normal_install():
    # A wheel/VCS install: no dir_info.editable -> not an editable source.
    assert update._editable_path_from_direct_url('{"url":"https://x/jp.whl"}') is None
    assert update._editable_path_from_direct_url('{"url":"file:///x","dir_info":{}}') is None
    assert update._editable_path_from_direct_url("not json") is None


def test_update_on_editable_install_points_to_git_pull_not_upgrade(monkeypatch, capsys):
    # Newer release exists, but the install is editable -> must NOT shell out to a
    # package manager (which would no-op and falsely claim success).
    monkeypatch.setattr(update, "_latest_release_tag", lambda: "v999.0.0")
    monkeypatch.setattr(update, "_editable_source", lambda: "/Users/me/Downloads/jp")

    def _boom(cmd):
        raise AssertionError(f"must not run a package manager for an editable install: {cmd}")

    monkeypatch.setattr(update, "_run", _boom)

    rc = cli.main(["update"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "editable" in out
    assert "git -C /Users/me/Downloads/jp pull" in out


def test_update_check_only_does_not_touch_editable_path(monkeypatch, capsys):
    # --check just reports; it should not run the upgrade nor the editable branch.
    monkeypatch.setattr(update, "_latest_release_tag", lambda: "v999.0.0")
    monkeypatch.setattr(update, "_run", lambda cmd: 0)

    rc = cli.main(["update", "--check"])
    assert rc == EXIT_OK
    assert "newer version is available" in capsys.readouterr().out

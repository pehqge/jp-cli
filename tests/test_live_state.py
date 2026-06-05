"""Live-mount registry: record/find by exact path & ancestor, deepest wins,
idempotent remove, corrupt files ignored."""

from __future__ import annotations

import os

import pytest

from jp.mount import live_state


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Point the live registry dir at a temp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def test_record_then_find_exact(cfg_home, tmp_path):
    mp = tmp_path / "mnt"
    mp.mkdir()
    live_state.record(str(mp), pid=4242, url="https://x/", display="lab")
    rec = live_state.find_for_path(str(mp))
    assert rec is not None
    assert rec["mountpoint"] == os.path.realpath(str(mp))
    assert rec["pid"] == 4242
    assert rec["url"] == "https://x/"
    assert rec["display"] == "lab"
    assert rec["created"] == 0.0


def test_created_stamp_stored_verbatim(cfg_home, tmp_path):
    mp = tmp_path / "mnt"
    mp.mkdir()
    live_state.record(str(mp), pid=1, created=123.5)
    rec = live_state.find_for_path(str(mp))
    assert rec is not None
    assert rec["created"] == 123.5


def test_find_from_subdir_ancestor_match(cfg_home, tmp_path):
    mp = tmp_path / "mnt"
    sub = mp / "a" / "b"
    sub.mkdir(parents=True)
    live_state.record(str(mp), pid=7)
    rec = live_state.find_for_path(str(sub))
    assert rec is not None
    assert rec["mountpoint"] == os.path.realpath(str(mp))


def test_unrelated_sibling_does_not_match(cfg_home, tmp_path):
    mp = tmp_path / "mnt"
    sibling = tmp_path / "mnt-other"
    mp.mkdir()
    sibling.mkdir()
    live_state.record(str(mp), pid=7)
    assert live_state.find_for_path(str(sibling)) is None


def test_no_match_returns_none(cfg_home, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert live_state.find_for_path(str(other)) is None


def test_deepest_ancestor_wins_when_nested(cfg_home, tmp_path):
    outer = tmp_path / "mnt"
    inner = outer / "inner"
    deep = inner / "x"
    deep.mkdir(parents=True)
    live_state.record(str(outer), pid=1, display="outer")
    live_state.record(str(inner), pid=2, display="inner")
    rec = live_state.find_for_path(str(deep))
    assert rec is not None
    assert rec["display"] == "inner"
    assert rec["pid"] == 2


def test_remove_deletes_and_is_idempotent(cfg_home, tmp_path):
    mp = tmp_path / "mnt"
    mp.mkdir()
    live_state.record(str(mp), pid=1)
    assert live_state.find_for_path(str(mp)) is not None
    live_state.remove(str(mp))
    assert live_state.find_for_path(str(mp)) is None
    # Second remove must not raise.
    live_state.remove(str(mp))
    assert live_state.find_for_path(str(mp)) is None


def test_list_active(cfg_home, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    live_state.record(str(a), pid=1)
    live_state.record(str(b), pid=2)
    mounts = {r["mountpoint"] for r in live_state.list_active()}
    assert mounts == {os.path.realpath(str(a)), os.path.realpath(str(b))}


def test_list_active_empty_when_no_dir(cfg_home):
    assert live_state.list_active() == []


def test_corrupt_record_ignored(cfg_home, tmp_path):
    mp = tmp_path / "mnt"
    mp.mkdir()
    path = live_state.record(str(mp), pid=9)
    # A good record still reads.
    assert live_state.find_for_path(str(mp)) is not None
    # Corrupt it: now it is silently skipped, not raised.
    path.write_text("{ broken", encoding="utf-8")
    assert live_state.find_for_path(str(mp)) is None
    assert live_state.list_active() == []

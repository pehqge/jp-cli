"""Tests for the staging area (.jp/staged.json) -- jp.versioning.staging.

Mirrors the index tests: tolerant load (missing -> empty, unsafe keys dropped,
unreadable JSON -> ConfigError), atomic 0600 save, normalize_rel keying. A hard
invariant verified here is isolation: staging must NEVER read or write
``.jp/index.json`` -- it is a separate store.
"""

from __future__ import annotations

import json
import os

import pytest

from jp.errors import ConfigError
from jp.versioning.staging import StagedEntry, Staging


def _mk(tmp_path):
    root = tmp_path / "work"
    (root / ".jp").mkdir(parents=True)
    return root


# --- load: missing -> empty -------------------------------------------------
def test_load_empty_when_missing(tmp_path):
    root = _mk(tmp_path)
    st = Staging.load(root)
    assert st.entries == {}
    assert st.path == root / ".jp" / "staged.json"


# --- set/get/save/reload round-trip -----------------------------------------
def test_roundtrip(tmp_path):
    root = _mk(tmp_path)
    st = Staging(root)
    st.set("a/b.txt", StagedEntry(sha256="abc", size=3, local_mtime=1.5))
    st.save()
    reloaded = Staging.load(root)
    e = reloaded.get("a/b.txt")
    assert e is not None
    assert e.sha256 == "abc"
    assert e.size == 3
    assert e.local_mtime == 1.5


def test_get_missing_returns_none(tmp_path):
    root = _mk(tmp_path)
    st = Staging(root)
    assert st.get("nope.txt") is None


def test_contains_and_remove_and_clear(tmp_path):
    root = _mk(tmp_path)
    st = Staging(root)
    st.set("x.txt", StagedEntry(sha256="s", size=1))
    assert "x.txt" in st
    st.remove("x.txt")
    assert "x.txt" not in st
    st.set("y.txt", StagedEntry(sha256="s", size=1))
    st.set("z.txt", StagedEntry(sha256="s", size=1))
    st.clear()
    assert st.entries == {}


# --- normalize_rel keying ---------------------------------------------------
def test_normalize_rel_keying(tmp_path):
    root = _mk(tmp_path)
    st = Staging(root)
    st.set("a//b", StagedEntry(sha256="s", size=1))
    # "./a/b" and "a//b" normalize to the same key "a/b".
    assert st.get("./a/b") is not None
    assert "a/b" in st.entries
    assert "a/b" in st


def test_default_local_mtime_is_zero(tmp_path):
    root = _mk(tmp_path)
    st = Staging(root)
    st.set("f.txt", StagedEntry(sha256="s", size=2))
    assert st.get("f.txt").local_mtime == 0.0


# --- tolerant load: unsafe keys dropped -------------------------------------
def test_load_drops_unsafe_keys(tmp_path):
    root = _mk(tmp_path)
    (root / ".jp" / "staged.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "../escape": {"sha256": "x", "size": 1},
                    "good.txt": {"sha256": "y", "size": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    st = Staging.load(root)
    assert "good.txt" in st
    assert "../escape" not in st.entries


def test_load_drops_entries_without_sha256(tmp_path):
    root = _mk(tmp_path)
    (root / ".jp" / "staged.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "nosha.txt": {"size": 1},
                    "good.txt": {"sha256": "y", "size": 2},
                },
            }
        ),
        encoding="utf-8",
    )
    st = Staging.load(root)
    assert "good.txt" in st
    assert "nosha.txt" not in st.entries


def test_load_raises_on_unreadable_json(tmp_path):
    root = _mk(tmp_path)
    (root / ".jp" / "staged.json").write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        Staging.load(root)


def test_load_raises_on_non_dict_top_level(tmp_path):
    root = _mk(tmp_path)
    (root / ".jp" / "staged.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ConfigError):
        Staging.load(root)


# --- save discipline: atomic, sorted keys, 0600 -----------------------------
def test_save_payload_shape_and_sorted_keys(tmp_path):
    root = _mk(tmp_path)
    st = Staging(root)
    st.set("b.txt", StagedEntry(sha256="bb", size=2, local_mtime=2.0))
    st.set("a.txt", StagedEntry(sha256="aa", size=1, local_mtime=1.0))
    st.save()
    raw = json.loads(st.path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert list(raw["entries"].keys()) == ["a.txt", "b.txt"]  # sorted
    assert raw["entries"]["a.txt"] == {"sha256": "aa", "size": 1, "local_mtime": 1.0}


def test_save_then_load_round_trips(tmp_path):
    """A save followed by a fresh load round-trips every entry exactly."""
    root = _mk(tmp_path)
    st = Staging(root)
    st.set("a.txt", StagedEntry(sha256="aa", size=1, local_mtime=1.0))
    st.set("dir/b.ipynb", StagedEntry(sha256="bb", size=22, local_mtime=2.0, nb_norm_sha="nn"))
    st.save()
    reloaded = Staging.load(root)
    ea = reloaded.get("a.txt")
    eb = reloaded.get("dir/b.ipynb")
    assert ea is not None and ea.sha256 == "aa" and ea.size == 1 and ea.local_mtime == 1.0
    assert ea.nb_norm_sha == ""  # plain file: no nb key emitted/read
    assert eb is not None and eb.sha256 == "bb" and eb.size == 22 and eb.nb_norm_sha == "nn"


def test_save_atomic_failure_preserves_old_file(tmp_path, monkeypatch):
    root = _mk(tmp_path)
    st = Staging(root)
    st.set("a.txt", StagedEntry(sha256="aa", size=1))
    st.save()
    original = st.path.read_text(encoding="utf-8")

    def _broken_replace(src, dst, *a, **k):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _broken_replace)
    st.set("b.txt", StagedEntry(sha256="bb", size=2))
    with pytest.raises(OSError):
        st.save()
    monkeypatch.undo()
    # Old file intact, no temp leftover beside it (legacy ".json.tmp" OR the new
    # unique "stagetmp-*" temp must both be absent after a failed save).
    assert st.path.read_text(encoding="utf-8") == original
    leftovers = [
        p
        for p in (root / ".jp").iterdir()
        if p.name.startswith("staged.json.") or p.name.startswith("stagetmp-")
    ]
    assert leftovers == []


def test_save_perms_0600(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX perms only")
    root = _mk(tmp_path)
    st = Staging(root)
    st.set("a.txt", StagedEntry(sha256="aa", size=1))
    st.save()
    mode = st.path.stat().st_mode & 0o777
    assert mode == 0o600


# --- isolation: never touches index.json ------------------------------------
def test_save_never_touches_index_json(tmp_path):
    root = _mk(tmp_path)
    st = Staging(root)
    st.set("a.txt", StagedEntry(sha256="aa", size=1))
    st.save()
    # staging must not have created index.json.
    assert not (root / ".jp" / "index.json").exists()


def test_load_does_not_read_index_json(tmp_path, monkeypatch):
    root = _mk(tmp_path)
    # Seed a DIFFERENT entry in index.json; staging.load must ignore it entirely.
    (root / ".jp" / "index.json").write_text(
        json.dumps({"version": 1, "entries": {"indexed.txt": {"sha256": "i", "size": 9}}}),
        encoding="utf-8",
    )
    st = Staging.load(root)
    assert "indexed.txt" not in st.entries
    assert st.entries == {}


def test_save_leaves_existing_index_json_untouched(tmp_path):
    root = _mk(tmp_path)
    index_path = root / ".jp" / "index.json"
    index_path.write_text(
        json.dumps({"version": 1, "entries": {"indexed.txt": {"sha256": "i", "size": 9}}}),
        encoding="utf-8",
    )
    before = index_path.read_text(encoding="utf-8")
    st = Staging(root)
    st.set("a.txt", StagedEntry(sha256="aa", size=1))
    st.save()
    assert index_path.read_text(encoding="utf-8") == before

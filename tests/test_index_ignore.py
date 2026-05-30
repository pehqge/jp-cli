"""Index persistence and ignore-matching behaviour."""

from __future__ import annotations

from jp.ignore import IgnoreSet
from jp.index import Entry, Index


def test_index_roundtrip(tmp_path):
    root = tmp_path / "r"
    (root / ".jp").mkdir(parents=True)
    idx = Index(root)
    idx.set("a/b.txt", Entry(sha256="abc", size=3, remote_mtime="t"))
    idx.save()
    reloaded = Index.load(root)
    e = reloaded.get("a/b.txt")
    assert e is not None and e.sha256 == "abc" and e.size == 3


def test_index_drops_unsafe_keys(tmp_path):
    import json

    root = tmp_path / "r"
    (root / ".jp").mkdir(parents=True)
    (root / ".jp" / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "../escape": {"sha256": "x", "size": 1},
                    "good.txt": {"sha256": "y", "size": 1},
                },
            }
        )
    )
    idx = Index.load(root)
    assert "good.txt" in idx
    assert "../escape" not in idx.entries


def test_ignore_always_ignores_dotjp(tmp_path):
    ig = IgnoreSet([])
    assert ig.is_ignored(".jp/config.json")
    assert ig.is_ignored(".jp", is_dir=True)
    # Even a negation cannot un-ignore .jp.
    ig2 = IgnoreSet(["!.jp"])
    assert ig2.is_ignored(".jp/index.json")


def test_ignore_basic_patterns():
    ig = IgnoreSet(["*.log", "build/", "secret.txt"])
    assert ig.is_ignored("app.log")
    assert ig.is_ignored("sub/dir/app.log")
    assert ig.is_ignored("build", is_dir=True)
    assert ig.is_ignored("secret.txt")
    assert not ig.is_ignored("app.py")


def test_ignore_negation():
    ig = IgnoreSet(["*.log", "!keep.log"])
    assert ig.is_ignored("x.log")
    assert not ig.is_ignored("keep.log")


def test_ignore_anchored():
    ig = IgnoreSet(["/top.txt"])
    assert ig.is_ignored("top.txt")
    assert not ig.is_ignored("sub/top.txt")


def test_ignore_doublestar():
    ig = IgnoreSet(["a/**/z.txt"])
    assert ig.is_ignored("a/b/c/z.txt")
    assert ig.is_ignored("a/z.txt")

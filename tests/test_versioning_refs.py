"""Tests for refs/HEAD, the format marker, and lazy init (jp.versioning.refs).

Written TDD-first against the documented contract: a git-like symbolic/detached
HEAD, atomic ref writes with the same discipline as the object store, a
compare-and-swap ``update_ref`` lost-update guard, strict single-segment ref
names (anti path-traversal into/out of ``refs/heads``), and a forward-compat
format-version guard.
"""

from __future__ import annotations

import json
import os

import pytest

from jp import config as config_mod
from jp.versioning import refs
from jp.versioning.objects import VersioningError


def _sha(n: int = 0) -> str:
    """A deterministic, valid 64-lowercase-hex sha for tests."""
    return f"{n:064x}"


# --- init_versioning --------------------------------------------------------
def test_init_versioning_creates_head_format_and_refs_dir(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)

    head = root / ".jp" / "HEAD"
    fmt = root / ".jp" / "format"
    heads = root / ".jp" / "refs" / "heads"
    assert head.is_file()
    assert head.read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert heads.is_dir()
    assert json.loads(fmt.read_text(encoding="utf-8")) == {"versioning": refs.FORMAT_VERSION}


def test_init_versioning_is_idempotent(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    # Advance the branch + detach to prove a second init does not clobber state.
    refs.write_ref(root, "main", _sha(7))
    head_before = (root / ".jp" / "HEAD").read_text(encoding="utf-8")

    refs.init_versioning(root)  # must not overwrite HEAD/format/refs

    assert (root / ".jp" / "HEAD").read_text(encoding="utf-8") == head_before
    assert refs.read_ref(root, "main") == _sha(7)


def test_init_versioning_writes_gitignore(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    assert (root / ".jp" / ".gitignore").is_file()


def test_init_versioning_head_perms_0600(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX perms only")
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    mode = (root / ".jp" / "HEAD").stat().st_mode & 0o777
    assert mode == 0o600


# --- read_head / resolve_head / current_branch ------------------------------
def test_read_head_none_when_missing(tmp_path):
    root = tmp_path / "work"
    (root / ".jp").mkdir(parents=True)
    assert refs.read_head(root) is None
    assert refs.resolve_head(root) is None
    assert refs.current_branch(root) is None


def test_read_head_symbolic_unborn(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    head = refs.read_head(root)
    assert head is not None
    assert head.symbolic is True
    assert head.branch == "main"
    assert head.target is None
    # Unborn branch: HEAD is symbolic but the ref file does not exist yet.
    assert refs.resolve_head(root) is None
    assert refs.current_branch(root) == "main"


def test_read_head_symbolic_born(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    refs.write_ref(root, "main", _sha(1))
    assert refs.resolve_head(root) == _sha(1)
    assert refs.current_branch(root) == "main"


def test_read_head_detached(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    refs.set_head_detached(root, _sha(2))
    head = refs.read_head(root)
    assert head is not None
    assert head.symbolic is False
    assert head.branch is None
    assert head.target == _sha(2)
    assert refs.resolve_head(root) == _sha(2)
    assert refs.current_branch(root) is None


def test_malformed_head_raises(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    (root / ".jp" / "HEAD").write_text("garbage not a ref or sha\n", encoding="utf-8")
    with pytest.raises(VersioningError):
        refs.read_head(root)


def test_malformed_head_bad_branch_name_raises(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    (root / ".jp" / "HEAD").write_text("ref: refs/heads/../evil\n", encoding="utf-8")
    with pytest.raises(VersioningError):
        refs.read_head(root)


# --- write_ref / read_ref round-trip ----------------------------------------
def test_write_ref_then_read_ref_roundtrip(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    refs.write_ref(root, "feature-1", _sha(3))
    assert refs.read_ref(root, "feature-1") == _sha(3)
    # Stored on disk as one 64-hex line + newline.
    on_disk = (root / ".jp" / "refs" / "heads" / "feature-1").read_text(encoding="utf-8")
    assert on_disk == _sha(3) + "\n"


def test_read_ref_none_when_absent(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    assert refs.read_ref(root, "nope") is None


def test_read_ref_raises_on_corruption(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    bad = root / ".jp" / "refs" / "heads" / "main"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not-a-valid-sha\n", encoding="utf-8")
    with pytest.raises(VersioningError):
        refs.read_ref(root, "main")


def test_write_ref_atomic_failure_preserves_old_value(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    refs.write_ref(root, "main", _sha(4))

    def _broken_replace(src, dst, *a, **k):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _broken_replace)
    with pytest.raises(OSError):
        refs.write_ref(root, "main", _sha(5))

    # Old value intact, no partial/temp leftover in refs/heads.
    monkeypatch.undo()
    assert refs.read_ref(root, "main") == _sha(4)
    heads = root / ".jp" / "refs" / "heads"
    leftovers = [p for p in heads.iterdir() if p.name != "main"]
    assert leftovers == []


# --- update_ref (compare-and-swap) ------------------------------------------
def test_update_ref_cas_match_writes(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    refs.write_ref(root, "main", _sha(10))
    refs.update_ref(root, "main", _sha(11), expected=_sha(10))
    assert refs.read_ref(root, "main") == _sha(11)


def test_update_ref_cas_mismatch_raises(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    refs.write_ref(root, "main", _sha(10))
    with pytest.raises(VersioningError):
        refs.update_ref(root, "main", _sha(11), expected=_sha(99))
    # The ref is untouched after a refused CAS.
    assert refs.read_ref(root, "main") == _sha(10)


def test_update_ref_expected_none_creates(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    refs.update_ref(root, "newbranch", _sha(12), expected=None)
    assert refs.read_ref(root, "newbranch") == _sha(12)


def test_update_ref_expected_none_but_ref_exists_raises(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    refs.write_ref(root, "main", _sha(10))
    with pytest.raises(VersioningError):
        refs.update_ref(root, "main", _sha(11), expected=None)
    assert refs.read_ref(root, "main") == _sha(10)


# --- set_head_branch / set_head_detached ------------------------------------
def test_set_head_branch_and_detached(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    refs.set_head_branch(root, "topic")
    assert (root / ".jp" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/topic\n"
    refs.set_head_detached(root, _sha(20))
    assert (root / ".jp" / "HEAD").read_text(encoding="utf-8") == _sha(20) + "\n"


def test_set_head_branch_rejects_bad_name(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    with pytest.raises(VersioningError):
        refs.set_head_branch(root, "../evil")


def test_set_head_detached_rejects_bad_sha(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    with pytest.raises(VersioningError):
        refs.set_head_detached(root, "not-a-sha")


# --- validate_ref_name ------------------------------------------------------
@pytest.mark.parametrize("name", ["main", "feature-1", "v1.2", "a_b", "X.y-z_1"])
def test_validate_ref_name_accepts(name):
    assert refs.validate_ref_name(name) == name


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".",
        "..",
        "-x",
        ".x",
        "a/b",
        "a\\b",
        "a b",
        "a\tb",
        "a\nb",
        "a\x00b",
        "refs/heads/main",
        "../escape",
    ],
)
def test_validate_ref_name_rejects(bad):
    with pytest.raises(VersioningError):
        refs.validate_ref_name(bad)


def test_write_ref_rejects_bad_name(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    with pytest.raises(VersioningError):
        refs.write_ref(root, "../escape", _sha(1))


def test_write_ref_rejects_bad_sha(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    with pytest.raises(VersioningError):
        refs.write_ref(root, "main", "deadbeef")  # too short


# --- format version guard ---------------------------------------------------
def test_read_format_none_when_missing(tmp_path):
    root = tmp_path / "work"
    (root / ".jp").mkdir(parents=True)
    assert refs.read_format(root) is None


def test_check_format_passes_on_equal_older_missing(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    # Missing: passes.
    (root / ".jp").mkdir(parents=True, exist_ok=True)
    refs.check_format(root)
    # Equal: passes.
    refs.init_versioning(root)
    refs.check_format(root)
    # Older: passes.
    (root / ".jp" / "format").write_text(json.dumps({"versioning": 0}), encoding="utf-8")
    refs.check_format(root)


def test_check_format_raises_on_future_version(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    refs.init_versioning(root)
    (root / ".jp" / "format").write_text(
        json.dumps({"versioning": refs.FORMAT_VERSION + 1}), encoding="utf-8"
    )
    with pytest.raises(VersioningError) as exc:
        refs.check_format(root)
    assert "newer" in str(exc.value).lower() or "upgrade" in str(exc.value).lower()


# --- gitignore integration is wired to config -------------------------------
def test_init_calls_config_ensure_dot_gitignore(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    called = {}

    real = config_mod.ensure_dot_gitignore

    def _spy(r):
        called["root"] = r
        return real(r)

    monkeypatch.setattr(config_mod, "ensure_dot_gitignore", _spy)
    refs.init_versioning(root)
    assert called.get("root") == root


# --- cross-platform guard ---------------------------------------------------
def test_refs_module_uses_o_nofollow_guard():
    import inspect

    import jp.versioning.refs as mod

    src = inspect.getsource(mod)
    assert 'getattr(os, "O_NOFOLLOW", 0)' in src or "getattr(os, 'O_NOFOLLOW', 0)" in src

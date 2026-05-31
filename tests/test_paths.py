"""Path-jail invariants: traversal, prefix validation, containment, sanitization.

Covers INV path-jail in both directions, download-containment against malicious
server entries, atomic-write, and no-symlink-write-through.
"""

from __future__ import annotations

import pytest

from jp import paths
from jp.errors import SafetyError


# --- normalize_rel ---------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "/etc/passwd",
        "../escape",
        "../../etc/shadow",
        "a/../../b",
        "foo/../../bar",
        "C:\\Windows\\system32",
        "C:/Windows",
        "\\\\server\\share\\x",
        "with\x00nul",
        ".",
        "..",
    ],
)
def test_normalize_rel_rejects_unsafe(bad):
    with pytest.raises(SafetyError):
        paths.normalize_rel(bad)


@pytest.mark.parametrize(
    "good,expected",
    [
        ("a/b/c.txt", "a/b/c.txt"),
        ("./a/b", "a/b"),
        ("a/./b", "a/b"),
        ("a/b/../c", "a/c"),
        ("notebook.ipynb", "notebook.ipynb"),
        ("dir\\sub\\file", "dir/sub/file"),  # windows-style on any OS
    ],
)
def test_normalize_rel_accepts_safe(good, expected):
    assert paths.normalize_rel(good) == expected


# --- validate_prefix -------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "  ",
        "/",
        ".",
        "shared",
        "Shared",
        "shared/x",
        "public",
        "common/y",
        "..",
        "../x",
        # The real the server shared roots must be refused as ANY segment, in any case.
        "compartilhado",
        "Compartilhado",
        "lapix",
        "LAPIX",
        "projetos/compartilhado",
        "me/lapix/datasets",
    ],
)
def test_validate_prefix_refuses_broad_or_shared(bad):
    with pytest.raises(SafetyError):
        paths.validate_prefix(bad)


def test_validate_prefix_normalizes_unicode_nfc():
    # The same accented name in NFD and NFC must collapse to one logical key.
    import unicodedata

    nfc = unicodedata.normalize("NFC", "usuário/ação")
    nfd = unicodedata.normalize("NFD", "usuário/ação")
    assert nfc != nfd  # different byte forms going in
    assert paths.validate_prefix(nfc) == paths.validate_prefix(nfd)


def test_normalize_rel_normalizes_unicode_nfc():
    import unicodedata

    nfc = unicodedata.normalize("NFC", "pasta/ação.txt")
    nfd = unicodedata.normalize("NFD", "pasta/ação.txt")
    assert paths.normalize_rel(nfc) == paths.normalize_rel(nfd)


@pytest.mark.parametrize(
    "good,expected",
    [
        ("users/alice", "users/alice"),
        ("/users/alice/", "users/alice"),
        ("home/bob/project", "home/bob/project"),
    ],
)
def test_validate_prefix_accepts_personal(good, expected):
    assert paths.validate_prefix(good) == expected


# --- assert_within_prefix (the trailing-slash terminator matters) ----------
def test_assert_within_prefix_blocks_sibling_prefix_attack():
    # 'users/aliceEVIL' must NOT be considered inside 'users/alice'.
    with pytest.raises(SafetyError):
        paths.assert_within_prefix("users/aliceEVIL/secret", "users/alice")


def test_assert_within_prefix_blocks_outside():
    for bad in ["etc/passwd", "users/bob/x", "users/alice/../bob/x", "../x"]:
        with pytest.raises(SafetyError):
            paths.assert_within_prefix(bad, "users/alice")


def test_assert_within_prefix_blocks_prefix_root_itself():
    with pytest.raises(SafetyError):
        paths.assert_within_prefix("users/alice", "users/alice")


def test_assert_within_prefix_allows_inside():
    assert paths.assert_within_prefix("users/alice/a/b.txt", "users/alice") == (
        "users/alice/a/b.txt"
    )


def test_remote_path_for_composes_and_validates():
    assert paths.remote_path_for("users/alice", "a/b.txt") == "users/alice/a/b.txt"
    with pytest.raises(SafetyError):
        paths.remote_path_for("users/alice", "../escape")


# --- safe_local_dest: download containment (CWE-22 / Zip-Slip) -------------
@pytest.mark.parametrize(
    "evil",
    [
        "../../etc/passwd",
        "..\\..\\Windows\\system32\\drivers\\etc\\hosts",
        "C:\\Windows\\x",
        "foo/../../bar",
        "with\x00nul",
    ],
)
def test_safe_local_dest_rejects_escapes(tmp_path, evil):
    with pytest.raises(SafetyError):
        paths.safe_local_dest(tmp_path, evil)


@pytest.mark.parametrize("absolute", ["/etc/passwd", "/var/log/x", "//etc//shadow"])
def test_safe_local_dest_forces_absolute_server_path_inside_root(tmp_path, absolute):
    # An absolute server path is NOT honored as absolute -- it is stripped of its
    # leading slash and contained INSIDE root (never writing to /etc/...).
    # Containment is checked path-wise (not by a hardcoded "/" separator) so the
    # assertion holds on Windows too, where paths use "\".
    dest = paths.safe_local_dest(tmp_path, absolute)
    root_abs = tmp_path.resolve()
    assert dest.is_relative_to(root_abs)
    assert dest != root_abs
    assert str(dest) != "/etc/passwd"


def test_safe_local_dest_rejects_windows_reserved(tmp_path):
    for name in ["NUL", "nul.txt", "CON", "com1", "LPT9.dat"]:
        with pytest.raises(SafetyError):
            paths.safe_local_dest(tmp_path, name)


def test_safe_local_dest_keeps_inside(tmp_path):
    dest = paths.safe_local_dest(tmp_path, "a/b/c.txt")
    assert str(dest).startswith(str(tmp_path.resolve()))
    assert dest.name == "c.txt"


def test_safe_local_dest_defeats_symlinked_ancestor(tmp_path):
    # Plant a symlink 'link' -> outside dir, then ask for 'link/evil.txt'.
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SafetyError):
        paths.safe_local_dest(root, "link/evil.txt")


# --- atomic_write: no symlink write-through --------------------------------
def test_atomic_write_basic(tmp_path):
    dest = tmp_path / "sub" / "file.txt"
    paths.atomic_write(dest, b"hello")
    assert dest.read_bytes() == b"hello"


def test_atomic_write_refuses_symlinked_dest(tmp_path):
    target = tmp_path / "real_target.txt"
    target.write_bytes(b"original")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(SafetyError):
        paths.atomic_write(link, b"attacker")
    # The real target must be untouched.
    assert target.read_bytes() == b"original"


def test_atomic_write_refuses_symlinked_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "subdir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SafetyError):
        paths.atomic_write(root / "subdir" / "x.txt", b"data")
    assert not (outside / "x.txt").exists()


def test_atomic_write_is_atomic_replace(tmp_path):
    dest = tmp_path / "f.txt"
    paths.atomic_write(dest, b"v1")
    paths.atomic_write(dest, b"v2")
    assert dest.read_bytes() == b"v2"
    # No stray temp files left behind.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".jp-write-")]
    assert leftovers == []


# --- find_root -------------------------------------------------------------
def test_find_root(tmp_path):
    root = tmp_path / "repo"
    (root / paths.DOT_DIR).mkdir(parents=True)
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    assert paths.find_root(sub) == root.resolve()


def test_find_root_none_outside(tmp_path):
    assert paths.find_root(tmp_path) is None


def test_find_root_stops_at_home(tmp_path, monkeypatch):
    # A stray .jp ABOVE $HOME must never capture a dir inside $HOME.
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    (tmp_path / paths.DOT_DIR).mkdir()  # stray .jp in home's PARENT
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: home))
    # Searching from inside home must NOT find the stray .jp above home.
    assert paths.find_root(home / "work") is None


def test_is_hidden():
    assert paths.is_hidden(".env")
    assert paths.is_hidden("a/.hidden/b")
    assert not paths.is_hidden("a/b/c.txt")


def test_remote_tmp_dir_is_not_dotted():
    # Our remote temp dir must NOT start with a dot (server allow_hidden=False).
    assert not paths.REMOTE_TMP_DIR.startswith(".")


# --- dotfile "protect" encoding -------------------------------------------
@pytest.mark.parametrize(
    "rel,enc",
    [
        (".gitignore", "__jpdot__1_gitignore"),
        (".env.local", "__jpdot__1_env.local"),
        ("..weird", "__jpdot__2_weird"),
        (".config/app.json", "__jpdot__1_config/app.json"),
        ("a/.hidden/b.txt", "a/__jpdot__1_hidden/b.txt"),
        ("plain/file.txt", "plain/file.txt"),
    ],
)
def test_protect_encode_decode_roundtrip(rel, enc):
    assert paths.encode_protected(rel) == enc
    assert paths.decode_protected(enc) == rel
    # Encoded form is never hidden (server-safe) ...
    assert not paths.is_hidden(enc)
    # ... and is recognizable as an alias only when it actually encoded a dotfile.
    assert paths.is_protected_encoded(enc) == (rel != enc)


def test_protect_encode_is_identity_for_plain_paths():
    for rel in ("a/b/c.txt", "file", "dir/sub/leaf"):
        assert paths.encode_protected(rel) == rel
        assert paths.decode_protected(rel) == rel
        assert not paths.is_protected_encoded(rel)

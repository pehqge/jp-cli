"""Tests for the content-addressed object store (jp.versioning.objects).

These are written TDD-first against the documented ObjectStore contract:
content addressing by sha256, write-once storage, a 1-byte compression marker,
a mandatory re-hash integrity check on read, strict sha validation (anti
path-traversal), atomic same-dir writes, and cross-platform guards.
"""

from __future__ import annotations

import hashlib
import os
import zlib

import pytest

from jp.errors import SafetyError
from jp.versioning.objects import ObjectStore, VersioningError


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "work"
    (root / ".jp").mkdir(parents=True)
    return ObjectStore(root)


# 1. round-trip + name == sha256(content) ------------------------------------
def test_roundtrip_and_name_is_sha256(store):
    data = b"hello, world\n"
    sha = store.write(data)
    assert sha == _sha(data)
    assert store.read(sha) == data
    # Object lives at <root>/.jp/objects/<sha[:2]>/<sha[2:]>.
    assert store.path_for(sha) == store.root / ".jp" / "objects" / sha[:2] / sha[2:]
    assert store.path_for(sha).is_file()


def test_empty_payload_roundtrips(store):
    sha = store.write(b"")
    assert sha == _sha(b"")
    assert store.read(sha) == b""


# 2. write-once: second write does not rewrite the object --------------------
def test_write_is_write_once_no_rewrite(store, monkeypatch):
    data = b"write once please"
    sha = store.write(data)
    target = store.path_for(sha)
    assert target.is_file()

    # Any temp-creation by mkstemp the 2nd time would mean a rewrite. Forbid it.
    import tempfile as _tempfile

    def _boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("write-once violated: store re-created a temp file")

    monkeypatch.setattr(_tempfile, "mkstemp", _boom)
    sha2 = store.write(data)
    assert sha2 == sha


def test_write_once_preserves_mtime(store):
    data = b"stable bytes"
    sha = store.write(data)
    target = store.path_for(sha)
    first = target.stat().st_mtime_ns
    sha2 = store.write(data)
    assert sha2 == sha
    assert target.stat().st_mtime_ns == first  # untouched


# 3. dedup: same content -> one object; different -> two ----------------------
def test_dedup_same_and_distinct(store):
    a = store.write(b"alpha")
    a_again = store.write(b"alpha")
    b = store.write(b"beta")
    assert a == a_again
    assert a != b
    objects_dir = store.root / ".jp" / "objects"
    files = [p for p in objects_dir.rglob("*") if p.is_file()]
    assert len(files) == 2


# 4. incompressible -> raw (marker 0x00), size <= original + 1 ----------------
def test_incompressible_stored_raw(store):
    data = os.urandom(4096)
    sha = store.write(data)
    raw = store.path_for(sha).read_bytes()
    assert raw[:1] == b"\x00"
    assert raw[1:] == data
    assert len(raw) <= len(data) + 1
    assert store.read(sha) == data


# 5. compressible -> zlib (marker 0x01), smaller than original ----------------
def test_compressible_stored_zlib(store):
    data = b"a" * 10000
    sha = store.write(data)
    raw = store.path_for(sha).read_bytes()
    assert raw[:1] == b"\x01"
    assert len(raw) < len(data)
    assert zlib.decompress(raw[1:]) == data
    assert store.read(sha) == data


# 6. read RAISES on a corrupted object ---------------------------------------
def test_read_raises_on_corruption(store):
    data = b"a" * 10000  # compressible so we exercise the zlib path too
    sha = store.write(data)
    target = store.path_for(sha)
    blob = bytearray(target.read_bytes())
    blob[-1] ^= 0xFF  # flip a byte in the stored body
    target.write_bytes(bytes(blob))
    with pytest.raises(VersioningError):
        store.read(sha)


def test_read_raises_on_corrupted_raw_object(store):
    data = os.urandom(2048)  # stored raw
    sha = store.write(data)
    target = store.path_for(sha)
    blob = bytearray(target.read_bytes())
    blob[5] ^= 0x01  # flip a body byte; marker stays 0x00
    target.write_bytes(bytes(blob))
    with pytest.raises(VersioningError):
        store.read(sha)


def test_read_raises_on_bad_marker(store):
    data = b"some content"
    sha = store.write(data)
    target = store.path_for(sha)
    blob = bytearray(target.read_bytes())
    blob[0] = 0x05  # not a known marker
    target.write_bytes(bytes(blob))
    with pytest.raises(VersioningError):
        store.read(sha)


def test_read_raises_on_empty_object_file(store):
    data = b"content"
    sha = store.write(data)
    store.path_for(sha).write_bytes(b"")  # no marker at all
    with pytest.raises(VersioningError):
        store.read(sha)


# 7. read RAISES on missing object -------------------------------------------
def test_read_raises_on_missing(store):
    missing = _sha(b"never stored")
    assert store.has(missing) is False
    with pytest.raises(VersioningError):
        store.read(missing)


# 8. invalid sha args rejected before any FS access --------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "abc",  # too short
        "A" * 64,  # uppercase
        "g" * 64,  # non-hex letter
        "0" * 63,  # 63 chars
        "0" * 65,  # 65 chars
        "0" * 63 + "/",  # contains slash
        "../" + "0" * 61,  # traversal
        "0" * 62 + "..",  # dot-dot tail
        "0" * 32 + "/" + "0" * 31,  # embedded slash, right length otherwise
        "a" * 64 + "\n",  # trailing newline: a "$"-anchored regex would let this slip
    ],
)
def test_invalid_sha_rejected(store, bad, monkeypatch):
    # Guarantee validation happens BEFORE touching the filesystem: make any
    # path composition explode if reached.
    def _no_fs(self, sha):  # pragma: no cover - must not be reached
        raise AssertionError("path composed before sha validation")

    monkeypatch.setattr(ObjectStore, "_shard_path", _no_fs, raising=False)
    with pytest.raises((SafetyError, VersioningError)):
        store.has(bad)
    with pytest.raises((SafetyError, VersioningError)):
        store.read(bad)
    with pytest.raises((SafetyError, VersioningError)):
        store.path_for(bad)


# 9. write_stream == write for identical bytes -------------------------------
def test_write_stream_matches_write(store, tmp_path):
    data = b"a" * 5000 + os.urandom(1000) + b"b" * 5000
    f = tmp_path / "blob.bin"
    f.write_bytes(data)

    sha_mem = store.write(data)
    # Fresh store so the stream path actually writes its own object.
    other_root = tmp_path / "other"
    (other_root / ".jp").mkdir(parents=True)
    other = ObjectStore(other_root)
    sha_stream = other.write_stream(f)

    assert sha_stream == sha_mem
    assert other.path_for(sha_stream).read_bytes() == store.path_for(sha_mem).read_bytes()
    assert other.read(sha_stream) == data


def test_write_stream_large_file_is_chunked(store, tmp_path):
    # A few MiB to exercise multi-chunk hashing without being slow.
    data = (b"lorem ipsum " * 1000) * 300  # ~3.6 MiB, highly compressible
    f = tmp_path / "big.txt"
    f.write_bytes(data)
    sha = store.write_stream(f)
    assert sha == _sha(data)
    assert store.read(sha) == data
    raw = store.path_for(sha).read_bytes()
    assert raw[:1] == b"\x01"  # compressible -> zlib


def test_write_stream_is_write_once(store, tmp_path):
    data = b"streamed once"
    f = tmp_path / "s.bin"
    f.write_bytes(data)
    sha = store.write_stream(f)
    mtime = store.path_for(sha).stat().st_mtime_ns
    sha2 = store.write_stream(f)
    assert sha2 == sha
    assert store.path_for(sha).stat().st_mtime_ns == mtime


# 10. objtmp-* files ignored + cleaned up after success ----------------------
def test_objtmp_files_ignored_and_cleaned(store):
    data = b"clean tmp"
    sha = store.write(data)
    shard = store.path_for(sha).parent

    # No stray objtmp-* left behind after a successful write.
    leftovers = list(shard.glob("objtmp-*"))
    assert leftovers == []

    # A planted objtmp-* with a valid-looking name must be ignored by has/read.
    fake = shard / ("objtmp-" + sha[2:])
    fake.write_bytes(b"\x00garbage")
    assert store.has(sha) is True  # real object still found
    assert store.read(sha) == data
    # And listing-style helpers must not surface it.
    objs = list(store.iter_objects()) if hasattr(store, "iter_objects") else []
    assert all(not str(p).endswith(fake.name) for p in objs)


def test_objtmp_not_treated_as_object(store):
    # A shard whose ONLY file is an objtmp-* must look empty to has().
    shard = store.root / ".jp" / "objects" / "ab"
    shard.mkdir(parents=True)
    sha = "ab" + "c" * 62
    (shard / ("objtmp-" + sha[2:])).write_bytes(b"\x00x")
    assert store.has(sha) is False


# 11. atomicity: replace fails mid-write -> no object at final path ----------
def test_atomic_write_failure_leaves_no_partial(store, monkeypatch):
    data = b"atomic or nothing"
    sha = _sha(data)
    target = store.path_for(sha)

    real_replace = os.replace

    def _broken_replace(src, dst, *a, **k):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _broken_replace)
    with pytest.raises(OSError):
        store.write(data)
    # No partial object, no stray temp.
    assert not target.exists()
    shard = target.parent
    if shard.exists():
        assert list(shard.glob("objtmp-*")) == []

    # Store stays consistent: a real write of the same data now succeeds.
    monkeypatch.setattr(os, "replace", real_replace)
    sha2 = store.write(data)
    assert sha2 == sha
    assert store.read(sha) == data


def test_atomic_write_failure_in_stream_leaves_no_partial(store, monkeypatch, tmp_path):
    data = b"stream atomic"
    f = tmp_path / "x.bin"
    f.write_bytes(data)
    sha = _sha(data)

    def _broken_replace(src, dst, *a, **k):
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", _broken_replace)
    with pytest.raises(OSError):
        store.write_stream(f)
    assert not store.path_for(sha).exists()
    shard = store.path_for(sha).parent
    if shard.exists():
        assert list(shard.glob("objtmp-*")) == []


# 12. cross-platform guards --------------------------------------------------
def test_references_o_nofollow_guard():
    import inspect

    import jp.versioning.objects as mod

    src = inspect.getsource(mod)
    assert 'getattr(os, "O_NOFOLLOW", 0)' in src or "getattr(os, 'O_NOFOLLOW', 0)" in src


def test_dir_fsync_oserror_is_suppressed(store, monkeypatch):
    data = b"durable enough"
    sha = _sha(data)

    real_fsync = os.fsync

    def _fsync(fd):
        # Raise only for directory fds; let the temp-file fsync succeed so we
        # specifically exercise the suppressed dir-fsync path.
        try:
            st = os.fstat(fd)
        except OSError:
            return real_fsync(fd)
        import stat as _stat

        if _stat.S_ISDIR(st.st_mode):
            raise OSError("dir fsync not supported here")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _fsync)
    # Must not crash despite the directory fsync raising.
    assert store.write(data) == sha
    assert store.read(sha) == data


# Symlink refusal (POSIX) ----------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_refuses_symlinked_shard_dir(store, tmp_path):
    data = b"symlink target test"
    sha = _sha(data)
    objects_dir = store.root / ".jp" / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "evil"
    elsewhere.mkdir()
    # Plant a symlinked shard dir; the store must refuse to write through it.
    (objects_dir / sha[:2]).symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(SafetyError):
        store.write(data)
    # Nothing leaked into the symlink target.
    assert list(elsewhere.iterdir()) == []


# has/path_for parity --------------------------------------------------------
def test_has_and_path_for(store):
    data = b"present"
    sha = store.write(data)
    assert store.has(sha) is True
    assert store.path_for(sha).is_file()
    other = _sha(b"absent")
    assert store.has(other) is False


# --- FIX 1: capped/streaming decompression (zlib-bomb defense) --------------
def _plant_object(store, marker: bytes, body: bytes, sha: str) -> None:
    """Write a raw object file (marker + body) at the path for ``sha``."""
    target = store.path_for(sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(marker + body)


def test_decompression_bomb_rejected_by_max_size(store):
    # A 5 MiB payload compresses to a few KiB; reading with a tiny max_size must
    # raise BEFORE the full output is materialized (capped at ~max_size bytes).
    big = b"\x00" * (5 * 1024 * 1024)
    sha = _sha(big)
    _plant_object(store, b"\x01", zlib.compress(big), sha)

    raw_on_disk = store.path_for(sha).read_bytes()
    assert len(raw_on_disk) < 64 * 1024  # the stored file itself is tiny

    with pytest.raises(VersioningError) as exc:
        store.read(sha, max_size=1024)
    assert "cap" in str(exc.value).lower()


def test_decompression_bomb_rejected_by_default_cap(store, monkeypatch):
    # With the default cap (monkeypatched tiny for speed), an object that
    # decompresses beyond it must raise even without an explicit max_size.
    import jp.versioning.objects as mod

    monkeypatch.setattr(mod, "_MAX_DECOMPRESSED_BYTES", 1024)
    big = b"\x00" * (5 * 1024 * 1024)
    sha = _sha(big)
    _plant_object(store, b"\x01", zlib.compress(big), sha)

    with pytest.raises(VersioningError) as exc:
        store.read(sha)  # no max_size -> default cap applies
    assert "cap" in str(exc.value).lower()


def test_oversized_raw_object_rejected_by_cap(store):
    # The RAW-marker path is bounded by the same cap.
    body = b"x" * 4096
    sha = _sha(body)
    _plant_object(store, b"\x00", body, sha)
    with pytest.raises(VersioningError) as exc:
        store.read(sha, max_size=1024)
    assert "cap" in str(exc.value).lower()


def test_max_size_exact_succeeds_and_minus_one_raises(store):
    # max_size == exact decompressed size succeeds; one byte tighter raises.
    data = b"a" * 10000  # compressible -> stored zlib
    sha = store.write(data)
    assert store.read(sha, max_size=len(data)) == data
    with pytest.raises(VersioningError):
        store.read(sha, max_size=len(data) - 1)


def test_max_size_exact_succeeds_for_raw_object(store):
    data = os.urandom(2048)  # incompressible -> stored raw
    sha = store.write(data)
    assert store.read(sha, max_size=len(data)) == data
    with pytest.raises(VersioningError):
        store.read(sha, max_size=len(data) - 1)


def test_default_cap_is_finite_and_generous():
    import jp.versioning.objects as mod

    # Documented default: 2 GiB. Finite, and far above any real tree/commit.
    assert mod._MAX_DECOMPRESSED_BYTES == 2 * 1024 * 1024 * 1024


def test_capped_read_still_verifies_hash(store):
    # A corrupted (but under-cap) object must still fail the integrity re-hash,
    # i.e. the cap does not bypass the mandatory verification.
    data = b"a" * 4096
    sha = store.write(data)
    target = store.path_for(sha)
    blob = bytearray(target.read_bytes())
    blob[-1] ^= 0xFF
    target.write_bytes(bytes(blob))
    with pytest.raises(VersioningError):
        store.read(sha, max_size=1 << 20)


# --- FIX 2: trailing-newline sha must be rejected everywhere ----------------
def test_trailing_newline_sha_rejected(store):
    bad = "a" * 64 + "\n"
    with pytest.raises(VersioningError):
        store.has(bad)
    with pytest.raises(VersioningError):
        store.read(bad)
    with pytest.raises(VersioningError):
        store.path_for(bad)


# =========================================================================== #
# import_object: the ONE trusted gateway for UNTRUSTED remote object bytes.
# =========================================================================== #
def _object_file_bytes(store: ObjectStore, sha: str) -> bytes:
    """The exact on-disk object-file bytes (marker + body) for a stored object."""
    return store.path_for(sha).read_bytes()


def test_import_object_places_verified_bytes(store):
    """A faithful object-file (the exact write() encoding) is verified and placed."""
    data = b"hello import"
    sha = _sha(data)
    payload = ObjectStore.__module__  # noqa: F841  (touch to keep import-light)
    # Build the object-file the way write() would, via a throwaway store.
    from jp.versioning.objects import _encode

    store.import_object(sha, _encode(data))
    assert store.has(sha)
    assert store.read(sha) == data


def test_import_object_tampered_bytes_raise_and_place_nothing(store):
    """Flipping a byte in the served object-file is rejected; nothing is placed."""
    from jp.versioning.objects import _encode

    data = b"x" * 4096  # compressible -> zlib body, so a flip corrupts the content
    sha = _sha(data)
    tampered = bytearray(_encode(data))
    tampered[-1] ^= 0xFF
    with pytest.raises(VersioningError):
        store.import_object(sha, bytes(tampered))
    # Nothing corrupt was placed at the hashed path, and no temp left behind.
    assert not store.has(sha)
    shard = store.objects_dir / sha[:2]
    if shard.is_dir():
        assert list(shard.iterdir()) == []


def test_import_object_wrong_name_rejected(store):
    """Bytes whose content hashes to X, imported under name Y, are rejected."""
    from jp.versioning.objects import _encode

    data = b"genuine content"
    wrong_sha = _sha(b"a different thing")
    with pytest.raises(VersioningError):
        store.import_object(wrong_sha, _encode(data))
    assert not store.has(wrong_sha)


def test_import_object_bad_marker_rejected(store):
    data = b"payload"
    sha = _sha(data)
    with pytest.raises(VersioningError):
        store.import_object(sha, b"\x09" + data)  # unknown marker
    assert not store.has(sha)


def test_import_object_decompression_bomb_capped(store):
    """A tiny zlib body that inflates hugely is refused via max_size; no OOM."""
    bomb_plain = b"\x00" * (50 * 1024 * 1024)  # 50 MiB of zeros -> tiny compressed
    sha = _sha(bomb_plain)
    payload = b"\x01" + zlib.compress(bomb_plain)
    assert len(payload) < 100 * 1024  # the served file is tiny
    # A tight max_size (the would-be tree-entry size) far below the real size:
    with pytest.raises(VersioningError):
        store.import_object(sha, payload, max_size=1024)
    assert not store.has(sha)


def test_import_object_write_once_skips_but_verifies(store):
    """An already-present object is left untouched, but the bytes are still verified."""
    from jp.versioning.objects import _encode

    data = b"already here"
    sha = store.write(data)
    original = _object_file_bytes(store, sha)

    # Importing the faithful bytes again is a no-op (write-once); file unchanged.
    store.import_object(sha, _encode(data))
    assert _object_file_bytes(store, sha) == original

    # Importing TAMPERED bytes for an object we already hold still RAISES (a hostile
    # remote gets no free pass), and never rewrites the immutable file.
    tampered = bytearray(_encode(data))
    tampered[0] = 0x09  # corrupt the marker
    with pytest.raises(VersioningError):
        store.import_object(sha, bytes(tampered))
    assert _object_file_bytes(store, sha) == original


def test_import_object_invalid_sha_rejected(store):
    from jp.versioning.objects import _encode

    for bad in ("", "xyz", "A" * 64, "../etc", "g" * 64, "a" * 63):
        with pytest.raises(VersioningError):
            store.import_object(bad, _encode(b"d"))


def test_import_object_raw_marker_roundtrips(store):
    """An incompressible (raw-marker) object-file imports and reads back faithfully."""
    import os as _os

    from jp.versioning.objects import _encode

    data = _os.urandom(2048)  # random -> stored raw (marker 0x00)
    sha = _sha(data)
    payload = _encode(data)
    assert payload[:1] == b"\x00"
    store.import_object(sha, payload)
    assert store.read(sha) == data


# =========================================================================== #
# FIX 1 (BLOCKER): import_object CLAMPS an untrusted max_size to the module cap.
# A hostile tree declaring size=10 GiB must NOT lift the bomb ceiling.
# =========================================================================== #
def test_import_object_clamps_untrusted_max_size_to_module_cap(store, monkeypatch):
    """A max_size LARGER than the module ceiling cannot raise the bomb ceiling.

    The module cap is patched tiny (1024) so CI never allocates GB. A bomb body
    that inflates to ~1 MiB is imported with max_size=1 MiB -- WAY above the patched
    cap. import_object must clamp the effective cap to 1024 and REJECT the bomb,
    placing nothing. This proves a hostile tree's declared size cannot lift the cap.
    """
    import jp.versioning.objects as objmod

    monkeypatch.setattr(objmod, "_MAX_DECOMPRESSED_BYTES", 1024)

    plain = b"\x00" * (1024 * 1024)  # 1 MiB -> tiny compressed
    sha = _sha(plain)
    payload = b"\x01" + zlib.compress(plain)
    # max_size is 1 MiB -- far above the patched 1024 module cap. Without the clamp
    # the bomb would inflate to 1 MiB; with the clamp it is refused at 1024.
    with pytest.raises(VersioningError):
        store.import_object(sha, payload, max_size=1024 * 1024)
    assert not store.has(sha)
    shard = store.objects_dir / sha[:2]
    if shard.is_dir():
        assert list(shard.iterdir()) == []  # no temp left, nothing placed


def test_import_object_clamp_allows_legitimate_under_cap(store, monkeypatch):
    """The clamp never blocks an HONEST object that fits under the module ceiling."""
    import jp.versioning.objects as objmod

    monkeypatch.setattr(objmod, "_MAX_DECOMPRESSED_BYTES", 1 << 20)  # 1 MiB
    data = b"x" * 4096  # tiny, well under the cap
    sha = _sha(data)
    from jp.versioning.objects import _encode

    # An over-large declared max_size is clamped DOWN, but the real object is small,
    # so it still imports + reads back faithfully.
    store.import_object(sha, _encode(data), max_size=10 * (1 << 30))
    assert store.read(sha) == data


def test_read_still_honors_large_max_size_trusted(store, monkeypatch):
    """TRUSTED read() is NOT clamped: a >module-cap max_size is honored as-is.

    A user who raised versioning.max_blob_mb may legitimately hold a blob larger
    than the default ceiling; read() must still return it. We patch the module cap
    tiny and prove read() honors a max_size ABOVE it (the data is small so CI is
    cheap) -- i.e. read() passes max_size through unchanged, unlike import_object.
    """
    import jp.versioning.objects as objmod

    monkeypatch.setattr(objmod, "_MAX_DECOMPRESSED_BYTES", 8)  # absurdly tiny
    data = b"y" * 4096  # 4096 > 8 (the patched module cap)
    sha = store.write(data)
    # read() with a generous max_size ABOVE the patched module cap still succeeds,
    # because the trusted path does NOT clamp to the module ceiling.
    assert store.read(sha, max_size=1 << 20) == data
    # And with max_size=None it WOULD clamp to the (tiny) module cap -> reject.
    with pytest.raises(VersioningError):
        store.read(sha)

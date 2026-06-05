"""Content-addressed, write-once object store -- the versioning foundation.

Everything else in :mod:`jp.versioning` (refs, trees, commits, staging) stores
its bytes here and refers to them by hash. The store is deliberately tiny and
paranoid; the threat model is the same as :mod:`jp.paths`: a SHARED, multi-user
machine with a possibly hostile filesystem (planted symlinks, racing writers).

Layout
------
Objects live at ``<root>/.jp/objects/<sha[:2]>/<sha[2:]>`` (a git-style 2-char
fan-out so a single directory never holds millions of entries). The object's
NAME is ``sha256(content)`` -- the sha256 of the ORIGINAL, uncompressed bytes,
matching :func:`jp.sync.sha256_file` so a synced file and its stored object
share one hash. Names are lowercase 64-hex; nothing else is ever a valid name.

On-disk payload format
----------------------
Each object file is a 1-byte compression marker followed by the body::

    marker b"\\x00"  -> body is the RAW, uncompressed content
    marker b"\\x01"  -> body is zlib.compress(content)

zlib is chosen ONLY when it actually shrinks the data; otherwise we store raw,
so the store can never inflate an object (important for already-compressed
inputs like notebooks-with-images, zips, random data). The marker is the only
in-band metadata; the sha is NEVER stored in the file (it is the file's path).

Security & durability invariants
--------------------------------
* WRITE-ONCE / IMMUTABLE: if an object already exists we do nothing and return
  its hash. We NEVER overwrite -- a content-addressed object can only ever hold
  one value, and a later commit may already name it.
* ATOMIC writes: we write to a UNIQUE same-directory temp (``objtmp-*`` via
  :func:`tempfile.mkstemp`), ``fsync`` it, then ``os.replace`` it onto the
  final hashed path (atomic on one filesystem, on every platform). After the
  rename we best-effort ``fsync`` the shard directory so the new name is durable
  before any future ref can point at it. Dir-fsync is unsupported on some
  platforms (notably Windows) -> wrapped in ``contextlib.suppress(OSError)``.
* NO SYMLINK FOLLOW: like :func:`jp.paths.atomic_write` we open with
  ``O_NOFOLLOW`` where available and refuse a symlink at the temp or final
  path, and we create shard dirs without traversing a symlinked ancestor. A
  planted symlink can therefore never redirect a write outside ``.jp/objects``.
* READ-TIME INTEGRITY: :meth:`read` re-hashes the decompressed bytes and raises
  if the result does not equal the requested sha. Silent bit-rot or tampering
  is turned into a loud, actionable error instead of corrupt history.
* PATH-TRAVERSAL DEFENSE: every ``sha`` argument is validated against
  ``^[0-9a-f]{64}$`` BEFORE it is used to compose a path, so ``..``, ``/``,
  uppercase, short, empty, or non-hex inputs are rejected up front.
* ``objtmp-*`` temp files are IGNORED by readers/listers and cleaned up on
  success (and on any mid-write failure), so a crashed writer leaves no garbage
  that could be mistaken for an object. The prefix has NO leading dot on
  purpose (dotted names are rejected by the remote and confuse listings).
* Object files are chmod 0o600 best-effort (private on a shared box).

Cross-platform: standard library only; ``os.replace`` for atomicity; guarded
``O_NOFOLLOW`` and dir-fsync; ``pathlib`` throughout.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import zlib
from collections.abc import Iterator
from pathlib import Path

from ..errors import JpError, SafetyError
from ..paths import DOT_DIR

# Subdir (under .jp) holding the content-addressed objects.
OBJECTS_DIR = "objects"

# On-disk compression markers (1 byte, leads every object file).
MARKER_RAW = b"\x00"  # body is the uncompressed content
MARKER_ZLIB = b"\x01"  # body is zlib.compress(content)

# Temp-file prefix for in-progress writes. No leading dot (dotted names are
# rejected by the remote and the readers below ignore anything starting here).
TMP_PREFIX = "objtmp-"

# Chunk size for streaming hashes/copies (1 MiB) -- memory-bounded for big files.
_CHUNK = 1 << 20

# Hard ceiling on the size of a SINGLE decompressed object when the caller does
# not supply an expected size. A crafted object can compress a few KiB into a
# multi-GB body (a "zip bomb"); read() refuses to materialize past this cap so a
# hostile object cannot OOM the process before the integrity re-hash runs. 2 GiB
# is far above any real tree/commit/blob jp produces; callers that know the exact
# expected size (from a tree entry) pass max_size to cap much tighter.
_MAX_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# A valid object name is exactly 64 lowercase hex chars (sha256 hexdigest). We
# use re.fullmatch (not a "$"-anchored search) because "$" also matches just
# before a trailing newline, so "<64 hex>\n" would wrongly pass; fullmatch
# requires the WHOLE string to be exactly 64 hex chars.
_SHA_RE = re.compile(r"[0-9a-f]{64}")

# O_NOFOLLOW exists on POSIX; on Windows it is absent -> fall back to 0 and rely
# on the explicit is_symlink() checks (mirrors jp.paths).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class VersioningError(JpError):
    """A versioning invariant failed (missing/corrupt object, bad object name).

    Lives in the versioning package rather than :mod:`jp.errors` so this task
    touches no shared module. It derives from :class:`jp.errors.JpError`, so it
    carries the generic exit code and is handled uniformly by the CLI; integrity
    failures are unexpected/corrupt-state, not the intentional refusals that
    :class:`jp.errors.SafetyError` represents (those keep their own exit code).
    """


class ObjectStore:
    """An immutable, content-addressed object store rooted at a repo ``root``.

    Construct with the repository root (the directory that contains ``.jp``).
    Objects are stored under ``<root>/.jp/objects``.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # --- paths --------------------------------------------------------------
    @property
    def objects_dir(self) -> Path:
        """The ``.jp/objects`` directory (not guaranteed to exist yet)."""
        return self.root / DOT_DIR / OBJECTS_DIR

    def _shard_path(self, sha: str) -> Path:
        """Compose the on-disk path for ``sha`` (validated by the caller)."""
        return self.objects_dir / sha[:2] / sha[2:]

    def path_for(self, sha: str) -> Path:
        """Return the on-disk path for ``sha`` after validating the name.

        Raises :class:`VersioningError` for any string that is not a lowercase
        64-hex sha256 (this is the anti path-traversal gate; see module docs).
        """
        _validate_sha(sha)
        return self._shard_path(sha)

    # --- queries ------------------------------------------------------------
    def has(self, sha: str) -> bool:
        """True iff a real object named ``sha`` exists (ignores ``objtmp-*``).

        ``sha`` is validated first, so an attacker-controlled string can never
        be turned into a stray ``stat`` outside the objects tree.
        """
        _validate_sha(sha)
        p = self._shard_path(sha)
        # A path that is exactly a hashed object name can never be an objtmp-*
        # temp (those carry the TMP_PREFIX), but guard anyway for symmetry.
        if p.name.startswith(TMP_PREFIX):
            return False
        return p.is_file()

    def iter_objects(self) -> Iterator[Path]:
        """Yield the path of every committed object, skipping ``objtmp-*`` temps.

        Used by later GC/fsck passes; kept here so the temp-skip rule lives with
        the writer that creates those temps.
        """
        base = self.objects_dir
        if not base.is_dir():
            return
        for shard in sorted(base.iterdir()):
            if not shard.is_dir() or shard.is_symlink():
                continue
            for entry in sorted(shard.iterdir()):
                if entry.name.startswith(TMP_PREFIX):
                    continue
                if entry.is_file() and not entry.is_symlink():
                    yield entry

    # --- read ---------------------------------------------------------------
    def read(self, sha: str, *, max_size: int | None = None) -> bytes:
        """Load, decompress (CAPPED), and INTEGRITY-VERIFY the object named ``sha``.

        The decompressed bytes are re-hashed and compared to ``sha``; a mismatch
        (bit-rot, truncation, tampering, an unknown marker) raises
        :class:`VersioningError`. A missing object also raises. The store never
        returns bytes it cannot prove are the requested content.

        Decompression is BOUNDED to defeat a zlib bomb: a crafted object can
        expand a few KiB into many GB, which would OOM the process BEFORE the
        re-hash could reject it. We therefore decompress incrementally and stop
        as soon as the output would exceed the cap, raising rather than
        allocating. ``max_size`` is the cap when the caller knows the blob's
        expected size (e.g. from a tree entry) -- output is allowed to equal it
        but never exceed it. When ``max_size`` is None the cap is the module
        constant :data:`_MAX_DECOMPRESSED_BYTES`. The RAW-marker path is bounded
        by the same cap, so an oversized raw body is also refused.
        """
        _validate_sha(sha)
        p = self._shard_path(sha)
        try:
            payload = p.read_bytes()
        except FileNotFoundError as exc:
            raise VersioningError(f"object not found: {sha}") from exc
        except OSError as exc:
            raise VersioningError(f"could not read object {sha}: {exc}") from exc
        return _decode_and_verify(sha, payload, max_size)

    # --- write --------------------------------------------------------------
    def write(self, data: bytes) -> str:
        """Store ``data`` and return its lowercase 64-hex sha256.

        Write-once: if the object already exists, returns the hash WITHOUT
        rewriting it. Otherwise encodes the payload (raw vs zlib, whichever is
        not larger) and writes it atomically (see :meth:`_atomic_store`).
        """
        # sha is self-computed by hashlib here, so it is ALWAYS a valid 64-hex
        # string; we compose the shard path directly (no _validate_sha needed).
        # The "validate before compose" invariant only guards CALLER-supplied
        # ids (read/has/path_for), never our own hexdigest.
        sha = hashlib.sha256(data).hexdigest()
        target = self._shard_path(sha)
        if target.is_file():
            return sha  # immutable & already present -> nothing to do
        self._atomic_store(target, _encode(data))
        return sha

    def write_stream(self, path: Path) -> str:
        """Store the contents of ``path`` memory-bounded; same result as ``write``.

        The file is hashed in ``_CHUNK``-sized reads so a multi-GB file never
        needs to fit in memory for hashing. If the resulting object already
        exists we skip writing entirely (write-once). Otherwise we read the file
        once more to encode + store it. The produced sha and the stored bytes are
        IDENTICAL to ``write`` of the same content.
        """
        src = Path(path)
        h = hashlib.sha256()
        with open(src, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                h.update(chunk)
        # As in write(), sha is self-computed by hashlib (always valid 64-hex),
        # so composing the shard path without _validate_sha is safe -- that gate
        # exists only for caller-supplied ids.
        sha = h.hexdigest()

        target = self._shard_path(sha)
        if target.is_file():
            return sha  # write-once short-circuit; never re-read the big file

        # We must decide raw-vs-zlib by the SAME rule as write(); that needs the
        # full body. Reading the whole file is acceptable here (we already know
        # we have to materialize one encoded copy to store it).
        data = src.read_bytes()
        # Defend against a concurrent truncation/modification between the hash
        # pass and this read: the bytes we store must match the name we hashed.
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha:
            raise VersioningError(
                f"file changed while streaming {src}: hashed {sha} but read {actual}"
            )
        self._atomic_store(target, _encode(data))
        return sha

    def import_object(
        self, sha: str, object_file_bytes: bytes, *, max_size: int | None = None
    ) -> None:
        """VERIFY untrusted ``object_file_bytes`` and, if valid, place them at ``sha``.

        The ONE trusted gateway for bytes that came from an UNTRUSTED source -- the
        remote ``__jp`` history backup (Task 9 fetch). ``object_file_bytes`` is the
        EXACT on-disk object-file payload (1-byte marker + raw|zlib body) the remote
        served; ``sha`` is the name we requested (already derived from an
        already-VERIFIED parent object, never a path the server volunteered).

        The contract, in order:

        1. ``sha`` is validated as lowercase 64-hex BEFORE any path is composed
           (anti path-traversal), exactly like :meth:`write`/:meth:`read`.
        2. WRITE-ONCE: if the object already exists locally we VERIFY the supplied
           bytes still decode+re-hash to ``sha`` (so a hostile remote cannot make us
           accept garbage even for an object we happen to already hold) and return
           WITHOUT rewriting the immutable file.
        3. Otherwise the bytes are written to a UNIQUE same-dir atomic temp, then
           DECODED (marker + capped decompress) and RE-HASHED via the SAME gate
           :meth:`read` uses (:func:`_decode_and_verify`). ``max_size`` caps the
           decompression tightly (a tree-entry size) so a 100KB->GB bomb is refused
           before it can OOM us. If the verification passes we ``os.replace`` the
           temp into place (atomic, fsync, symlink-safe -- identical to
           :meth:`write`); if it FAILS we delete the temp and raise
           :class:`VersioningError`, having placed NOTHING corrupt.

        UNTRUSTED-CAP CLAMP: ``max_size`` here is attacker-influenced -- on fetch it
        is a tree entry's declared ``size``, and the tree's bytes verifying does NOT
        make that number honest (a tree declaring ``size = 10 GiB`` is self-
        consistent). So we CLAMP the effective decompression cap DOWN to the module
        ceiling :data:`_MAX_DECOMPRESSED_BYTES`; a hostile tree can only ever make
        the cap TIGHTER, never raise the bomb ceiling. (The trusted :meth:`read`
        path deliberately does NOT clamp -- a user who raised
        ``versioning.max_blob_mb`` may legitimately hold a >2 GiB local blob and
        must still be able to read it back.)

        Raises :class:`VersioningError` on a bad sha, a bad marker, a size-cap/bomb
        violation, or a hash mismatch. Never places unverified bytes.
        """
        _validate_sha(sha)
        target = self._shard_path(sha)

        # Clamp the UNTRUSTED max_size to the module ceiling so a hostile tree's
        # declared size can only tighten the cap, never lift the bomb ceiling.
        effective = (
            _MAX_DECOMPRESSED_BYTES if max_size is None else min(max_size, _MAX_DECOMPRESSED_BYTES)
        )

        if target.is_file():
            # WRITE-ONCE: the immutable file is already here. Still VERIFY the
            # supplied bytes are the real content for this sha (a hostile remote
            # must never get a free pass just because we already hold the object),
            # then leave the existing file untouched.
            _decode_and_verify(sha, object_file_bytes, effective)
            return

        shard = target.parent
        _ensure_dir_no_symlink(shard)

        # Never replace through a symlink planted at the final destination.
        if target.is_symlink():
            raise SafetyError(f"refusing to write an object through a symlink: {target}")

        fd, tmp_name = tempfile.mkstemp(prefix=TMP_PREFIX, dir=str(shard))
        tmp = Path(tmp_name)
        try:
            if tmp.is_symlink():
                raise SafetyError(f"refusing to write through a symlinked temp file: {tmp}")
            with os.fdopen(fd, "wb") as fh:
                fh.write(object_file_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            with contextlib.suppress(OSError):
                os.chmod(str(tmp), 0o600)
            # VERIFY BEFORE PLACING: decode+cap+re-hash the bytes we just wrote.
            # A failure here means the remote served a corrupt/tampered/bomb object;
            # we raise WITHOUT renaming, so nothing untrusted reaches a hashed path.
            # ``effective`` is clamped to the module ceiling (see the clamp above).
            _decode_and_verify(sha, object_file_bytes, effective)
            os.replace(str(tmp), str(target))
        except BaseException:
            # Leave NO partial/unverified object: drop the temp. Best-effort --
            # never mask the original error (the verification VersioningError).
            with contextlib.suppress(OSError):
                os.unlink(str(tmp))
            raise

        _fsync_dir(shard)

    # --- internal: the one place that ever writes an object -----------------
    def _atomic_store(self, target: Path, payload: bytes) -> None:
        """Atomically materialize ``payload`` at ``target`` (a hashed path).

        Steps: ensure the shard dir (no symlink traversal); refuse a symlinked
        final path; mkstemp a UNIQUE same-dir ``objtmp-*``; refuse a symlinked
        temp; write+fsync the temp; ``os.replace`` it onto ``target``; chmod
        0o600 and fsync the shard dir best-effort. On ANY failure the temp is
        removed so no partial object is left at ``target``.
        """
        shard = target.parent
        _ensure_dir_no_symlink(shard)

        # Never replace through a symlink planted at the final destination.
        if target.is_symlink():
            raise SafetyError(f"refusing to write an object through a symlink: {target}")

        # Unique same-directory temp. mkstemp opens with O_EXCL + 0o600 and
        # returns a guaranteed-unique name, so concurrent writers never collide.
        fd, tmp_name = tempfile.mkstemp(prefix=TMP_PREFIX, dir=str(shard))
        tmp = Path(tmp_name)
        try:
            # mkstemp just created this regular file; a symlink here would mean
            # something raced us with a hostile replacement -- refuse it.
            if tmp.is_symlink():
                raise SafetyError(f"refusing to write through a symlinked temp file: {tmp}")
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            with contextlib.suppress(OSError):
                os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(target))
        except BaseException:
            # Leave NO partial object: drop the temp (the final path was never
            # created unless os.replace succeeded, in which case we don't get
            # here). Best-effort -- never mask the original error.
            with contextlib.suppress(OSError):
                os.unlink(str(tmp))
            raise

        # Rename durability: fsync the shard dir so the new name survives a
        # crash before any ref can reference it. Unsupported on some platforms
        # (e.g. Windows) -> suppress OSError.
        _fsync_dir(shard)


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #
def _validate_sha(sha: str) -> None:
    """Reject anything that is not a lowercase 64-hex sha256 BEFORE path use.

    This is the anti path-traversal gate: it runs before any ``sha`` value is
    composed into a filesystem path, so ``..``, ``/``, uppercase, short, empty,
    or non-hex inputs can never reach the filesystem.
    """
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise VersioningError(f"invalid object id: expected 64 lowercase hex chars, got {sha!r}")


def _decode_and_verify(sha: str, payload: bytes, max_size: int | None) -> bytes:
    """Decode an object-file ``payload`` (marker + body), CAP it, and re-hash it.

    The single decode+integrity gate shared by :meth:`ObjectStore.read` (which
    reads ``payload`` from disk) and :meth:`ObjectStore.import_object` (which is
    handed UNTRUSTED remote bytes). The decompressed bytes are re-hashed and
    compared to ``sha``; a mismatch, a bad/unknown marker, an empty file, or a
    decompression bomb (capped) raises :class:`VersioningError`. ``max_size`` caps
    decompression tightly when the caller knows the expected size (a tree-entry
    size); ``None`` falls back to :data:`_MAX_DECOMPRESSED_BYTES`. The cap runs
    BEFORE the re-hash so a hostile "zip bomb" cannot OOM the process first.
    """
    cap = _MAX_DECOMPRESSED_BYTES if max_size is None else max_size
    if len(payload) < 1:
        raise VersioningError(f"corrupt object {sha}: empty file (no marker)")
    marker, body = payload[:1], payload[1:]
    if marker == MARKER_RAW:
        # Even an uncompressed body must respect the cap (it could itself be an
        # oversized planted object); no decompression to bound here.
        if len(body) > cap:
            raise VersioningError(f"object {sha} exceeds size cap ({len(body)} > {cap})")
        data = body
    elif marker == MARKER_ZLIB:
        data = _decompress_capped(body, cap, sha)
    else:
        raise VersioningError(f"corrupt object {sha}: unknown marker {marker!r}")

    actual = hashlib.sha256(data).hexdigest()
    if actual != sha:
        raise VersioningError(
            f"object integrity check failed: stored content hashes to {actual}, expected {sha}"
        )
    return data


def _encode(data: bytes) -> bytes:
    """Return the on-disk payload: marker + (zlib body if smaller, else raw).

    zlib is used ONLY when it produces a strictly smaller body, so an object is
    never larger than ``len(data) + 1`` (the marker byte).
    """
    compressed = zlib.compress(data)
    if len(compressed) < len(data):
        return MARKER_ZLIB + compressed
    return MARKER_RAW + data


def _decompress_capped(body: bytes, cap: int, sha: str) -> bytes:
    """Incrementally inflate ``body``, refusing to exceed ``cap`` bytes of output.

    Uses :func:`zlib.decompressobj` with the ``max_length`` argument so each step
    yields at most a bounded slice; we stop the moment total output would pass
    ``cap`` and raise instead of allocating the rest. This caps memory at roughly
    ``cap`` even for a maliciously crafted "zip bomb" object, well before the
    integrity re-hash in :meth:`ObjectStore.read` would otherwise reject it.
    """
    if cap < 0:
        raise VersioningError(f"object {sha} exceeds size cap (0 > {cap})")
    dobj = zlib.decompressobj()
    out = bytearray()
    # We ask for one byte MORE than the remaining budget each step. As soon as a
    # step yields output that pushes us past ``cap`` we stop and raise, so at most
    # ``cap + 1`` bytes are ever materialized regardless of how large the object
    # claims to inflate to. ``unconsumed_tail`` carries the compressed input that
    # didn't fit under max_length; we feed it back to continue (or detect a stall).
    pending = body
    try:
        while True:
            want = cap - len(out) + 1
            chunk = dobj.decompress(pending, want)
            out.extend(chunk)
            if len(out) > cap:
                raise VersioningError(f"object {sha} decompresses beyond cap ({cap} bytes)")
            pending = dobj.unconsumed_tail
            if not pending:
                break
        out.extend(dobj.flush())
    except zlib.error as exc:
        raise VersioningError(f"corrupt object {sha}: zlib error: {exc}") from exc
    if len(out) > cap:
        raise VersioningError(f"object {sha} decompresses beyond cap ({cap} bytes)")
    return bytes(out)


def _ensure_dir_no_symlink(directory: Path) -> None:
    """``mkdir -p`` ``directory`` but refuse if any existing ancestor is a symlink.

    Mirrors :func:`jp.paths._ensure_dir_no_symlink` so a planted symlinked
    ancestor cannot redirect object writes out of the ``.jp/objects`` tree.
    """
    directory = Path(directory)
    parts: list[Path] = []
    cur = directory
    while True:
        parts.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    for node in reversed(parts):
        if node.exists():
            if node.is_symlink():
                raise SafetyError(f"refusing to traverse a symlinked directory: {node}")
            continue
        try:
            os.mkdir(str(node))
        except FileExistsError:
            # Race: another writer created it -- re-check it is not a symlink.
            if Path(node).is_symlink():
                raise SafetyError(f"refusing to traverse a symlinked directory: {node}") from None


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory for rename durability.

    Opening a directory with ``O_NOFOLLOW`` (where available) refuses a planted
    symlink in place of the shard dir. Directory fsync is unsupported on some
    platforms (notably Windows) and on some filesystems -> any ``OSError`` is
    suppressed (durability is best-effort; correctness does not depend on it).
    """
    flags = getattr(os, "O_RDONLY", 0) | _O_NOFOLLOW
    with contextlib.suppress(OSError):
        fd = os.open(str(directory), flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

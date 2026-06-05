"""HEAD, branch refs, the on-disk format marker, and lazy versioning init.

This is the pointer layer that sits above the content-addressed object store
(:mod:`jp.versioning.objects`). Where the store maps a sha256 to immutable
bytes, this module maps human-meaningful *names* (HEAD, branches) to a commit
sha. It is deliberately git-shaped so the mental model transfers.

On-disk layout (all under ``<root>/.jp/``)
------------------------------------------
* ``HEAD``                -- either ``ref: refs/heads/<branch>\\n`` (SYMBOLIC,
                             the normal case: HEAD follows a branch) or a raw
                             64-hex sha + ``\\n`` (DETACHED: HEAD points straight
                             at a commit, e.g. while inspecting old history).
* ``refs/heads/<branch>`` -- one lowercase 64-hex sha + ``\\n`` (the branch tip).
* ``format``              -- JSON ``{"versioning": <int>}`` -- a forward-compat
                             marker so a newer on-disk format is refused by an
                             older jp instead of being misread.

Security & durability invariants (same threat model as objects.py / paths.py:
a SHARED, multi-user box with a possibly hostile filesystem)
-----------------------------------------------------------------------------
* PATH-TRAVERSAL DEFENSE: a branch name is validated by
  :func:`validate_ref_name` (single safe segment, ``^[A-Za-z0-9._-]+$``, never
  ``.``/``..``/leading-dash/leading-dot/separators/whitespace/control chars)
  BEFORE it is composed into a ``refs/heads/<name>`` path, so a crafted name can
  never read or write outside ``refs/heads``. A sha is validated as 64 lowercase
  hex BEFORE use, exactly as in objects.py.
* ATOMIC writes: every HEAD/ref write uses the SAME discipline as the object
  store -- a unique same-directory ``reftmp-*`` temp via :func:`tempfile.mkstemp`,
  ``fsync`` the temp, ``os.replace`` onto the final path (atomic on one
  filesystem on every platform), then best-effort ``fsync`` the parent dir so the
  new value is durable before anything can depend on it.
* NO SYMLINK FOLLOW: temp/final paths are refused if they are symlinks, and
  directories are created without traversing a symlinked ancestor (mirrors
  objects.py / paths.py), so a planted symlink cannot redirect a ref write.
* LOST-UPDATE GUARD: :func:`update_ref` is a compare-and-swap -- it re-reads the
  current value and refuses if it is not the ``expected`` one. Callers hold the
  process lock (:mod:`jp.versioning.lock`) around ref updates; the CAS is
  defense-in-depth against a concurrent advance that slipped past the lock.
* CORRUPTION IS LOUD: a malformed HEAD or a ref file whose content is not a valid
  sha raises :class:`VersioningError` rather than being silently coerced.

Cross-platform: standard library only; ``os.replace`` for atomicity; guarded
``O_NOFOLLOW`` and dir-fsync; ``pathlib`` throughout.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import config as config_mod
from ..errors import SafetyError
from ..paths import DOT_DIR
from .objects import VersioningError

# The branch a fresh repo's HEAD points at before the first commit.
DEFAULT_BRANCH = "main"
# On-disk versioning format version. Bump when the layout changes incompatibly.
FORMAT_VERSION = 1

# File / dir names under .jp.
HEAD_NAME = "HEAD"
FORMAT_NAME = "format"
REFS_DIR = "refs"
HEADS_DIR = "heads"

# Prefix used inside a symbolic HEAD: "ref: refs/heads/<branch>".
_REF_PREFIX = "ref: refs/heads/"

# Temp-file prefix for in-progress ref/HEAD writes (no leading dot, mirroring the
# object store's objtmp-* convention).
_TMP_PREFIX = "reftmp-"

# A valid sha is exactly 64 lowercase hex. fullmatch (not "$"-anchored) so a
# trailing newline cannot slip through (mirrors objects.py rationale).
_SHA_RE = re.compile(r"[0-9a-f]{64}")

# A safe single-segment ref name: letters/digits/dot/underscore/dash only, and
# (enforced separately) not "."/".." and not starting with "-" or ".".
_REF_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")

# O_NOFOLLOW exists on POSIX; absent on Windows -> fall back to 0 and rely on the
# explicit is_symlink() checks (mirrors objects.py / paths.py).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass
class Head:
    """The parsed state of the HEAD pointer.

    ``symbolic`` HEAD follows a branch (the normal case): ``branch`` is the
    branch name and ``target`` is None. A DETACHED HEAD points straight at a
    commit: ``branch`` is None and ``target`` is the raw 64-hex sha.
    """

    symbolic: bool
    branch: str | None
    target: str | None


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #
def _dot(root: Path) -> Path:
    return Path(root) / DOT_DIR


def _head_path(root: Path) -> Path:
    return _dot(root) / HEAD_NAME


def _format_path(root: Path) -> Path:
    return _dot(root) / FORMAT_NAME


def _heads_dir(root: Path) -> Path:
    return _dot(root) / REFS_DIR / HEADS_DIR


def _ref_path(root: Path, name: str) -> Path:
    """Compose the on-disk path for branch ``name`` (validated by the caller)."""
    return _heads_dir(root) / name


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validate_sha(sha: str) -> str:
    """Reject anything that is not a lowercase 64-hex sha256 BEFORE path use.

    A locally defined twin of the object store's gate so the modules stay
    decoupled (we never import the private one). Returns the sha on success.
    """
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise VersioningError(f"invalid commit id: expected 64 lowercase hex chars, got {sha!r}")
    return sha


def validate_ref_name(name: str) -> str:
    """Validate a branch name as a single safe path segment; return it.

    Accepts ONLY ``^[A-Za-z0-9._-]+$`` and additionally rejects ``""``, ``.``,
    ``..``, names starting with ``-`` or ``.``, and (implied by the charset)
    anything containing ``/``, ``\\``, whitespace, or control characters. This
    is the anti path-traversal gate for everything composed into
    ``refs/heads/<name>``. Raises :class:`VersioningError` on an invalid name.
    """
    if not isinstance(name, str) or not _REF_NAME_RE.fullmatch(name):
        raise VersioningError(f"invalid ref name: {name!r}")
    if name in (".", ".."):
        raise VersioningError(f"invalid ref name: {name!r}")
    if name.startswith("-") or name.startswith("."):
        raise VersioningError(f"ref name may not start with '-' or '.': {name!r}")
    return name


# --------------------------------------------------------------------------- #
# Atomic, symlink-refusing write (same discipline as objects.py)
# --------------------------------------------------------------------------- #
def _atomic_write_text(target: Path, text: str) -> None:
    """Atomically write ``text`` to ``target`` (0o600), refusing symlinks.

    Ensures the parent dir without traversing a symlinked ancestor, refuses a
    symlinked final/temp path, writes a unique same-dir ``reftmp-*`` temp,
    fsyncs it, ``os.replace`` onto ``target``, then best-effort fsyncs the parent
    dir. On ANY failure the temp is removed so no partial value is left behind.
    """
    target = Path(target)
    parent = target.parent
    _ensure_dir_no_symlink(parent)

    if target.is_symlink():
        raise SafetyError(f"refusing to write through a symlink: {target}")

    fd, tmp_name = tempfile.mkstemp(prefix=_TMP_PREFIX, dir=str(parent))
    tmp = Path(tmp_name)
    try:
        if tmp.is_symlink():
            raise SafetyError(f"refusing to write through a symlinked temp file: {tmp}")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        with contextlib.suppress(OSError):
            os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(target))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(str(tmp))
        raise

    _fsync_dir(parent)


# --------------------------------------------------------------------------- #
# Initialization & format guard
# --------------------------------------------------------------------------- #
def init_versioning(root: Path) -> None:
    """Lazily initialize versioning metadata under ``<root>/.jp``. Idempotent.

    Creates ``refs/heads`` (no symlink traversal); if HEAD is absent, writes a
    symbolic HEAD pointing at the default branch; if the format marker is absent,
    writes it. Existing values are NEVER clobbered, so calling this repeatedly is
    safe. Also ensures ``.jp/.gitignore`` so the metadata dir stays git-ignored.
    """
    root = Path(root)
    _ensure_dir_no_symlink(_heads_dir(root))

    # Keep .jp git-ignored (defense in depth; never raises -- see config.py).
    config_mod.ensure_dot_gitignore(root)

    if not _head_path(root).exists():
        _atomic_write_text(_head_path(root), f"{_REF_PREFIX}{DEFAULT_BRANCH}\n")

    if not _format_path(root).exists():
        _atomic_write_text(
            _format_path(root),
            json.dumps({"versioning": FORMAT_VERSION}, sort_keys=True) + "\n",
        )


def read_format(root: Path) -> dict | None:
    """Return the parsed format marker, or None if it is absent.

    Raises :class:`VersioningError` if the file exists but is not readable JSON
    object -- a corrupt marker is a loud error, not silently ignored.
    """
    p = _format_path(root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VersioningError(f"could not read versioning format marker {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise VersioningError(f"malformed versioning format marker: {p}")
    return data


def check_format(root: Path) -> None:
    """Refuse a repo whose versioning format is NEWER than this jp understands.

    Forward-compat guard: if the marker records a versioning version greater than
    :data:`FORMAT_VERSION`, raise with an upgrade hint. A missing marker, or an
    equal/older version, is fine.
    """
    data = read_format(root)
    if data is None:
        return
    raw = data.get("versioning", FORMAT_VERSION)
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise VersioningError(f"malformed versioning format version: {raw!r}") from exc
    if version > FORMAT_VERSION:
        raise VersioningError(
            "this repo's versioning was written by a newer jp "
            f"(format version {version} > {FORMAT_VERSION}); upgrade jp to continue."
        )


# --------------------------------------------------------------------------- #
# HEAD
# --------------------------------------------------------------------------- #
def read_head(root: Path) -> Head | None:
    """Parse HEAD. None if the HEAD file is missing (uninitialized repo).

    A ``ref: refs/heads/<X>`` line yields a symbolic Head (with ``<X>``
    validated); a bare 64-hex line yields a detached Head. Anything else raises
    :class:`VersioningError` (a malformed HEAD is corruption, never guessed at).
    """
    p = _head_path(root)
    if not p.is_file():
        return None
    try:
        content = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersioningError(f"could not read HEAD {p}: {exc}") from exc
    line = content.strip()
    if not line:
        raise VersioningError(f"malformed HEAD (empty): {p}")
    if line.startswith(_REF_PREFIX):
        branch = line[len(_REF_PREFIX) :]
        validate_ref_name(branch)  # raises on a traversal-y branch name
        return Head(symbolic=True, branch=branch, target=None)
    # Otherwise it must be a detached raw sha.
    if _SHA_RE.fullmatch(line):
        return Head(symbolic=False, branch=None, target=line)
    raise VersioningError(f"malformed HEAD: {p}")


def resolve_head(root: Path) -> str | None:
    """Return the commit sha HEAD ultimately points to, or None.

    Symbolic HEAD -> the target ref's sha, or None if that ref does not exist yet
    (a valid "unborn branch", normal before the first commit). Detached HEAD ->
    its sha. None if the repo is uninitialized.
    """
    head = read_head(root)
    if head is None:
        return None
    if head.symbolic:
        assert head.branch is not None  # symbolic always carries a branch
        return read_ref(root, head.branch)
    return head.target


def current_branch(root: Path) -> str | None:
    """Return the branch name if HEAD is symbolic, else None (detached/unborn)."""
    head = read_head(root)
    if head is None:
        return None
    return head.branch if head.symbolic else None


def set_head_branch(root: Path, name: str) -> None:
    """Point HEAD at branch ``name`` (symbolic). Validates the name; atomic."""
    validate_ref_name(name)
    _atomic_write_text(_head_path(root), f"{_REF_PREFIX}{name}\n")


def set_head_detached(root: Path, sha: str) -> None:
    """Point HEAD straight at commit ``sha`` (detached). Validates sha; atomic."""
    _validate_sha(sha)
    _atomic_write_text(_head_path(root), f"{sha}\n")


# --------------------------------------------------------------------------- #
# Branch refs
# --------------------------------------------------------------------------- #
def read_ref(root: Path, name: str) -> str | None:
    """Read the sha stored at ``refs/heads/<name>``; None if the ref is absent.

    The name is validated before composing the path, and the stored value is
    validated as a 64-hex sha -- a corrupt ref file raises :class:`VersioningError`.
    """
    validate_ref_name(name)
    p = _ref_path(root, name)
    if not p.is_file():
        return None
    try:
        content = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersioningError(f"could not read ref {name}: {exc}") from exc
    line = content.strip()
    if not _SHA_RE.fullmatch(line):
        raise VersioningError(f"corrupt ref '{name}': not a valid commit id")
    return line


def list_heads(root: Path) -> dict[str, str]:
    """Return a ``branch name -> tip sha`` map for every file under ``refs/heads``.

    Walks ``.jp/refs/heads`` (skipping symlinks, exactly as the object store and
    the prefix-resolver do) and reads each ref through :func:`read_ref`, so every
    branch name is re-validated and every stored value is re-validated as a
    lowercase 64-hex sha. A corrupt ref (a bad name or a non-sha body) is SKIPPED
    rather than aborting the whole listing -- ``fsck``/``gc`` then gather their own
    universe from this map, and ``fsck`` separately reports a ref that cannot be
    read. Returns an empty dict for an uninitialized repo (no ``refs/heads``).

    This is deliberately tolerant (skip a corrupt entry) so a single bad ref can
    never hide every OTHER reachable commit from ``gc``'s reachability walk -- the
    safety bias is to KEEP data, never to prune it because one ref was unreadable.
    """
    heads_dir = _heads_dir(root)
    out: dict[str, str] = {}
    if not heads_dir.is_dir():
        return out
    for ref_file in sorted(heads_dir.iterdir()):
        # A ref is a single regular file named after the branch; skip dirs and
        # any symlink (a planted symlink must never be followed).
        if not ref_file.is_file() or ref_file.is_symlink():
            continue
        name = ref_file.name
        try:
            validate_ref_name(name)
            sha = read_ref(root, name)
        except VersioningError:
            # Corrupt name or body -> skip this one ref; keep gathering the rest.
            continue
        if sha:
            out[name] = sha
    return out


def write_ref(root: Path, name: str, sha: str) -> None:
    """Write ``refs/heads/<name>`` = ``sha`` atomically (0o600), refusing symlinks.

    Validates both the name and the sha before any filesystem access. This is the
    unconditional write; use :func:`update_ref` when a lost-update check matters.
    """
    validate_ref_name(name)
    _validate_sha(sha)
    _atomic_write_text(_ref_path(root, name), f"{sha}\n")


def update_ref(root: Path, name: str, new_sha: str, *, expected: str | None) -> None:
    """Compare-and-swap ``refs/heads/<name>`` from ``expected`` to ``new_sha``.

    ``expected`` is the sha the ref is required to currently hold; ``None`` means
    the ref must NOT exist yet. If the re-read value does not match, raise
    :class:`VersioningError` (the lost-update guard -- the branch advanced
    concurrently). Otherwise write ``new_sha``.
    """
    validate_ref_name(name)
    _validate_sha(new_sha)
    if expected is not None:
        _validate_sha(expected)
    current = read_ref(root, name)
    if current != expected:
        raise VersioningError(f"branch '{name}' advanced concurrently; re-run")
    write_ref(root, name, new_sha)


# --------------------------------------------------------------------------- #
# Filesystem helpers (mirror objects.py)
# --------------------------------------------------------------------------- #
def _ensure_dir_no_symlink(directory: Path) -> None:
    """``mkdir -p`` ``directory`` but refuse if any existing ancestor is a symlink.

    Mirrors :func:`jp.paths._ensure_dir_no_symlink` so a planted symlinked
    ancestor cannot redirect a ref write out of the ``.jp`` tree.
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
            if Path(node).is_symlink():
                raise SafetyError(f"refusing to traverse a symlinked directory: {node}") from None


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory for rename durability.

    Opens with ``O_NOFOLLOW`` (where available) to refuse a planted symlink.
    Directory fsync is unsupported on some platforms (notably Windows) and on
    some filesystems -> any ``OSError`` is suppressed (durability is best-effort;
    correctness does not depend on it).
    """
    flags = getattr(os, "O_RDONLY", 0) | _O_NOFOLLOW
    with contextlib.suppress(OSError):
        fd = os.open(str(directory), flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

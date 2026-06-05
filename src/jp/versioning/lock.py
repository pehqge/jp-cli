"""A cross-platform, advisory, per-repo process lock for versioning writes.

Versioning mutations (commit, branch update, gc, ...) must not interleave: two
``jp commit`` processes racing on the same repo could lose a ref update or
corrupt the staging area. This module provides a single guard --
:func:`versioning_lock` -- that callers wrap around any such mutation so only one
process at a time can be inside a versioning critical section for a given repo.

Design
------
* EXCLUSIVE + NON-BLOCKING: if another process already holds the lock we raise
  IMMEDIATELY (:class:`VersioningError`) rather than blocking. A jp command
  should fail fast with a clear "another operation is in progress" message, not
  hang behind a long-running peer.
* ADVISORY OS LOCK (not a lockfile-existence check): we take a real OS lock on an
  open file descriptor -- ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
  Windows. The kernel releases the lock automatically when the fd is closed OR
  when the holding process EXITS (even on a crash / kill -9). That auto-release
  is the key robustness property: a crashed ``jp`` can never strand a stale lock
  that requires manual cleanup. We deliberately do NOT gate on the lock file's
  existence and do NOT delete it on release -- deletion would reintroduce the
  classic stale-lock and delete-the-file-someone-else-locked races.
* The lock file (``.jp/versioning.lock``) is created 0o600 (private on a shared
  box). Its contents are irrelevant; only the OS lock on its fd matters.

Cross-platform: ``fcntl`` (POSIX) and ``msvcrt`` (Windows) are imported under a
guard so the module imports on either platform; exactly one backend is active.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..paths import DOT_DIR
from .objects import VersioningError

# Pick the locking backend at import time. POSIX has fcntl; Windows has msvcrt.
# Guarding the import keeps the module importable on both platforms.
try:
    import fcntl as _fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - exercised only on Windows
    _fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False

try:
    import msvcrt as _msvcrt

    _HAVE_MSVCRT = True
except ImportError:
    _msvcrt = None  # type: ignore[assignment]
    _HAVE_MSVCRT = False

LOCK_NAME = "versioning.lock"

# O_NOFOLLOW exists on POSIX; absent on Windows -> fall back to 0 (mirrors the
# rest of the package). Combined with O_CREAT we still create-or-open the lock.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def lock_path(root: Path) -> Path:
    """Return the path of the per-repo versioning lock file."""
    return Path(root) / DOT_DIR / LOCK_NAME


def _try_lock(fd: int) -> None:
    """Take an EXCLUSIVE, NON-BLOCKING lock on ``fd``; raise if already held.

    Raises :class:`VersioningError` if another process holds the lock, mapping
    the platform-specific "would block" error to one clear message.
    """
    if _HAVE_FCNTL:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:  # BlockingIOError is a subclass of OSError
            raise VersioningError(
                "another jp versioning operation is in progress for this repo"
            ) from exc
    elif _HAVE_MSVCRT:  # pragma: no cover - exercised only on Windows
        try:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError as exc:
            raise VersioningError(
                "another jp versioning operation is in progress for this repo"
            ) from exc
    else:  # pragma: no cover - neither backend present
        raise VersioningError("no file-locking backend available on this platform")


def _unlock(fd: int) -> None:
    """Best-effort release of the lock on ``fd`` (the close also releases it)."""
    with contextlib.suppress(OSError):
        if _HAVE_FCNTL:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        elif _HAVE_MSVCRT:  # pragma: no cover - Windows only
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]


@contextmanager
def versioning_lock(root: Path) -> Iterator[None]:
    """Hold the exclusive per-repo versioning lock for the duration of the block.

    Acquires immediately or raises :class:`VersioningError` if another process is
    mid-operation. The lock is released on normal exit AND on exception (the OS
    also releases it if the process dies, so a crash never strands it).
    """
    path = lock_path(root)
    # The .jp dir should already exist for any real operation, but create it
    # defensively so the lock can be taken even on a freshly-made root.
    path.parent.mkdir(parents=True, exist_ok=True)

    # O_CREAT so the lock file is made if absent; 0o600 keeps it private. We hold
    # this fd for the whole context -- the OS lock lives on the fd, not the name.
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | _O_NOFOLLOW, 0o600)
    try:
        _try_lock(fd)
    except BaseException:
        os.close(fd)
        raise
    try:
        yield
    finally:
        _unlock(fd)
        os.close(fd)  # closing also releases the OS lock (belt and suspenders)

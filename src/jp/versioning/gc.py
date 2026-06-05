"""Prune UNREACHABLE loose objects from the local versioning store.

``jp gc`` reclaims space taken by objects that no commit, branch tip, HEAD, or the
staging area references any more (e.g. blobs written for a commit that was never
made, or orphaned by a ref move). It is the most space-sensitive AND the most
safety-sensitive maintenance op, so its contract is conservative by construction:

SAFETY CONTRACT (gc NEVER prunes reachable or staged data)
----------------------------------------------------------
* The reachable set is computed by :func:`jp.versioning.fsck.reachable_objects`
  with ``include_staged=True`` -- it includes HEAD, EVERY branch tip, the FULL
  parent walk of every tip, AND every staged-but-uncommitted blob. A candidate for
  deletion must be NOT in that set. Because the reachability walk is robust (a
  corrupt object encountered mid-walk is still counted as reachable), a damaged
  store can never trick gc into deleting a sibling that is still needed.
* GRACE WINDOW: a candidate must ALSO be older than ``grace_days`` (default 14).
  Reachability is a snapshot; a concurrent ``jp commit`` may have just written a
  blob/tree and not yet moved the ref when gc runs. The grace window means gc only
  ever deletes objects that have been orphaned for at least ``grace_days``, so an
  in-flight operation's brand-new objects are protected even though they are not
  yet referenced. ``gc`` also runs under :func:`jp.versioning.lock.versioning_lock`
  so it cannot interleave with a commit in the SAME process group.
* DEFAULT IS DRY-RUN: without ``--prune`` gc writes NOTHING -- it reports what it
  WOULD reclaim. ``--prune`` is required to actually delete.
* SYMLINK-SAFE deletion: a candidate is unlinked only after refusing to follow a
  symlink, and we never delete anything outside ``.jp/objects``.
* Remote gc is NOT done in v1: the remote ``__jp`` mirror is append-only and
  reclaiming it is a future feature; gc is purely LOCAL.

Cross-platform: standard library only; ``time.time()`` for the grace clock.
"""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .fsck import reachable_objects
from .lock import versioning_lock
from .objects import ObjectStore

# Default grace window, in days, before an unreachable object may be pruned.
DEFAULT_GRACE_DAYS = 14

_SECONDS_PER_DAY = 86400


@dataclass
class GcResult:
    """The outcome of a gc run (dry-run or pruning).

    ``candidates`` is the list of ``(sha, size)`` that are unreachable AND older
    than the grace window -- i.e. eligible to prune. ``pruned`` lists the shas
    actually deleted (empty in a dry run). ``reclaimed_bytes`` sums the sizes of
    the deleted objects (or, in a dry run, of the candidates that WOULD be deleted).
    ``pruned_flag`` records whether ``--prune`` was requested.
    """

    pruned_flag: bool = False
    grace_days: int = DEFAULT_GRACE_DAYS
    candidates: list[tuple[str, int]] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    reclaimed_bytes: int = 0

    @property
    def candidate_bytes(self) -> int:
        """Total bytes the candidates occupy (what a ``--prune`` WOULD reclaim)."""
        return sum(size for _, size in self.candidates)


def run_gc(root: Path, *, prune: bool, grace_days: int = DEFAULT_GRACE_DAYS) -> GcResult:
    """Compute (and optionally prune) unreachable, aged-out loose objects.

    Runs under the per-repo versioning lock so it cannot race a concurrent commit
    in the same process. Computes the reachable set with staged blobs INCLUDED,
    then walks every loose object: a candidate is one that is (a) NOT reachable and
    (b) older than ``grace_days``. With ``prune`` we delete the candidates with a
    symlink-safe unlink, tolerate one that vanished concurrently, and best-effort
    remove now-empty shard dirs. Without ``prune`` we touch nothing. NEVER deletes a
    reachable object; NEVER touches anything outside ``.jp/objects``.
    """
    if grace_days < 0:
        grace_days = 0
    root = Path(root).resolve()
    result = GcResult(pruned_flag=prune, grace_days=grace_days)

    with versioning_lock(root):
        store = ObjectStore(root)
        reachable = reachable_objects(root, store, include_staged=True)

        cutoff = time.time() - grace_days * _SECONDS_PER_DAY
        objects_root = store.objects_dir.resolve()

        for path in store.iter_objects():
            sha = path.parent.name + path.name
            if sha in reachable:
                continue  # NEVER prune a reachable object, regardless of age.
            try:
                st = path.stat()
            except OSError:
                continue  # vanished concurrently -- nothing to reclaim.
            if st.st_mtime > cutoff:
                continue  # within the grace window -- protect it.
            result.candidates.append((sha, st.st_size))

            if prune and _safe_unlink(path, objects_root):
                result.pruned.append(sha)
                result.reclaimed_bytes += st.st_size

        if prune:
            _remove_empty_shards(store, objects_root)
        else:
            # Dry run: report what WOULD be reclaimed.
            result.reclaimed_bytes = result.candidate_bytes

    return result


def _safe_unlink(path: Path, objects_root: Path) -> bool:
    """Symlink-safe unlink of a single object file; return True iff it was removed.

    Refuses to follow a symlink (an object file must be a regular file) and refuses
    to delete anything outside ``objects_root`` (defense against a path that somehow
    resolved out of the objects tree). A file that vanished concurrently is
    tolerated (returns False, no error). NEVER follows a symlink to delete its
    target.
    """
    p = Path(path)
    # Containment: the resolved parent must stay under .jp/objects. We resolve the
    # PARENT (not the file, which we must not follow if it is a symlink) and compare.
    try:
        parent_real = p.parent.resolve()
    except OSError:
        return False
    try:
        common = os.path.commonpath([str(objects_root), str(parent_real)])
    except ValueError:
        return False
    if common != str(objects_root):
        return False  # outside the objects tree -- refuse.

    # Refuse to unlink through a symlink: an object file is always a regular file;
    # a symlink in its place is hostile and must not be followed/removed as if it
    # were the object.
    if p.is_symlink():
        return False

    try:
        os.unlink(str(p))
        return True
    except FileNotFoundError:
        return False  # raced with another remover -- fine.
    except OSError:
        return False


def _remove_empty_shards(store: ObjectStore, objects_root: Path) -> None:
    """Best-effort removal of now-empty shard dirs under ``.jp/objects``.

    Only removes a directory that is a real (non-symlinked) directory, lives
    directly under ``objects_root``, and is empty. Any failure is suppressed --
    leaving an empty shard dir is harmless, so cleanup is strictly best-effort and
    never blocks or fails the gc run.
    """
    base = store.objects_dir
    if not base.is_dir():
        return
    for shard in list(base.iterdir()):
        if not shard.is_dir() or shard.is_symlink():
            continue
        # Containment guard (mirror _safe_unlink): the shard must be under objects.
        try:
            if shard.resolve().parent != objects_root:
                continue
        except OSError:
            continue
        with contextlib.suppress(OSError):
            # rmdir only succeeds on an empty dir, so this never deletes objects.
            os.rmdir(str(shard))

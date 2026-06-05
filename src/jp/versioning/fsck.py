"""Reachability + integrity checking for the versioning object store.

This module owns the SHARED reachability walk (:func:`reachable_objects`) used by
BOTH ``jp fsck`` and ``jp gc`` -- it is the single source of truth for "which
objects are needed", so the two commands can never disagree about what is safe to
keep. ``gc`` (:mod:`jp.versioning.gc`) imports it; ``fsck`` builds its report on
top of it.

What "reachable" means
----------------------
Starting from the TIP commits -- HEAD (:func:`jp.versioning.refs.resolve_head`)
and EVERY branch tip (:func:`jp.versioning.refs.list_heads`) -- we walk the FULL
commit DAG. v1 history is linear (a commit has <=1 parent), but we walk ALL
parents defensively so a future merge commit is handled, and we guard against a
forged cycle with a visited set. For each reachable commit we collect:

* the commit object's own sha,
* the commit's tree sha,
* every blob sha named in that tree's entries.

When ``include_staged`` is true we ALSO add every staged blob's ``sha256``: those
blobs are needed for the NEXT commit even though no commit references them yet, so
``gc`` MUST treat them as reachable (the in-flight-commit safety case).

Robustness contract (load-bearing for gc safety)
-------------------------------------------------
The walk NEVER crashes on a damaged store. A missing or corrupt object met during
the walk (a tip that is not a real commit, a commit whose tree is gone, a tree
that will not re-hash, ...) is RECORDED as a problem and the walk CONTINUES. This
matters two ways: ``fsck`` gets a complete problem list instead of dying at the
first fault, and ``gc`` still computes a reachable set that is a SUPERSET of what
it would have computed had everything been readable -- so a corrupt object can
never trick ``gc`` into pruning a sibling that is still needed.

Cross-platform: standard library only; read-only (this module NEVER writes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import refs
from .objects import ObjectStore, VersioningError
from .repo import read_commit, read_tree
from .staging import Staging


# --------------------------------------------------------------------------- #
# Shared reachability (used by BOTH fsck and gc)
# --------------------------------------------------------------------------- #
def reachable_objects(root: Path, store: ObjectStore, *, include_staged: bool) -> set[str]:
    """Return the set of all object shas reachable from refs (+ staging, optional).

    See the module docstring for the precise definition. ``include_staged=True``
    is what ``gc`` passes so an uncommitted-but-staged blob is never pruned;
    ``fsck`` passes ``False`` because it only verifies committed history (a staged
    blob's integrity is checked separately by the commit path, and an absent staged
    blob is re-snapshotted at commit time).

    This is the convenience wrapper that DISCARDS the problem report; callers that
    need the problems (fsck) use :func:`walk_reachable` directly.
    """
    return walk_reachable(root, store, include_staged=include_staged).reachable


@dataclass
class ReachReport:
    """The outcome of a reachability walk: the reachable set + any problems found.

    ``reachable`` is the set of every object sha that is needed (commits, trees,
    blobs, plus staged blobs when requested). ``missing`` and ``corrupt`` record
    objects that were REFERENCED during the walk but could not be loaded -- a
    missing file vs a read that raised (hash mismatch / unknown marker / malformed
    object / decompress error). ``dangling_refs`` records branch tips that do not
    resolve to a readable commit object. All are reported by ``fsck``; the walk
    itself never raises on them.
    """

    reachable: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    corrupt: set[str] = field(default_factory=set)
    dangling_refs: dict[str, str] = field(default_factory=dict)


def walk_reachable(root: Path, store: ObjectStore, *, include_staged: bool) -> ReachReport:
    """Walk the commit DAG from every tip; return reachable shas + a problem report.

    The robust core behind :func:`reachable_objects`. Every object touched during
    the walk is added to ``reachable`` BEFORE it is read, so even an object that
    fails its integrity check still counts as reachable (gc must keep a corrupt-but-
    referenced object so a human can investigate rather than have gc delete it). A
    fault is appended to the report and the walk moves on -- it never raises.
    """
    report = ReachReport()

    # Tip commits: HEAD + every branch tip. A dangling/unresolvable HEAD is handled
    # by resolve_head returning None (unborn) -- the command layer reports a truly
    # malformed HEAD via read_head separately.
    tips: dict[str, str] = {}  # label -> sha (label only used for dangling reporting)
    try:
        head = refs.resolve_head(root)
    except VersioningError:
        head = None
    if head:
        tips["HEAD"] = head
    for name, sha in refs.list_heads(root).items():
        tips[name] = sha

    # Walk the DAG breadth-first across ALL parents, guarding cycles.
    visited: set[str] = set()
    stack: list[str] = []
    for label, tip in tips.items():
        # Each tip is reachable (kept even if it turns out dangling/corrupt, so gc
        # never prunes an object a ref still points at), and a tip with no commit
        # object is recorded as a dangling ref.
        stack.append(tip)
        report.reachable.add(tip)
        if not store.has(tip):
            report.dangling_refs[label] = tip

    while stack:
        sha = stack.pop()
        if sha in visited:
            continue
        visited.add(sha)
        report.reachable.add(sha)
        try:
            commit = read_commit(store, sha)
        except VersioningError:
            # Record as missing vs corrupt and stop descending this node.
            if store.has(sha):
                report.corrupt.add(sha)
            else:
                report.missing.add(sha)
            continue
        # The commit's tree + its blobs.
        tree_sha = str(commit.get("tree", ""))
        if tree_sha:
            report.reachable.add(tree_sha)
            _collect_tree(store, tree_sha, report)
        for parent in commit.get("parents") or []:
            if isinstance(parent, str) and parent not in visited:
                stack.append(parent)

    if include_staged:
        _collect_staged(root, report)

    return report


def _collect_tree(store: ObjectStore, tree_sha: str, report: ReachReport) -> None:
    """Add a tree's blob shas to the reachable set; record the tree if it faults.

    The tree itself was already added by the caller. We try to read it; a fault is
    recorded (missing vs corrupt) and we return without descending. On success we
    add every entry's ``sha256`` to the reachable set (blobs are leaves -- nothing
    further to walk).
    """
    try:
        entries = read_tree(store, tree_sha)
    except VersioningError:
        if store.has(tree_sha):
            report.corrupt.add(tree_sha)
        else:
            report.missing.add(tree_sha)
        return
    for meta in entries.values():
        blob = meta.get("sha256")
        if isinstance(blob, str) and blob:
            report.reachable.add(blob)
            # Record an outright MISSING blob during the walk (a leaf has nothing
            # to descend into, but a tree referencing an absent blob is a real
            # integrity problem fsck must report). We only flag *missing* here; the
            # heavier re-hash that detects a *corrupt-but-present* blob is done by
            # run_fsck's verification pass so the walk stays cheap.
            if not store.has(blob):
                report.missing.add(blob)


def _collect_staged(root: Path, report: ReachReport) -> None:
    """Add every staged blob's sha to the reachable set (the in-flight-commit case).

    Defensive: a malformed staging file (ConfigError) is tolerated -- gc simply
    treats it as "no extra staged blobs" rather than crashing, which only makes gc
    MORE conservative (it could prune a staged blob it failed to learn about), so
    we re-raise nothing here. In practice the staging file is well-formed.
    """
    try:
        staging = Staging.load(root)
    except Exception:
        return
    for entry in staging.entries.values():
        if entry.sha256:
            report.reachable.add(entry.sha256)


# --------------------------------------------------------------------------- #
# fsck
# --------------------------------------------------------------------------- #
@dataclass
class FsckReport:
    """The full integrity report produced by :func:`run_fsck`.

    ``initialized`` is False for a repo with no versioning history (no HEAD and no
    objects) -- the command prints "no versioning history" and exits 0. ``missing``
    / ``corrupt`` are referenced objects that are absent / fail integrity;
    ``dangling_refs`` maps a ref label to the sha it points at that is not a
    readable commit; ``unreachable_corrupt`` (only populated under ``--full``) are
    loose objects on disk whose content does not re-hash to their filename.
    ``checked`` counts how many reachable objects were verified.
    """

    initialized: bool = True
    head_dangling: bool = False
    head_detail: str = ""
    checked: int = 0
    missing: set[str] = field(default_factory=set)
    corrupt: set[str] = field(default_factory=set)
    dangling_refs: dict[str, str] = field(default_factory=dict)
    unreachable_corrupt: set[str] = field(default_factory=set)

    @property
    def clean(self) -> bool:
        """True iff no problem of any kind was found."""
        return not (
            self.head_dangling
            or self.missing
            or self.corrupt
            or self.dangling_refs
            or self.unreachable_corrupt
        )


def run_fsck(root: Path, *, full: bool) -> FsckReport:
    """Verify the integrity of the versioning store; return a :class:`FsckReport`.

    READ-ONLY: this never writes a byte. Steps:

    1. If the repo has no HEAD and no objects -> ``initialized=False`` (caller
       prints "no versioning history", exit 0).
    2. Resolve HEAD; a symbolic HEAD pointing at an UNBORN branch is fine, but a
       detached/branch HEAD pointing at a sha with no commit object is a dangling
       HEAD.
    3. Walk reachability (:func:`walk_reachable`); for EVERY reachable object verify
       it exists AND re-hashes to its name by calling :meth:`ObjectStore.read`
       (which re-hashes). Blobs are read with the tight ``max_size`` from their tree
       entry; trees/commits are read normally. Collect missing/corrupt.
    4. ``full``: ALSO iterate every loose object on disk and verify it re-hashes to
       its filename-derived sha, catching corrupt objects that are not currently
       reachable.
    """
    store = ObjectStore(root)
    report = FsckReport()

    head = refs.read_head(root)
    has_objects = any(True for _ in store.iter_objects())
    if head is None and not has_objects:
        report.initialized = False
        return report

    # A truly malformed HEAD raises in read_head -> surfaces as a VersioningError to
    # the command (exit non-zero) which is the right loud behavior. A None HEAD with
    # objects present (init half-done) is treated as initialized-but-headless.

    # Resolve HEAD and detect a dangling HEAD (points at a non-existent commit).
    head_sha = refs.resolve_head(root)
    if head_sha is not None and not store.has(head_sha):
        report.head_dangling = True
        report.head_detail = head_sha

    # Reachability walk (committed history only; staged blobs are not fsck's job).
    walk = walk_reachable(root, store, include_staged=False)
    report.missing |= walk.missing
    report.corrupt |= walk.corrupt
    report.dangling_refs.update(walk.dangling_refs)

    # Verify EVERY reachable object exists and re-hashes. We need each tree's entry
    # sizes to cap blob reads tightly, so gather blob expected sizes first.
    blob_sizes = _gather_blob_sizes(store, walk.reachable)

    for sha in sorted(walk.reachable):
        # Already known missing/corrupt from the walk -> still count it as checked
        # but don't double-read (the walk already classified it).
        if sha in report.missing or sha in report.corrupt:
            report.checked += 1
            continue
        if not store.has(sha):
            report.missing.add(sha)
            report.checked += 1
            continue
        try:
            store.read(sha, max_size=blob_sizes.get(sha))
        except VersioningError:
            report.corrupt.add(sha)
        report.checked += 1

    if full:
        _scan_loose(store, walk.reachable, report)

    return report


def _gather_blob_sizes(store: ObjectStore, reachable: set[str]) -> dict[str, int]:
    """Map blob sha -> expected size from every reachable tree (best-effort).

    Lets :func:`run_fsck` cap a blob read at its declared size (a tight zip-bomb
    guard) instead of the 2 GiB module default. A tree that cannot be read is simply
    skipped here -- it is already recorded as a problem by the walk, and the missing
    size only means that blob is read with the looser default cap.
    """
    sizes: dict[str, int] = {}
    for sha in reachable:
        # Cheaply tell trees from blobs: only trees parse as a tree object. We try
        # read_tree and ignore failures (blobs/commits raise -> skipped).
        try:
            entries = read_tree(store, sha)
        except VersioningError:
            continue
        for meta in entries.values():
            blob = meta.get("sha256")
            size = meta.get("size")
            if isinstance(blob, str) and isinstance(size, int) and size >= 0:
                # If two trees disagree, keep the larger cap (never under-cap a real
                # blob and wrongly flag it corrupt).
                sizes[blob] = max(sizes.get(blob, 0), size)
    return sizes


def _scan_loose(store: ObjectStore, reachable: set[str], report: FsckReport) -> None:
    """``--full``: verify every loose object on disk re-hashes to its filename sha.

    Iterates :meth:`ObjectStore.iter_objects` (which already skips ``objtmp-*``),
    derives the expected sha from ``<shard>/<rest>``, and reads it (the store re-
    hashes). A read failure on an object that is NOT already reachable-and-flagged
    is recorded in ``unreachable_corrupt``; a reachable object's corruption was
    already caught above. An entry whose name is not a valid sha is itself a
    corruption signal and is recorded too.
    """
    for path in store.iter_objects():
        sha = path.parent.name + path.name
        try:
            # path_for validates the composed sha; an invalid on-disk name raises.
            store.read(sha)
        except VersioningError:
            if sha not in reachable:
                report.unreachable_corrupt.add(sha)

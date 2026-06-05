"""``jp checkout`` planning + apply -- the most DESTRUCTIVE versioning path.

This module restores files from a committed tree into the WORKING TREE, and (in
full mode) moves HEAD. It overwrites and can delete a user's working files, runs
on a SHARED academic Jupyter box holding irreplaceable research, and must be
cross-platform. The whole design is therefore paranoid; the load-bearing safety
properties are:

PLAN-THEN-APPLY (no half-done checkout)
    The full plan is computed FIRST -- every target path is classified against
    the working tree and HEAD using the truth table below -- and if ANY file is
    BLOCKED (a clean refusal: uncommitted local edits without ``--force``) the
    whole operation ABORTS before a single byte is written. We never clobber work
    and then bail; either the plan is clean (or ``--force``) or nothing is touched.

THE PER-FILE TRUTH TABLE (matches git's safety contract)
    For each target path let ``W`` = the working file's content sha (None if the
    file is absent), ``H`` = the sha in HEAD's tree (None if untracked), and
    ``T`` = the sha in the target tree. For a NOTEBOOK, ``W``/``H``/``T`` are the
    *normalized* shas (a pure re-run is NOT "dirty"); the BYTES WRITTEN are always
    the target blob's faithful original.

      W is None                      -> WRITE   (additive restore of a missing file)
      W == T                         -> SKIP    (already at the target content)
      W != T and W == H              -> WRITE   (clean: no uncommitted change)
      W != T, W != H, H is not None  -> DIRTY   (uncommitted edits) -> BLOCK / --force
      W != T, W != H, H is None      -> DIRTY   (untracked file collides) -> BLOCK / --force

WRITE SAFETY
    Every target key is re-sanitized with :func:`jp.paths.safe_local_dest` before
    writing (anti zip-slip defense-in-depth even though keys were normalized at
    commit). The blob is read via :meth:`ObjectStore.read` (integrity-verified +
    decompression-capped to the tree-entry size), then written with
    :func:`jp.paths.atomic_write`, which REFUSES to write through a symlink. A
    symlink at (or above) a destination raises :class:`jp.errors.SafetyError`,
    which we catch PER FILE -- that one path is recorded as failed and skipped, the
    rest of the checkout still proceeds (per-file resilience), and we never write
    through the symlink. A corrupt object likewise fails just that path.

EXTRAS (full mode only -- files present locally but not in the target tree)
    Default: left untouched and REPORTED. With ``--remove-extra``: a file that IS
    in HEAD (tracked, deleted in the target) is removed (recoverable from history);
    a file that is NOT in HEAD (never committed / untracked work) requires explicit
    CONFIRMATION -- in a tty we ask, defaulting to NO; in a non-tty we REFUSE
    (:class:`SafetyError`) rather than ever delete untracked work silently. The
    untracked-extra confirmation is resolved UP FRONT, before any write, so a
    refusal aborts with ZERO side effects (same contract as a BLOCKED file).
    Deletions go through a symlink-safe unlink that never follows a symlink.

ORDER / ATOMICITY
    The whole operation runs under :func:`jp.versioning.lock.versioning_lock`.
    The order is: PLAN -> GATE (blocked-check AND untracked-extra confirmation) ->
    WRITE files -> DELETE extras -> MOVE HEAD + sync staging. HEAD moves LAST, so a
    crash leaves the new files with the OLD HEAD (recoverable) rather than a HEAD
    pointing at a tree that was never written. If ANY per-file write/delete FAILS
    (a symlink conflict, a corrupt object), HEAD is NOT moved and staging is NOT
    rewritten -- the checkout is reported INCOMPLETE (working tree partially
    updated; HEAD left at the previous commit) and the command exits PARTIAL, so
    HEAD/staging never lie about a tree that was only half-materialized. After a
    fully-successful full checkout we rewrite the staging area to match the
    checked-out tree (so ``jp status`` is coherent) but NEVER touch
    ``.jp/index.json`` (the sync base is independent of versioning).

Cross-platform: standard library only; all os-level symlink checks are guarded;
:func:`atomic_write`/:func:`safe_local_dest` already handle Windows.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .. import paths
from ..errors import SafetyError
from ..ignore import IgnoreSet
from ..paths import safe_local_dest
from ..sync import scan_local, sha256_file
from . import refs
from .lock import versioning_lock
from .notebooks import is_notebook, normalized_sha
from .objects import ObjectStore, VersioningError
from .repo import read_commit, read_tree, resolve_commitish
from .staging import StagedEntry, Staging


class Action(str, Enum):
    """The decided fate of one target path under the truth table."""

    WRITE = "write"  # restore the target blob (absent / clean / forced)
    SKIP = "skip"  # working file already equals the target
    BLOCKED = "blocked"  # dirty (uncommitted edits) and no --force -> aborts


@dataclass
class FilePlan:
    """One planned target-path operation produced by :func:`plan_checkout`."""

    rel: str
    action: Action
    sha: str  # the target blob sha (the bytes to write, for WRITE)
    size: int  # the target tree-entry size (the read cap for the blob)
    reason: str = ""  # human note (e.g. why BLOCKED)


@dataclass
class ExtraPlan:
    """A working-tree file NOT present in the target tree (full mode only)."""

    rel: str
    tracked: bool  # True iff it exists in HEAD's tree (recoverable via history)


@dataclass
class CheckoutPlan:
    """The full, computed-before-any-write plan for a checkout."""

    commit: str
    short: str
    path_scoped: bool
    writes: list[FilePlan] = field(default_factory=list)
    skips: list[FilePlan] = field(default_factory=list)
    blocked: list[FilePlan] = field(default_factory=list)
    extras: list[ExtraPlan] = field(default_factory=list)
    # Whether HEAD would attach to a branch (the resolved name) or detach (None).
    attach_branch: str | None = None
    detach_to: str | None = None

    @property
    def has_blocked(self) -> bool:
        return bool(self.blocked)


@dataclass
class CheckoutResult:
    """The outcome of applying a checkout (or the dry-run preview)."""

    plan: CheckoutPlan
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    extras_kept: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (rel, message)
    head_moved: bool = False
    dry_run: bool = False
    # True iff a real full checkout ended with per-file failures: HEAD was NOT
    # moved and staging was NOT rewritten, so the command must report INCOMPLETE.
    incomplete: bool = False
    # Dry-run extras preview, split so ``-n`` predicts real behavior: tracked
    # extras are deleted unconditionally, untracked ones only after confirmation
    # (and are REFUSED in a non-tty).
    would_delete_tracked: list[str] = field(default_factory=list)
    would_delete_untracked: list[str] = field(default_factory=list)


# Type of the untracked-extra deletion confirmer the command layer supplies.
ConfirmDeleteUntracked = Callable[[list[str]], bool]


# --------------------------------------------------------------------------- #
# Per-file classification (the truth table)
# --------------------------------------------------------------------------- #
def _entry_norm_sha(entry: dict, store: ObjectStore) -> str | None:
    """The NORMALIZED sha for a notebook tree entry, or None if it is opaque.

    Prefers the recorded ``nb.norm_sha`` (written at commit time). When it is
    absent (a v3 / legacy-committed ``.ipynb`` stored as a plain blob) we derive it
    by reading the target blob and normalizing it on the fly, so the target side of
    a comparison is symmetric with the working side. Returns None when the blob is
    genuinely not a normalizable notebook (truly opaque) or cannot be read.
    """
    nb = entry.get("nb")
    if isinstance(nb, dict):
        norm = nb.get("norm_sha")
        if isinstance(norm, str) and norm:
            return norm
    sha = str(entry.get("sha256", ""))
    if not sha:
        return None
    try:
        data = store.read(sha, max_size=int(entry.get("size", 0)) or None)
    except (VersioningError, OSError):
        return None
    return normalized_sha(data)


def _entry_raw_sha(entry: dict) -> str:
    """The RAW blob sha of a tree entry (used for opaque, non-normalizable files)."""
    return str(entry.get("sha256", ""))


def _working_norm_sha(abspath: Path) -> str | None:
    """The NORMALIZED sha of a working notebook, or None if it isn't one / unreadable."""
    try:
        data = abspath.read_bytes()
    except OSError:
        return None
    return normalized_sha(data)


def _working_raw_sha(abspath: Path) -> str | None:
    """The RAW content sha of a working file, or None if it cannot be read."""
    try:
        return sha256_file(abspath)
    except OSError:
        return None


def _present(abspath: Path) -> bool:
    """True iff a regular (non-symlink) file exists at ``abspath``.

    A symlink is treated as ABSENT: we never follow it and the writer refuses it,
    so for comparison purposes a symlinked path has no working content.
    """
    return abspath.is_file() and not abspath.is_symlink()


def classify(
    rel: str,
    entry: dict,
    head_entries: dict,
    root: Path,
    store: ObjectStore,
    *,
    force: bool,
) -> FilePlan:
    """Classify one target path against the working tree + HEAD (the truth table).

    Returns a :class:`FilePlan` whose ``action`` is WRITE, SKIP, or BLOCKED. The
    blob to write is ALWAYS the target's faithful original (``entry['sha256']`` /
    ``entry['size']``); only the *comparison* uses normalized shas for notebooks.
    ``force`` downgrades a would-be BLOCKED (dirty) result to WRITE.

    NOTEBOOK SYMMETRY: for a ``.ipynb`` path we compare the working file, HEAD, and
    the target ALL the same way. If the target is a normalizable notebook (recorded
    or derived ``norm_sha``) we compare NORMALIZED shas on every side, so a pure
    re-run (same code, new outputs) reads as identical and SKIPs -- even when the
    target entry lacks an ``nb`` sub-key. Only when the target is genuinely opaque
    (normalization yields None, e.g. an nbformat v3 file) do we compare RAW shas on
    every side. We never compare normalized-on-one-side vs raw-on-the-other (which
    would false-flag identical content as dirty). When in genuine doubt (a value we
    cannot compute) the comparison falls through to the safe BLOCK, never overwrite.
    """
    blob_sha = str(entry.get("sha256", ""))
    size = int(entry.get("size", 0))
    fp = FilePlan(rel=rel, action=Action.WRITE, sha=blob_sha, size=size)

    abspath = root / rel
    h_entry = head_entries.get(rel)
    h_entry = h_entry if isinstance(h_entry, dict) else None

    # Absent locally -> additive restore (no comparison needed).
    if not _present(abspath):
        fp.action = Action.WRITE
        return fp

    w, t, h = _compare_triplet(rel, abspath, entry, h_entry, store)

    if w == t:
        fp.action = Action.SKIP
        return fp
    if h is not None and w == h:
        # Clean: the working file matches HEAD (no uncommitted change) -> overwrite.
        fp.action = Action.WRITE
        return fp
    # Otherwise the working file differs from BOTH the target and HEAD (or is an
    # untracked file colliding with a target path) -> uncommitted local work.
    if force:
        fp.action = Action.WRITE
        return fp
    fp.action = Action.BLOCKED
    fp.reason = "untracked local file" if h is None else "uncommitted local changes"
    return fp


def _compare_triplet(
    rel: str,
    abspath: Path,
    entry: dict,
    h_entry: dict | None,
    store: ObjectStore,
) -> tuple[str | None, str, str | None]:
    """Return symmetric comparison shas ``(W, T, H)`` for one path.

    ``W`` is the working file's value (the path is known to exist when this is
    called), ``T`` the target's, ``H`` HEAD's (None when untracked). For a notebook
    we pick a SINGLE comparison mode from the TARGET -- normalized if the target is
    a normalizable notebook, else raw -- and apply it to all three sides. A side
    whose value cannot be computed in the chosen mode yields a sentinel that can
    never equal the others (so the path stays dirty/BLOCKED rather than wrongly
    SKIP/overwrite). Non-notebooks always compare by raw sha.
    """
    if not is_notebook(rel):
        return (
            _working_raw_sha(abspath),
            _entry_raw_sha(entry),
            _entry_raw_sha(h_entry) if h_entry is not None else None,
        )

    t_norm = _entry_norm_sha(entry, store)
    if t_norm is not None:
        # Normalized mode on EVERY side (symmetric). A working/HEAD value that is
        # not a normalizable notebook becomes a never-matching sentinel.
        w = _working_norm_sha(abspath) or _UNCOMPARABLE
        h = None
        if h_entry is not None:
            h = _entry_norm_sha(h_entry, store) or _UNCOMPARABLE
        return w, t_norm, h
    # Target is opaque (e.g. nbformat v3) -> raw mode on every side.
    return (
        _working_raw_sha(abspath),
        _entry_raw_sha(entry),
        _entry_raw_sha(h_entry) if h_entry is not None else None,
    )


# A sentinel that is not a valid 64-hex sha, so it can never equal any real
# comparison value -- used to keep an uncomparable notebook side "different" and
# therefore on the safe (BLOCK, not overwrite) branch.
_UNCOMPARABLE = "?uncomparable?"


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def _head_entries(root: Path, store: ObjectStore) -> dict:
    """Return HEAD's tree entries, or ``{}`` for an unborn HEAD (no commits)."""
    head_sha = refs.resolve_head(root)
    if head_sha is None:
        return {}
    commit = read_commit(store, head_sha)
    return read_tree(store, commit["tree"])


def _resolve_target(root: Path, store: ObjectStore, commitish: str) -> tuple[str, dict, str | None]:
    """Resolve ``commitish`` to ``(commit_sha, target_tree_entries, branch_or_None)``.

    The branch name is returned only when the user named a BRANCH (or ``HEAD``
    while HEAD is symbolic): full checkout then ATTACHES HEAD to that branch.
    Naming a raw sha / prefix / detached ``HEAD`` returns None -> full checkout
    DETACHES to the sha. The resolution itself is read-only.
    """
    commit_sha = resolve_commitish(root, store, commitish)
    commit = read_commit(store, commit_sha)
    target = read_tree(store, commit["tree"])

    branch: str | None = None
    ref = (commitish or "").strip()
    if ref == "HEAD":
        branch = refs.current_branch(root)  # None when HEAD is detached
    else:
        try:
            refs.validate_ref_name(ref)
            valid_name = True
        except VersioningError:
            valid_name = False
        if valid_name and refs.read_ref(root, ref) is not None:
            branch = ref
    return commit_sha, target, branch


def plan_checkout(
    root: Path,
    store: ObjectStore,
    commitish: str,
    paths_arg: list[str] | None,
    *,
    force: bool,
) -> CheckoutPlan:
    """Compute the FULL checkout plan WITHOUT writing anything (read-only).

    Path-scoped mode (``paths_arg`` non-empty) restores only the named paths and
    never computes extras or a HEAD move; each named path must exist in the target
    tree (else a per-path :class:`VersioningError`). Full mode classifies the whole
    target tree, computes extras (working files absent from the target), and decides
    whether HEAD would attach to a branch or detach to the sha.
    """
    root = Path(root).resolve()
    commit_sha, target, branch = _resolve_target(root, store, commitish)
    head_entries = _head_entries(root, store)

    plan = CheckoutPlan(commit=commit_sha, short=commit_sha[:12], path_scoped=bool(paths_arg))

    if paths_arg:
        # PATH-SCOPED: restore only the requested paths; do not move HEAD or touch
        # extras. A named path missing from the target tree is a hard error.
        wanted: list[str] = []
        for raw in paths_arg:
            try:
                rel = paths.normalize_rel(raw)
            except Exception as exc:
                raise VersioningError(f"invalid path {raw!r}: {exc}") from exc
            if rel not in target:
                raise VersioningError(f"path {rel!r} does not exist in commit {commit_sha[:12]}")
            wanted.append(rel)
        for rel in sorted(set(wanted)):
            _bucket(plan, classify(rel, target[rel], head_entries, root, store, force=force))
        return plan

    # FULL: classify every target path, then compute extras + HEAD move.
    for rel in sorted(target):
        _bucket(plan, classify(rel, target[rel], head_entries, root, store, force=force))

    ignore = IgnoreSet.from_root(root)
    working = scan_local(root, ignore)  # {rel: abspath}; skips .jp/ + symlinks
    for rel in sorted(working):
        if rel in target:
            continue
        plan.extras.append(ExtraPlan(rel=rel, tracked=rel in head_entries))

    if branch is not None:
        plan.attach_branch = branch
    else:
        plan.detach_to = commit_sha
    return plan


def _bucket(plan: CheckoutPlan, fp: FilePlan) -> None:
    """Drop a classified :class:`FilePlan` into the right plan bucket."""
    if fp.action is Action.WRITE:
        plan.writes.append(fp)
    elif fp.action is Action.SKIP:
        plan.skips.append(fp)
    else:
        plan.blocked.append(fp)


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def apply_checkout(
    root: Path,
    cfg: object,
    commitish: str,
    paths_arg: list[str] | None,
    *,
    force: bool,
    remove_extra: bool,
    dry_run: bool,
    confirm_delete_untracked: ConfirmDeleteUntracked,
) -> CheckoutResult:
    """Plan and (unless ``dry_run``) apply a checkout under the versioning lock.

    ``confirm_delete_untracked(rels)`` is a callable the command layer supplies; it
    is invoked ONLY in full mode with ``--remove-extra`` when there are UNTRACKED
    extras to delete, and must return True to proceed. In a non-tty it must refuse
    (the command passes a callable that raises :class:`SafetyError`), so untracked
    work is never deleted silently.

    Ordering (the crash-safety contract), all under the lock:

    1. PLAN everything (read-only classification + extras + HEAD-move decision).
    2. GATE before any write: if any file is BLOCKED (dirty without ``--force``)
       abort with :class:`SafetyError`; and resolve the untracked-extra
       confirmation NOW -- a refusal (or a non-tty :class:`SafetyError`) aborts
       here with ZERO side effects, exactly like a BLOCKED file.
    3. WRITE the target blobs (per-file symlink/integrity failures are recorded,
       not fatal). 4. DELETE the confirmed extras (per-file failures recorded).
    5. MOVE HEAD + sync staging -- ONLY if every write/delete succeeded. If any
       per-file failure occurred the working tree is partially updated, so HEAD is
       left at the previous commit and staging is left untouched (``incomplete``),
       and the command reports the partial run rather than letting HEAD/staging lie.

    ``dry_run`` computes the plan and returns it with nothing written.
    """
    root = Path(root).resolve()
    with versioning_lock(root):
        refs.check_format(root)
        store = ObjectStore(root)
        plan = plan_checkout(root, store, commitish, paths_arg, force=force)

        result = CheckoutResult(plan=plan, dry_run=dry_run)

        # FAIL SAFE: any blocked (dirty) file aborts before a single write. This
        # holds for both dry-run (so the preview ends on the abort) and real runs.
        if plan.has_blocked:
            names = ", ".join(fp.rel for fp in plan.blocked)
            raise SafetyError(
                "refusing to overwrite uncommitted local changes in: "
                f"{names}. Commit them, or pass --force to discard them."
            )

        if dry_run:
            # Preview only: report what WOULD happen; touch nothing and NEVER prompt
            # (a dry run must not call the confirm callable). The untracked-extra
            # split lets ``-n`` predict that those need confirmation / are refused
            # in a non-tty.
            result.written = [fp.rel for fp in plan.writes]
            result.skipped = [fp.rel for fp in plan.skips]
            if not plan.path_scoped:
                _preview_extras(plan, result, remove_extra)
            return result

        # Decide which extras to delete BEFORE touching anything (full mode only).
        # This resolves the untracked-extra confirmation up front, so a refusal or a
        # non-tty SafetyError aborts with ZERO writes (consistent with BLOCKED).
        delete_rels: list[str] = []
        if not plan.path_scoped:
            delete_rels = _gate_extras(plan, result, remove_extra, confirm_delete_untracked)

        # --- WRITE the target blobs FIRST (HEAD moves last) ---
        for fp in plan.writes:
            ok, err = _write_one(root, store, fp)
            if ok:
                result.written.append(fp.rel)
            else:
                result.failures.append((fp.rel, err))
        result.skipped = [fp.rel for fp in plan.skips]

        # --- DELETE the confirmed extras (full mode only) ---
        for rel in delete_rels:
            ok, err = _delete_one(root, rel)
            if ok:
                result.deleted.append(rel)
            else:
                result.failures.append((rel, err))

        # --- MOVE HEAD LAST + sync staging (full mode only, ALL succeeded) ---
        if not plan.path_scoped:
            if result.failures:
                # Partial run: the working tree is half-updated. Do NOT move HEAD or
                # rewrite staging -- leaving them at the previous commit keeps them
                # honest (they never claim a tree that was only partially written).
                result.incomplete = True
            else:
                if plan.attach_branch is not None:
                    refs.set_head_branch(root, plan.attach_branch)
                else:
                    assert plan.detach_to is not None
                    refs.set_head_detached(root, plan.detach_to)
                result.head_moved = True
                _sync_staging_to_commit(root, store, plan.commit)

        return result


def _preview_extras(plan: CheckoutPlan, result: CheckoutResult, remove_extra: bool) -> None:
    """Compute the dry-run extras preview WITHOUT prompting (never calls confirm).

    Without ``--remove-extra`` every extra is reported as kept. With it, the would-
    delete set is SPLIT: tracked extras (recoverable from history) would be deleted
    unconditionally; untracked extras would need confirmation and are refused in a
    non-tty -- so ``-n`` predicts the real behavior instead of lumping them together.
    """
    if not remove_extra:
        result.extras_kept = [ex.rel for ex in plan.extras]
        return
    result.would_delete_tracked = sorted(ex.rel for ex in plan.extras if ex.tracked)
    result.would_delete_untracked = sorted(ex.rel for ex in plan.extras if not ex.tracked)


def _gate_extras(
    plan: CheckoutPlan,
    result: CheckoutResult,
    remove_extra: bool,
    confirm_delete_untracked: ConfirmDeleteUntracked,
) -> list[str]:
    """Resolve which extras to delete, BEFORE any write (the up-front extras gate).

    Without ``--remove-extra`` every extra is KEPT and reported, and nothing is
    deleted. With it: tracked extras (in HEAD -> recoverable from history) are
    deleted unconditionally; untracked extras are deleted ONLY if
    ``confirm_delete_untracked`` returns True. A non-tty caller's confirm raises
    :class:`SafetyError` here -- before any write -- so a refusal leaves ZERO side
    effects. Returns the sorted list of paths to delete; records the kept extras on
    ``result``.
    """
    if not remove_extra:
        result.extras_kept = [ex.rel for ex in plan.extras]
        return []

    tracked = sorted(ex.rel for ex in plan.extras if ex.tracked)
    untracked = sorted(ex.rel for ex in plan.extras if not ex.tracked)

    delete_set = list(tracked)
    if untracked:
        # May raise SafetyError in a non-tty (the command's callable refuses there),
        # aborting the whole checkout before a single byte is written.
        if confirm_delete_untracked(untracked):
            delete_set.extend(untracked)
        else:
            result.extras_kept.extend(untracked)

    return sorted(delete_set)


def _write_one(root: Path, store: ObjectStore, fp: FilePlan) -> tuple[bool, str]:
    """Write one target blob to its sanitized destination; never raise on a path.

    Re-sanitizes the key with :func:`safe_local_dest` (anti zip-slip), reads the
    blob INTEGRITY-VERIFIED and capped to the tree-entry size, then writes it with
    the symlink-refusing :func:`atomic_write`. A :class:`SafetyError` (a symlink at
    or above the destination) or :class:`VersioningError` (a corrupt/missing object)
    is caught and returned as a per-file failure so one bad path never aborts -- or
    half-applies -- the whole checkout. The symlink target is therefore NEVER
    written through.
    """
    try:
        dest = safe_local_dest(root, fp.rel)
        data = store.read(fp.sha, max_size=fp.size)
        paths.atomic_write(dest, data)
        return True, ""
    except (SafetyError, VersioningError, OSError) as exc:
        return False, str(exc)


def _delete_one(root: Path, rel: str) -> tuple[bool, str]:
    """Symlink-safely unlink one working file; best-effort prune empty parents.

    Refuses to unlink THROUGH a symlink (the file itself being a symlink, or any
    ancestor being one) so a planted symlink can never be followed to delete an
    arbitrary target. Re-sanitizes the path first. Returns a per-file failure
    rather than raising, mirroring :func:`_write_one`.
    """
    try:
        dest = safe_local_dest(root, rel)
        if dest.is_symlink():
            raise SafetyError(f"refusing to delete through a symlink: {dest}")
        if _has_symlink_ancestor(root, dest):
            raise SafetyError(f"refusing to delete through a symlinked directory: {dest}")
        if dest.exists():
            os.unlink(str(dest))
        _prune_empty_dirs(root, dest.parent)
        return True, ""
    except (SafetyError, OSError) as exc:
        return False, str(exc)


def _has_symlink_ancestor(root: Path, dest: Path) -> bool:
    """True if any directory between ``root`` (exclusive) and ``dest`` is a symlink."""
    root = Path(root).resolve()
    cur = dest.parent
    while True:
        try:
            if cur.resolve() == root:
                return False
        except OSError:
            return True
        if cur.is_symlink():
            return True
        if cur.parent == cur:
            return False
        cur = cur.parent


def _prune_empty_dirs(root: Path, start: Path) -> None:
    """Best-effort: remove now-empty parent dirs up to (not including) ``root``.

    Never raises and never crosses a symlink or ``root`` itself; a non-empty or
    symlinked directory simply stops the walk.
    """
    root = Path(root).resolve()
    cur = start
    while True:
        try:
            if cur.resolve() == root:
                return
        except OSError:
            return
        if cur == cur.parent:
            return
        if cur.is_symlink() or not cur.is_dir():
            return
        try:
            next(cur.iterdir())
            return  # not empty
        except StopIteration:
            pass
        except OSError:
            return
        with contextlib.suppress(OSError):
            cur.rmdir()
        cur = cur.parent


def _sync_staging_to_commit(root: Path, store: ObjectStore, commit_sha: str) -> None:
    """Rewrite the staging area to EQUAL the checked-out commit's tree.

    After a full checkout the working tree matches ``commit_sha``; making staging
    equal that tree keeps ``jp status`` coherent (staged == new working tree, so it
    reports no pending stage). Builds :class:`StagedEntry` values from the commit's
    tree entries (carrying the notebook normalized sha so hybrid detection keeps
    working). NEVER touches ``.jp/index.json`` (the sync base is independent).
    """
    commit = read_commit(store, commit_sha)
    tree = read_tree(store, commit["tree"])
    staging = Staging(root)
    for rel, entry in tree.items():
        nb_norm = ""
        nb = entry.get("nb")
        if isinstance(nb, dict):
            norm = nb.get("norm_sha")
            if isinstance(norm, str):
                nb_norm = norm
        staging.set(
            rel,
            StagedEntry(
                sha256=str(entry.get("sha256", "")),
                size=int(entry.get("size", 0)),
                local_mtime=0.0,
                nb_norm_sha=nb_norm,
            ),
        )
    staging.save()

"""Fetch committed history BACK from the remote ``__jp`` backup -- THE read path.

This is the ONLY module in the versioning feature that READS remote history bytes
back into the local object store, so it is the single most security-sensitive
corner of the whole feature. The remote Jupyter server is treated as POSSIBLY
HOSTILE: every byte it serves under ``<prefix>/__jp/`` is UNTRUSTED until it has
been decoded, decompression-capped, and re-hashed to the exact name we requested.

Security contract (every clause is exercised by tests/test_fetch.py)
--------------------------------------------------------------------
* NAMES WE DERIVE, NEVER PATHS THE SERVER VOLUNTEERS. The first sha we trust is
  the remote branch ref (validated 64-lowercase-hex). Every subsequent sha comes
  from INSIDE an already-VERIFIED object (a commit's tree/parents, a tree's blob
  shas -- the latter re-validated by :func:`read_tree`). We NEVER list and walk
  the remote ``__jp/objects`` tree: a hostile or enormous listing must not be
  enumerated. Downloads are REACHABILITY-DRIVEN -> O(reachable) GETs only.
* VERIFY-BEFORE-PLACE. Each downloaded object-file is handed to
  :meth:`ObjectStore.import_object`, the ONE trusted gateway: it decodes (marker
  + CAPPED decompress), re-hashes, and only then atomically places the bytes --
  or raises and places nothing. A mismatch / bad marker / decompression bomb
  ABORTS the fetch with a clear message (a corrupt remote object means the
  history is not trustworthy -- fail loudly, never place garbage).
* DECOMPRESSION CAP. A blob's expected size (from its VERIFIED tree entry) is
  passed as ``max_size`` so a 100 KB -> GB "zip bomb" is refused before it can
  OOM us. Commits and trees use the module default cap.
* CYCLE GUARD. The commit DAG walk carries a visited set, so a forged
  ``parents`` cycle can never hang the fetch.
* PATH JAIL. Every remote object/ref path is composed with
  :func:`jp.paths.remote_path_for` and re-checked with
  :func:`jp.paths.assert_within_prefix` as hygiene before the GET.
* FAST-FORWARD ONLY. After all reachable objects are present+verified we move the
  LOCAL ref to the remote sha ONLY when the local ref is ABSENT or the remote sha
  is a FAST-FORWARD of it (the local sha is an ancestor of the remote sha, proven
  by the now-local history walk). Diverged history is NEVER silently rewritten:
  the objects are downloaded (harmless, resumable) but the ref is left and a
  warning is recorded. A remote that is BEHIND local leaves the ref untouched.
* CLEAN ABORT. A network error mid-walk aborts without corrupting local state --
  only fully-verified objects are ever placed (orphans are harmless + resumable),
  and a ref is never advanced past what is actually present locally.

Cross-platform: standard library only; reuses the path-jail + the atomic,
symlink-safe :meth:`ObjectStore.import_object` for every local write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .. import paths
from ..api import Api
from ..config import Config
from ..errors import ApiError, JpError, NetworkError
from .lock import versioning_lock
from .objects import ObjectStore, VersioningError
from .refs import (
    DEFAULT_BRANCH,
    check_format,
    init_versioning,
    read_ref,
    update_ref,
    validate_ref_name,
)
from .repo import read_commit, read_tree

if TYPE_CHECKING:
    # Import only for the type annotations -- checkout.py imports this module's
    # siblings (repo/refs/objects/lock) but NOT fetch itself, so there is no true
    # runtime cycle; we still defer it to keep the import graph tidy and explicit.
    from .checkout import CheckoutResult, ConfirmDeleteUntracked

# Top-level remote meta dir the mirror writes under (kept in sync with
# :data:`jp.versioning.mirror.MIRROR_DIR`). NON-dotted: the server rejects dotted
# names (allow_hidden=False), and the mirror chose this name on purpose.
MIRROR_DIR = "__jp"

# A valid object/commit sha is exactly 64 lowercase hex chars. fullmatch (not a
# "$"-anchored search) so a trailing newline / stray char can never slip through
# and produce a traversal-y remote path (mirrors objects.py / mirror.py).
_SHA_RE = re.compile(r"[0-9a-f]{64}")

# Decompression cap for COMMIT and TREE (meta) objects fetched from the UNTRUSTED
# remote. Canonical-JSON commits/trees are realistically tiny (a few KiB even for a
# large working tree), so a crafted meta object must never force a giant single
# allocation. 16 MiB is far above any honest meta object yet bounds a hostile one
# hard. Blobs are NOT capped by this -- they pass their (now module-clamped, see
# ObjectStore.import_object) tree-entry size, which is their legitimate ceiling.
_FETCH_META_MAX = 16 * 1024 * 1024  # 16 MiB

# FAN-OUT / CHAIN-DEPTH NOTE: the reachability walk is bounded ONLY by what the
# remote history actually references (trust-the-committer): a branch you fetch can
# name an arbitrarily long commit chain or a wide tree, and v1 follows all of it.
# Per-object DoS is fully bounded (every object is decompression-capped + re-hashed
# via the import_object gateway before it is trusted), but the TOTAL object COUNT
# is not separately capped. A configurable max-objects / max-depth guard is a
# planned future hardening; it is intentionally out of scope for v1.


@dataclass
class BranchFetch:
    """The fetch outcome for a single branch.

    ``ref_updated`` is True only when we advanced the LOCAL ref to ``remote_sha``
    (the ref was absent or the remote sha is a fast-forward). ``ref_skipped_reason``
    records WHY a ref was not advanced (diverged, remote behind local, already up
    to date, the branch was absent/corrupt remotely) so the caller can report
    honestly. ``downloaded``/``skipped`` count objects fetched vs already-local.
    """

    branch: str
    remote_sha: str = ""
    downloaded: int = 0
    skipped: int = 0
    ref_updated: bool = False
    ref_skipped_reason: str = ""


@dataclass
class FetchResult:
    """Aggregate outcome of :func:`fetch_history` across every requested branch.

    ``downloaded``/``skipped`` are object counts summed over branches. ``branches``
    carries the per-branch detail (including whether each ref advanced).
    ``warnings`` collects human-readable, non-fatal notes (an absent/corrupt remote
    branch, a diverged ref). ``failed`` is the count of objects that failed
    verification -- ANY failure means the fetch aborted (a hostile/corrupt object),
    and the command layer exits non-zero.
    """

    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    branches: list[BranchFetch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        """True iff at least one branch was processed (even if it was absent)."""
        return bool(self.branches)


def _validate_object_sha(sha: str) -> None:
    """HEX GUARD: refuse any sha that is not lowercase 64-hex BEFORE path use.

    Runs before a sha is composed into ``__jp/objects/<2>/<62>`` so ``..``, ``/``,
    uppercase, short, empty, or non-hex inputs can never reach the remote path.
    Mirrors :func:`jp.versioning.mirror._validate_object_sha`.
    """
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise VersioningError(f"refusing to fetch an invalid object id: {sha!r}")


def _object_remote_rel(sha: str) -> str:
    """Compose the prefix-relative mirror path for an object (post hex-guard)."""
    _validate_object_sha(sha)
    return f"{MIRROR_DIR}/objects/{sha[:2]}/{sha[2:]}"


def _ref_remote_rel(branch: str) -> str:
    """Compose the prefix-relative mirror path for a branch ref (post name-guard)."""
    validate_ref_name(branch)
    return f"{MIRROR_DIR}/refs/heads/{branch}"


def _read_remote_ref(api: Api, prefix: str, branch: str) -> str | None:
    """Read the remote branch ref under ``__jp/refs/heads``; None if absent/corrupt.

    Tolerates a 404 (the ref does not exist yet) and any malformed body (returns
    None rather than trusting garbage -- a ref MUST be a single 64-hex sha). A
    genuine network error propagates so the caller records a clean abort.
    """
    remote_path = paths.remote_path_for(prefix, _ref_remote_rel(branch))
    try:
        data = api.get_file_bytes(remote_path)
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise
    text = data.decode("utf-8", "replace").strip()
    return text if _SHA_RE.fullmatch(text) else None


def _download_object(api: Api, prefix: str, sha: str) -> bytes:
    """GET one object's raw on-disk bytes from ``__jp/objects/<2>/<62>``.

    ``sha`` is hex-guarded, composed via the path-jail, and re-asserted within the
    prefix immediately before the GET. The returned bytes are STILL UNTRUSTED --
    the caller MUST pass them through :meth:`ObjectStore.import_object` (which
    verifies+places) before trusting them. Raises an ApiError/NetworkError on a
    transport failure (the caller turns that into a clean abort).
    """
    remote_path = paths.remote_path_for(prefix, _object_remote_rel(sha))
    paths.assert_within_prefix(remote_path, prefix)
    return api.get_file_bytes(remote_path)


def _ensure_object(
    api: Api, store: ObjectStore, prefix: str, sha: str, br: BranchFetch, *, max_size: int | None
) -> None:
    """Make ``sha`` present+VERIFIED locally: skip if already held, else download.

    If ``store.has(sha)`` the object (and -- for a commit fetched in a prior run --
    its closure) is already local and verified, so we skip. Otherwise we GET the
    untrusted bytes and hand them to :meth:`ObjectStore.import_object`, the trusted
    gateway, which decodes+caps+re-hashes and only then places them. ``max_size``
    is the decompression cap: the blob's tree-entry size for blobs, and
    :data:`_FETCH_META_MAX` (16 MiB) for commits/trees. Either way ``import_object``
    further CLAMPS it down to the module ceiling, so an attacker-authored tree size
    can only tighten the cap, never raise it.

    Raises :class:`VersioningError` if the bytes do not verify (a hostile/corrupt
    object -> the fetch aborts and nothing corrupt is placed).
    """
    if store.has(sha):
        br.skipped += 1
        return
    raw = _download_object(api, prefix, sha)
    store.import_object(sha, raw, max_size=max_size)
    br.downloaded += 1


def _fetch_reachable(api: Api, store: ObjectStore, prefix: str, tip: str, br: BranchFetch) -> None:
    """Download + verify every object reachable from commit ``tip`` (O(reachable)).

    Reachability walk (visited-guarded against a forged parents cycle):

    1. Ensure the commit object itself (download+verify if absent, capped at
       :data:`_FETCH_META_MAX`). If it was already local, treat its whole closure as
       present and DO NOT re-descend it (a clean store that holds a commit holds its
       subtree -- we fetched it as a unit before, ref-last on the mirror side
       guarantees completeness).
    2. Read the now-local commit; ensure its tree (also capped at
       :data:`_FETCH_META_MAX`), then enqueue every blob sha from
       :func:`read_tree` (which re-validates keys via normalize_rel + sha format,
       so a hostile ``"../x"`` key is rejected and the fetch aborts). Each blob is
       fetched with its tree-entry size as the decompression cap (clamped to the
       module ceiling inside :meth:`ObjectStore.import_object`).
    3. Recurse into each parent sha (visited-guarded).

    Raises on any verification failure (hostile object) or unreadable structure --
    the caller records a clean abort; only verified objects were ever placed.
    """
    visited: set[str] = set()
    stack = [tip]
    while stack:
        commit_sha = stack.pop()
        if commit_sha in visited:
            continue
        visited.add(commit_sha)

        # 1) The commit object (meta-capped). If we already held it, closure present.
        had_commit = store.has(commit_sha)
        _ensure_object(api, store, prefix, commit_sha, br, max_size=_FETCH_META_MAX)
        if had_commit:
            continue

        # 2) Read the now-local (verified) commit; fetch its tree (meta-capped) + blobs.
        commit = read_commit(store, commit_sha)
        tree_sha = str(commit.get("tree", ""))
        _validate_object_sha(tree_sha)
        _ensure_object(api, store, prefix, tree_sha, br, max_size=_FETCH_META_MAX)
        entries = read_tree(store, tree_sha)  # re-validates keys + blob sha format
        for meta in entries.values():
            blob = str(meta.get("sha256", ""))
            try:
                size = int(meta.get("size", 0))
            except (TypeError, ValueError):
                size = 0
            _ensure_object(api, store, prefix, blob, br, max_size=size or None)

        # 3) Recurse parents (visited-guarded).
        for parent in commit.get("parents") or []:
            if isinstance(parent, str) and parent not in visited:
                stack.append(parent)


def _is_ancestor(store: ObjectStore, ancestor: str, descendant: str) -> bool:
    """True iff ``ancestor`` is reachable from ``descendant`` via the local DAG.

    Walks the FULL parent DAG from ``descendant`` (all parents, visited-guarded
    against a cycle), so it answers "is the local ref a fast-forward base of the
    remote tip?" once both sides' objects are present locally. ``ancestor ==
    descendant`` is True (nothing to do). A commit we cannot read stops that branch
    of the walk (it cannot extend the ancestry), so a partially-corrupt closure can
    never falsely CLAIM an ancestor relationship -> we stay on the safe side and do
    not fast-forward over uncertainty.
    """
    if ancestor == descendant:
        return True
    visited: set[str] = set()
    stack = [descendant]
    while stack:
        sha = stack.pop()
        if sha in visited:
            continue
        visited.add(sha)
        try:
            commit = read_commit(store, sha)
        except JpError:
            continue
        for parent in commit.get("parents") or []:
            if not isinstance(parent, str):
                continue
            if parent == ancestor:
                return True
            if parent not in visited:
                stack.append(parent)
    return False


def _update_local_ref(
    root: Path, store: ObjectStore, branch: str, remote_sha: str, br: BranchFetch
) -> None:
    """Advance the LOCAL ref to ``remote_sha`` IFF that is a safe fast-forward.

    Policy (all decided from the now-local, verified history):

    * local ref ABSENT       -> set it to ``remote_sha`` (first fetch of this branch).
    * local == remote        -> nothing to do (already up to date).
    * local is an ANCESTOR of remote (fast-forward) -> advance to ``remote_sha``.
    * remote is an ANCESTOR of local (remote behind) -> leave the local ref.
    * otherwise DIVERGED      -> leave the local ref + record a warning. We NEVER
      silently rewrite local history; the objects are downloaded and available, but
      reconciliation is the user's call.

    DEFENSE-IN-DEPTH CAS: the advancement is correct because fetch holds the
    versioning lock, but we still advance via :func:`update_ref` with the local sha
    we just read as ``expected`` (``None`` when the ref was absent) -- mirroring
    commit's compare-and-swap. Under the lock this never fails; if anything raced
    past the lock the mismatch RAISES rather than blindly overwriting the local ref.
    """
    local_sha = read_ref(root, branch)
    if local_sha is None:
        update_ref(root, branch, remote_sha, expected=None)
        br.ref_updated = True
        return
    if local_sha == remote_sha:
        br.ref_skipped_reason = "already up to date"
        return
    if _is_ancestor(store, local_sha, remote_sha):
        update_ref(root, branch, remote_sha, expected=local_sha)
        br.ref_updated = True
        return
    if _is_ancestor(store, remote_sha, local_sha):
        br.ref_skipped_reason = "remote is behind local; ref left unchanged"
        return
    br.ref_skipped_reason = f"local and remote history diverged on {branch}; not fast-forwarding"


def _fetch_one_branch(
    root: Path, store: ObjectStore, prefix: str, api: Api, branch: str, result: FetchResult
) -> None:
    """Fetch a single branch: read its remote ref, download its closure, FF the ref.

    An absent or corrupt remote ref is a recorded warning (the branch is skipped),
    not an error. A verification failure (hostile/corrupt object) or a transport
    error during the walk raises out of here so :func:`fetch_history` records a
    clean abort -- only verified objects were placed and the ref was not advanced.
    """
    br = BranchFetch(branch=branch)
    result.branches.append(br)

    remote_sha = _read_remote_ref(api, prefix, branch)
    if remote_sha is None:
        msg = f"branch {branch!r}: no usable ref on the remote (absent or corrupt); skipping"
        br.ref_skipped_reason = "remote ref absent/corrupt"
        result.warnings.append(msg)
        return
    br.remote_sha = remote_sha

    # Download + verify every reachable object FIRST (objects before the ref move).
    _fetch_reachable(api, store, prefix, remote_sha, br)
    result.downloaded += br.downloaded
    result.skipped += br.skipped

    # Only now -- with the full verified closure local -- decide the FF ref move.
    _update_local_ref(root, store, branch, remote_sha, br)
    if br.ref_skipped_reason.startswith("local and remote history diverged"):
        result.warnings.append(br.ref_skipped_reason)


def fetch_history(
    root: Path, cfg: Config, api: Api, *, branches: list[str] | None = None
) -> FetchResult:
    """Fetch committed history from ``<prefix>/__jp/`` into the LOCAL object store.

    Runs UNDER :func:`jp.versioning.lock.versioning_lock` so it never interleaves
    with a concurrent commit/gc. For each requested branch (default: the configured
    DEFAULT_BRANCH ``main``) it reads the remote ref, downloads+verifies every
    REACHABLE object (O(reachable) GETs -- the remote ``__jp/objects`` tree is
    NEVER enumerated), and then advances the local ref ONLY on a proven
    fast-forward (see the module docstring for the full UNTRUSTED-REMOTE contract).

    A verification failure (a tampered/corrupt remote object, a decompression bomb,
    a hostile tree key) or a transport error ABORTS cleanly: only fully-verified
    objects were placed (harmless, resumable orphans) and no ref was advanced past
    what is present locally. The failure is recorded on the result and re-raised as
    a :class:`VersioningError` so the command layer exits non-zero.
    """
    result = FetchResult()
    root = Path(root).resolve()
    prefix = paths.validate_prefix(cfg.prefix)

    wanted = list(branches) if branches else [DEFAULT_BRANCH]
    # Validate every requested name up front (anti path-traversal) before any I/O.
    for branch in wanted:
        validate_ref_name(branch)

    with versioning_lock(root):
        init_versioning(root)
        check_format(root)
        store = ObjectStore(root)
        for branch in wanted:
            try:
                _fetch_one_branch(root, store, prefix, api, branch, result)
            except VersioningError as exc:
                # A corrupt/hostile remote object: the history is not trustworthy.
                # Abort loudly -- only verified objects were placed; ref not moved.
                result.failed += 1
                msg = f"branch {branch!r}: {exc.message}"
                result.warnings.append(msg)
                raise VersioningError(
                    f"fetch aborted: {msg} (no corrupt object was placed; "
                    "the remote history is not trustworthy)"
                ) from exc
            except (ApiError, NetworkError) as exc:
                # A transport failure mid-walk: clean abort. Verified orphans remain
                # (harmless, resumable); the ref was not advanced.
                result.failed += 1
                detail = getattr(exc, "message", None) or str(exc)
                result.warnings.append(f"branch {branch!r}: network error: {detail}")
                raise

    return result


@dataclass
class RestoreResult:
    """Outcome of :func:`restore`: the fetch result + the (optional) checkout one.

    ``checkout`` is None when HEAD does not resolve to a commit after the fetch
    (an unborn branch / nothing was restored), so the caller can report "history
    rebuilt, nothing to check out". Otherwise it is the :class:`CheckoutResult`
    from materializing the working tree to HEAD.
    """

    fetch: FetchResult
    checkout: CheckoutResult | None = None
    head_resolved: bool = False


def restore(
    root: Path,
    cfg: Config,
    api: Api,
    *,
    force: bool = False,
    confirm_delete_untracked: ConfirmDeleteUntracked | None = None,
) -> RestoreResult:
    """Rebuild LOCAL history from the remote backup, then check out HEAD.

    The intended use is recovery: the local ``.jp`` (objects + refs) was deleted
    but the remote ``<prefix>/__jp/`` backup survived. :func:`fetch_history` brings
    the verified objects + refs back; then, if HEAD now resolves to a commit, we
    materialize the working tree to it via the Task-5 :func:`apply_checkout`.

    The working dir COMMONLY still holds the user's files (only ``.jp`` was lost),
    so the checkout RESPECTS its safety gates: an empty working dir gets every
    committed file written additively, but a file with uncommitted local edits
    BLOCKS the checkout unless ``--force`` is passed through. We never clobber dirty
    work silently. ``confirm_delete_untracked`` is forwarded to the checkout (only
    consulted with ``--remove-extra``, which restore does NOT request -- so extras
    are simply kept). Same UNTRUSTED-REMOTE guarantees as :func:`fetch_history`: a
    corrupt remote object aborts before any checkout.
    """
    from . import refs
    from .checkout import apply_checkout

    fetch_result = fetch_history(root, cfg, api)

    head = refs.resolve_head(root)
    if head is None:
        return RestoreResult(fetch=fetch_result, checkout=None, head_resolved=False)

    def _refuse(_rels: list[str]) -> bool:
        # restore never passes --remove-extra, so this is never actually consulted;
        # default to a safe refusal if a future caller wires it up differently.
        return False

    confirm = confirm_delete_untracked if confirm_delete_untracked is not None else _refuse
    checkout_result = apply_checkout(
        root,
        cfg,
        "HEAD",
        None,
        force=force,
        remove_extra=False,
        dry_run=False,
        confirm_delete_untracked=confirm,
    )
    return RestoreResult(fetch=fetch_result, checkout=checkout_result, head_resolved=True)

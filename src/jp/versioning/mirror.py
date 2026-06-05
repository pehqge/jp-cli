"""Remote history mirror -- back COMMITTED versioning history up to the remote.

This is the RISKIEST corner of the versioning feature: it talks to a SHARED,
possibly-hostile Jupyter server over the Contents API. A mistake here could
corrupt the working tree or destroy the user's history, so the whole module is
written to be paranoid, opt-in, and NEVER able to fail the data push.

Safety contract (every claim is exercised by tests/test_mirror.py)
------------------------------------------------------------------
* ``__jp`` EXCLUDED FROM SYNC (both ends): the mirror lives at ``<prefix>/__jp/``
  on the remote. :mod:`jp.sync` skips ``__jp``/``jp-tmp`` as the FIRST segment on
  BOTH sides -- ``scan_remote`` never returns them (so ``jp pull`` never downloads
  the object store and a mirror-mode ``jp push`` never lists them deletable) AND
  ``scan_local`` never returns a top-level ``__jp``/``jp-tmp`` (so a normal push
  can never UPLOAD a local meta dir over the remote mirror and corrupt it). The
  exclusion lives in ``sync``; this module only WRITES under ``__jp``.
* WRITE-ONCE / IDEMPOTENT: objects are content-addressed and immutable. We PUT
  an object's exact on-disk bytes; a re-PUT of the same sha is byte-identical and
  harmless. ``PutResult.created is False`` means the remote already had it -> we
  count it "skipped", never re-upload it as "new". The whole operation is
  resumable by construction: a failed push leaves orphan objects and the ref
  un-advanced, and the next push fills the gap.
* FAST-FORWARD ONLY + REF LAST + OPTIMISTIC CAS: for each branch we push ALL
  reachable objects FIRST, then move the ref LAST. The ref is advanced ONLY when
  it is a proven fast-forward: the remote ref is absent, OR our tip provably
  descends from the remote sha (the remote sha is in our reachable history). If
  the remote points at history this clone doesn't have (post-gc / loss / a
  divergent sibling), advancing would be a non-fast-forward overwrite, so we
  WITHHOLD the ref and report the branch incomplete (run ``jp fetch`` first). When
  advancing is allowed we still RE-READ the remote ref and abort if it changed
  since the baseline (another machine advanced it). Otherwise we PUT the ref (the
  tip sha + "\n"). A crash before the ref PUT leaves orphan objects, not corrupt
  history -- exactly the same crash-safety the local commit path has.
* HEX GUARD: every object sha is validated as ``^[0-9a-f]{64}$`` BEFORE it is
  composed into a remote path, so a bogus id can never produce ``__jp/objects//``
  or a traversal.
* PATH JAIL: every remote path is composed with :func:`jp.paths.remote_path_for`
  and re-checked with :func:`jp.paths.assert_within_prefix` IMMEDIATELY before the
  mutating PUT, exactly like the sync engine.
* BEST-EFFORT, NEVER BLOCKS: any network/ApiError is caught and recorded as a
  failure; the function returns a :class:`MirrorResult` and never raises out in a
  way that could fail the (already-successful) data push.

base64/compression tradeoff
---------------------------
We mirror the EXACT on-disk object file (1-byte marker + raw|zlib body), so Task
9 fetch can byte-copy and verify it through :meth:`ObjectStore.read`. Binary
object files therefore go up base64 (~33% inflation) on the wire. We accept that:
objects are already zlib-compressed where it helps, so a second "text" encoding
would not shrink them, and round-trip fidelity (the stored bytes hash back to the
sha) is worth far more than dodging the base64 tax.

Cross-platform: standard library only; reuses the path-jail for every write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths, ui
from ..api import Api
from ..config import Config
from ..errors import ApiError, JpError
from .fsck import reachable_objects
from .lock import versioning_lock
from .objects import ObjectStore
from .refs import list_heads, validate_ref_name

# Top-level remote meta dir the mirror writes under. NON-dotted on purpose: the
# server runs allow_hidden=False, so a dotted name would be rejected (HTTP 400).
# Kept in sync with ``jp.sync.REMOTE_META_DIRS`` (which EXCLUDES it from sync).
MIRROR_DIR = "__jp"

# A valid object name is exactly 64 lowercase hex chars (sha256). Anchored with
# fullmatch so a trailing newline / stray char can never slip through and produce
# a traversal-y remote path.
_SHA_RE = re.compile(r"[0-9a-f]{64}")

# On-disk object files above this size MAY be probed (api.hash) before a re-PUT to
# skip re-uploading a body the remote already has. Below it we just PUT (cheaper
# than a round-trip): correctness comes from write-once immutability, not the
# probe. 1 MiB matches the object store's streaming chunk.
_LARGE_OBJECT_BYTES = 1 * 1024 * 1024

# Default refuse-to-mirror blob threshold in MiB (mirrors config's default) used
# when cfg carries no usable ``versioning_max_blob_mb``.
_DEFAULT_MAX_BLOB_MB = 100


def _max_blob_bytes(cfg: Config) -> int:
    """Read ``cfg.versioning_max_blob_mb`` defensively; return the cap in BYTES.

    A missing / non-positive / non-numeric value falls back to the default so a
    bare or tampered config can never disable the mirror size guard.
    """
    raw = getattr(cfg, "versioning_max_blob_mb", _DEFAULT_MAX_BLOB_MB)
    try:
        mb = int(raw)
    except (TypeError, ValueError):
        mb = _DEFAULT_MAX_BLOB_MB
    if mb <= 0:
        mb = _DEFAULT_MAX_BLOB_MB
    return mb * 1024 * 1024


@dataclass
class BranchResult:
    """The mirror outcome for a single branch.

    ``ref_advanced`` is True only when we successfully PUT the branch ref to the
    local tip. ``ref_skipped_reason`` records WHY a ref was not advanced (CAS
    conflict, an object push failed, or the tip was already mirrored), so the
    caller can report honestly and never claim history is safe when it is not.
    """

    branch: str
    tip: str
    pushed: int = 0
    skipped: int = 0
    failed: int = 0
    ref_advanced: bool = False
    ref_skipped_reason: str = ""


@dataclass
class MirrorResult:
    """Aggregate outcome of :func:`mirror_history` across every branch.

    ``pushed``/``skipped``/``failed`` are object counts summed over branches;
    ``branches`` carries the per-branch detail (including whether each ref
    advanced). ``warnings`` collects human-readable, non-fatal notes (CAS
    conflicts, divergence, per-object failures). ``ran`` is False when there was
    nothing to mirror (no committed history at all).
    """

    ran: bool = False
    pushed: int = 0
    skipped: int = 0
    failed: int = 0
    branches: list[BranchResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Objects we attempted to push this run (new uploads + failures)."""
        return self.pushed + self.failed

    @property
    def complete(self) -> bool:
        """True iff nothing failed and every branch ref that needed it advanced."""
        if self.failed:
            return False
        return all(b.ref_advanced or b.ref_skipped_reason == _ALREADY for b in self.branches)


# Sentinel reason recorded when a branch tip was already mirrored (nothing to do).
_ALREADY = "already-up-to-date"


def _validate_object_sha(sha: str) -> None:
    """HEX GUARD: refuse any object id that is not lowercase 64-hex BEFORE path use.

    Runs before a sha is composed into ``__jp/objects/<2>/<62>`` so ``..``, ``/``,
    uppercase, short, empty, or non-hex inputs can never reach the remote path.
    """
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise JpError(f"refusing to mirror an invalid object id: {sha!r}")


def _object_remote_rel(sha: str) -> str:
    """Compose the prefix-relative mirror path for an object (post hex-guard)."""
    _validate_object_sha(sha)
    return f"{MIRROR_DIR}/objects/{sha[:2]}/{sha[2:]}"


def _ref_remote_rel(branch: str) -> str:
    """Compose the prefix-relative mirror path for a branch ref (post name-guard).

    ``branch`` normally comes from :func:`list_heads`, which already re-validated
    every name via ``validate_ref_name`` (a single safe segment). We re-validate
    here too (defense-in-depth, mirroring :func:`jp.versioning.fetch._ref_remote_rel`)
    so this function is self-defending regardless of caller -- a crafted name can
    never be composed into a traversal-y remote ref path. Zero behavior change for
    valid callers.
    """
    validate_ref_name(branch)
    return f"{MIRROR_DIR}/refs/heads/{branch}"


def _read_remote_ref(api: Api, prefix: str, branch: str) -> str | None:
    """Read the remote branch ref under ``__jp/refs/heads``; None if absent.

    Tolerates a 404 (the ref does not exist yet) and any malformed body (returns
    None rather than trusting garbage). Never raises on a missing ref; a genuine
    network error propagates to the caller's try/except so it becomes a failure,
    never a crash.
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


def _reachable_from_tip(root: Path, store: ObjectStore, tip: str) -> set[str]:
    """Objects reachable from a single commit ``tip`` (commit + tree + blobs).

    Reuses the shared DAG walk via a throwaway single-tip resolution: we walk the
    first-parent + all-parents DAG exactly as ``reachable_objects`` does for the
    whole repo, but seeded from one tip. Implemented by delegating to the repo's
    history primitives so the mirror can never disagree with fsck/gc about what an
    object set contains.
    """
    from .repo import read_commit, read_tree

    found: set[str] = set()
    stack = [tip]
    visited: set[str] = set()
    while stack:
        sha = stack.pop()
        if sha in visited:
            continue
        visited.add(sha)
        found.add(sha)
        try:
            commit = read_commit(store, sha)
        except JpError:
            # A tip/parent we cannot read is still "reachable" (kept), but we
            # cannot descend it. fsck reports such corruption separately.
            continue
        tree_sha = str(commit.get("tree", ""))
        if tree_sha:
            found.add(tree_sha)
            try:
                entries = read_tree(store, tree_sha)
            except JpError:
                entries = {}
            for meta in entries.values():
                blob = meta.get("sha256")
                if isinstance(blob, str) and blob:
                    found.add(blob)
        for parent in commit.get("parents") or []:
            if isinstance(parent, str) and parent not in visited:
                stack.append(parent)
    return found


# Sentinel returned by :func:`_push_object` when an object exceeds the max-blob
# cap: a string carrying the human reason (the caller records it as a failure and
# withholds the ref). Distinct from the bool/None create-vs-skip-vs-gone signals.
class _Oversized(str):
    """A push-skip reason string for an object that is too large to mirror."""


def _push_object(
    api: Api, store: ObjectStore, prefix: str, sha: str, *, max_blob_bytes: int
) -> bool | None | _Oversized:
    """PUT one object's on-disk bytes under ``__jp/objects``; report the outcome.

    Returns one of:
      * ``True``        -- a NEW remote object was created (HTTP 201);
      * ``False``       -- the remote already had it (HTTP 200 -> write-once skip);
      * ``None``        -- the local object file is unexpectedly gone (a failure);
      * ``_Oversized``  -- the on-disk object exceeds the max-blob cap; NOT PUT
                           (a failure -> the branch is incomplete and the ref is
                           withheld).

    SIZE GUARD: the stage-time guard (repo.stage_paths) keeps oversized blobs out
    of the store in the first place, but an object committed BEFORE the guard
    existed (or written via another path) could still be huge -- reading it fully
    into memory and PUTting it could hang near the timeout. So this is a SECOND,
    independent enforcement point: an over-cap object is hard-skipped here too.

    WRITE-ONCE: we never stat/hash a small object before the PUT -- a re-PUT is
    byte-identical and harmless, and the create/skip signal comes back FROM the
    PUT. For a LARGE object file we MAY probe the remote first to skip re-uploading
    the body, purely as an optimization.
    """
    local_path = store.path_for(sha)  # validates the sha again (defense in depth)
    try:
        size = local_path.stat().st_size
    except OSError:
        return None

    # HARD SKIP: an over-cap object is never read into memory or PUT.
    if size > max_blob_bytes:
        mb = size / (1024 * 1024)
        cap_mb = max_blob_bytes // (1024 * 1024)
        return _Oversized(
            f"object {sha[:12]} is {mb:.0f} MiB > versioning.max_blob_mb={cap_mb}; "
            "not mirrored -- raise the limit or await chunked packs"
        )

    try:
        body = local_path.read_bytes()
    except OSError:
        return None

    remote_rel = _object_remote_rel(sha)
    remote_path = paths.remote_path_for(prefix, remote_rel)
    # SAFETY: assert immediately before the mutating call.
    paths.assert_within_prefix(remote_path, prefix)

    # Large-object optimization: a remote object is immutable, so if it already
    # exists we can skip re-uploading its (base64-inflated) body entirely.
    if len(body) >= _LARGE_OBJECT_BYTES:
        try:
            if api.stat(remote_path) is not None:
                return False
        except ApiError:
            # If the probe fails, fall through to the PUT -- correctness does not
            # depend on the probe, only on the write-once PUT below.
            pass
        # Best-effort: give a very large body proportionally more time, restoring
        # the original timeout afterwards (see _put_with_timeout).
        return _put_with_timeout(api, remote_path, body)

    result = api.put_file(remote_path, body)
    return result.created


def _put_with_timeout(api: Api, remote_path: str, body: bytes) -> bool:
    """PUT ``body`` with a temporarily-raised timeout, then RESTORE the original.

    A legitimately large object may need more than the default 30s; we bump the
    per-call timeout proportionally for the duration of this one PUT and restore
    the previous value in a ``finally`` so the inflated timeout never leaks into
    the rest of the session. Defensive: a missing/odd ``timeout`` attribute must
    never break the push.
    """
    saved = getattr(api, "timeout", None)
    try:
        try:
            want = 30.0 + (len(body) / (1024 * 1024)) * 1.5
            current = float(saved or 0.0)
            if want > current:
                api.timeout = want
        except Exception:
            pass
        return api.put_file(remote_path, body).created
    finally:
        try:
            if saved is not None:
                api.timeout = saved
        except Exception:
            pass


def _mirror_branch(
    root: Path,
    prefix: str,
    api: Api,
    store: ObjectStore,
    branch: str,
    tip: str,
    result: MirrorResult,
    *,
    max_blob_bytes: int,
) -> BranchResult:
    """Mirror a single branch: push the delta, then advance the ref under CAS.

    Incremental: if the remote already has a ref AND we hold that commit locally
    (i.e. our tip is a verified DESCENDANT of the remote sha -- the remote sha is
    in our reachable history), we push only the objects reachable from the local
    tip but NOT from the remote sha (the delta) and advance the ref (a fast-forward).
    If the remote ref is ABSENT we push the full reachable set and advance.

    If the remote ref is PRESENT but NOT locally resolvable (post-gc / loss /
    divergence), we CANNOT prove our tip descends from it, so advancing would be a
    non-fast-forward overwrite of history this clone doesn't have. We refuse: we
    still push our objects (harmless, resumable) but WITHHOLD the ref and report
    the branch as incomplete. The user must reconcile with ``jp fetch`` (Task 9).
    """
    br = BranchResult(branch=branch, tip=tip)

    # 1) Read the remote ref ONCE up front -- this is the CAS baseline.
    remote_sha = _read_remote_ref(api, prefix, branch)

    # 2) Compute the object set to push (incremental delta when possible) and
    #    decide whether advancing the ref would be a safe fast-forward.
    local_set = _reachable_from_tip(root, store, tip)
    # We may advance only when the remote ref is absent OR our tip provably
    # descends from it (remote_sha is in our reachable history). An unresolvable
    # remote sha can never be confirmed an ancestor -> ref is withheld.
    ref_advance_allowed = True
    if remote_sha == tip:
        # The remote tip already equals ours: nothing to push, nothing to move.
        br.ref_advanced = False
        br.ref_skipped_reason = _ALREADY
        return br
    if remote_sha and store.has(remote_sha) and remote_sha in local_set:
        # Fast-forward: our tip descends from the remote sha. Push only the delta.
        already = _reachable_from_tip(root, store, remote_sha)
        to_push = local_set - already
    elif remote_sha:
        # Present but NOT a confirmed ancestor of our tip (missing locally, or a
        # divergent sibling). Refuse to overwrite unknown/divergent remote history.
        ref_advance_allowed = False
        msg = (
            f"branch {branch!r}: remote points at history this clone doesn't have "
            f"({_short(remote_sha)}); refusing to overwrite -- run 'jp fetch' first (Task 9). "
            "Objects are uploaded but the ref is NOT moved."
        )
        result.warnings.append(msg)
        ui.warn(msg)
        to_push = local_set
    else:
        # Remote ref absent -> first push of this branch; full set + advance.
        to_push = local_set

    # 3) Push every object in the delta. WRITE-ONCE: created -> new, else skipped.
    any_failed = False
    for sha in sorted(to_push):
        try:
            outcome = _push_object(api, store, prefix, sha, max_blob_bytes=max_blob_bytes)
        except (ApiError, JpError) as exc:
            any_failed = True
            br.failed += 1
            note = f"branch {branch!r}: failed to mirror object {sha[:12]}: {_msg(exc)}"
            result.warnings.append(note)
            ui.warn(note)
            continue
        if isinstance(outcome, _Oversized):
            any_failed = True
            br.failed += 1
            note = f"branch {branch!r}: {outcome}"
            result.warnings.append(note)
            ui.warn(note)
        elif outcome is None:
            any_failed = True
            br.failed += 1
            note = f"branch {branch!r}: local object {sha[:12]} vanished before upload"
            result.warnings.append(note)
            ui.warn(note)
        elif outcome:
            br.pushed += 1
        else:
            br.skipped += 1

    # 4) If ANY object failed, do NOT advance the ref -- the history is not fully
    #    backed up yet. The orphan objects are harmless and the next push resumes.
    if any_failed:
        br.ref_advanced = False
        br.ref_skipped_reason = "objects failed; ref left to resume next push"
        return br

    # 4b) Unresolvable/divergent remote ref -> never advance (FIX 2). We pushed
    #     our objects but must not overwrite history we cannot prove we descend from.
    if not ref_advance_allowed:
        br.ref_advanced = False
        br.ref_skipped_reason = (
            "remote points at history this clone doesn't have; refusing to overwrite "
            "-- run jp fetch first (Task 9)"
        )
        return br

    # 5) REF LAST + OPTIMISTIC CAS: re-read the remote ref; abort if it advanced.
    try:
        current_remote = _read_remote_ref(api, prefix, branch)
    except ApiError as exc:
        br.ref_advanced = False
        br.ref_skipped_reason = f"could not re-read remote ref: {_msg(exc)}"
        result.warnings.append(f"branch {branch!r}: {br.ref_skipped_reason}")
        return br
    if current_remote != remote_sha:
        note = (
            f"branch {branch!r}: remote history advanced from another machine "
            f"(was {(_short(remote_sha))}, now {_short(current_remote)}); run 'jp fetch' first. "
            "Objects were uploaded but the ref was NOT moved."
        )
        br.ref_advanced = False
        br.ref_skipped_reason = "CAS conflict; remote ref advanced"
        result.warnings.append(note)
        ui.warn(note)
        return br

    # 6) Move the ref LAST -- a single PUT, the one atomic-enough mutation here.
    ref_path = paths.remote_path_for(prefix, _ref_remote_rel(branch))
    paths.assert_within_prefix(ref_path, prefix)
    try:
        api.put_file(ref_path, (tip + "\n").encode("ascii"))
    except (ApiError, JpError) as exc:
        br.ref_advanced = False
        br.ref_skipped_reason = f"ref PUT failed: {_msg(exc)}"
        result.warnings.append(f"branch {branch!r}: {br.ref_skipped_reason}")
        ui.warn(f"branch {branch!r}: {br.ref_skipped_reason}")
        return br
    br.ref_advanced = True
    return br


def mirror_history(root: Path, cfg: Config, api: Api) -> MirrorResult:
    """Mirror COMMITTED versioning history to ``<prefix>/__jp/`` on the remote.

    Runs UNDER :func:`jp.versioning.lock.versioning_lock` so it never interleaves
    with a concurrent commit/gc. For EVERY local branch we push the (incremental)
    set of reachable objects and then advance the remote ref under an optimistic
    compare-and-swap (see the module docstring for the full safety contract).

    BEST-EFFORT: this catches network/ApiError and records them as failures; it
    NEVER raises out in a way that could fail the data push. The returned
    :class:`MirrorResult` carries honest pushed/skipped/failed counts and whether
    each ref advanced, so the caller can report truthfully.
    """
    result = MirrorResult()
    root = Path(root).resolve()

    try:
        prefix = paths.validate_prefix(cfg.prefix)
    except JpError as exc:
        result.warnings.append(f"refusing to mirror: {_msg(exc)}")
        return result

    try:
        with versioning_lock(root):
            store = ObjectStore(root)
            heads = list_heads(root)
            if not heads:
                # No committed history at all -> nothing to mirror.
                return result
            # Sanity: confirm there is at least one reachable object (committed
            # history). reachable_objects (committed-only) is the authoritative set.
            committed = reachable_objects(root, store, include_staged=False)
            if not committed:
                return result
            result.ran = True
            max_blob_bytes = _max_blob_bytes(cfg)
            for branch, tip in sorted(heads.items()):
                br = _mirror_branch(
                    root, prefix, api, store, branch, tip, result, max_blob_bytes=max_blob_bytes
                )
                result.branches.append(br)
                result.pushed += br.pushed
                result.skipped += br.skipped
                result.failed += br.failed
    except JpError as exc:
        # A lock contention or other versioning error must NEVER fail the push.
        result.warnings.append(f"history mirror could not run: {_msg(exc)}")
        ui.warn(f"history mirror could not run: {_msg(exc)}")
    except OSError as exc:
        result.warnings.append(f"history mirror could not run: {exc}")
        ui.warn(f"history mirror could not run: {exc}")

    return result


def _msg(exc: BaseException) -> str:
    """Best-effort human message from an exception (JpError carries ``.message``)."""
    return getattr(exc, "message", None) or str(exc)


def _short(sha: str | None) -> str:
    """Short-hash display for a possibly-absent sha."""
    if not sha:
        return "(absent)"
    return sha[:12]

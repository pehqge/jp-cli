"""The tree/commit object model and the high-level add/commit operations.

This module sits on top of the content-addressed object store
(:mod:`jp.versioning.objects`), the ref/HEAD layer (:mod:`jp.versioning.refs`),
the staging area (:mod:`jp.versioning.staging`), and the per-repo write lock
(:mod:`jp.versioning.lock`). It introduces the two higher-level object kinds that
make jp git-shaped:

* a **tree** -- a FLAT (non-recursive) snapshot of the next commit's files, keyed
  by normalized POSIX relative path -> ``{"sha256", "size", "mode"}`` (a future
  ``"nb"`` sub-key is reserved for the notebook-hybrid work and tolerated on read
  but not produced here);
* a **commit** -- a tree pointer plus parent links, message, author, and time.

Both are stored as the object store's immutable, content-addressed bytes, encoded
as CANONICAL JSON (sorted keys, no whitespace, no trailing newline) so two
semantically identical objects hash to the SAME sha and therefore DEDUPLICATE to
a single stored object. That determinism is load-bearing: it is what lets an
unchanged tree across two commits cost zero extra storage, and it is what the
later notebook/diff/checkout tasks rely on.

Safety model (the same shared-machine, hostile-filesystem threat model as the
rest of the package):

* Every tree key is run through :func:`jp.paths.normalize_rel`, so a traversal-y
  path (``../x``, an absolute path, a drive path) can never be STORED in a tree
  -- it raises :class:`VersioningError` up front. The ``.git/`` hard-skip that
  protects against committing a git object store lives in :func:`stage_paths`.
* Every blob/tree/parent sha embedded in a tree or commit is validated as a
  lowercase 64-hex string before it is trusted; a malformed object raises rather
  than being silently coerced.
* All mutating, multi-step operations (:func:`create_commit`) run UNDER the
  per-repo :func:`jp.versioning.lock.versioning_lock` and advance the branch ref
  with a compare-and-swap (:func:`jp.versioning.refs.update_ref`), so two racing
  commits can never lose an update. The ordering is crash-safe: objects (blobs,
  tree, commit) are written FIRST and the ref is moved LAST, so a crash before
  the ref move leaves the new objects as harmless, unreferenced orphans and HEAD
  still resolves to the previous commit.
* The versioning store is entirely SEPARATE from the sync base
  (``.jp/index.json``); nothing here ever reads or writes that file.

Cross-platform: standard library only.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

from .. import __version__
from ..ignore import IgnoreSet
from ..paths import normalize_rel
from ..sync import scan_local
from . import refs
from .lock import versioning_lock
from .notebooks import is_notebook, normalized_sha
from .objects import ObjectStore, VersioningError
from .refs import validate_ref_name
from .staging import StagedEntry, Staging

# Format version embedded in every tree/commit object we write.
OBJ_VERSION = 1

# A valid embedded sha is exactly 64 lowercase hex chars (mirrors objects.py).
_SHA_RE = re.compile(r"[0-9a-f]{64}")
# A short sha PREFIX accepted by resolve_commitish: 4..63 lowercase hex chars.
_SHA_PREFIX_RE = re.compile(r"[0-9a-f]{4,63}")

# The first normalized path segment we refuse to ever stage/commit. Committing a
# git object store into jp's history would be catastrophic (huge, and it can hold
# secrets), so a path whose first segment is ".git" is hard-skipped in both add
# modes. Task 6 will generalize this into a proper versioning-ignore; a minimal
# first-segment skip is sufficient here.
_GIT_DIR = ".git"


def _validate_sha(sha: object, kind: str) -> None:
    """Reject anything that is not a lowercase 64-hex sha256.

    A locally defined twin of the object store's gate (we never import the private
    one) so a malformed sha embedded in a tree/commit raises loudly instead of
    being trusted. ``kind`` ("tree"/"parent") sharpens the error message.
    """
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise VersioningError(f"invalid {kind} id: expected 64 lowercase hex chars, got {sha!r}")


# --------------------------------------------------------------------------- #
# Tree objects
# --------------------------------------------------------------------------- #
def build_tree_bytes(entries: dict[str, dict]) -> bytes:
    """Return the CANONICAL JSON bytes of a flat tree built from ``entries``.

    ``entries`` maps a relative path to ``{"sha256", "size", "mode", [..]}``.
    Keys are normalized via :func:`jp.paths.normalize_rel` (so ``./a//b`` ->
    ``a/b``); a key that escapes the repo root raises :class:`VersioningError`
    rather than being stored. The bytes are deterministic (sorted keys, no
    whitespace, no trailing newline) so identical trees hash identically and
    therefore deduplicate in the object store.
    """
    normalized: dict[str, dict] = {}
    for rel, meta in entries.items():
        try:
            key = normalize_rel(rel)
        except Exception as exc:
            # A traversal-y / unsafe key must never be persisted in a tree.
            raise VersioningError(f"refusing to store an unsafe tree path {rel!r}: {exc}") from exc
        normalized[key] = _normalize_entry(rel, meta)
    obj = {"version": OBJ_VERSION, "entries": normalized}
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _normalize_entry(rel: str, meta: dict) -> dict:
    """Validate and canonicalize one tree entry's value dict."""
    if not isinstance(meta, dict):
        raise VersioningError(f"malformed tree entry for {rel!r}: not an object")
    sha = meta.get("sha256")
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise VersioningError(f"malformed tree entry for {rel!r}: bad sha256 {sha!r}")
    try:
        size = int(meta.get("size", 0))
    except (TypeError, ValueError) as exc:
        raise VersioningError(f"malformed tree entry for {rel!r}: bad size") from exc
    if size < 0:
        raise VersioningError(f"malformed tree entry for {rel!r}: negative size")
    out: dict = {"sha256": sha, "size": size, "mode": str(meta.get("mode", "file"))}
    # Reserve the optional notebook sub-key for the notebook-hybrid task: pass it
    # through untouched on round-trips, but do NOT implement any logic for it here.
    if "nb" in meta:
        out["nb"] = meta["nb"]
    return out


def write_tree(store: ObjectStore, entries: dict[str, dict]) -> str:
    """Write the tree for ``entries`` and return its sha (write-once / dedup)."""
    return store.write(build_tree_bytes(entries))


def read_tree(store: ObjectStore, sha: str) -> dict:
    """Load and re-validate the tree object named ``sha``; return its entries.

    Raises :class:`VersioningError` if the object is not a well-formed tree (wrong
    version, missing/!=dict ``entries``, an unsafe key, or a malformed per-entry
    value). The returned dict maps normalized rel path -> validated entry dict.
    """
    raw = store.read(sha)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise VersioningError(f"malformed tree object {sha}: not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise VersioningError(f"malformed tree object {sha}: not an object")
    if obj.get("version") != OBJ_VERSION:
        raise VersioningError(f"unsupported tree version in {sha}: {obj.get('version')!r}")
    raw_entries = obj.get("entries")
    if not isinstance(raw_entries, dict):
        raise VersioningError(f"malformed tree object {sha}: 'entries' is not an object")
    out: dict[str, dict] = {}
    for rel, meta in raw_entries.items():
        try:
            key = normalize_rel(rel)
        except Exception as exc:
            raise VersioningError(f"malformed tree object {sha}: unsafe key {rel!r}") from exc
        out[key] = _normalize_entry(rel, meta)
    return out


# --------------------------------------------------------------------------- #
# Commit objects
# --------------------------------------------------------------------------- #
def write_commit(
    store: ObjectStore,
    *,
    tree: str,
    parents: list[str],
    message: str,
    author: str,
    timestamp_epoch: int,
    jp_version: str,
) -> str:
    """Write a commit object and return its sha.

    ``tree`` and every entry of ``parents`` must be lowercase 64-hex shas (the
    root commit has ``parents == []``). The encoded bytes are canonical JSON, so a
    byte-identical commit deduplicates -- but in practice the embedded epoch/time
    make each real commit unique. The ISO-8601 ``time`` string is derived from the
    epoch in the local timezone so it is human-meaningful while ``epoch`` stays the
    authoritative machine-comparable field.
    """
    _validate_sha(tree, "tree")
    for p in parents:
        _validate_sha(p, "parent")
    iso = datetime.datetime.fromtimestamp(timestamp_epoch).astimezone().isoformat()
    obj = {
        "version": OBJ_VERSION,
        "tree": tree,
        "parents": list(parents),
        "message": message,
        "author": author,
        "time": iso,
        "epoch": int(timestamp_epoch),
        "jp": jp_version,
    }
    return store.write(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def read_commit(store: ObjectStore, sha: str) -> dict:
    """Load and re-validate the commit object named ``sha``.

    Validates the embedded tree sha and every parent sha as lowercase 64-hex, and
    that ``parents`` is a list. Raises :class:`VersioningError` on any malformed
    field. Returns the parsed commit dict.
    """
    raw = store.read(sha)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise VersioningError(f"malformed commit object {sha}: not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise VersioningError(f"malformed commit object {sha}: not an object")
    if obj.get("version") != OBJ_VERSION:
        raise VersioningError(f"unsupported commit version in {sha}: {obj.get('version')!r}")
    _validate_sha(obj.get("tree"), "tree")
    parents = obj.get("parents")
    if not isinstance(parents, list):
        raise VersioningError(f"malformed commit object {sha}: 'parents' is not a list")
    for p in parents:
        _validate_sha(p, "parent")
    return obj


def iter_history(store: ObjectStore, start_sha: str) -> Iterator[tuple[str, dict]]:
    """Yield ``(sha, commit)`` from ``start_sha`` back along ``parents[0]``.

    Follows the FIRST-parent chain (a linear history walk), stopping at the root
    commit (empty ``parents``). Guards against a corrupted/forged cycle: every
    visited sha is tracked and a repeat raises :class:`VersioningError` rather
    than looping forever.
    """
    seen: set[str] = set()
    cur: str | None = start_sha
    while cur is not None:
        if cur in seen:
            raise VersioningError(f"cycle detected in commit history at {cur}")
        seen.add(cur)
        commit = read_commit(store, cur)
        yield cur, commit
        parents = commit.get("parents") or []
        cur = parents[0] if parents else None


# --------------------------------------------------------------------------- #
# Revision resolution
# --------------------------------------------------------------------------- #
def _all_commit_shas(root: Path, store: ObjectStore) -> set[str]:
    """Collect every commit sha reachable from ALL branch refs + HEAD.

    Walks the first-parent history from each ref tip and from HEAD via
    :func:`iter_history`, deduplicating. This is the universe a sha PREFIX may
    resolve against. A ref pointing at a missing/corrupt object is skipped rather
    than aborting the whole resolution (best-effort gathering for prefix matching).
    """
    starts: set[str] = set()
    head = refs.resolve_head(root)
    if head:
        starts.add(head)
    heads_dir = Path(root) / ".jp" / refs.REFS_DIR / refs.HEADS_DIR
    if heads_dir.is_dir():
        for ref_file in heads_dir.iterdir():
            if not ref_file.is_file() or ref_file.is_symlink():
                continue
            try:
                tip = refs.read_ref(root, ref_file.name)
            except VersioningError:
                continue
            if tip:
                starts.add(tip)

    found: set[str] = set()
    for start in starts:
        try:
            for sha, _ in iter_history(store, start):
                found.add(sha)
        except VersioningError:
            # A corrupt/cyclic chain must not break prefix resolution overall.
            continue
    return found


def resolve_commitish(root: Path, store: ObjectStore, ref: str) -> str:
    """Resolve a user revision ``ref`` to a FULL commit sha (or raise).

    Accepted forms, in order:

    * ``"HEAD"`` -> the current commit (raises if HEAD is unborn -- no commits yet);
    * a branch name (validated, then read from ``refs/heads/<name>``);
    * a full 64-hex sha that names an EXISTING commit object;
    * a sha PREFIX of length >= 4, matched UNIQUELY against the shas of all commits
      reachable from every ref + HEAD (ambiguous -> raise; no match -> raise).

    Raises :class:`VersioningError` on an unborn HEAD, an unknown branch/sha, an
    ambiguous prefix, or a too-short / malformed ref. Read-only.
    """
    ref = (ref or "").strip()
    if not ref:
        raise VersioningError("empty revision")

    if ref == "HEAD":
        sha = refs.resolve_head(root)
        if sha is None:
            raise VersioningError("HEAD does not point at any commit yet (no commits)")
        return sha

    # A full 64-hex sha: must name an existing commit object.
    if len(ref) == 64 and _SHA_RE.fullmatch(ref):
        if not store.has(ref):
            raise VersioningError(f"unknown revision: no commit object {ref}")
        # Confirm it is actually a commit (not some blob/tree) so callers can trust it.
        read_commit(store, ref)
        return ref

    # A branch name (only if it is a valid ref name). We try this before prefix
    # matching so an unambiguous branch always wins; a name that is also a valid
    # hex prefix (e.g. "dead") is extremely unlikely as a branch but handled by
    # falling through to prefix resolution when no such branch exists.
    is_hex_prefix = _SHA_PREFIX_RE.fullmatch(ref) is not None
    try:
        validate_ref_name(ref)
        branch_ok = True
    except VersioningError:
        branch_ok = False
    if branch_ok:
        tip = refs.read_ref(root, ref)
        if tip is not None:
            return tip
        if not is_hex_prefix:
            raise VersioningError(f"unknown revision: no such branch or commit {ref!r}")

    # A sha prefix (>= 4 hex). Match uniquely against all reachable commit shas.
    if is_hex_prefix:
        candidates = sorted(s for s in _all_commit_shas(root, store) if s.startswith(ref))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise VersioningError(f"ambiguous revision {ref!r}: matches {len(candidates)} commits")
        raise VersioningError(f"unknown revision: {ref!r}")

    raise VersioningError(f"unknown revision: {ref!r}")


# --------------------------------------------------------------------------- #
# Tree diff
# --------------------------------------------------------------------------- #
def diff_trees(a_entries: dict, b_entries: dict) -> dict:
    """Compare two flat tree-entry maps FROM ``a`` TO ``b`` by blob sha.

    Returns ``{"added": [...], "modified": [...], "deleted": [...]}`` with each
    list SORTED. A path present only in ``b`` is *added*; present only in ``a`` is
    *deleted*; present in both with a different ``sha256`` is *modified*.
    """
    a_keys = set(a_entries)
    b_keys = set(b_entries)
    added = sorted(b_keys - a_keys)
    deleted = sorted(a_keys - b_keys)
    modified = sorted(
        k for k in (a_keys & b_keys) if _sha_of(a_entries[k]) != _sha_of(b_entries[k])
    )
    return {"added": added, "modified": modified, "deleted": deleted}


def _sha_of(entry: dict) -> str:
    return str(entry.get("sha256", "")) if isinstance(entry, dict) else ""


def working_vs_head(root: Path, cfg: object, ignore: IgnoreSet) -> set[str]:
    """Return the normalized rels where the WORKING tree differs from HEAD's tree.

    This is the OFFLINE "uncommitted changes" set the push commit-gate (Task 7)
    decides on: a path is included when adding/modifying/deleting it relative to
    the HEAD commit. It reuses the same primitives as ``jp status`` /
    ``create_commit`` -- :func:`read_commit`, :func:`read_tree`,
    :func:`_entries_from_working`, and :func:`diff_trees`.

    The notebook HYBRID rule is honored exactly as ``status`` honors it for the
    working-vs-staged diff: under the default ``hybrid`` mode a pure re-run of a
    ``.ipynb`` (same CODE, new outputs/execution-count) is NOT a change. We detect
    that by comparing the working file's NORMALIZED (outputs-free) sha against the
    HEAD entry's recorded ``nb.norm_sha`` and, when they match, reusing the HEAD
    entry verbatim so ``diff_trees`` (which compares by raw ``sha256``) sees no
    difference. Under ``full`` no ``nb`` key is recorded, so every byte change
    counts -- a re-run is a real change.

    Returns an EMPTY set when nothing differs (including the unborn-HEAD + empty-
    tree case). Reads objects/refs/working files but writes NOTHING. A
    ``VersioningError`` (corrupt history) propagates to the caller, which treats
    the gate defensively.
    """
    root = Path(root).resolve()
    store = ObjectStore(root)
    head_sha = refs.resolve_head(root)
    head_entries: dict[str, dict] = {}
    if head_sha:
        head_commit = read_commit(store, head_sha)
        head_entries = read_tree(store, head_commit["tree"])

    full = _notebook_outputs_mode(cfg) == "full"
    work_entries = _entries_from_working(root, cfg)
    if not full:
        # HYBRID notebook suppression: when only a notebook's outputs changed
        # (its normalized sha still matches HEAD's), reuse the HEAD entry so the
        # raw-sha diff treats it as unchanged -- mirroring status/preview_stage.
        for rel, head_meta in head_entries.items():
            if rel not in work_entries or not is_notebook(rel):
                continue
            head_norm = _entry_norm_sha(head_meta)
            work_norm = _entry_norm_sha(work_entries[rel])
            if head_norm and work_norm and head_norm == work_norm:
                work_entries[rel] = head_meta

    delta = diff_trees(head_entries, work_entries)
    return set(delta["added"]) | set(delta["modified"]) | set(delta["deleted"])


def _entry_norm_sha(meta: dict) -> str:
    """Extract a tree entry's recorded notebook normalized sha (``nb.norm_sha``)."""
    nb = meta.get("nb") if isinstance(meta, dict) else None
    return str(nb.get("norm_sha", "")) if isinstance(nb, dict) else ""


# --------------------------------------------------------------------------- #
# Author resolution
# --------------------------------------------------------------------------- #
def resolve_author(cfg: object) -> str:
    """Resolve the commit author, reading config defensively.

    Order: a truthy ``cfg.versioning_author`` attribute, else a truthy
    ``cfg.extra["versioning.author"]``, else the ``USER``/``USERNAME`` environment
    variable, else ``"unknown"``. Task 6 wires the real config key; this reads it
    defensively so it works before that lands.
    """
    attr = getattr(cfg, "versioning_author", "")
    if isinstance(attr, str) and attr.strip():
        return attr.strip()
    extra = getattr(cfg, "extra", None)
    if isinstance(extra, dict):
        val = extra.get("versioning.author", "")
        if isinstance(val, str) and val.strip():
            return val.strip()
    for var in ("USER", "USERNAME"):
        env = os.environ.get(var, "")
        if env.strip():
            return env.strip()
    return "unknown"


# --------------------------------------------------------------------------- #
# Staging
# --------------------------------------------------------------------------- #
def _is_git_path(rel: str) -> bool:
    """True if the normalized rel path's FIRST segment is ``.git`` (hard-skip)."""
    return rel.split("/", 1)[0] == _GIT_DIR


def _notebook_outputs_mode(cfg: object) -> str:
    """Read ``cfg.versioning_notebook_outputs`` defensively; default "hybrid".

    Only "full" disables the hybrid normalization; any other value (including a
    missing attribute or the deliberately-unsupported "strip") is treated as the
    safe, default "hybrid" so a caller that passes a bare object still works.
    """
    mode = getattr(cfg, "versioning_notebook_outputs", "hybrid")
    return "full" if mode == "full" else "hybrid"


# Default refuse-to-version blob threshold in MiB (mirrors config's default) used
# when a caller passes a bare object with no ``versioning_max_blob_mb`` attribute.
_DEFAULT_MAX_BLOB_MB = 100


def _max_blob_bytes(cfg: object) -> int:
    """Read ``cfg.versioning_max_blob_mb`` defensively; return the cap in BYTES.

    A missing / non-positive / non-numeric value falls back to the default so a
    bare object (or a tampered value that slipped past config sanitization) can
    never disable the guard or set a nonsensical cap.
    """
    raw = getattr(cfg, "versioning_max_blob_mb", _DEFAULT_MAX_BLOB_MB)
    try:
        mb = int(raw)
    except (TypeError, ValueError):
        mb = _DEFAULT_MAX_BLOB_MB
    if mb <= 0:
        mb = _DEFAULT_MAX_BLOB_MB
    return mb * 1024 * 1024


def _oversized(rel: str, abspath: Path, cfg: object) -> bool:
    """True iff ``abspath`` exceeds the max-blob cap; warn (once) and skip if so.

    COMMIT-TIME SIZE GUARD: an oversized working file is NOT staged, so the object
    store -- and therefore the remote history mirror -- never has to hold a giant
    blob. The warning tells the user exactly how to raise the threshold. A file we
    cannot stat is treated as in-bounds (the normal snapshot path will surface any
    real read error). The check reads only the size, never the body.
    """
    cap = _max_blob_bytes(cfg)
    try:
        size = abspath.stat().st_size
    except OSError:
        return False
    if size <= cap:
        return False
    cap_mb = cap // (1024 * 1024)
    size_mb = size / (1024 * 1024)
    from .. import ui

    ui.warn(
        f"{rel} is {size_mb:.0f} MiB > versioning.max_blob_mb={cap_mb}; not versioned. "
        "Raise versioning.max_blob_mb or wait for chunked packs."
    )
    return True


def _entry_from_file(store: ObjectStore, abspath: Path, rel: str, *, full: bool) -> StagedEntry:
    """Snapshot a working file into the object store, returning its StagedEntry.

    The blob is ALWAYS the ORIGINAL bytes, written AT STAGE TIME (git semantics),
    so a later edit before the commit cannot corrupt the snapshot -- and for a
    notebook this keeps checkout faithful (outputs included). Under the default
    HYBRID mode a ``.ipynb`` ALSO records ``nb_norm_sha`` (the sha of its
    outputs-free normalized form) so change detection can ignore a pure re-run;
    that is ``""`` for every non-notebook and for a ``.ipynb`` that is not actually
    a valid notebook. Under ``full`` we set ``nb_norm_sha = ""`` even for a valid
    notebook, so the tree carries NO "nb" key and the file is versioned byte-for-
    byte (every output change is a new version), exactly like a plain file.
    """
    sha = store.write_stream(abspath)
    try:
        st = abspath.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    nb_norm = ""
    if not full and is_notebook(rel):
        try:
            data = abspath.read_bytes()
        except OSError:
            data = b""
        nb_norm = normalized_sha(data) or ""
    return StagedEntry(sha256=sha, size=size, local_mtime=mtime, nb_norm_sha=nb_norm)


def _notebook_unchanged(rel: str, abspath: Path, staged: StagedEntry, *, full: bool) -> bool:
    """True iff ``rel`` is a notebook whose code is unchanged vs the staged entry.

    The HYBRID rule: a notebook counts as modified ONLY when its current normalized
    sha differs from the staged ``nb_norm_sha`` -- so a pure re-run (same code, new
    outputs) is reported as unchanged and is never re-staged. Returns False (i.e.
    "treat normally / restage") under ``full`` mode (every byte change is a real
    change there), for a non-notebook, for a staged entry that has no recorded
    normalized sha (older entry / opaque .ipynb / staged under full), or if the
    file cannot be read or is not actually a valid notebook on disk now.
    """
    if full or not is_notebook(rel) or not staged.nb_norm_sha:
        return False
    try:
        data = abspath.read_bytes()
    except OSError:
        return False
    cur = normalized_sha(data)
    return cur is not None and cur == staged.nb_norm_sha


def _tree_entry_for(entry: StagedEntry) -> dict:
    """Build the tree-entry dict for a StagedEntry, including the notebook sub-key.

    A non-empty ``nb_norm_sha`` (a notebook) adds ``"nb": {"norm_sha": <sha>}`` so
    later diff/checkout can recognise a notebook and diff it outputs-free; a plain
    file's entry keeps the bare ``{sha256, size, mode}`` shape.
    """
    out: dict = {"sha256": entry.sha256, "size": entry.size, "mode": "file"}
    if entry.nb_norm_sha:
        out["nb"] = {"norm_sha": entry.nb_norm_sha}
    return out


def stage_paths(
    root: Path,
    cfg: object,
    ignore: IgnoreSet,
    paths: list[str],
    *,
    all_files: bool,
    dry_run: bool,
) -> dict:
    """Stage paths into ``.jp/staged.json`` using the git index (full-tree) model.

    ``.jp/staged.json`` holds the FULL content of the next commit's tree (not a
    delta). Two modes:

    * ``all_files`` (``-A``): make staging EQUAL the working tree -- snapshot every
      eligible file via :meth:`ObjectStore.write_stream` and REMOVE any staged path
      that no longer exists in the working tree (staging the deletion).
    * explicit ``paths``: each path that exists (and is eligible) is snapshotted;
      a named path that does NOT exist but IS currently staged is removed (staging
      that one deletion); a named path matching neither is a per-path error.

    The ``.git/`` directory is HARD-SKIPPED in both modes. ``dry_run`` computes and
    returns the same summary but writes NOTHING (no blobs, no objects, no staging
    file). Returns a summary dict the command layer prints.

    CONCURRENCY: a real (non-dry-run) stage is a load->mutate->save of
    ``.jp/staged.json``, so it runs UNDER :func:`jp.versioning.lock.versioning_lock`
    -- two concurrent ``jp add`` runs (or an ``add`` racing the ``-A`` step inside a
    locked ``jp commit -A``) would otherwise last-writer-win and silently drop
    staged entries. A ``dry_run`` writes nothing, so it stays lock-free. Callers
    that ALREADY hold the lock (e.g. :func:`create_commit`'s ``stage_all`` step)
    must call :func:`_stage_paths_locked` directly -- the lock is NON-REENTRANT, so
    re-taking it here would self-deadlock (raise "another ... in progress").
    """
    if dry_run:
        # A dry run writes nothing, so it is safe lock-free.
        return _stage_paths_locked(root, cfg, ignore, paths, all_files=all_files, dry_run=dry_run)
    with versioning_lock(root):
        return _stage_paths_locked(root, cfg, ignore, paths, all_files=all_files, dry_run=dry_run)


def _stage_paths_locked(
    root: Path,
    cfg: object,
    ignore: IgnoreSet,
    paths: list[str],
    *,
    all_files: bool,
    dry_run: bool,
) -> dict:
    """The MUTATING body of :func:`stage_paths`; assumes the caller holds the lock.

    Identical behavior to :func:`stage_paths` but does NOT take
    :func:`versioning_lock` itself. Use this from a context that already holds the
    lock (the lock is NON-REENTRANT). External callers should use the public
    :func:`stage_paths` wrapper, which acquires the lock for a real stage.
    """
    root = Path(root).resolve()
    store = ObjectStore(root)
    staging = Staging.load(root)
    working = scan_local(root, ignore)  # {rel: abspath}; already skips .jp/ + symlinks
    full = _notebook_outputs_mode(cfg) == "full"

    staged_paths: list[str] = []
    removed_paths: list[str] = []
    errors: list[str] = []

    if all_files:
        # 1) Snapshot every eligible working-tree file (skipping .git/).
        for rel, abspath in sorted(working.items()):
            if _is_git_path(rel):
                continue
            # SIZE GUARD: an oversized working file is never versioned (it would
            # bloat the store and the remote mirror). Warned + skipped here so it
            # simply does not enter the next commit's tree.
            if _oversized(rel, abspath, cfg):
                continue
            # HYBRID notebook suppression: a notebook whose code is unchanged (only
            # its outputs/execution-count churned, i.e. a pure re-run) is NOT
            # re-staged -- the existing staged original blob and norm sha stand.
            # Disabled under "full", where every byte change is a real change.
            existing = staging.get(rel)
            if existing is not None and _notebook_unchanged(rel, abspath, existing, full=full):
                continue
            if not dry_run:
                staging.set(rel, _entry_from_file(store, abspath, rel, full=full))
            staged_paths.append(rel)
        # 2) Stage deletions: anything currently staged that is gone from the tree.
        # sorted() already snapshots the keys, so mutating inside the loop is safe.
        for rel in sorted(staging.entries):
            if rel not in working or _is_git_path(rel):
                if not dry_run:
                    staging.remove(rel)
                removed_paths.append(rel)
    else:
        for raw in paths:
            try:
                rel = normalize_rel(raw)
            except Exception as exc:
                errors.append(f"{raw}: {exc}")
                continue
            if _is_git_path(rel):
                # Never stage a git object store; report it as a skipped error so a
                # caller asking only for .git/ exits non-zero.
                errors.append(f"{raw}: refusing to stage paths under {_GIT_DIR}/")
                continue
            if rel in working:
                # SIZE GUARD: refuse to version an oversized file even when it is
                # named explicitly (warned + reported as a skipped error so a
                # caller asking only for that path exits non-zero).
                if _oversized(rel, working[rel], cfg):
                    errors.append(f"{raw}: exceeds versioning.max_blob_mb; not versioned")
                    continue
                existing = staging.get(rel)
                # HYBRID: a pure re-run of an already-staged notebook is a no-op
                # even when named explicitly (its code is unchanged). Under "full"
                # it is restaged like any other changed file.
                if existing is not None and _notebook_unchanged(
                    rel, working[rel], existing, full=full
                ):
                    continue
                if not dry_run:
                    staging.set(rel, _entry_from_file(store, working[rel], rel, full=full))
                staged_paths.append(rel)
            elif rel in staging.entries:
                # Tracked but no longer present + explicitly named -> stage delete.
                if not dry_run:
                    staging.remove(rel)
                removed_paths.append(rel)
            else:
                errors.append(f"{raw}: no such file in the working tree")

    if not dry_run and (staged_paths or removed_paths):
        staging.save()

    return {
        "staged": staged_paths,
        "removed": removed_paths,
        "errors": errors,
        "dry_run": dry_run,
        "all_files": all_files,
    }


def preview_stage(root: Path, cfg: object, ignore: IgnoreSet) -> dict:
    """Compute the working-tree-vs-staging delta WITHOUT writing anything.

    Used by ``jp add`` with no paths and no ``-A`` (a read-only preview of what an
    ``-A`` would stage). Returns ``{"added", "modified", "deleted"}`` of normalized
    rel paths, comparing the current staging snapshot to the working tree by sha.
    """
    root = Path(root).resolve()
    staging = Staging.load(root)
    working = scan_local(root, ignore)
    full = _notebook_outputs_mode(cfg) == "full"

    staged_entries: dict[str, dict] = {
        rel: _tree_entry_for(e) for rel, e in staging.entries.items() if not _is_git_path(rel)
    }
    work_entries: dict[str, dict] = {}
    from ..sync import sha256_file

    for rel, abspath in working.items():
        if _is_git_path(rel):
            continue
        # HYBRID: a pure notebook re-run reports as UNCHANGED in the preview. We
        # surface this by reusing the staged entry verbatim when only outputs
        # changed, so diff_trees (which compares by sha256) sees no difference.
        # Under "full" this suppression is off and the raw sha drives the diff.
        existing = staging.get(rel)
        if existing is not None and _notebook_unchanged(rel, abspath, existing, full=full):
            work_entries[rel] = _tree_entry_for(existing)
            continue
        try:
            work_entries[rel] = {
                "sha256": sha256_file(abspath),
                "size": abspath.stat().st_size,
                "mode": "file",
            }
        except OSError:
            continue
    return diff_trees(staged_entries, work_entries)


# --------------------------------------------------------------------------- #
# High-level commit
# --------------------------------------------------------------------------- #
def create_commit(
    root: Path,
    cfg: object,
    *,
    message: str,
    stage_all: bool,
    allow_empty: bool,
    dry_run: bool,
) -> dict:
    """Create a commit from the staging area; the crash-safe, locked commit op.

    Order (all under :func:`versioning_lock`):

    1. ``refs.check_format`` then ``refs.init_versioning``.
    2. If ``stage_all``: run an ``-A`` stage first via the LOCK-FREE
       :func:`_stage_paths_locked` (we already hold the non-reentrant lock).
    3. Load staging, build the tree entries. Refuse an EMPTY commit (one whose tree
       equals the parent's tree -- no add/modify/delete) unless ``allow_empty``.
    4. Defensively ensure every staged blob exists in the store (re-snapshot from
       the working file if it is still present, else raise).
    5. Write the tree, 6. determine parents from HEAD, 7. write the commit,
    8. advance the branch with a compare-and-swap (or move detached HEAD).

    Objects are written BEFORE the ref moves, so a crash between 7 and 8 leaves the
    new objects as harmless orphans and HEAD still resolves to the old commit.

    ``dry_run`` computes everything that WOULD happen -- the would-be tree built
    from the working tree, the diff vs the parent, and the parents -- but writes
    NOTHING (no blobs, objects, or ref). Returns a summary dict with the keys
    ``sha, short, tree, added, modified, deleted, parents`` (``sha``/``tree`` are
    empty strings in a dry run).
    """
    root = Path(root).resolve()
    with versioning_lock(root):
        refs.check_format(root)
        refs.init_versioning(root)
        store = ObjectStore(root)

        if stage_all:
            # We ALREADY hold versioning_lock here, and it is NON-REENTRANT, so we
            # call the lock-free internal stage directly -- the public stage_paths
            # would re-acquire the lock and self-deadlock (raise "in progress").
            _stage_paths_locked(
                root, cfg, IgnoreSet.from_root(root), [], all_files=True, dry_run=dry_run
            )

        # Determine the parent commit + its tree entries (for the empty-commit and
        # diff checks). resolve_head returns None on an unborn branch.
        parent_sha = refs.resolve_head(root)
        parents = [parent_sha] if parent_sha else []
        parent_tree_entries: dict[str, dict] = {}
        if parent_sha:
            parent_commit = read_commit(store, parent_sha)
            parent_tree_entries = read_tree(store, parent_commit["tree"])

        # Build the would-be tree entries.
        if dry_run:
            # Faithful preview without writing objects: with -A the tree is the
            # full working tree; without -A it is the current staging snapshot
            # (stage_paths above was itself a no-op dry-run, so staging is intact).
            if stage_all:
                entries = _entries_from_working(root, cfg)
            else:
                staging = Staging.load(root)
                entries = {
                    rel: _tree_entry_for(e)
                    for rel, e in staging.entries.items()
                    if not _is_git_path(rel)
                }
        else:
            staging = Staging.load(root)
            entries = {}
            for rel, e in staging.entries.items():
                if _is_git_path(rel):
                    continue  # never commit a git object store
                _ensure_blob(store, root, rel, e)
                entries[rel] = _tree_entry_for(e)

        delta = diff_trees(parent_tree_entries, entries)
        is_empty = not (delta["added"] or delta["modified"] or delta["deleted"])
        if is_empty and not allow_empty:
            raise VersioningError("nothing to commit (the tree is unchanged)")

        if dry_run:
            return {
                "sha": "",
                "short": "",
                "tree": "",
                "added": delta["added"],
                "modified": delta["modified"],
                "deleted": delta["deleted"],
                "parents": parents,
            }

        tree_sha = write_tree(store, entries)
        commit_sha = write_commit(
            store,
            tree=tree_sha,
            parents=parents,
            message=message,
            author=resolve_author(cfg),
            timestamp_epoch=int(time.time()),
            jp_version=__version__,
        )

        # Advance the branch LAST so a crash before this leaves orphan objects, not
        # corrupt history. update_ref is a compare-and-swap against the parent sha.
        head = refs.read_head(root)
        if head is not None and head.symbolic and head.branch is not None:
            refs.update_ref(root, head.branch, commit_sha, expected=parent_sha)
        else:
            # Detached HEAD (or, defensively, a missing HEAD): point straight at it.
            refs.set_head_detached(root, commit_sha)

        return {
            "sha": commit_sha,
            "short": commit_sha[:12],
            "tree": tree_sha,
            "added": delta["added"],
            "modified": delta["modified"],
            "deleted": delta["deleted"],
            "parents": parents,
        }


def _ensure_blob(store: ObjectStore, root: Path, rel: str, entry: StagedEntry) -> None:
    """Defensively guarantee a staged blob exists in the store before committing.

    Blobs are normally written at add time; this is belt-and-suspenders against a
    store that lost the object. If it is missing we re-snapshot from the working
    file (if present and matching the staged sha); otherwise we raise a clear error
    rather than write a commit that references a missing blob.
    """
    if store.has(entry.sha256):
        return
    abspath = root / rel
    if abspath.is_file() and not abspath.is_symlink():
        written = store.write_stream(abspath)
        if written == entry.sha256:
            return
        raise VersioningError(
            f"staged content for {rel!r} changed on disk (staged {entry.sha256}, now {written}); "
            "re-run 'jp add' for this path"
        )
    raise VersioningError(
        f"staged blob for {rel!r} is missing from the object store and the working "
        f"file is gone; re-run 'jp add' for this path"
    )


def _entries_from_working(root: Path, cfg: object) -> dict[str, dict]:
    """Build would-be tree entries from the WORKING tree without writing objects.

    Used by the dry-run commit so the preview is faithful (it reflects the files
    that an ``-A`` + commit would capture) while touching nothing on disk. Hashes
    each eligible file in place; never writes a blob.
    """
    from ..sync import sha256_file

    ignore = IgnoreSet.from_root(root)
    working = scan_local(root, ignore)
    full = _notebook_outputs_mode(cfg) == "full"
    entries: dict[str, dict] = {}
    for rel, abspath in working.items():
        if _is_git_path(rel):
            continue
        try:
            meta: dict = {
                "sha256": sha256_file(abspath),
                "size": abspath.stat().st_size,
                "mode": "file",
            }
            # Under HYBRID a notebook carries an "nb" key (so diff/checkout treat it
            # outputs-free); under "full" we omit it, so the would-be tree matches
            # what a real "full" stage produces and the notebook diffs as a plain
            # file (every byte change shows).
            if not full and is_notebook(rel):
                try:
                    nb_norm = normalized_sha(abspath.read_bytes())
                except OSError:
                    nb_norm = None
                if nb_norm:
                    meta["nb"] = {"norm_sha": nb_norm}
            entries[rel] = meta
        except OSError:
            continue
    return entries

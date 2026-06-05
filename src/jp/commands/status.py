"""``jp status`` -- read-only summary of local vs remote vs index state.

Guaranteed read-only: it calls ``sync.diff`` which never writes locally or
remotely and never touches the index. When versioning is ACTIVE (HEAD resolves to
a commit OR a ``staged.json`` exists), it ALSO appends a purely OFFLINE
"Versioning" section (staged / not-staged), computed without any API call and
without ever reading or writing ``.jp/index.json``. If versioning is inactive the
output is byte-identical to the pre-versioning status -- the opt-in invariant.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import paths, ui
from ..errors import EXIT_OK, EXIT_SAFETY
from ..sync import Change
from . import _context
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("status", help="show local/remote sync status (read-only)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    api = _context.build_api(ctx.cfg)
    from .. import sync

    states = sync.diff(ctx.root, ctx.cfg, api, ctx.index, ctx.ignore)

    buckets: dict[Change, list[str]] = {c: [] for c in Change}
    hidden: list[str] = []
    for st in states:
        if st.change == Change.UNCHANGED:
            continue
        if (
            paths.is_hidden(st.rel)
            and st.local_exists
            and st.change
            in (
                Change.LOCAL_NEW,
                Change.LOCAL_MODIFIED,
                Change.CONFLICT,
            )
        ):
            hidden.append(st.rel)
            continue
        buckets[st.change].append(st.rel)

    _section("local-only (to push)", buckets[Change.LOCAL_NEW])
    _section("locally modified (to push)", buckets[Change.LOCAL_MODIFIED])
    _section("remote-only (to pull)", buckets[Change.REMOTE_NEW])
    _section("remotely modified (to pull)", buckets[Change.REMOTE_MODIFIED])
    _section("CONFLICTS (resolve manually)", buckets[Change.CONFLICT])
    _section("hidden/dotfiles (will be skipped on push)", hidden)

    total = sum(len(v) for v in buckets.values()) + len(hidden)
    if total == 0:
        ui.success("clean: everything is in sync")
    else:
        ui.info(f"{total} path(s) differ")

    # OPT-IN: append the local versioning section ONLY when versioning is active.
    # Inactive (no HEAD, no staged.json) -> nothing is emitted, so the output is
    # identical to the pre-versioning status. Computed offline; never touches the
    # API or the sync base index.
    _versioning_section(ctx.root, ctx.cfg, ctx.ignore)

    return EXIT_SAFETY if buckets[Change.CONFLICT] else EXIT_OK


def _section(title: str, items: list[str]) -> None:
    if not items:
        return
    ui.heading(title + ":")
    ui.bullets(sorted(items), indent="    ")


# --------------------------------------------------------------------------- #
# Local versioning section (offline)
# --------------------------------------------------------------------------- #
def _versioning_active(root: Path) -> bool:
    """True iff versioning is in use: HEAD resolves OR a staging file exists.

    Read-only and crash-safe: a missing format/HEAD is fine (not active), and a
    corrupt format/HEAD degrades to "not active" rather than raising, so a
    first-run / fresh repo never tracebacks here.
    """
    from ..versioning import refs
    from ..versioning.objects import VersioningError
    from ..versioning.staging import Staging

    if Staging(root).path.is_file():
        return True
    try:
        refs.check_format(root)
        return refs.resolve_head(root) is not None
    except VersioningError:
        # A corrupt marker/HEAD must not crash status; report inactive.
        return False


def _versioning_section(root: Path, cfg: object, ignore: object) -> None:
    """Print the offline staged / not-staged versioning section, if active.

    * "staged (to be committed)": ``diff_trees(HEAD_tree, staged_tree)`` ->
      added/modified/deleted (HEAD_tree is ``{}`` on an unborn HEAD).
    * "not staged": the WORKING tree vs the staged tree. For a hybrid notebook a
      pure re-run (same code, new outputs) is reported separately as
      "outputs only (not staged)" rather than "modified"; under ``full`` every
      byte change is a plain modification.

    Entirely read-only: it loads objects/refs/staging but writes nothing and never
    reads ``.jp/index.json``. Any versioning corruption degrades gracefully (the
    section is skipped) so status never tracebacks on a damaged repo.
    """
    if not _versioning_active(root):
        return

    from ..sync import sha256_file
    from ..versioning import refs, repo
    from ..versioning.notebooks import is_notebook, normalized_sha
    from ..versioning.objects import ObjectStore, VersioningError
    from ..versioning.staging import Staging

    full = repo._notebook_outputs_mode(cfg) == "full"
    try:
        store = ObjectStore(root)
        staging = Staging.load(root)

        # HEAD tree ({} if unborn) and the staged tree.
        head_entries: dict[str, dict] = {}
        head_sha = refs.resolve_head(root)
        if head_sha:
            head_commit = repo.read_commit(store, head_sha)
            head_entries = repo.read_tree(store, head_commit["tree"])
        staged_entries: dict[str, dict] = {
            rel: repo._tree_entry_for(e)
            for rel, e in staging.entries.items()
            if not repo._is_git_path(rel)
        }

        staged_delta = repo.diff_trees(head_entries, staged_entries)

        # Working vs staged. We classify each working path against the staged tree.
        working = _scan_working(root, ignore)
        modified: list[str] = []
        outputs_only: list[str] = []
        new: list[str] = []
        for rel, abspath in working.items():
            if repo._is_git_path(rel):
                continue
            existing = staging.get(rel)
            if existing is None:
                new.append(rel)
                continue
            # Hybrid notebook: a pure re-run is "outputs only", not a real change.
            if (
                not full
                and is_notebook(rel)
                and existing.nb_norm_sha
                and _safe_norm(abspath, normalized_sha) == existing.nb_norm_sha
            ):
                # Only flag it when the bytes actually differ (a true re-run);
                # an untouched notebook is simply unchanged.
                if _safe_sha(abspath, sha256_file) != existing.sha256:
                    outputs_only.append(rel)
                continue
            if _safe_sha(abspath, sha256_file) != existing.sha256:
                modified.append(rel)
        deleted_working = [
            rel for rel in staging.entries if rel not in working and not repo._is_git_path(rel)
        ]
    except (VersioningError, OSError):
        # Damaged versioning state must never crash status; skip the section.
        return

    ui.info("")
    ui.heading("Versioning:")
    _section(
        "  staged (to be committed)",
        staged_delta["added"] + staged_delta["modified"] + staged_delta["deleted"],
    )
    _section("  modified (not staged)", modified)
    _section("  outputs only (not staged)", outputs_only)
    _section("  new (not staged)", new)
    _section("  deleted (not staged)", deleted_working)


def _scan_working(root: Path, ignore: object) -> dict[str, Path]:
    """Scan the working tree -> {rel: abspath} (skips .jp/ + symlinks + ignored)."""
    from ..ignore import IgnoreSet
    from ..sync import scan_local

    if not isinstance(ignore, IgnoreSet):
        ignore = IgnoreSet.from_root(root)
    return scan_local(root, ignore)


def _safe_sha(abspath: Path, hasher) -> str:
    try:
        return hasher(abspath)
    except OSError:
        return ""


def _safe_norm(abspath: Path, normalizer) -> str:
    try:
        return normalizer(abspath.read_bytes()) or ""
    except OSError:
        return ""

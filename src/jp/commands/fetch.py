"""``jp fetch [BRANCH...]`` -- bring committed history DOWN from the remote backup.

Fetches the version history that ``jp push`` (mirror) backed up to
``<prefix>/__jp/`` on the (POSSIBLY HOSTILE) remote, into the LOCAL object store.
Every downloaded object is byte-VERIFIED before it is placed (see
:mod:`jp.versioning.fetch`), so a corrupt/tampered remote object aborts the fetch
with a non-zero exit and never plants garbage locally.

The local ref is advanced ONLY on a proven fast-forward; diverged history is
reported but never silently rewritten. With no BRANCH argument the configured
default branch (``main``) is fetched. Read-mostly: it writes only verified objects
and a fast-forwarded ref. Requires versioning to be set up enough to know the
remote (``cfg.prefix``).
"""

from __future__ import annotations

import argparse

from .. import ui
from ..errors import EXIT_OK, EXIT_PARTIAL
from ..versioning import fetch as fetch_mod
from ..versioning.fetch import FetchResult
from . import _context
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "fetch",
        help="download committed version history from the remote backup (verified)",
    )
    p.add_argument(
        "branches",
        nargs="*",
        help="branch name(s) to fetch (default: the configured default branch)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    api = _context.build_api(ctx.cfg)

    branches = list(args.branches) or None
    # A verification failure raises VersioningError -> the CLI maps it to a
    # non-zero exit, exactly as the security contract requires. We do NOT catch it.
    result = fetch_mod.fetch_history(ctx.root, ctx.cfg, api, branches=branches)
    return _report(result)


def _report(result: FetchResult) -> int:
    """Print an honest per-branch summary; return the process exit code.

    Short (12-char) hashes throughout. Diverged / absent-branch notes are surfaced
    as warnings. A verification failure would have already raised before reaching
    here, so this only reports the success/no-op/diverged cases.
    """
    for br in result.branches:
        tip = _short(br.remote_sha)
        if br.ref_updated:
            ui.success(f"{br.branch}: fetched {br.downloaded} object(s) -> {tip}")
        elif br.ref_skipped_reason == "already up to date":
            ui.info(f"{br.branch}: already up to date ({tip})")
        elif br.ref_skipped_reason.startswith("local and remote history diverged"):
            ui.warn(
                f"{br.branch}: {br.ref_skipped_reason} ({tip}); objects downloaded "
                "but the local ref was NOT moved -- reconcile manually"
            )
        elif br.ref_skipped_reason.startswith("remote is behind"):
            ui.info(f"{br.branch}: remote is behind local; nothing to update")
        elif br.ref_skipped_reason == "remote ref absent/corrupt":
            ui.warn(f"{br.branch}: no version history found on the remote backup")
        else:
            ui.info(f"{br.branch}: {br.downloaded} downloaded, {br.skipped} already local")

    ui.info(
        f"fetch: {result.downloaded} downloaded, {result.skipped} already local, "
        f"{len(result.branches)} branch(es)"
    )
    # A diverged branch is a soft partial outcome the user must reconcile.
    diverged = any(
        b.ref_skipped_reason.startswith("local and remote history diverged")
        for b in result.branches
    )
    return EXIT_PARTIAL if diverged else EXIT_OK


def _short(sha: str) -> str:
    """Short-hash display (12 chars); ``(none)`` for an empty/absent sha."""
    return sha[:12] if sha else "(none)"

"""``jp restore [-f/--force]`` -- rebuild LOCAL history from the remote backup.

Recovery command for the case where the local ``.jp`` (objects + refs) was lost
but the remote ``<prefix>/__jp/`` backup survived. It runs
:func:`jp.versioning.fetch.fetch_history` (downloading + byte-VERIFYING every
reachable object from the POSSIBLY HOSTILE remote) and then, if HEAD resolves,
checks out HEAD into the working tree via the Task-5 apply.

The working dir COMMONLY still holds the user's files (only ``.jp`` was deleted),
so the checkout RESPECTS its safety gates: an empty dir gets everything written,
but a file with uncommitted local edits BLOCKS the restore unless ``--force`` is
passed. A corrupt/tampered remote object aborts the whole restore with a non-zero
exit before any working-tree write (the fetch verification contract).
"""

from __future__ import annotations

import argparse
import sys

from .. import ui
from ..errors import EXIT_OK, EXIT_PARTIAL, SafetyError
from ..versioning import fetch as fetch_mod
from . import _context
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "restore",
        help="rebuild local version history from the remote backup, then check out HEAD",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="overwrite working files that have uncommitted local changes",
    )
    p.set_defaults(func=run)


def _make_confirm(assume_yes: bool):
    """Build the untracked-extra deletion confirmer for the checkout layer.

    restore never passes ``--remove-extra`` to the checkout, so this is never
    actually consulted; provided for symmetry with ``jp checkout`` and to refuse
    safely in a non-tty if a future change wires extra-removal in.
    """

    def _confirm(rels: list[str]) -> bool:
        if assume_yes:
            return True
        if not sys.stdin.isatty():
            raise SafetyError("refusing to delete untracked files in a non-interactive shell")
        return ui.confirm("delete these untracked files?", default=False)

    return _confirm


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    api = _context.build_api(ctx.cfg)

    ui.heading("restoring version history from the remote backup")
    # A verification failure raises (VersioningError) -> non-zero exit; a dirty
    # working file raises SafetyError from the checkout gate. We do NOT swallow
    # either -- the CLI maps them to the right exit code.
    result = fetch_mod.restore(
        ctx.root, ctx.cfg, api, force=args.force, confirm_delete_untracked=_make_confirm(False)
    )

    fr = result.fetch
    ui.info(
        f"history: {fr.downloaded} object(s) downloaded, {fr.skipped} already local, "
        f"{len(fr.branches)} branch(es)"
    )
    for w in fr.warnings:
        ui.warn(w)

    if not result.head_resolved:
        ui.warn(
            "no version history was found on the remote backup; "
            "nothing was checked out (HEAD does not resolve to a commit)"
        )
        return EXIT_OK

    co = result.checkout
    assert co is not None  # head_resolved implies a checkout ran
    if co.failures:
        for rel, err in co.failures:
            ui.error(f"{rel}: {err}")
        ui.warn("restore finished with per-file failures; see above")
        return EXIT_PARTIAL

    ui.success(
        f"restored history and working tree ({len(co.written)} file(s) written, "
        f"{len(co.skipped)} already current)"
    )
    return EXIT_OK

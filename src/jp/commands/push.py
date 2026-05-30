"""``jp push`` -- upload local changes to the remote. Never deletes."""

from __future__ import annotations

import argparse

from .. import sync, ui
from ..errors import EXIT_OK, EXIT_PARTIAL, EXIT_SAFETY
from . import _context
from ._context import load_repo
from ._report import report_outcome


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("push", help="upload local changes to the remote (no deletes)")
    p.add_argument(
        "--dry-run", action="store_true", help="show what would be pushed; write nothing"
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    api = _context.build_api(ctx.cfg)

    ui.heading(f"push -> {ctx.cfg.prefix}{' (dry-run)' if args.dry_run else ''}")
    outcome = sync.push(ctx.root, ctx.cfg, api, ctx.index, ctx.ignore, dry_run=args.dry_run)
    report_outcome("push", outcome, dry_run=args.dry_run)

    if outcome.had_failures:
        return EXIT_PARTIAL
    if outcome.had_conflicts:
        # Conflicts are a safety refusal: signal them in the exit code.
        return EXIT_SAFETY
    return EXIT_OK

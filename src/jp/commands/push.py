"""``jp push`` -- upload local changes to the remote.

Additive by default (never deletes). With mirror mode on (config ``mirror`` or
``--mirror``), remote files that no longer exist locally become deletion
candidates -- and jp asks, file by file, before removing any of them.
"""

from __future__ import annotations

import argparse

from .. import sync
from ..errors import EXIT_OK, EXIT_PARTIAL, EXIT_SAFETY
from . import _context, _mirror
from ._context import load_repo
from ._report import report_outcome


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "push", help="upload local changes (additive; mirror deletes are opt-in)"
    )
    p.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    p.add_argument(
        "--mirror",
        dest="mirror",
        action="store_true",
        default=None,
        help="enable mirror deletes for this run (overrides config)",
    )
    p.add_argument(
        "--no-mirror",
        dest="mirror",
        action="store_false",
        help="disable mirror deletes for this run (overrides config)",
    )
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="in mirror mode, delete all candidates without prompting",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    api = _context.build_api(ctx.cfg)
    outcome = sync.push(ctx.root, ctx.cfg, api, ctx.index, ctx.ignore, dry_run=args.dry_run)

    mirror = ctx.cfg.mirror if args.mirror is None else args.mirror
    if mirror:
        _mirror.handle("remote", ctx, api, outcome, yes=args.yes, dry_run=args.dry_run)

    report_outcome("push", outcome, dry_run=args.dry_run)
    if outcome.had_conflicts:
        return EXIT_SAFETY
    return EXIT_PARTIAL if outcome.had_failures else EXIT_OK

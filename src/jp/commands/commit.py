"""``jp commit`` -- record the staged tree as a new commit (offline).

Builds a tree from the staging area, writes a commit object, and advances the
current branch with a compare-and-swap -- all under the per-repo versioning lock
and in a crash-safe order (objects first, ref last). Offline: it never touches the
remote or the sync base (``.jp/index.json``).
"""

from __future__ import annotations

import argparse

from .. import ui
from ..errors import EXIT_OK
from ..versioning import repo
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("commit", help="record the staged tree as a commit (offline)")
    p.add_argument("-m", "--message", default="", help="commit message (required)")
    p.add_argument(
        "-A", "--all", action="store_true", help="stage the whole working tree before committing"
    )
    p.add_argument(
        "--allow-empty", action="store_true", help="allow a commit whose tree is unchanged"
    )
    p.add_argument(
        "-n", "--dry-run", action="store_true", help="show what would be committed; write nothing"
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    # -m is required and must be non-empty (mirror argparse-style error handling).
    message = (args.message or "").strip()
    if not message:
        from ..errors import UsageError

        raise UsageError("a commit message is required: use -m/--message")

    ctx = load_repo()
    result = repo.create_commit(
        ctx.root,
        ctx.cfg,
        message=message,
        stage_all=args.all,
        allow_empty=args.allow_empty,
        dry_run=args.dry_run,
    )

    a, m, d = len(result["added"]), len(result["modified"]), len(result["deleted"])
    total = a + m + d
    counts = f"{total} file(s) (+{a} ~{m} -{d})"
    if args.dry_run:
        ui.info(f"would commit {counts}")
    else:
        ui.success(f"[{result['short']}] {counts}")
    return EXIT_OK

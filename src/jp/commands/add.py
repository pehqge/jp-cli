"""``jp add`` -- stage files into the versioning staging area (offline).

Mirrors git's ``add``: it snapshots file content into the object store AT ADD
TIME and records it in ``.jp/staged.json`` (the full content of the next commit's
tree, not a delta). With no paths and no ``-A`` it is a READ-ONLY preview of what
an ``-A`` would stage. Entirely offline -- it never touches the remote or the sync
base (``.jp/index.json``).
"""

from __future__ import annotations

import argparse

from .. import ui
from ..errors import EXIT_OK, EXIT_USAGE
from ..versioning import repo
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("add", help="stage files for the next commit (offline)")
    p.add_argument("paths", nargs="*", help="files to stage (relative to the repo root)")
    p.add_argument("-A", "--all", action="store_true", help="stage the whole working tree")
    p.add_argument(
        "-n", "--dry-run", action="store_true", help="show what would be staged; write nothing"
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()

    # No paths and no -A: a read-only preview of the working-tree-vs-staging delta.
    if not args.paths and not args.all:
        delta = repo.preview_stage(ctx.root, ctx.cfg, ctx.ignore)
        total = len(delta["added"]) + len(delta["modified"]) + len(delta["deleted"])
        if total == 0:
            ui.info("nothing to stage (staging already matches the working tree)")
            return EXIT_OK
        ui.heading("would stage (run 'jp add -A' or name paths):")
        _section("new", delta["added"])
        _section("modified", delta["modified"])
        _section("deleted", delta["deleted"])
        return EXIT_OK

    summary = repo.stage_paths(
        ctx.root,
        ctx.cfg,
        ctx.ignore,
        list(args.paths),
        all_files=args.all,
        dry_run=args.dry_run,
    )

    for err in summary["errors"]:
        ui.error(err)

    staged, removed = summary["staged"], summary["removed"]
    verb = "would stage" if args.dry_run else "staged"
    if staged:
        ui.success(f"{verb} {len(staged)} file(s)")
        _section("staged", staged)
    if removed:
        ui.success(f"{verb} {len(removed)} deletion(s)")
        _section("deleted", removed)
    if not staged and not removed and not summary["errors"]:
        ui.info("nothing to stage")

    # Non-zero exit if the user named paths that matched nothing and nothing else
    # was staged (a clear "you asked to add something that does not exist").
    if summary["errors"] and not staged and not removed:
        return EXIT_USAGE
    return EXIT_OK


def _section(title: str, items: list[str]) -> None:
    if not items:
        return
    ui.heading(f"  {title}:")
    ui.bullets(sorted(items), indent="    ")

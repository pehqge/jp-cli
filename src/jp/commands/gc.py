"""``jp gc`` -- prune unreachable loose objects from the LOCAL versioning store.

Default is a DRY RUN: it reports how many objects and bytes WOULD be reclaimed and
writes nothing. ``--prune`` actually deletes them. An object is a candidate only
when it is BOTH unreachable (no commit / branch tip / HEAD / staged blob references
it -- the full parent walk is used) AND older than ``--grace`` days (default 14),
so a brand-new object from an in-flight commit is never pruned. Runs under the
per-repo versioning lock. Remote gc is NOT performed in v1 (the remote ``__jp``
mirror is append-only; reclaiming it is a future feature).
"""

from __future__ import annotations

import argparse

from .. import ui
from ..errors import EXIT_OK
from ..versioning import gc as gc_mod
from ._context import load_repo

# How many candidate shas to list in the report before truncating with a count.
_PREVIEW = 10


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "gc",
        help="prune unreachable local objects (dry-run unless --prune)",
    )
    p.add_argument(
        "--prune",
        action="store_true",
        help="actually delete the unreachable objects (default: dry-run only)",
    )
    p.add_argument(
        "--grace",
        type=int,
        default=gc_mod.DEFAULT_GRACE_DAYS,
        metavar="DAYS",
        help=f"only prune objects older than this many days (default: {gc_mod.DEFAULT_GRACE_DAYS})",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    result = gc_mod.run_gc(ctx.root, prune=args.prune, grace_days=args.grace)

    if args.prune:
        _print_pruned(result)
    else:
        _print_dry_run(result)
    # Remote gc is out of scope for v1; make that explicit so a user does not expect
    # gc to reclaim space on the remote mirror.
    ui.detail("note: gc is local only; the remote __jp mirror is append-only and not reclaimed.")
    return EXIT_OK


def _print_candidates(result: gc_mod.GcResult) -> None:
    shas = [sha for sha, _ in result.candidates]
    preview = shas[:_PREVIEW]
    ui.bullets([sha[:12] for sha in preview], indent="    ")
    if len(shas) > _PREVIEW:
        ui.detail(f"    ... and {len(shas) - _PREVIEW} more")


def _print_dry_run(result: gc_mod.GcResult) -> None:
    n = len(result.candidates)
    if n == 0:
        ui.success(
            f"nothing to prune (no unreachable objects older than {result.grace_days} day(s))"
        )
        return
    ui.heading(
        f"would reclaim {n} object(s), {_human(result.candidate_bytes)} "
        f"(unreachable, older than {result.grace_days} day(s)):"
    )
    _print_candidates(result)
    ui.info("run 'jp gc --prune' to delete them.")


def _print_pruned(result: gc_mod.GcResult) -> None:
    n = len(result.pruned)
    if n == 0:
        ui.success(
            f"nothing to prune (no unreachable objects older than {result.grace_days} day(s))"
        )
        return
    ui.success(f"pruned {n} object(s), reclaimed {_human(result.reclaimed_bytes)}")


def _human(n: int) -> str:
    """Render a byte count as a short human string (B/KiB/MiB/GiB)."""
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"

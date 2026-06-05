"""``jp fsck`` -- verify the integrity of the local versioning store (read-only).

Walks reachability from HEAD + every branch tip and re-hashes every reachable
object (commits, trees, blobs); with ``--full`` it ALSO re-hashes every loose
object on disk to catch corruption in objects that are not currently reachable.
Reports MISSING (referenced but absent), CORRUPT (present but fails its integrity
re-hash), DANGLING refs/HEAD (point at a non-existent commit), and -- under
``--full`` -- unreachable corrupt objects. NEVER writes anything. Exit 0 if the
store is clean (or there is no versioning history yet); non-zero if any problem is
found.
"""

from __future__ import annotations

import argparse

from .. import ui
from ..errors import EXIT_GENERIC, EXIT_OK
from ..versioning import fsck as fsck_mod
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "fsck",
        help="check versioning store integrity (read-only)",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="also re-hash every loose object on disk, not just reachable ones",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    report = fsck_mod.run_fsck(ctx.root, full=args.full)

    if not report.initialized:
        ui.info("no versioning history (run 'jp commit' to start versioning)")
        return EXIT_OK

    if report.clean:
        ui.success(f"versioning store is clean ({report.checked} object(s) verified)")
        return EXIT_OK

    _print_problems(report)
    return EXIT_GENERIC


def _print_problems(report: fsck_mod.FsckReport) -> None:
    """Print every detected problem with SHORT hashes; called only when not clean.

    Every problem (header AND the offending short shas) is emitted via ``ui.error``
    so the entire fault report lands on STDERR as one coherent stream -- a caller
    grepping stderr for the bad shas sees them, and stdout stays clean for any
    machine-readable summary.
    """
    ui.error("fsck found problems:")

    if report.head_dangling:
        ui.error(f"  dangling HEAD: points at {report.head_detail[:12]} (no commit object)")

    if report.dangling_refs:
        ui.error(f"  {len(report.dangling_refs)} dangling ref(s):")
        for label, sha in sorted(report.dangling_refs.items()):
            ui.error(f"    {label} -> {sha[:12]} (no commit object)")

    if report.missing:
        ui.error(f"  {len(report.missing)} missing object(s) (referenced but absent):")
        for sha in sorted(report.missing):
            ui.error(f"    {sha[:12]}")

    if report.corrupt:
        ui.error(f"  {len(report.corrupt)} corrupt object(s) (failed integrity re-hash):")
        for sha in sorted(report.corrupt):
            ui.error(f"    {sha[:12]}")

    if report.unreachable_corrupt:
        ui.error(
            f"  {len(report.unreachable_corrupt)} corrupt loose object(s) on disk "
            "(unreachable; found by --full):"
        )
        for sha in sorted(report.unreachable_corrupt):
            ui.error(f"    {sha[:12]}")

    n = (
        len(report.missing)
        + len(report.corrupt)
        + len(report.dangling_refs)
        + len(report.unreachable_corrupt)
        + (1 if report.head_dangling else 0)
    )
    ui.error(f"{report.checked} object(s) verified; {n} problem(s) found")

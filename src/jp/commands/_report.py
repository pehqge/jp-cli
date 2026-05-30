"""Shared summary printing for sync outcomes (push/pull/clone)."""

from __future__ import annotations

from .. import ui
from ..sync import Outcome


def report_outcome(verb: str, outcome: Outcome, *, dry_run: bool) -> None:
    """Print a human summary of a sync run.

    Conflicts and per-file failures are surfaced clearly. Skipped dotfiles are
    reported but never treated as an error (see docs/architecture.md).
    """
    prefix = "[dry-run] would " if dry_run else ""

    for rel in outcome.transferred:
        ui.out(f"  {prefix}{verb}: {rel}")

    if outcome.skipped_hidden:
        ui.warn(
            f"skipped {len(outcome.skipped_hidden)} hidden/dotfile(s) "
            "(server rejects hidden uploads):"
        )
        ui.bullets(outcome.skipped_hidden, indent="    - ")

    if outcome.conflicts:
        ui.warn(
            f"{len(outcome.conflicts)} conflict(s) NOT touched "
            "(both sides changed; resolve manually):"
        )
        ui.bullets(outcome.conflicts, indent="    ! ")

    if outcome.failures:
        ui.error(f"{len(outcome.failures)} file(s) failed:")
        for rel, reason in outcome.failures:
            ui.error(f"    x {rel}: {reason}")

    n = len(outcome.transferred)
    if dry_run:
        ui.info(f"{prefix}{verb} {n} file(s); {len(outcome.up_to_date)} already up to date")
    else:
        ui.success(
            f"{verb}: {n} transferred, {len(outcome.up_to_date)} up to date, "
            f"{len(outcome.skipped_hidden)} skipped, {len(outcome.conflicts)} conflict(s), "
            f"{len(outcome.failures)} failed"
        )

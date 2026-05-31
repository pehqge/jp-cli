"""Mirror-mode deletion handling, shared by ``jp push`` and ``jp pull``.

Mirror mode is OFF by default. When ON, after the additive sync, files that
exist on one side but not the other become *deletion candidates*. This module
NEVER deletes without consent:

  * In an interactive terminal it shows a keep/delete selector (every file
    defaults to KEEP) and only deletes what the user explicitly marks.
  * With ``--yes`` it deletes every candidate (for scripts that opt in).
  * In a non-interactive shell WITHOUT ``--yes`` it deletes nothing and says so.

Every remote deletion is re-validated against the workspace prefix immediately
before the call, so a mirror delete can never escape the user's own subtree.
"""

from __future__ import annotations

from .. import paths, tui, ui
from ..sync import Outcome
from ._context import RepoContext


def handle(
    side: str,  # "remote" (push) or "local" (pull)
    ctx: RepoContext,
    api: object,
    outcome: Outcome,
    *,
    yes: bool,
    dry_run: bool,
) -> None:
    candidates = list(outcome.deletable)
    if not candidates:
        return

    if dry_run:
        ui.warn(
            f"mirror mode: {len(candidates)} file(s) exist on {side} but not on the "
            f"other side and could be deleted (run without --dry-run to choose):"
        )
        for rel in candidates:
            ui.detail(f"    {rel}")
        return

    # Decide what to delete.
    if yes:
        selected = candidates
    elif tui.interactive():
        selected = tui.confirm_deletions(candidates, side)
    else:
        ui.warn(
            f"mirror mode: {len(candidates)} file(s) exist on {side} but not on the "
            "other side. Refusing to delete without a terminal; re-run interactively "
            "or pass --yes to delete them all."
        )
        for rel in candidates:
            ui.detail(f"    {rel}")
        return

    if not selected:
        ui.info("mirror: kept everything (nothing deleted)")
        return

    if side == "remote":
        _delete_remote(ctx, api, selected, outcome)
    else:
        _delete_local(ctx, selected, outcome)


def _delete_remote(ctx: RepoContext, api: object, selected: list[str], outcome: Outcome) -> None:
    prefix = paths.validate_prefix(ctx.cfg.prefix)
    for rel in selected:
        try:
            remote_path = paths.remote_path_for(prefix, rel)
            # SAFETY: re-assert containment immediately before the destructive call.
            paths.assert_within_prefix(remote_path, prefix)
            api.delete(remote_path)  # type: ignore[attr-defined]
            ctx.index.remove(rel)
            ctx.index.save()
            outcome.deleted.append(rel)
            ui.info(f"  deleted remote: {rel}")
        except Exception as exc:  # keep going; report per-file
            outcome.failures.append((rel, f"delete failed: {exc}"))


def _delete_local(ctx: RepoContext, selected: list[str], outcome: Outcome) -> None:
    for rel in selected:
        try:
            dest = paths.safe_local_dest(ctx.root, rel)
            if dest.is_symlink():
                outcome.failures.append((rel, "refusing to delete through a symlink"))
                continue
            if dest.is_file():
                dest.unlink()
            ctx.index.remove(rel)
            ctx.index.save()
            outcome.deleted.append(rel)
            ui.info(f"  deleted local: {rel}")
        except Exception as exc:
            outcome.failures.append((rel, f"delete failed: {exc}"))

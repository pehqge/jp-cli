"""``jp checkout`` -- restore files from a commit into the working tree (offline).

The MOST DESTRUCTIVE jp command: it overwrites and can delete working-tree files.
It is therefore conservative by construction (see :mod:`jp.versioning.checkout`):

* ``jp checkout COMMIT path...`` restores ONLY those paths, never moves HEAD, never
  deletes extras (path-scoped mode);
* ``jp checkout COMMIT`` restores the whole tree, handles extras per
  ``--remove-extra``, and moves HEAD (attaching to a branch when COMMIT is a
  branch / symbolic HEAD, else detaching to the sha) -- full mode.

A file with uncommitted local edits BLOCKS the whole checkout (zero writes) unless
``--force``. Deleting untracked extra work requires interactive confirmation and is
REFUSED in a non-tty. ``--dry-run`` prints the full plan and writes nothing. All of
this is offline: it never touches the remote or the sync base (``.jp/index.json``).
"""

from __future__ import annotations

import argparse

from .. import ui
from ..errors import EXIT_OK, EXIT_PARTIAL, SafetyError
from ..versioning import checkout as checkout_mod
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "checkout",
        help="restore files from a commit into the working tree (offline)",
    )
    p.add_argument("commit", help="commit-ish: HEAD, a branch, a full sha, or a sha prefix")
    p.add_argument(
        "paths",
        nargs="*",
        help="restore only these paths (path-scoped; does not move HEAD)",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="overwrite files with uncommitted local changes",
    )
    p.add_argument(
        "--remove-extra",
        action="store_true",
        help="(full checkout) remove working files not in the target commit",
    )
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would change; write nothing",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="assume 'yes' to confirmation prompts (e.g. deleting untracked extras)",
    )
    p.set_defaults(func=run)


def _make_confirm(assume_yes: bool) -> checkout_mod.ConfirmDeleteUntracked:
    """Build the untracked-extra deletion confirmer used by the apply layer.

    In a tty it lists the untracked files and asks (default NO); ``--yes`` short-
    circuits to True. In a NON-tty :func:`jp.ui.confirm` returns the default
    (False) without blocking -- but deleting never-committed work silently would be
    unsafe, so we RAISE :class:`SafetyError` there instead, refusing the delete and
    pointing at ``--yes``. Untracked work is therefore never deleted silently.
    """
    import sys

    def _confirm(rels: list[str]) -> bool:
        if assume_yes:
            return True
        if not sys.stdin.isatty():
            raise SafetyError(
                f"refusing to delete {len(rels)} untracked file(s) not in any commit "
                "in a non-interactive shell; re-run with --yes to confirm: " + ", ".join(rels)
            )
        ui.warn("the following files are NOT in any commit and would be PERMANENTLY deleted:")
        ui.bullets(sorted(rels), indent="    ")
        return ui.confirm("delete these untracked files?", default=False)

    return _confirm


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    paths_arg = list(args.paths or [])

    result = checkout_mod.apply_checkout(
        ctx.root,
        ctx.cfg,
        args.commit,
        paths_arg,
        force=args.force,
        remove_extra=args.remove_extra,
        dry_run=args.dry_run,
        confirm_delete_untracked=_make_confirm(args.yes),
    )

    if args.dry_run:
        _print_dry_run(result)
        return EXIT_OK

    return _print_applied(result)


def _print_dry_run(result: checkout_mod.CheckoutResult) -> None:
    plan = result.plan
    ui.heading(f"dry run: checkout {plan.short}")
    _list("would write", result.written)
    _list("would skip (already current)", result.skipped)
    n_delete = 0
    if not plan.path_scoped:
        _list("would delete (extras, tracked)", result.would_delete_tracked)
        _list(
            "would delete (extras, untracked -- needs confirmation; refused in non-tty)",
            result.would_delete_untracked,
        )
        _list("would keep (extras)", result.extras_kept)
        n_delete = len(result.would_delete_tracked) + len(result.would_delete_untracked)
        if plan.attach_branch is not None:
            ui.info(f"would attach HEAD to branch '{plan.attach_branch}'")
        else:
            ui.info(f"would detach HEAD at {plan.short}")
    ui.info(
        f"{len(result.written)} to write, {len(result.skipped)} unchanged"
        + (
            ""
            if plan.path_scoped
            else f", {n_delete} to delete, {len(result.extras_kept)} extra kept"
        )
    )


def _print_applied(result: checkout_mod.CheckoutResult) -> int:
    plan = result.plan
    _list("restored", result.written)
    _list("deleted", result.deleted)
    if result.extras_kept and not plan.path_scoped:
        _list("kept (not in target commit)", result.extras_kept)
    for rel, err in result.failures:
        ui.error(f"{rel}: {err}")

    if result.incomplete:
        # FULL checkout that hit per-file failures: HEAD was deliberately NOT moved
        # and staging was NOT rewritten, so neither lies about a tree that was only
        # partially materialized. Make the partial state LOUD.
        failed = sorted(rel for rel, _ in result.failures)
        ui.warn(
            f"checkout of {plan.short} is INCOMPLETE: the working tree was only "
            "partially updated. HEAD was left at the previous commit and the "
            "staging area was not changed."
        )
        ui.warn("the following paths failed and were NOT updated:")
        ui.bullets(failed, indent="    ")
        ui.warn(
            "resolve the cause (e.g. remove a conflicting symlink, or fix a corrupt "
            f"object) and re-run 'jp checkout {plan.short}' to finish."
        )
        return EXIT_PARTIAL

    if result.head_moved:
        if plan.attach_branch is not None:
            ui.success(f"checked out {plan.short} (HEAD -> {plan.attach_branch})")
        else:
            ui.success(f"checked out {plan.short} (detached HEAD)")
        # DIVERGENCE WARNING: the working tree now matches an older/other commit, so
        # the sync engine sees these as local modifications and the NEXT push will
        # overwrite the remote with this content.
        ui.warn(
            f"working tree now matches {plan.short}; the next 'jp push' will push "
            "this state to the remote (overwriting remote files)."
        )
    else:
        ui.success(f"restored {len(result.written)} path(s) from {plan.short}")

    # A per-file failure (a symlink conflict / corrupt object) is a partial run.
    # Path-scoped mode never sets `incomplete`, so its failures fall through here.
    return EXIT_PARTIAL if result.failures else EXIT_OK


def _list(title: str, items: list[str]) -> None:
    if not items:
        return
    ui.heading(title + ":")
    ui.bullets(sorted(items), indent="    ")

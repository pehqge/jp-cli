"""``jp push`` -- upload local changes to the remote.

Additive by default (never deletes). With mirror mode on (config ``mirror`` or
``--mirror``), remote files that no longer exist locally become deletion
candidates -- and jp asks, file by file, before removing any of them.

Two OPT-IN extensions layer on top of the original behavior (both no-ops for a
repo that never adopted them, so the no-PATH / no-versioning path is byte-
identical to before):

* PATH-SCOPED push (``jp push PATH...``): upload ONLY the named files/dirs. A
  scoped push is additive-only -- mirror-delete is never offered, because naming
  one local file cannot imply which remote files should be deleted.
* The COMMIT-GATE prompt: when versioning is ACTIVE (HEAD resolves to a commit)
  and the working tree has uncommitted changes, jp interactively offers to commit
  first. The gate is OFFLINE (decided from local state only), never blocks CI
  (non-tty prints one warning and proceeds), and is fully skippable
  (``--raw``/``--no-verify`` or ``versioning.push_prompt == "never"``).
"""

from __future__ import annotations

import argparse
import datetime
import sys

from .. import config as config_mod
from .. import paths, sync, ui
from ..errors import EXIT_OK, EXIT_PARTIAL, EXIT_SAFETY, EXIT_USAGE, JpError
from . import _context, _mirror
from ._context import RepoContext, load_repo
from ._report import report_outcome


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "push", help="upload local changes (additive; mirror deletes are opt-in)"
    )
    p.add_argument(
        "path",
        nargs="*",
        help="push ONLY these files/directories (additive; no mirror deletes)",
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
    p.add_argument(
        "--raw",
        "--no-verify",
        dest="raw",
        action="store_true",
        help="skip the versioning commit-gate; push exactly what is on disk",
    )
    p.add_argument(
        "--backup-history",
        dest="backup_history",
        action="store_true",
        help="also back up committed version history to the remote (does not change config)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()

    # 1) PATH scope: normalize each arg (rejecting anything outside the repo) and
    #    confirm each one matches at least one eligible local file. With no PATH
    #    args, ``only_paths`` is None -> today's whole-tree behavior exactly.
    only_paths = _collect_scope(ctx, args.path)
    if only_paths is not None and not only_paths:
        # Every named path matched nothing eligible (the per-path errors were
        # already printed by _collect_scope). Nothing to do -> usage error.
        return EXIT_USAGE

    # 2) The commit-gate (OFFLINE; versioning is opt-in). On a cancel it returns
    #    a non-None exit code and we stop without sending anything.
    gate_exit = _commit_gate(ctx, only_paths=only_paths, dry_run=args.dry_run, raw=args.raw)
    if gate_exit is not None:
        return gate_exit

    # 3) The actual upload (network). Building the api stays here -- the gate
    #    above used only local state, so a push that the gate cancelled never
    #    touched the network.
    api = _context.build_api(ctx.cfg)
    outcome = sync.push(
        ctx.root, ctx.cfg, api, ctx.index, ctx.ignore, dry_run=args.dry_run, only_paths=only_paths
    )

    # 4) Mirror-delete: SKIPPED entirely for a scoped push (additive-only).
    mirror = ctx.cfg.mirror if args.mirror is None else args.mirror
    if mirror and only_paths is None:
        _mirror.handle("remote", ctx, api, outcome, yes=args.yes, dry_run=args.dry_run)

    # Post-push HISTORY MIRROR (Task 8): after the data push succeeds, optionally
    # back the COMMITTED versioning history up to ``<prefix>/__jp/`` on the remote.
    # OPT-IN, best-effort, and NEVER changes the push exit code -- a failure is a
    # warning only. --dry-run never mirrors and never persists config.
    _maybe_mirror_history(
        ctx, api, dry_run=args.dry_run, backup_history=getattr(args, "backup_history", False)
    )

    report_outcome("push", outcome, dry_run=args.dry_run)
    if outcome.had_conflicts:
        return EXIT_SAFETY
    return EXIT_PARTIAL if outcome.had_failures else EXIT_OK


# --------------------------------------------------------------------------- #
# PATH scope (Feature A)
# --------------------------------------------------------------------------- #
def _collect_scope(ctx: RepoContext, raw_paths: list[str]) -> set[str] | None:
    """Normalize the PATH args into a push scope, or ``None`` for no scope.

    Returns ``None`` when no PATHs were given (the unchanged whole-tree push).
    Otherwise returns the set of NORMALIZED rels that should be pushed: the union
    of every eligible local file that equals, or lives under, a named path. A
    named path that matches NO eligible file prints a clear per-path error and
    contributes nothing; if EVERY named path matched nothing the result is an
    EMPTY set (the caller turns that into a non-zero exit).

    "Eligible" means discovered by :func:`sync.scan_local` (so ignored files,
    symlinks and ``.jp/`` are already excluded) AND, under the default ``skip``
    dotfile policy, not hidden -- a hidden file would be skipped on push anyway,
    so naming it explicitly is reported as "matched nothing".
    """
    if not raw_paths:
        return None

    eligible = set(sync.scan_local(ctx.root, ctx.ignore))
    protect = ctx.cfg.dotfiles == "protect"
    if not protect:
        eligible = {rel for rel in eligible if not paths.is_hidden(rel)}

    scope: set[str] = set()
    for raw in raw_paths:
        try:
            norm = paths.normalize_rel(raw)
        except JpError as exc:
            # Outside the repo / traversal / absolute -> a per-path safety error.
            ui.error(f"{raw}: {exc.message}")
            continue
        matched = {rel for rel in eligible if rel == norm or rel.startswith(norm + "/")}
        if not matched:
            ui.error(f"{raw}: no eligible file to push (not found, ignored, or hidden)")
            continue
        scope |= matched
    return scope


# --------------------------------------------------------------------------- #
# Commit-gate (Feature B)
# --------------------------------------------------------------------------- #
def _commit_gate(
    ctx: RepoContext,
    *,
    only_paths: set[str] | None,
    dry_run: bool,
    raw: bool,
) -> int | None:
    """Run the OFFLINE commit-gate; return a non-None exit code to ABORT.

    Returns ``None`` to proceed with the push (the common case) or an exit code
    when the user chose to cancel. The gate runs IF AND ONLY IF versioning is
    active (``resolve_head`` is not None): a repo that never initialized
    versioning never reaches any prompt and pushes exactly as today -- the opt-in
    invariant.

    Decision order (all from LOCAL state -- no network):

    * No HEAD -> not active -> proceed.
    * ``--dry-run`` -> never prompt and never commit; print a note if there are
      uncommitted changes in scope, then proceed (so an ``[a]`` choice can never
      persist config on a dry run).
    * ``--raw``/``--no-verify`` or ``push_prompt == "never"`` -> proceed silently.
    * No uncommitted changes in scope -> proceed (no prompt).
    * Not a tty -> print ONE warning and proceed (CI-safe; never blocks).
    * tty + ``push_prompt in ("ask", "always")`` -> show the box and act on the
      choice (commit / push raw / always-raw / cancel).
    """
    from ..versioning import refs
    from ..versioning.objects import VersioningError

    # OPT-IN: inactive versioning (no HEAD) -> no gate at all.
    try:
        if refs.resolve_head(ctx.root) is None:
            return None
    except VersioningError:
        # A corrupt HEAD must never block a push; treat as inactive.
        return None

    # Compute the uncommitted set OFFLINE; scope it to the pushed paths if any.
    uncommitted = _uncommitted_in_scope(ctx, only_paths)
    if not uncommitted:
        return None  # nothing uncommitted in scope -> no prompt

    # --dry-run: never prompt, never commit, never persist config. Just note it.
    if dry_run:
        ui.info(
            f"note: {len(uncommitted)} uncommitted change(s) in the pushed scope are "
            "NOT versioned (commit first or pass --raw to silence)"
        )
        return None

    # Explicit opt-out: --raw / --no-verify or push_prompt=never -> push raw.
    if raw or ctx.cfg.versioning_push_prompt == "never":
        return None

    # Non-interactive (CI/pipe): never block -- one warning, then proceed.
    if not sys.stdin.isatty():
        ui.warn(
            "uncommitted changes are not versioned; run 'jp commit -m ...' first, "
            "or pass --raw to silence"
        )
        return None

    # Interactive: ask|always both show the box.
    return _prompt_box(ctx)


def _uncommitted_in_scope(ctx: RepoContext, only_paths: set[str] | None) -> set[str]:
    """The working-vs-HEAD change set, scoped to ``only_paths`` when given.

    Reuses :func:`jp.versioning.repo.working_vs_head` (the same primitives status
    uses) so the notebook HYBRID rule is honored: a pure re-run is NOT a change.
    A corrupt versioning store degrades to "no uncommitted changes" rather than
    blocking the push.
    """
    from ..versioning import repo
    from ..versioning.objects import VersioningError

    try:
        changes = repo.working_vs_head(ctx.root, ctx.cfg, ctx.ignore)
    except (VersioningError, OSError):
        return set()
    if only_paths is not None:
        changes = {rel for rel in changes if sync._in_scope(rel, only_paths)}
    return changes


def _prompt_box(ctx: RepoContext) -> int | None:
    """Show the interactive commit-gate box; return None to proceed or an exit code.

    Reads the choice via :func:`ui.ask_line`; an unrecognized answer re-asks a
    couple of times, then an empty/EOF answer is treated as cancel. The four
    actions match the spec (§5): commit / push raw / always-raw / cancel.
    """
    ui.heading("You have uncommitted changes.")
    ui.out("  [c] commit them first, then push")
    ui.out("  [p] push without versioning  (this once)")
    ui.out("  [a] always push without versioning  (don't ask again)")
    ui.out("  [x] cancel")

    for _ in range(3):
        choice = ui.ask_line("choose [c/p/a/x]: ").strip().lower()
        if choice == "c":
            return _gate_commit(ctx)
        if choice == "p":
            return None  # raw push, this once
        if choice == "a":
            return _gate_always(ctx)
        if choice in ("x", ""):
            ui.info("push cancelled (nothing was sent)")
            return EXIT_OK
        ui.warn(f"unrecognized choice {choice!r}; enter one of c/p/a/x")
    # Exhausted retries without a clear answer -> cancel safely.
    ui.info("push cancelled (nothing was sent)")
    return EXIT_OK


def _gate_commit(ctx: RepoContext) -> int | None:
    """The ``[c]`` action: stage-all + commit, then proceed to push.

    Asks for a message (empty -> a dated default). A "nothing to commit" outcome
    is informational, not fatal -- we still proceed to push.
    """
    from ..versioning import repo
    from ..versioning.objects import VersioningError

    message = ui.ask_line("commit message (Enter for a default): ").strip()
    if not message:
        today = datetime.date.today().isoformat()
        message = f"jp push snapshot {today}"
    try:
        result = repo.create_commit(
            ctx.root, ctx.cfg, message=message, stage_all=True, allow_empty=False, dry_run=False
        )
    except VersioningError as exc:
        # "nothing to commit" (and similar) is not fatal: inform and push anyway.
        ui.info(f"nothing committed ({exc.message}); proceeding to push")
        return None
    a, m, d = len(result["added"]), len(result["modified"]), len(result["deleted"])
    ui.success(f"[{result['short']}] committed {a + m + d} file(s) (+{a} ~{m} -{d})")
    return None  # proceed to push


def _gate_always(ctx: RepoContext) -> int | None:
    """The ``[a]`` action: persist ``push_prompt=never`` synchronously, then push."""
    from dataclasses import replace

    new_cfg = replace(ctx.cfg, versioning_push_prompt="never")
    new_cfg._config_dir = ctx.cfg.config_dir
    config_mod.save(ctx.root, new_cfg)
    ctx.cfg = new_cfg
    ui.info(
        "the commit-gate is now off for this repo; "
        "run 'jp config set versioning.push_prompt ask' to re-enable"
    )
    return None  # proceed to push


# --------------------------------------------------------------------------- #
# History mirror (Task 8) -- best-effort, never fails/blocks the data push
# --------------------------------------------------------------------------- #
_BACKUP_HINT = "run 'jp push --backup-history' to back up history any time"


def _maybe_mirror_history(ctx: RepoContext, api, *, dry_run: bool, backup_history: bool) -> None:
    """Decide whether to mirror committed history, then do it -- best-effort.

    Safety contract: this NEVER changes the push exit code and NEVER raises out --
    every path is wrapped so a mirror failure is at most a ``ui.warn``. ``--dry-run``
    never mirrors and never persists config. The decision matrix:

    * ``--backup-history`` -> always mirror; do NOT change config.
    * config ``always``    -> mirror.
    * config ``never``     -> do nothing (unless --backup-history).
    * config ``ask``       -> on a TTY, show the ask-once prompt ONLY when there is
      unmirrored history; on a non-TTY, never prompt/mirror -- print a one-line hint.

    Versioning must be ACTIVE (HEAD resolves to a commit) and there must be
    committed history to back up, else this is a silent no-op.
    """
    if dry_run:
        return
    try:
        from ..versioning import refs
        from ..versioning.objects import VersioningError

        try:
            if refs.resolve_head(ctx.root) is None:
                return  # versioning inactive -> nothing to mirror
        except VersioningError:
            return  # corrupt HEAD must never block/perturb a push

        mode = ctx.cfg.versioning_mirror_history

        if backup_history:
            _mirror_now(ctx, api)
            return
        if mode == "always":
            _mirror_now(ctx, api)
            return
        if mode == "never":
            return
        # mode == "ask"
        if not sys.stdin.isatty():
            # CI / pipe: never prompt, never mirror -- just a one-line hint.
            ui.info(f"history not backed up (versioning.mirror_history=ask); {_BACKUP_HINT}")
            return
        # Only nag when there is actually unmirrored history.
        if not _has_unmirrored_history(ctx, api):
            return
        _ask_once_and_mirror(ctx, api)
    except Exception as exc:  # noqa: BLE001 - mirror is best-effort, never fatal
        ui.warn(f"history mirror skipped: {exc}")


def _mirror_now(ctx: RepoContext, api) -> None:
    """Run the mirror and report HONEST counts; never raises out."""
    from ..versioning import mirror

    try:
        result = mirror.mirror_history(ctx.root, ctx.cfg, api)
    except Exception as exc:  # noqa: BLE001 - defense in depth; mirror catches internally
        ui.warn(f"history mirror failed: {exc}")
        return
    _report_mirror(result)


def _report_mirror(result) -> None:
    """Print an honest one-line mirror summary (never claims safety falsely)."""
    from ..versioning.mirror import MirrorResult

    if not isinstance(result, MirrorResult) or not result.ran:
        return
    if result.complete:
        ui.success(f"history mirrored: {result.pushed} new object(s), ref advanced")
    else:
        ui.warn(
            f"history mirror incomplete: {result.pushed}/{result.total} object(s), "
            "ref NOT advanced -- not yet safe, will resume next push"
        )


def _has_unmirrored_history(ctx: RepoContext, api) -> bool:
    """True iff at least one local branch tip is not already the remote ref.

    Best-effort and read-only: a network hiccup degrades to "assume there is
    something to back up" so the user still gets the one-time prompt rather than
    silently skipping the backup. Compares the local branch tips against the
    remote ``__jp/refs/heads/<branch>`` values.
    """
    from .. import paths
    from ..versioning import mirror
    from ..versioning.refs import list_heads

    try:
        prefix = paths.validate_prefix(ctx.cfg.prefix)
        heads = list_heads(ctx.root)
    except Exception:  # noqa: BLE001
        return True
    if not heads:
        return False
    for branch, tip in heads.items():
        try:
            remote_sha = mirror._read_remote_ref(api, prefix, branch)
        except Exception:  # noqa: BLE001 - a read error -> assume unmirrored
            return True
        if remote_sha != tip:
            return True
    return False


def _ask_once_and_mirror(ctx: RepoContext, api) -> None:
    """Show the ask-once prompt; persist the choice and mirror per the answer.

    The prompt is shown only on a TTY and only when there is unmirrored history
    (the caller checks). Choices:

    * ``a`` always -> persist ``mirror_history=always``, then mirror now.
    * ``n`` never  -> persist ``mirror_history=never``, do NOT mirror; print hint.
    * ``o`` once   -> mirror now AND persist ``never`` (so it won't nag again);
      print the manual hint. An empty/EOF/unknown answer is treated as ``o``
      (back up this once but don't change behavior) after a couple of retries.
    """
    ui.heading("Save version history to the remote too? (survives deleting local .jp)")
    ui.out("  [a] always   back up on every push")
    ui.out("  [n] never    keep history local only")
    ui.out("  [o] once     back up now -- run `jp push --backup-history` to repeat later")

    for _ in range(3):
        choice = ui.ask_line("choose [a/n/o]: ").strip().lower()
        if choice == "a":
            _set_mirror_mode(ctx, "always")
            _mirror_now(ctx, api)
            return
        if choice == "n":
            _set_mirror_mode(ctx, "never")
            ui.info(f"history kept local only; {_BACKUP_HINT}")
            return
        if choice in ("o", ""):
            _mirror_now(ctx, api)
            _set_mirror_mode(ctx, "never")
            ui.info(_BACKUP_HINT)
            return
        ui.warn(f"unrecognized choice {choice!r}; enter one of a/n/o")
    # Exhausted retries -> back up once and stop nagging (the safe, useful default).
    _mirror_now(ctx, api)
    _set_mirror_mode(ctx, "never")
    ui.info(_BACKUP_HINT)


def _set_mirror_mode(ctx: RepoContext, mode: str) -> None:
    """Persist ``versioning.mirror_history`` synchronously; update the live ctx."""
    from dataclasses import replace

    new_cfg = replace(ctx.cfg, versioning_mirror_history=mode)
    new_cfg._config_dir = ctx.cfg.config_dir
    config_mod.save(ctx.root, new_cfg)
    ctx.cfg = new_cfg

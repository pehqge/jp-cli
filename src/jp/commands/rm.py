"""``jp rm`` -- the ONLY command that deletes (see docs/architecture.md), and it is gated.

Deletion is dangerous on a shared box, so it is:
  * explicit (you must name the path),
  * confirmed (interactive prompt unless --yes),
  * path-jailed (assert_within_prefix immediately before EVERY DELETE),
  * scoped (default removes only the remote copy; --local also removes the
    sanitized local file).

The server's DELETE is NOT recursive: a non-empty directory is refused with
HTTP 400 "not empty" (research §5). So removing a tree means walking it and
deleting BOTTOM-UP -- files first, then the deepest subdirs up to the named dir,
each DELETE validated by the path-jail. Without ``--recursive`` we refuse a
non-empty directory up front with a clear message rather than letting the server
fail ugly.

push and pull never call into this module.
"""

from __future__ import annotations

import argparse
import sys

from .. import paths, ui
from ..errors import EXIT_OK, SafetyError, UsageError
from . import _context
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("rm", help="delete a path on the remote (gated; the only deleter)")
    p.add_argument("path", help="relative path to delete (under the prefix)")
    p.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="delete a directory and everything under it (bottom-up)",
    )
    p.add_argument(
        "-n", "--dry-run", action="store_true", help="show what would be deleted; delete nothing"
    )
    p.add_argument("--local", action="store_true", help="also delete the local copy")
    p.add_argument("--keep-local", action="store_true", help="never touch the local copy (default)")
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.local and args.keep_local:
        raise UsageError("--local and --keep-local are mutually exclusive")

    ctx = load_repo()
    api = _context.build_api(ctx.cfg)
    prefix = paths.validate_prefix(ctx.cfg.prefix)

    rel = paths.normalize_rel(args.path)
    remote_path = paths.remote_path_for(prefix, rel)
    # Confirm the target is strictly within the prefix BEFORE anything else.
    paths.assert_within_prefix(remote_path, prefix)

    entry = api.stat(remote_path)
    is_dir = entry is not None and entry.type == "directory"

    if is_dir:
        return _rm_dir(args, ctx, api, prefix, rel, remote_path)
    return _rm_file(args, ctx, api, prefix, rel, remote_path)


# --------------------------------------------------------------------------- #
# Single file
# --------------------------------------------------------------------------- #
def _rm_file(args, ctx, api, prefix: str, rel: str, remote_path: str) -> int:
    target_desc = f"remote {ctx.cfg.prefix}/{rel}"
    if args.local:
        target_desc += " AND the local copy"

    if args.dry_run:
        ui.heading("[dry-run] would delete:")
        ui.bullets([f"{ctx.cfg.prefix}/{rel}"], indent="    - ")
        return EXIT_OK

    if not args.yes:
        if not sys.stdin.isatty():
            raise SafetyError(f"refusing to delete {target_desc} without confirmation; pass --yes")
        ui.warn(f"About to permanently delete {target_desc}.")
        ans = input("Type the path to confirm: ").strip()
        if ans != rel:
            ui.info("aborted (confirmation did not match)")
            return EXIT_OK

    # SAFETY: re-assert immediately before the destructive call.
    paths.assert_within_prefix(remote_path, prefix)
    api.delete(remote_path)
    ui.success(f"deleted remote: {ctx.cfg.prefix}/{rel}")

    _maybe_delete_local(args, ctx, rel)
    ctx.index.remove(rel)
    ctx.index.save()
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Directory (recursive, bottom-up)
# --------------------------------------------------------------------------- #
def _rm_dir(args, ctx, api, prefix: str, rel: str, remote_path: str) -> int:
    children = api.list_dir(remote_path)
    is_empty = not children

    if not args.recursive and not is_empty:
        # Don't let the server fail ugly with a 400 "not empty": refuse clearly.
        raise SafetyError(
            f"{ctx.cfg.prefix}/{rel} is a non-empty directory. "
            "DELETE is not recursive; pass --recursive to remove it and its contents."
        )

    # Collect every path under the dir, deepest-first, plus the dir itself last.
    victims = _collect_tree(api, prefix, remote_path)

    if args.dry_run:
        ui.heading(f"[dry-run] would delete {len(victims)} path(s), bottom-up:")
        ui.bullets([_rel_under(prefix, v) for v in victims], indent="    - ")
        return EXIT_OK

    if not args.yes:
        if not sys.stdin.isatty():
            raise SafetyError(
                f"refusing to delete directory {ctx.cfg.prefix}/{rel} "
                f"({len(victims)} path(s)) without confirmation; pass --yes"
            )
        ui.warn(
            f"About to permanently delete the directory {ctx.cfg.prefix}/{rel} "
            f"and ALL {len(victims)} path(s) under it."
        )
        # Stronger gate for a directory: the user must type the dir NAME.
        dir_name = rel.rsplit("/", 1)[-1]
        ans = input(f"Type the directory name '{dir_name}' to confirm: ").strip()
        if ans != dir_name:
            ui.info("aborted (confirmation did not match)")
            return EXIT_OK

    deleted = 0
    for victim in victims:  # already deepest-first (bottom-up)
        # SAFETY: validate EVERY single delete immediately before issuing it.
        paths.assert_within_prefix(victim, prefix)
        api.delete(victim)
        vrel = _rel_under(prefix, victim)
        if vrel:
            ctx.index.remove(vrel)
        deleted += 1
    ctx.index.save()
    ui.success(f"deleted {deleted} path(s) under {ctx.cfg.prefix}/{rel}")

    # Local recursive removal is intentionally NOT done here; --local handles a
    # single file only. (Local tree removal belongs to a future 'jp clean'.)
    if args.local:
        ui.warn("--local removes a single file only; the local directory was left untouched")
    return EXIT_OK


def _collect_tree(api, prefix: str, dir_path: str) -> list[str]:
    """Return every path under (and including) ``dir_path``, deepest-FIRST.

    Bottom-up order guarantees a directory is only deleted after its contents,
    matching the server's non-recursive DELETE (research §5).
    """
    files: list[str] = []
    dirs: list[tuple[int, str]] = []  # (depth, path)

    def walk(path: str) -> None:
        for entry in api.list_dir(path):
            paths.assert_within_prefix(entry.path, prefix)
            if entry.type == "directory":
                dirs.append((entry.path.count("/"), entry.path))
                walk(entry.path)
            else:
                files.append(entry.path)

    walk(dir_path)
    # Files first (order among files does not matter), then subdirs deepest
    # first, then the named directory itself last.
    dirs.sort(key=lambda t: t[0], reverse=True)
    ordered = files + [p for _, p in dirs]
    ordered.append(dir_path)
    return ordered


def _rel_under(prefix: str, full: str) -> str:
    base = prefix + "/"
    if full.startswith(base):
        return full[len(base) :]
    return ""


def _maybe_delete_local(args, ctx, rel: str) -> None:
    if not args.local:
        return
    dest = paths.safe_local_dest(ctx.root, rel)
    try:
        if dest.is_file():
            dest.unlink()
            ui.success(f"deleted local: {rel}")
    except OSError as exc:
        ui.warn(f"could not delete local {rel}: {exc}")

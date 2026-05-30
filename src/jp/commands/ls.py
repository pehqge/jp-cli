"""``jp ls`` -- list the remote prefix contents (read-only)."""

from __future__ import annotations

import argparse

from .. import paths, ui
from ..errors import EXIT_OK
from . import _context
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("ls", help="list remote files under the prefix (read-only)")
    p.add_argument("subpath", nargs="?", default="", help="relative subdirectory to list")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    api = _context.build_api(ctx.cfg)
    prefix = paths.validate_prefix(ctx.cfg.prefix)

    api_path = prefix
    if args.subpath:
        rel = paths.normalize_rel(args.subpath)
        api_path = paths.remote_path_for(prefix, rel)
        # Read-only, but still assert containment for defense in depth.
        paths.assert_within_prefix(api_path, prefix)

    entries = api.list_dir(api_path)
    if not entries:
        ui.info(f"(empty or not found): {ctx.cfg.prefix}/{args.subpath}".rstrip("/"))
        return EXIT_OK

    for e in sorted(entries, key=lambda x: (x.type != "directory", x.name)):
        marker = "/" if e.type == "directory" else " "
        size = "" if e.size is None else f"  {e.size}b"
        ui.out(f"  {e.name}{marker}{size}")
    return EXIT_OK

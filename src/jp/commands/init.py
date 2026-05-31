"""``jp init`` -- create a .jp/ workspace in an existing local directory.

You can pass a Jupyter URL (same forms as ``jp clone``) or the explicit
``--base-url``/``--prefix`` pair. Unlike ``clone``, ``init`` does not download
anything; it just records where this folder syncs to.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import config as config_mod
from .. import ui
from ..config import Config
from ..errors import EXIT_OK, UsageError
from ..index import Index
from ..paths import DOT_DIR, validate_prefix
from ..urls import parse_clone_url
from . import _context


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("init", help="initialize a .jp workspace in the current directory")
    p.add_argument(
        "url",
        nargs="?",
        default="",
        help="Jupyter folder URL (or use --base-url/--prefix)",
    )
    p.add_argument("--base-url", default="", help="Contents API base URL (instead of a URL)")
    p.add_argument("--prefix", default="", help="remote prefix (instead of a URL)")
    p.add_argument("--token-path", default="", help="path to the token file")
    p.add_argument(
        "--credential", default="", help="name of a saved credential to use (see 'jp login')"
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.url:
        base_url, prefix = parse_clone_url(args.url)
    else:
        base_url, prefix = str(args.base_url).strip(), str(args.prefix).strip()
    if not base_url:
        raise UsageError("provide a Jupyter URL, or both --base-url and --prefix")
    if not base_url.startswith(("http://", "https://")):
        raise UsageError(f"base URL must be http(s): {base_url!r}")
    prefix = validate_prefix(prefix)

    root = Path.cwd()
    if (root / DOT_DIR).exists():
        raise UsageError(f"{root} is already a jp workspace")

    credential = _context.choose_credential(args, root=None)
    cfg = Config(
        base_url=base_url.rstrip("/"),
        prefix=prefix,
        token_path=str(args.token_path or ""),
        credential=credential,
    )
    cfg._config_dir = root / DOT_DIR
    config_mod.save(root, cfg)
    Index(root).save()
    ui.success(f"initialized jp workspace in {root} (remote: {prefix})")
    return EXIT_OK

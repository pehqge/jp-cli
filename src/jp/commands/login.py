"""``jp login`` -- record the *path* to the token file (never the token value).

Phase-1 policy (see docs/architecture.md): jp stores only a path in config. If the user passes
``--token-path`` we record it. Otherwise we read a token from stdin and write it
to a private (0600) file under ``~/.config/jp/token`` and record THAT path. The
token value is registered for redaction and never echoed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .. import config as config_mod
from .. import ui
from ..errors import EXIT_OK, AuthError
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("login", help="register your API token (path only)")
    p.add_argument(
        "--token-path",
        default="",
        help="path to an existing token file to reference (value never stored)",
    )
    p.add_argument(
        "--stdin",
        action="store_true",
        help="read the token from stdin and save it to a private file",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()

    if args.token_path:
        path = Path(os.path.expanduser(args.token_path))
        if not path.is_file():
            raise AuthError(f"token file does not exist: {path}")
        ctx.cfg.token_path = str(args.token_path)
        config_mod.save(ctx.root, ctx.cfg)
        # Validate we can read it (registers + redacts the value).
        config_mod.load_token(ctx.cfg)
        ui.success(f"registered token path: {path}")
        return EXIT_OK

    if args.stdin or not sys.stdin.isatty():
        token = sys.stdin.readline().strip()
        if not token:
            raise AuthError("no token provided on stdin")
        dest = Path(os.path.expanduser("~/.config/jp/token"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Write with private permissions BEFORE writing content.
        fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        os.chmod(dest, 0o600)
        ui.register_secret(token)
        ctx.cfg.token_path = str(dest)
        config_mod.save(ctx.root, ctx.cfg)
        ui.success(f"token saved to {dest} (mode 600) and path registered")
        return EXIT_OK

    raise AuthError(
        "provide --token-path PATH, or pipe the token via --stdin "
        "(e.g. 'jp login --stdin < token.txt')."
    )

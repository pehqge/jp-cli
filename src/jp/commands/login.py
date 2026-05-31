"""``jp login`` -- save a named API-token credential, interactively.

The token VALUE is written to a private 0600 file on this machine and is never
echoed, logged, or committed; nothing is ever sent anywhere except as the
``Authorization`` header to your own JupyterHub. You give each credential a NAME
(e.g. the server it belongs to) and choose where to keep it:

  * global -- usable from any directory (``~/.config/jp/``)
  * local  -- usable only inside the current workspace (``<repo>/.jp/``)

Run it as many times as you like to store multiple servers' tokens; ``jp clone``
and ``jp init`` then let you pick which one a workspace uses. See credentials.py.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .. import config as config_mod
from .. import credentials, ui
from ..errors import EXIT_OK, AuthError, UsageError
from ..paths import find_root


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("login", help="save a named API-token credential")
    p.add_argument("--name", default="", help="name for this credential/server (e.g. ufsc)")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument(
        "--global",
        dest="scope_global",
        action="store_true",
        help="save the credential globally (usable from anywhere)",
    )
    scope.add_argument(
        "--local",
        dest="scope_local",
        action="store_true",
        help="save the credential only in the current workspace",
    )
    p.add_argument(
        "--token-path",
        default="",
        help="register an existing token file by path instead of pasting a value",
    )
    p.add_argument(
        "--token-stdin",
        "--stdin",
        dest="token_stdin",
        action="store_true",
        help="read the token from stdin instead of prompting",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing credential of the same name",
    )
    p.set_defaults(func=run)


_INSTRUCTIONS = (
    "To get a JupyterHub API token:",
    "  1. Open your JupyterHub in a browser and log in.",
    "  2. Go to the Token page (the 'Token' link, or <your-hub>/hub/token).",
    "  3. Click 'Request new API token' and copy it (it is shown only once).",
)


def _acquire_token(args: argparse.Namespace) -> str:
    if args.token_stdin or not sys.stdin.isatty():
        token = sys.stdin.readline().strip()
    else:
        for line in _INSTRUCTIONS:
            ui.info(line)
        ui.info("")
        # getpass does not echo the token to the terminal.
        token = getpass.getpass("Paste your API token (input hidden): ").strip()
    if not token:
        raise AuthError("no token provided")
    ui.register_secret(token)
    return token


def _ask_name(args: argparse.Namespace) -> str:
    name = (args.name or "").strip()
    if not name:
        if not sys.stdin.isatty():
            raise UsageError("a credential name is required (pass --name NAME)")
        name = input("Name this server/credential (e.g. ufsc): ").strip()
    return credentials.validate_name(name)


def _ask_scope(args: argparse.Namespace, in_repo: bool) -> str:
    if args.scope_local:
        if not in_repo:
            raise UsageError(
                "--local requires being inside a jp workspace; cd into one or use --global"
            )
        return "local"
    if args.scope_global:
        return "global"
    # No flag: decide. Outside a repo only global makes sense.
    if not in_repo:
        return "global"
    if sys.stdin.isatty():
        ans = input("Save in THIS workspace only (local) or globally? [g/l] (default g): ")
        return "local" if ans.strip().lower().startswith("l") else "global"
    return "global"


def run(args: argparse.Namespace) -> int:
    root = find_root()  # may be None -- login works outside a workspace (global only)

    name = _ask_name(args)
    scope = _ask_scope(args, in_repo=root is not None)

    if args.token_path:
        cred = credentials.add_path(
            name, args.token_path, scope=scope, root=root, overwrite=args.force
        )
        # Validate it is readable now (registers + redacts the value).
        credentials.read_token(cred)
    else:
        token = _acquire_token(args)
        cred = credentials.add(name, token, scope=scope, root=root, overwrite=args.force)

    # If we're inside a workspace, make it use this credential right away.
    if root is not None:
        cfg = config_mod.load(root)
        cfg.credential = cred.name
        config_mod.save(root, cfg)
        ui.success(f"saved {scope} credential {cred.name!r}; this workspace will use it")
    else:
        ui.success(f"saved {scope} credential {cred.name!r}")
    return EXIT_OK

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
import re
import sys
import webbrowser

from .. import config as config_mod
from .. import credentials, ui, urls
from ..errors import EXIT_OK, AuthError, UsageError
from ..paths import find_root


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("login", help="save a named API-token credential")
    p.add_argument(
        "--url",
        default="",
        help="Jupyter URL to link this credential to its site",
    )
    p.add_argument("--name", default="", help="name for this credential/server (e.g. myserver)")
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
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the token page in a browser",
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


def _ask_site(args: argparse.Namespace) -> tuple[str, str]:
    """Ask for (or read) a Jupyter URL and return ``(origin, username)``.

    The URL only links the credential to its server; an empty value is fine.
    Anything that is not an http(s) URL is ignored (with a warning) so a typo
    never blocks the login. Returns ``("", "")`` when there is no usable URL.
    """
    url = (args.url or "").strip()
    if not url:
        if sys.stdin.isatty():
            url = input(
                "Paste your Jupyter URL (to link this credential to its server), or leave blank: "
            ).strip()
        else:
            url = ""
    if not url:
        return "", ""
    origin = urls.origin_of(url)
    if not origin:
        ui.warn(f"ignoring {url!r}: not an http(s) URL; the credential will have no site")
        return "", ""
    return origin, urls.username_of(url)


def _default_name(origin: str, user: str) -> str:
    """Suggest a credential name from the site origin and (optional) username.

    ``<user>-<host>`` when both are known, else just ``<host>``. The result is
    sanitized to the ``validate_name`` alphabet; if nothing valid survives we
    return ``""`` so the caller falls back to asking outright.
    """
    host = origin
    for scheme in ("https://", "http://"):
        if host.startswith(scheme):
            host = host[len(scheme) :]
            break
    if origin == "":
        host = ""
    candidate = f"{user}-{host}" if user and host else host
    # Map to [A-Za-z0-9._-], must start alnum, no runs of '-', max 64 chars.
    candidate = re.sub(r"[^A-Za-z0-9._-]", "-", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate)
    candidate = re.sub(r"^[^A-Za-z0-9]+", "", candidate)[:64].rstrip("-._")
    try:
        return credentials.validate_name(candidate)
    except UsageError:
        return ""


def _ask_name(args: argparse.Namespace, default: str = "") -> str:
    name = (args.name or "").strip()
    if not name:
        if not sys.stdin.isatty():
            if default:
                name = default
            else:
                raise UsageError("a credential name is required (pass --name NAME)")
        else:
            prompt = (
                f"Name this server/credential [{default}]: "
                if default
                else "Name this server/credential (e.g. myserver): "
            )
            ans = input(prompt).strip()
            name = ans or default
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


def _maybe_open_token_page(origin: str, args: argparse.Namespace) -> None:
    """Point the user at the hub's token page (and optionally open a browser).

    Only acts when we know the ``origin`` and ``--no-browser`` was not passed.
    Interactively we offer to open ``<origin>/hub/token`` (default yes); with no
    tty we just print the URL so the user can open it themselves.
    """
    if not origin or args.no_browser:
        return
    token_url = f"{origin}/hub/token"
    if sys.stdin.isatty():
        ui.info(f"Opening the token page to create an API token: {token_url}")
        ans = input("Open it in your browser now? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            webbrowser.open(token_url)
        ui.info("Generate a token there and paste it below.")
    else:
        ui.info(f"Create an API token here: {token_url}")


def run(args: argparse.Namespace) -> int:
    root = find_root()  # may be None -- login works outside a workspace (global only)

    origin, user = _ask_site(args)
    name = _ask_name(args, default=_default_name(origin, user))
    scope = _ask_scope(args, in_repo=root is not None)
    _maybe_open_token_page(origin, args)

    if args.token_path:
        cred = credentials.add_path(
            name, args.token_path, scope=scope, root=root, overwrite=args.force, site=origin
        )
        # Validate it is readable now (registers + redacts the value).
        credentials.read_token(cred)
    else:
        token = _acquire_token(args)
        cred = credentials.add(
            name, token, scope=scope, root=root, overwrite=args.force, site=origin
        )

    # If we're inside a workspace, make it use this credential right away.
    if root is not None:
        cfg = config_mod.load(root)
        cfg.credential = cred.name
        config_mod.save(root, cfg)
        ui.success(f"saved {scope} credential {cred.name!r}; this workspace will use it")
    else:
        ui.success(f"saved {scope} credential {cred.name!r}")
    return EXIT_OK

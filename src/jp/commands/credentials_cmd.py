"""``jp credentials`` -- list, edit, or delete saved credentials.

With no flags, opens an interactive manager (arrows to move, ``d`` delete,
``s`` set site, ``r`` rename, ``q`` quit); in a non-interactive shell it falls
back to printing a table. Scriptable flags (``--list``, ``--rm``, ``--rename``,
``--set-site``) cover automation. A token VALUE is NEVER printed by any path.
"""

from __future__ import annotations

import argparse
import sys


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("credentials", help="list, edit, or delete saved credentials")
    p.add_argument("--list", action="store_true", help="print saved credentials and exit")
    p.add_argument("--rm", default="", metavar="NAME", help="delete the credential named NAME")
    p.add_argument(
        "--rename",
        nargs=2,
        metavar=("OLD", "NEW"),
        default=None,
        help="rename credential OLD to NEW",
    )
    p.add_argument(
        "--set-site",
        nargs=2,
        metavar=("NAME", "URL"),
        default=None,
        help="set NAME's site to the origin of URL",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="skip the delete confirmation prompt",
    )
    p.set_defaults(func=run)


def _print_table(creds) -> None:
    from .. import ui

    ui.info("NAME  SCOPE  SITE")
    for c in creds:
        ui.info(f"{c.name}  {c.scope}  {c.site or '-'}")


def run(args: argparse.Namespace) -> int:
    from .. import config as config_mod
    from .. import credentials, tui, ui, urls
    from ..errors import EXIT_OK, UsageError
    from ..paths import find_root

    root = find_root()  # may be None -- only global credentials exist outside a workspace

    def _find(name: str):
        for cred in credentials.list_credentials(root):
            if cred.name == name:
                return cred
        raise UsageError(f"no saved credential named {name!r}")

    # --- scriptable flags (one action at a time) ---------------------------
    if args.set_site is not None:
        name, url = args.set_site
        cred = _find(name)
        origin = urls.origin_of(url)
        if not origin:
            raise UsageError(f"not a valid http(s) URL: {url!r}")
        credentials.set_site(name, origin, scope=cred.scope, root=root)
        ui.success(f"{name!r} site set to {origin}")
        return EXIT_OK

    if args.rename is not None:
        old, new = args.rename
        cred = _find(old)
        credentials.rename(old, new, scope=cred.scope, root=root)
        ui.success(f"renamed credential {old!r} to {new!r}")
        return EXIT_OK

    if args.rm:
        name = args.rm
        cred = _find(name)
        is_tty = sys.stdin.isatty()
        if not args.force and not is_tty:
            raise UsageError("refusing to delete without --force in a non-interactive shell")
        if not args.force and is_tty:
            ans = input(f"Delete credential {name!r} ({cred.scope})? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                ui.info("aborted")
                return EXIT_OK
        if root is not None:
            try:
                cfg = config_mod.load(root)
                if cfg.credential == name:
                    ui.warn(f"this workspace currently uses credential {name!r}")
            except Exception:
                pass
        credentials.remove(name, scope=cred.scope, root=root)
        ui.success(f"deleted credential {name!r}")
        return EXIT_OK

    if args.list:
        _print_table(credentials.list_credentials(root))
        return EXIT_OK

    # --- no flags ----------------------------------------------------------
    available = credentials.list_credentials(root)
    if not available:
        ui.info("no saved credentials. Run 'jp login' to add one.")
        return EXIT_OK

    if not tui.interactive():
        _print_table(available)
        return EXIT_OK

    def on_delete(cred):
        credentials.remove(cred.name, scope=cred.scope, root=root)
        return True

    def on_set_site(cred, raw):
        origin = urls.origin_of(raw)
        if not origin:
            return ""
        credentials.set_site(cred.name, origin, scope=cred.scope, root=root)
        return origin

    def on_rename(cred, new):
        try:
            credentials.rename(cred.name, new, scope=cred.scope, root=root)
            return True
        except Exception as e:
            ui.warn(str(e))
            return False

    tui.credential_manager(
        list(available),
        on_delete=on_delete,
        on_set_site=on_set_site,
        on_rename=on_rename,
        title="Saved credentials",
    )
    return EXIT_OK

"""``jp config`` -- view and edit workspace configuration.

With no action, opens an interactive settings screen (arrows to move, Space to
change, ``i`` for info, ``/`` to search, Enter to save, Esc to cancel) -- much
like Claude Code's settings. In a non-interactive shell it falls back to
printing the current settings.

The scriptable forms still work for automation:

    jp config list
    jp config get <key>
    jp config set <key> <value>
"""

from __future__ import annotations

import argparse

from .. import config as config_mod
from .. import credentials, global_prefs, tui, ui
from ..errors import EXIT_OK, UsageError
from ..settings_schema import BY_KEY, SPECS
from ._context import load_repo


def _run_global(args: argparse.Namespace) -> int:
    if args.action == "get":
        ui.info(f"{global_prefs.get(args.key)}")
        return EXIT_OK
    if args.value is None:
        raise UsageError("config set requires a key and a value")
    value = global_prefs.coerce_bool(args.value)
    global_prefs.set(args.key, value)
    ui.success(f"set {args.key} = {str(value).lower()}")
    return EXIT_OK


# Connection fields shown as read-only context above the editable settings.
_CONNECTION_KEYS = ("base_url", "prefix", "credential", "token_path")


def _token_source(cfg: config_mod.Config, root: object) -> str:
    """Human-readable description of where this workspace's token comes from.

    Prefers the named credential (the modern path); falls back to a direct
    ``token_path`` in the config; otherwise reports that none is configured.
    Never prints the token value itself -- only its name/location.
    """
    if cfg.credential:
        from pathlib import Path

        cred = credentials.resolve(cfg.credential, Path(str(root)))
        if cred is not None:
            return f"{cfg.credential} ({cred.scope}: {cred.token_path})"
        return f"{cfg.credential} (not found -- run 'jp login --name {cfg.credential}')"
    if cfg.token_path:
        return cfg.token_path
    return "(unset -- run 'jp login')"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("config", help="view/edit settings (interactive by default)")
    p.add_argument("action", nargs="?", choices=["get", "set", "list"], help="scriptable action")
    p.add_argument("key", nargs="?", help="config key")
    p.add_argument("value", nargs="?", help="value to set")
    p.set_defaults(func=run)


def _print_list(cfg: config_mod.Config) -> None:
    for key in (*_CONNECTION_KEYS, *(s.key for s in SPECS)):
        ui.info(f"{key} = {getattr(cfg, key, '')}")


def run(args: argparse.Namespace) -> int:
    # Machine-wide preferences (notifier / auto-update) are handled before
    # workspace resolution, so they work from any directory.
    if args.action in ("get", "set") and args.key in global_prefs.GLOBAL_KEYS:
        return _run_global(args)

    ctx = load_repo()
    cfg = ctx.cfg

    # --- scriptable paths ---------------------------------------------------
    if args.action == "list":
        _print_list(cfg)
        ui.info("")
        ui.heading("machine settings (global):")
        for key in sorted(global_prefs.GLOBAL_KEYS):
            ui.info(f"{key} = {str(global_prefs.get(key)).lower()}")
        return EXIT_OK
    if args.action == "get":
        if not args.key:
            raise UsageError("config get requires a key")
        ui.info(f"{getattr(cfg, args.key, '')}")
        return EXIT_OK
    if args.action == "set":
        if not args.key or args.value is None:
            raise UsageError("config set requires a key and a value")
        if not hasattr(cfg, args.key):
            raise UsageError(f"unknown config key: {args.key}")
        spec = BY_KEY.get(args.key)
        value: object = args.value
        if spec is not None:
            try:
                value = spec.coerce(args.value)
            except (TypeError, ValueError) as exc:
                raise UsageError(f"invalid value for {args.key}: {args.value!r} ({exc})") from exc
            if spec.options and value not in spec.options:
                allowed = ", ".join(spec.fmt(o) for o in spec.options)
                raise UsageError(f"{args.key} must be one of: {allowed}")
        setattr(cfg, args.key, value)
        config_mod.save(ctx.root, cfg)
        ui.success(f"set {args.key} = {spec.fmt(value) if spec else value}")
        return EXIT_OK

    # --- interactive (no action) -------------------------------------------
    if not tui.interactive():
        # Non-interactive shell: just show the settings (never block on a prompt).
        _print_list(cfg)
        ui.info("\n(run in a terminal for the interactive editor, or use 'jp config set')")
        return EXIT_OK

    # Connection context (read-only here; change via 'jp config set').
    ui.heading(f"jp workspace: {ctx.root}")
    ui.detail(f"  base_url = {cfg.base_url or '(unset)'}")
    ui.detail(f"  prefix   = {cfg.prefix or '(unset)'}")
    ui.detail(f"  token    = {_token_source(cfg, ctx.root)}")
    ui.info("")

    rows = [
        tui.Setting(
            key=s.key,
            label=s.label,
            value=getattr(cfg, s.key),
            options=s.options,
            help_text=s.help_text,
            fmt=s.fmt,
        )
        for s in SPECS
    ]
    result = tui.settings_menu(rows, title="Settings")
    if result is None:
        ui.info("no changes saved")
        return EXIT_OK

    changed = [r for r in result if r.changed]
    if not changed:
        ui.info("no changes")
        return EXIT_OK
    for r in changed:
        setattr(cfg, r.key, r.value)
    config_mod.save(ctx.root, cfg)
    ui.success(f"saved {len(changed)} change(s): " + ", ".join(r.key for r in changed))
    return EXIT_OK

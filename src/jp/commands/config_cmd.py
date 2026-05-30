"""``jp config`` -- view or set repo config values (never prints the token).

Only the token *path* is ever shown; the token value lives outside the config
and is never read or printed by this command.
"""

from __future__ import annotations

import argparse

from .. import config as config_mod
from .. import ui
from ..errors import EXIT_OK, UsageError
from ..paths import validate_prefix
from ._context import load_repo

_SETTABLE = {"base_url", "prefix", "token_path", "dotfiles"}


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("config", help="view or set repo configuration")
    p.add_argument("key", nargs="?", help="config key to read or set")
    p.add_argument("value", nargs="?", help="new value (omit to read)")
    p.add_argument("--list", action="store_true", help="list all config values")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    cfg = ctx.cfg

    if args.list or (not args.key):
        ui.heading("config:")
        ui.out(f"  base_url   = {cfg.base_url}")
        ui.out(f"  prefix     = {cfg.prefix}")
        ui.out(f"  dotfiles   = {cfg.dotfiles}")
        # Only the PATH is shown, never the token value.
        ui.out(f"  token_path = {cfg.token_path or '(unset)'}")
        return EXIT_OK

    key = args.key
    if key not in _SETTABLE:
        raise UsageError(f"unknown config key: {key} (settable: {sorted(_SETTABLE)})")

    if args.value is None:
        val = getattr(cfg, key)
        ui.out(str(val))
        return EXIT_OK

    new = args.value
    if key == "prefix":
        new = validate_prefix(new)  # refuse shared/root prefixes on set too
    if key == "base_url" and not new.startswith(("http://", "https://")):
        raise UsageError("base_url must be an http(s) URL")
    if key == "dotfiles" and new != "skip":
        raise UsageError("dotfiles only supports 'skip' in phase 1")
    setattr(cfg, key, new)
    config_mod.save(ctx.root, cfg)
    ui.success(f"set {key} = {new}")
    return EXIT_OK

"""``jp version`` -- print the jp version (optionally with release notes)."""

from __future__ import annotations

import argparse

from .. import __version__, ui
from ..errors import EXIT_NETWORK, EXIT_OK


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("version", help="print the jp version")
    p.add_argument(
        "--changelog", action="store_true", help="also show this version's release notes"
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ui.out(f"jp {__version__}")
    if getattr(args, "changelog", False):
        from .. import changelog as _cl

        rel = _cl.release_for(__version__) or _cl.latest_release()
        if rel is None:
            ui.warn("could not fetch release notes from GitHub.")
            return EXIT_NETWORK
        ui.out("")
        _cl.render(rel)
    return EXIT_OK

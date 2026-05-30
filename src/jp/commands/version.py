"""``jp version`` -- print the jp version."""

from __future__ import annotations

import argparse

from .. import __version__, ui
from ..errors import EXIT_OK


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("version", help="print the jp version")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ui.out(f"jp {__version__}")
    return EXIT_OK

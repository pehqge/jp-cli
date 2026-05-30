"""``jp ignore`` -- view or append patterns to ``.jpignore``."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import ui
from ..errors import EXIT_OK
from ..ignore import IGNORE_NAME
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("ignore", help="view or add .jpignore patterns")
    p.add_argument("pattern", nargs="?", help="pattern to append (omit to list)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    path = Path(ctx.root) / IGNORE_NAME

    if not args.pattern:
        if path.is_file():
            ui.heading(f"{IGNORE_NAME}:")
            for line in path.read_text(encoding="utf-8").splitlines():
                ui.out(f"  {line}")
        else:
            ui.info(f"no {IGNORE_NAME} yet")
        return EXIT_OK

    existing = ""
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        if not existing.endswith("\n") and existing:
            existing += "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(existing + args.pattern.strip() + "\n")
    ui.success(f"added pattern to {IGNORE_NAME}: {args.pattern.strip()}")
    return EXIT_OK

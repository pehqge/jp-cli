"""``jp changelog`` -- show release notes from GitHub."""

from __future__ import annotations

import argparse

from .. import __version__, ui
from .. import changelog as _cl
from ..errors import EXIT_NETWORK, EXIT_OK


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("changelog", help="show jp release notes")
    p.add_argument("version", nargs="?", help="show notes for a specific version (e.g. 1.2.0)")
    p.add_argument("--all", action="store_true", help="show recent releases")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.version:
        rel = _cl.release_for(args.version)
        if rel is None:
            ui.warn("could not fetch that release from GitHub.")
            return EXIT_NETWORK
        _cl.render(rel)
        return EXIT_OK

    if args.all:
        rels = _cl.releases_since("0")
        if not rels:
            ui.warn("could not fetch releases from GitHub.")
            return EXIT_NETWORK
        for rel in rels:
            _cl.render(rel)
            ui.out("")
        return EXIT_OK

    rels = _cl.releases_since(__version__)
    if rels:
        ui.info(f"jp {__version__} -- newer releases available:\n")
        for rel in rels:
            _cl.render(rel)
            ui.out("")
        return EXIT_OK

    rel = _cl.latest_release()
    if rel is None:
        ui.warn("could not fetch releases from GitHub.")
        return EXIT_NETWORK
    ui.success(f"jp {__version__} is up to date. Latest release:")
    _cl.render(rel)
    return EXIT_OK

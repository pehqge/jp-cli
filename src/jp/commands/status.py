"""``jp status`` -- read-only summary of local vs remote vs index state.

Guaranteed read-only: it calls ``sync.diff`` which never writes locally or
remotely and never touches the index.
"""

from __future__ import annotations

import argparse

from .. import paths, ui
from ..errors import EXIT_OK, EXIT_SAFETY
from ..sync import Change
from . import _context
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("status", help="show local/remote sync status (read-only)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    api = _context.build_api(ctx.cfg)
    from .. import sync

    states = sync.diff(ctx.root, ctx.cfg, api, ctx.index, ctx.ignore)

    buckets: dict[Change, list[str]] = {c: [] for c in Change}
    hidden: list[str] = []
    for st in states:
        if st.change == Change.UNCHANGED:
            continue
        if (
            paths.is_hidden(st.rel)
            and st.local_exists
            and st.change
            in (
                Change.LOCAL_NEW,
                Change.LOCAL_MODIFIED,
                Change.CONFLICT,
            )
        ):
            hidden.append(st.rel)
            continue
        buckets[st.change].append(st.rel)

    _section("local-only (to push)", buckets[Change.LOCAL_NEW])
    _section("locally modified (to push)", buckets[Change.LOCAL_MODIFIED])
    _section("remote-only (to pull)", buckets[Change.REMOTE_NEW])
    _section("remotely modified (to pull)", buckets[Change.REMOTE_MODIFIED])
    _section("CONFLICTS (resolve manually)", buckets[Change.CONFLICT])
    _section("hidden/dotfiles (will be skipped on push)", hidden)

    total = sum(len(v) for v in buckets.values()) + len(hidden)
    if total == 0:
        ui.success("clean: everything is in sync")
    else:
        ui.info(f"{total} path(s) differ")

    return EXIT_SAFETY if buckets[Change.CONFLICT] else EXIT_OK


def _section(title: str, items: list[str]) -> None:
    if not items:
        return
    ui.heading(title + ":")
    ui.bullets(sorted(items), indent="    ")

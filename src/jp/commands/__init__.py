"""Command registry: each module exposes add_parser(subparsers)."""

from __future__ import annotations

from . import (
    changelog,
    clone,
    config_cmd,
    credentials_cmd,
    diff,
    doctor,
    ignore_cmd,
    init,
    kernel,
    login,
    ls,
    open_cmd,
    pull,
    push,
    rm,
    run,
    status,
    terminal,
    update,
    version,
)

ALL = [
    clone,
    init,
    login,
    credentials_cmd,
    pull,
    push,
    status,
    ls,
    diff,
    config_cmd,
    ignore_cmd,
    rm,
    run,
    kernel,
    open_cmd,
    terminal,
    doctor,
    update,
    version,
    changelog,
]

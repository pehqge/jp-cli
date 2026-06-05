"""Command registry: each module exposes add_parser(subparsers)."""

from __future__ import annotations

from . import (
    changelog,
    clone,
    config_cmd,
    diff,
    doctor,
    ignore_cmd,
    init,
    kernel,
    login,
    ls,
    pull,
    push,
    rm,
    status,
    terminal,
    update,
    version,
)

ALL = [
    clone,
    init,
    login,
    pull,
    push,
    status,
    ls,
    diff,
    config_cmd,
    ignore_cmd,
    rm,
    kernel,
    terminal,
    doctor,
    update,
    version,
    changelog,
]

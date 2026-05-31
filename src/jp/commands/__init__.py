"""Command registry: each module exposes add_parser(subparsers)."""

from __future__ import annotations

from . import (
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
    doctor,
    update,
    version,
]

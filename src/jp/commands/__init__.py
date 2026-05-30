"""jp subcommands. Each module exposes ``add_parser(subparsers)`` and ``run(args)``.

``run`` returns an integer exit code (or raises a :class:`jp.errors.JpError`,
which the dispatcher maps to its ``exit_code``).
"""

from __future__ import annotations

from . import (
    clone,
    config_cmd,
    diff,
    doctor,
    ignore_cmd,
    init,
    login,
    ls,
    pull,
    push,
    rm,
    status,
    version,
)

# Ordered for a sensible --help listing.
ALL = [
    init,
    clone,
    login,
    status,
    diff,
    push,
    pull,
    ls,
    rm,
    ignore_cmd,
    config_cmd,
    doctor,
    version,
]

__all__ = ["ALL"]

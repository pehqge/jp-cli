"""Hidden ``jp _update-check`` -- the background worker for the update notifier.

Spawned detached by ``update_notify``. Does the network check, rewrites the
cache, and -- when ``auto_update`` is enabled and the install is upgradeable --
performs the upgrade and records an announcement for the next session. Always
exits 0 and never prints to the user (it runs with stdout/stderr to DEVNULL).
"""

from __future__ import annotations

import argparse
import contextlib

from .. import __version__
from ..errors import EXIT_OK


def _perform_auto_update() -> bool:
    """Run the upgrade non-interactively. Returns True on success."""
    from . import update as _update

    args = argparse.Namespace(check=False, source="auto", quiet=True)
    return _update.run(args) == EXIT_OK


def run(args: argparse.Namespace) -> int:
    with contextlib.suppress(Exception):
        _run()
    return EXIT_OK


def _run() -> None:
    import time

    from .. import global_prefs, update_notify
    from . import update as _update

    latest = _update._latest_release_tag()
    cache = update_notify._load_cache()
    cache["last_check"] = time.time()
    cache["checked_version"] = __version__
    if latest:
        cache["latest"] = latest
    update_notify._save_cache(cache)

    if not latest or _update._norm(latest) <= _update._norm(__version__):
        return
    if not global_prefs.get("auto_update", False):
        return
    if _update._editable_source() or _update._running_as_binary() or update_notify._is_ci():
        return
    if _perform_auto_update():
        cache = update_notify._load_cache()
        cache["pending_announcement"] = {"from": __version__, "to": latest.lstrip("vV")}
        update_notify._save_cache(cache)

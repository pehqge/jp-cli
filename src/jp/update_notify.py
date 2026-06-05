"""Passive 'update available' notifier (npm/gh-style).

The hot path reads a small JSON cache only -- never the network -- so it adds no
latency. When the cache is stale, a detached ``jp _update-check`` worker does the
network call (and, if enabled, the auto-update) and rewrites the cache; the
notice therefore appears on the *next* command, never blocking the current one.

Cross-module references (``global_prefs``, ``commands.update``) are imported
lazily inside functions to avoid an import cycle through ``commands/__init__``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, ui

_TTL_SECONDS = 24 * 60 * 60
_CACHE_NAME = "update-check.json"
_EXCLUDED_COMMANDS = frozenset({"update", "version", "_update-check"})
_CI_ENV_VARS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "BUILDKITE",
    "JENKINS_URL",
    "TEAMCITY_VERSION",
)


def _cache_path() -> Path:
    from .credentials import global_dir

    return global_dir() / _CACHE_NAME


def _load_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(data: dict) -> None:
    from .paths import atomic_write

    atomic_write(_cache_path(), json.dumps(data).encode("utf-8"))


def _is_ci() -> bool:
    return any(os.environ.get(v) for v in _CI_ENV_VARS)


def _notifier_pref_on() -> bool:
    from . import global_prefs

    return bool(global_prefs.get("update_notifier", True))


def _install_is_special() -> bool:
    """True for editable/dev or frozen installs, where notifying is noise."""
    from .commands import update as _update

    return bool(_update._editable_source() or _update._running_as_binary())


def _suppressed(args) -> bool:
    if getattr(args, "quiet", False):
        return True
    if os.environ.get("JP_NO_UPDATE_NOTIFIER"):
        return True
    if _is_ci():
        return True
    if not getattr(sys.stderr, "isatty", lambda: False)():
        return True
    if getattr(args, "command", None) in _EXCLUDED_COMMANDS:
        return True
    if not _notifier_pref_on():
        return True
    return bool(_install_is_special())


def _jp_executable() -> list[str]:
    exe = shutil.which("jp") or sys.argv[0] or ""
    name = Path(exe).name.lower()
    if exe and not name.startswith("python") and not exe.endswith(".py"):
        return [exe]
    return [sys.executable, "-m", "jp"]


def _spawn_worker() -> None:
    cmd = _jp_executable() + ["_update-check"]
    with contextlib.suppress(OSError):
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _version_is_newer(latest: str) -> bool:
    from .commands import update as _update

    return _update._norm(latest) > _update._norm(__version__)


def _print_notice(latest: str) -> None:
    err = sys.stderr
    latest_clean = latest.lstrip("vV")
    head = ui._wrap(f"jp {__version__} → {latest_clean}", ui._Style.BOLD, err)
    tag = ui._wrap("(update available)", ui._Style.YELLOW, err)
    hint = ui._wrap(
        "run `jp changelog` to see what's new · `jp update` to upgrade",
        ui._Style.DIM,
        err,
    )
    print(f"{head}  {tag}", file=err)
    print(hint, file=err)


def _print_announcement(ann: dict) -> None:
    msg = (
        f"✓ jp auto-updated {ann.get('from', '?')} → {ann.get('to', '?')}"
        " — run `jp changelog` to see what's new"
    )
    print(ui._wrap(msg, ui._Style.GREEN, sys.stderr), file=sys.stderr)


def maybe_notify(args) -> None:
    """Show the notice if warranted; spawn a refresh if the cache is stale.

    Wrapped so it can never raise into ``main()`` or change the exit code.
    """
    with contextlib.suppress(Exception):
        _maybe_notify(args)


def _maybe_notify(args) -> None:
    if _suppressed(args):
        return
    cache = _load_cache()
    ann = cache.get("pending_announcement")
    if ann:
        _print_announcement(ann)
        cache.pop("pending_announcement", None)
        _save_cache(cache)
        return
    last = float(cache.get("last_check") or 0)
    if (time.time() - last) > _TTL_SECONDS:
        cache["last_check"] = time.time()  # debounce: avoid a spawn storm
        _save_cache(cache)
        _spawn_worker()
    latest = cache.get("latest")
    if latest and _version_is_newer(latest):
        _print_notice(latest)

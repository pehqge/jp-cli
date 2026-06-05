"""Per-user global preferences (``~/.config/jp/prefs.json``).

Small, non-secret settings that persist across workspaces -- e.g. the saved
``jp open`` action chosen via "remember & skip next time". This is deliberately
separate from per-repo ``.jp/config.json`` (workspace state) and from the
credential registry (secrets): preferences are global, plain, and best-effort.

Every function is forgiving: a missing or corrupt file reads as "no preference"
and a failed write never raises (a preference that does not persist is not worth
crashing a command over).
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

PREFS_NAME = "prefs.json"


def _prefs_path() -> Path:
    from .credentials import global_dir

    return global_dir() / PREFS_NAME


def _read() -> dict[str, Any]:
    path = _prefs_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str, default: str = "") -> str:
    """Return the stored string preference for ``key`` (or ``default``)."""
    value = _read().get(key, default)
    return value if isinstance(value, str) else default


def set(key: str, value: str) -> None:  # noqa: A001 -- mirrors dict.set ergonomics
    """Persist ``value`` under ``key``. Best-effort: never raises."""
    data = _read()
    data[key] = value
    _write(data)


def clear(key: str) -> None:
    """Remove ``key`` if present. Best-effort: never raises."""
    data = _read()
    if key in data:
        del data[key]
        _write(data)


def _write(data: dict[str, Any]) -> None:
    path = _prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        # A preference that fails to persist is not fatal; the command still ran.
        pass

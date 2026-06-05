"""User-global jp preferences (machine-wide), stored at ``~/.config/jp/prefs.json``.

Distinct from the per-repo ``.jp/config.json``: these govern jp itself (the
update notifier and opt-in auto-update), not a workspace. Corruption-safe;
written atomically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PREFS_NAME = "prefs.json"
_DEFAULTS: dict[str, Any] = {"auto_update": False, "update_notifier": True}
GLOBAL_KEYS = frozenset(_DEFAULTS)


def _path() -> Path:
    from .credentials import global_dir

    return global_dir() / _PREFS_NAME


def load() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get(key: str, default: Any = None) -> Any:
    if default is None and key in _DEFAULTS:
        default = _DEFAULTS[key]
    return load().get(key, default)


def set(key: str, value: Any) -> None:  # noqa: A001 - intentional public name
    from .paths import atomic_write

    data = load()
    data[key] = value
    atomic_write(_path(), (json.dumps(data, indent=2) + "\n").encode("utf-8"))


def coerce_bool(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "on")

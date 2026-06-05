"""Global, user-level settings for the workspace-free ``jp live`` command.

A jp *config* (see :mod:`jp.config`) is per-repo: it lives inside a workspace's
``.jp/`` and cannot exist for ``jp live``, which may run from any directory with
no workspace at all. The handful of preferences ``jp live`` needs -- default
mount access, whether to pop a code terminal -- therefore live here, in a single
small JSON file under the user's global config dir.

Storage rules mirror :mod:`jp.credentials`:

  * Path: ``$XDG_CONFIG_HOME/jp/settings.json`` (falling back to
    ``~/.config/jp/settings.json`` when ``XDG_CONFIG_HOME`` is unset/empty).
  * The directory is created on demand; the file is written atomically with
    private 0600 permissions (``os.open`` ``O_CREAT`` 0o600 + ``os.replace``).

Reads are total: a missing or corrupt file yields documented defaults and never
raises. Writes merge, so setting one key never clobbers the other.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

SETTINGS_NAME = "settings.json"

# live.access -- how a freshly mounted live folder is exposed.
ACCESS_ASK = "ask"
ACCESS_WRITABLE = "writable"
ACCESS_READ_ONLY = "read-only"
_ACCESS_VALUES = (ACCESS_ASK, ACCESS_WRITABLE, ACCESS_READ_ONLY)

_DEFAULT_ACCESS = ACCESS_ASK
_DEFAULT_CODE_TERMINAL = True

_KEY_ACCESS = "live.access"
_KEY_CODE_TERMINAL = "live.code_terminal"


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #
def _config_home() -> Path:
    """The user's base config dir: ``$XDG_CONFIG_HOME`` or ``~/.config``."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and xdg.strip():
        return Path(xdg)
    return Path(os.path.expanduser("~/.config"))


def settings_path() -> Path:
    """Absolute path to the settings file (for messages and tests)."""
    return _config_home() / "jp" / SETTINGS_NAME


# --------------------------------------------------------------------------- #
# I/O (private file only)
# --------------------------------------------------------------------------- #
def _read_all() -> dict:
    """Return the raw settings dict, or ``{}`` for a missing/corrupt file."""
    path = settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_private(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` with 0600 perms (dir 0700)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(str(tmp), str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(str(tmp))
        raise


def _set_key(key: str, value: object) -> None:
    """Merge a single key into the settings file (preserves other keys)."""
    data = _read_all()
    data[key] = value
    path = settings_path()
    _write_private(path, json.dumps(data, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def get_live_access() -> str:
    """Default live-mount access. Unknown/missing values fall back to ``ask``."""
    value = _read_all().get(_KEY_ACCESS)
    if isinstance(value, str) and value in _ACCESS_VALUES:
        return value
    return _DEFAULT_ACCESS


def set_live_access(value: str) -> None:
    """Persist the default live-mount access. Rejects values outside the three
    allowed options with ``ValueError``."""
    if value not in _ACCESS_VALUES:
        allowed = ", ".join(repr(v) for v in _ACCESS_VALUES)
        raise ValueError(f"invalid live access {value!r}: expected one of {allowed}")
    _set_key(_KEY_ACCESS, value)


def get_live_code_terminal() -> bool:
    """Whether ``jp live`` opens a code terminal by default."""
    value = _read_all().get(_KEY_CODE_TERMINAL)
    if isinstance(value, bool):
        return value
    return _DEFAULT_CODE_TERMINAL


def set_live_code_terminal(value: bool) -> None:
    """Persist whether ``jp live`` opens a code terminal by default."""
    _set_key(_KEY_CODE_TERMINAL, bool(value))

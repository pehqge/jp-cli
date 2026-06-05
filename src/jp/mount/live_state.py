"""Registry of currently-running ``jp live`` mounts.

``jp live`` runs a foreground process that mounts a remote folder at some local
mountpoint. ``jp live unmount`` is typically run *from inside* that mounted
folder and needs to find the owning process (its pid, the URL it serves, a human
display string) without being told the mountpoint explicitly.

To bridge the two, each active mount drops one small JSON record under
``$XDG_CONFIG_HOME/jp/live/`` (falling back to ``~/.config/jp/live/``). The file
name is a hash of the *resolved* mountpoint, so re-recording the same mount
overwrites cleanly and unrelated mounts never collide.

Matching is done on resolved absolute paths (``os.path.realpath``): a lookup
from the mountpoint itself, or from any directory inside it, finds the record;
an unrelated sibling never matches. When mounts are nested, the deepest
(most-specific) mountpoint wins.

Every read tolerates a missing or corrupt file and never raises; ``remove`` is
best-effort and idempotent. We never call the clock ourselves -- the ``created``
stamp is supplied by the caller (some jp contexts forbid wallclock reads).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path

LIVE_DIRNAME = "live"


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #
def _config_home() -> Path:
    """The user's base config dir: ``$XDG_CONFIG_HOME`` or ``~/.config``."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and xdg.strip():
        return Path(xdg)
    return Path(os.path.expanduser("~/.config"))


def _live_dir() -> Path:
    return _config_home() / "jp" / LIVE_DIRNAME


def _resolve(path: str) -> str:
    """Resolved absolute path used for both storage keys and matching."""
    return os.path.realpath(os.path.expanduser(path))


def _record_path(resolved_mountpoint: str) -> Path:
    digest = hashlib.sha256(resolved_mountpoint.encode("utf-8")).hexdigest()[:32]
    return _live_dir() / f"{digest}.json"


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
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


def _read_record(path: Path) -> dict | None:
    """Load a single record, or ``None`` if missing/corrupt/malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("mountpoint"), str):
        return None
    return data


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def record(
    mountpoint: str,
    *,
    pid: int,
    url: str = "",
    display: str = "",
    created: float = 0.0,
) -> Path:
    """Register an active mount and return the path of the record written.

    ``created`` is stored verbatim (the caller owns the clock); it defaults to
    ``0.0``. The record is keyed by the resolved absolute ``mountpoint``.
    """
    resolved = _resolve(mountpoint)
    rec = {
        "mountpoint": resolved,
        "pid": int(pid),
        "url": str(url),
        "display": str(display),
        "created": float(created),
    }
    path = _record_path(resolved)
    _write_private(path, json.dumps(rec, indent=2) + "\n")
    return path


def list_active() -> list[dict]:
    """Every readable mount record (corrupt/missing files are skipped)."""
    out: list[dict] = []
    live = _live_dir()
    try:
        names = sorted(p.name for p in live.iterdir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = _read_record(live / name)
        if rec is not None:
            out.append(rec)
    return out


def find_for_path(path: str) -> dict | None:
    """The record whose mountpoint equals ``path`` or is an ancestor of it.

    Running from inside a mounted folder (or the folder itself) finds the owning
    mount; an unrelated sibling never matches. The deepest (most-specific)
    mountpoint wins when mounts are nested.
    """
    target = _resolve(path)
    best: dict | None = None
    best_len = -1
    for rec in list_active():
        mount = rec.get("mountpoint")
        if not isinstance(mount, str):
            continue
        is_match = target == mount or target.startswith(mount.rstrip(os.sep) + os.sep)
        if is_match and len(mount) > best_len:
            best = rec
            best_len = len(mount)
    return best


def remove(mountpoint: str) -> None:
    """Delete a mount's record. Best-effort and idempotent; never raises."""
    with contextlib.suppress(OSError):
        os.unlink(str(_record_path(_resolve(mountpoint))))

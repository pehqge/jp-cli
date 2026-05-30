"""The sync index (``.jp/index.json``): the last-known-good 3-way base.

The index records, per relative path, the state both sides agreed on at the end
of the last successful sync. We use it as the *base* for 3-way conflict
detection (DESIGN §6):

    base == local == remote   -> in sync, nothing to do
    base == local, base != remote -> remote changed -> pull updates local
    base == remote, base != local -> local changed  -> push updates remote
    base != local AND base != remote -> CONFLICT -> ABORT (never auto-merge)

CRITICAL: the index is updated only AFTER an operation is verified successful,
one entry at a time. A crash mid-run can never leave the index claiming success
for a transfer that did not happen.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import ConfigError
from .paths import DOT_DIR, normalize_rel

INDEX_NAME = "index.json"
INDEX_VERSION = 1


@dataclass
class Entry:
    """Per-file agreed state from the last successful sync.

    The remote side is tracked by ``last_modified`` + the server's sha256
    (``remote_hash``), NEVER by ``created`` -- the server's ``created`` field is
    unreliable (it changes on overwrite/rename, research §1). For a byte-faithful
    sync, ``remote_hash`` equals ``sha256`` (both are the sha256 of the agreed
    bytes), but we store it separately so a future content-format change cannot
    silently break the cheap remote-equality check.
    """

    sha256: str  # content hash of the agreed bytes (local & remote agree)
    size: int  # byte length
    # Remote validators we can cheaply re-check (without downloading).
    remote_mtime: str = ""  # ISO last_modified the server reported (NOT created)
    remote_hash: str = ""  # server sha256 at sync time (== sha256 normally)
    local_mtime: float = 0.0  # local mtime at sync time (advisory only)


class Index:
    """In-memory index with explicit, post-success persistence."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.entries: dict[str, Entry] = {}

    # --- path ---------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.root / DOT_DIR / INDEX_NAME

    # --- load / save --------------------------------------------------------
    @classmethod
    def load(cls, root: Path) -> Index:
        idx = cls(root)
        p = idx.path
        if not p.is_file():
            return idx  # fresh / empty index is valid
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"could not read index {p}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"malformed index: {p}")
        for rel, data in (raw.get("entries") or {}).items():
            try:
                norm = normalize_rel(rel)
            except Exception:
                # Drop entries with unsafe keys rather than trust them.
                continue
            if isinstance(data, dict) and "sha256" in data:
                sha = str(data.get("sha256", ""))
                idx.entries[norm] = Entry(
                    sha256=sha,
                    size=int(data.get("size", 0)),
                    remote_mtime=str(data.get("remote_mtime", "")),
                    # Backfill remote_hash from sha256 for indexes written before
                    # the field existed (old entries agreed byte-for-byte).
                    remote_hash=str(data.get("remote_hash", "") or sha),
                    local_mtime=float(data.get("local_mtime", 0.0)),
                )
        return idx

    def save(self) -> None:
        """Persist atomically. Call only after verified successful transfers."""
        dot = self.root / DOT_DIR
        dot.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_VERSION,
            "entries": {rel: asdict(e) for rel, e in sorted(self.entries.items())},
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(str(tmp), str(self.path))

    # --- accessors ----------------------------------------------------------
    def get(self, rel: str) -> Entry | None:
        return self.entries.get(normalize_rel(rel))

    def set(self, rel: str, entry: Entry) -> None:
        self.entries[normalize_rel(rel)] = entry

    def remove(self, rel: str) -> None:
        self.entries.pop(normalize_rel(rel), None)

    def __contains__(self, rel: str) -> bool:
        return normalize_rel(rel) in self.entries

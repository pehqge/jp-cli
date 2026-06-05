"""The staging area (``.jp/staged.json``): files marked for the next commit.

This is the versioning counterpart of git's "index" (confusingly, jp already
has a *different* ``.jp/index.json`` -- the sync base for 3-way conflict
detection). To avoid any collision, the staging area is a SEPARATE store with
its own file, and this module NEVER reads or writes ``.jp/index.json``. The two
must stay independent: a commit staging a path must not perturb the sync base,
and a sync updating the base must not perturb what is staged.

Structure & discipline mirror :mod:`jp.index`:
  * keys are :func:`jp.paths.normalize_rel`-normalized relative paths, so
    ``a//b`` and ``./a/b`` collapse to one logical entry and a traversal-y key
    is rejected;
  * :meth:`Staging.load` is TOLERANT -- a missing file is an empty staging area,
    entries with unsafe keys or no ``sha256`` are dropped, but an *unreadable*
    JSON file raises :class:`jp.errors.ConfigError` (corruption is loud, exactly
    as the index behaves);
  * :meth:`Staging.save` writes atomically with private (0o600) permissions
    (temp in the same dir + ``os.replace``), with sorted keys for stable diffs.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigError, SafetyError
from ..paths import DOT_DIR, normalize_rel

STAGED_NAME = "staged.json"
STAGED_VERSION = 1

# Temp-file prefix for in-progress staging writes (no leading dot, mirroring the
# object store's ``objtmp-*`` and refs' ``reftmp-*`` conventions). mkstemp gives a
# UNIQUE name so two concurrent savers never collide on the same temp.
_TMP_PREFIX = "stagetmp-"

# O_NOFOLLOW exists on POSIX; absent on Windows -> fall back to 0 and rely on the
# explicit is_symlink() checks (mirrors objects.py / refs.py).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass
class StagedEntry:
    """One path staged for the next commit.

    ``sha256`` is the content hash of the staged bytes (the object stored in the
    object store). ``size`` is the byte length. ``local_mtime`` is the working
    file's mtime at stage time (advisory: lets a later status quickly tell
    whether the file changed since it was staged).

    ``nb_norm_sha`` is the NOTEBOOK hybrid key: for a ``.ipynb`` file it holds the
    sha256 of the notebook's *normalized* form (see :mod:`jp.versioning.notebooks`
    -- outputs/execution-counts/widget-state stripped), and is "" for every
    non-notebook. It drives hybrid change detection: a notebook is re-staged only
    when its normalized sha changes (a real code edit), NOT when a pure re-run only
    churns outputs. The stored blob (named by ``sha256``) is always the ORIGINAL
    bytes, so checkout stays faithful.
    """

    sha256: str
    size: int
    local_mtime: float = 0.0
    nb_norm_sha: str = ""


class Staging:
    """In-memory staging area with atomic, post-success persistence."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.entries: dict[str, StagedEntry] = {}

    # --- path ---------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.root / DOT_DIR / STAGED_NAME

    # --- load / save --------------------------------------------------------
    @classmethod
    def load(cls, root: Path) -> Staging:
        """Load the staging area; a missing file is a valid empty staging area.

        Tolerant exactly like :meth:`jp.index.Index.load`: unsafe keys and
        entries without a ``sha256`` are dropped; an unreadable/malformed JSON
        file raises :class:`ConfigError`.
        """
        st = cls(root)
        p = st.path
        if not p.is_file():
            return st  # fresh / empty staging area is valid
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"could not read staging area {p}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"malformed staging area: {p}")
        for rel, data in (raw.get("entries") or {}).items():
            try:
                norm = normalize_rel(rel)
            except Exception:
                # Drop entries with unsafe keys rather than trust them.
                continue
            if isinstance(data, dict) and "sha256" in data:
                st.entries[norm] = StagedEntry(
                    sha256=str(data.get("sha256", "")),
                    size=int(data.get("size", 0)),
                    local_mtime=float(data.get("local_mtime", 0.0)),
                    # Tolerant: older staging files predate this field -> default "".
                    nb_norm_sha=str(data.get("nb_norm_sha", "")),
                )
        return st

    def save(self) -> None:
        """Persist atomically + durably with 0o600 perms. Call after staging succeeds.

        Mirrors the package's atomic-write discipline (see
        :meth:`jp.versioning.objects.ObjectStore._atomic_store` /
        :func:`jp.versioning.refs._atomic_write_text`): write to a UNIQUE same-dir
        ``stagetmp-*`` temp via :func:`tempfile.mkstemp`, ``fsync`` the temp fd
        BEFORE :func:`os.replace`, then best-effort ``fsync`` the parent ``.jp`` dir
        so the new name is durable. A crash mid-write therefore never leaves a
        corrupt ``staged.json`` (which would loudly block commits on the next load),
        and a unique temp means two concurrent savers never collide. We refuse a
        symlink at the temp or final path. On ANY failure the temp is removed so no
        partial/garbage file is left. Only ``.jp/staged.json`` is touched -- never
        ``.jp/index.json``.
        """
        dot = self.root / DOT_DIR
        dot.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STAGED_VERSION,
            "entries": {rel: _entry_dict(e) for rel, e in sorted(self.entries.items())},
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        target = self.path
        # Never replace through a symlink planted at the final destination.
        if target.is_symlink():
            raise SafetyError(f"refusing to write the staging area through a symlink: {target}")

        fd, tmp_name = tempfile.mkstemp(prefix=_TMP_PREFIX, dir=str(dot))
        tmp = Path(tmp_name)
        try:
            # mkstemp just created this regular file; a symlink here would mean a
            # hostile race replaced it -- refuse rather than write through it.
            if tmp.is_symlink():
                raise SafetyError(f"refusing to write through a symlinked temp file: {tmp}")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            with contextlib.suppress(OSError):
                os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(target))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(str(tmp))
            raise

        # Rename durability: best-effort fsync of the .jp dir so the new name
        # survives a crash. Unsupported on some platforms (Windows) -> suppress.
        _fsync_dir(dot)

    # --- accessors ----------------------------------------------------------
    def get(self, rel: str) -> StagedEntry | None:
        return self.entries.get(normalize_rel(rel))

    def set(self, rel: str, entry: StagedEntry) -> None:
        self.entries[normalize_rel(rel)] = entry

    def remove(self, rel: str) -> None:
        self.entries.pop(normalize_rel(rel), None)

    def clear(self) -> None:
        self.entries.clear()

    def __contains__(self, rel: str) -> bool:
        return normalize_rel(rel) in self.entries


def _entry_dict(e: StagedEntry) -> dict:
    """Serialize one StagedEntry to its on-disk dict.

    ``nb_norm_sha`` is emitted ONLY for notebooks (a non-empty value), so a plain
    file's entry keeps the exact pre-notebook shape ``{sha256, size, local_mtime}``
    -- no churn in existing staging files and no surprise key for non-notebooks.
    """
    out: dict = {"sha256": e.sha256, "size": e.size, "local_mtime": e.local_mtime}
    if e.nb_norm_sha:
        out["nb_norm_sha"] = e.nb_norm_sha
    return out


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory for rename durability.

    Opens with ``O_NOFOLLOW`` (where available) to refuse a planted symlink in
    place of the ``.jp`` dir. Directory fsync is unsupported on some platforms
    (notably Windows) and on some filesystems -> any ``OSError`` is suppressed
    (durability is best-effort; correctness does not depend on it). Mirrors
    :func:`jp.versioning.objects._fsync_dir` / :func:`jp.versioning.refs._fsync_dir`.
    """
    flags = getattr(os, "O_RDONLY", 0) | _O_NOFOLLOW
    with contextlib.suppress(OSError):
        fd = os.open(str(directory), flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

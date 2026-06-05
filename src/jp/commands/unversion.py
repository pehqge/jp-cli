"""``jp unversion`` -- opt out of versioning by removing the LOCAL version store.

Removes ONLY the local versioning metadata -- HEAD, ``staged.json``, the ``format``
marker, the versioning lock, and the ``objects/`` ``refs/`` ``packs/`` directories
-- and leaves EVERYTHING else untouched: ``config.json``, credentials,
``.jp/index.json`` (the sync base), ``.jp/.gitignore``, and ALL working files. It
NEVER touches the remote: if a mirror exists it only PRINTS how to remove it
manually.

Safety contract
---------------
* Requires confirmation (``ui.confirm`` with ``default=False``). In a NON-tty
  without ``--yes`` it REFUSES with :class:`SafetyError` -- version history is
  never destroyed silently.
* Removal is SYMLINK-SAFE: we never follow a symlink. Files are unlinked only after
  refusing a symlink at the path; directories are walked bottom-up unlinking
  regular files and rmdir-ing dirs, refusing to descend into a symlinked directory
  (mirrors the project's no-symlink-traversal posture). We deliberately do NOT use
  ``shutil.rmtree`` (which would follow a planted symlink out of ``.jp``).
* Runs under the per-repo versioning lock so it cannot race a concurrent commit.
* If there is no local version store at all -> prints "nothing to remove", exit 0.
"""

from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path

from .. import ui
from ..errors import EXIT_OK, SafetyError
from ..paths import DOT_DIR
from ..versioning.lock import LOCK_NAME, versioning_lock

# Files (under .jp) that make up the local version store. NEVER includes
# config.json, credentials*, index.json, or .gitignore.
_VERSIONING_FILES = ("HEAD", "staged.json", "format", LOCK_NAME)
# Directories (under .jp) that make up the local version store.
_VERSIONING_DIRS = ("objects", "refs", "packs")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "unversion",
        help="remove the LOCAL version history (config + working files untouched)",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (required in a non-interactive shell)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo_root()
    dot = ctx / DOT_DIR

    targets_files = [dot / name for name in _VERSIONING_FILES if (dot / name).exists()]
    targets_dirs = [dot / name for name in _VERSIONING_DIRS if (dot / name).exists()]

    if not targets_files and not targets_dirs:
        ui.info("no local version history to remove")
        return EXIT_OK

    if not ui.confirm(
        "This deletes ALL local version history (commits, staged, objects). "
        "Working files and config are untouched. Continue?",
        default=False,
        assume_yes=args.yes,
    ):
        # In a non-tty without --yes, confirm() returns the default (False) WITHOUT
        # prompting. Refuse loudly rather than silently doing nothing OR silently
        # destroying history -- this is the safety gate.
        raise SafetyError(
            "refused: removing version history requires confirmation. "
            "Re-run with --yes to confirm (history is never deleted silently)."
        )

    # Hold the lock so we cannot race a concurrent commit. Note: the lock file is
    # itself one of our targets; we remove it AFTER releasing the lock (the OS lock
    # lives on the open fd, not the path, so unlinking the name later is safe).
    removed_files = 0
    removed_dirs = 0
    with versioning_lock(ctx):
        for d in targets_dirs:
            if _remove_tree_no_symlink(d):
                removed_dirs += 1
        for f in targets_files:
            # Never unlink the live lock file while we hold it under this context;
            # defer the lock file to after the lock is released.
            if f.name == LOCK_NAME:
                continue
            if _safe_unlink_file(f):
                removed_files += 1

    # Now that the lock is released, drop the lock file itself (symlink-safe).
    lock_file = dot / LOCK_NAME
    if lock_file.exists() and _safe_unlink_file(lock_file):
        removed_files += 1

    ui.success(
        f"removed local version history ({removed_files} file(s), {removed_dirs} director(ies)). "
        "Config, credentials, the sync base (index.json), and working files were untouched."
    )
    _print_remote_note(ctx)
    return EXIT_OK


def load_repo_root() -> Path:
    """Locate the repo root (reuses :func:`jp.commands._context.load_repo`)."""
    from ._context import load_repo

    return load_repo().root


def _print_remote_note(root: Path) -> None:
    """Tell the user the remote may still hold history and how to remove it.

    jp NEVER auto-deletes the remote mirror (too dangerous on a shared server), so
    we only print the manual path. The mirror, when present, lives at
    ``<prefix>/__jp/`` under the configured remote prefix.
    """
    prefix = ""
    with contextlib.suppress(Exception):
        from .. import config as config_mod

        cfg = config_mod.load(root)
        prefix = (getattr(cfg, "prefix", "") or "").strip("/")
    location = f"{prefix}/__jp/" if prefix else "<prefix>/__jp/"
    ui.warn(
        "the version history may still exist on the REMOTE; jp does NOT delete it "
        f"automatically. To remove it, delete '{location}' on the server manually "
        "(e.g. via the Jupyter file browser)."
    )


# --------------------------------------------------------------------------- #
# Symlink-safe removal (NEVER follows a symlink; mirrors the package posture)
# --------------------------------------------------------------------------- #
def _safe_unlink_file(path: Path) -> bool:
    """Unlink a single file/symlink-name without following a symlink to its target.

    ``os.unlink`` removes the NAME, not the symlink's target, so unlinking a symlink
    here removes only the planted link (never its target) -- which is exactly what
    we want when clearing ``.jp`` entries. Returns True iff something was removed; a
    file that vanished concurrently is tolerated.
    """
    p = Path(path)
    try:
        os.unlink(str(p))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _remove_tree_no_symlink(directory: Path) -> bool:
    """Recursively remove ``directory``, refusing to descend through any symlink.

    If ``directory`` itself is a symlink we unlink the LINK only (never its target)
    and stop. Otherwise we walk bottom-up: regular files are unlinked, sub-symlinks
    (file or dir) are unlinked as names (never followed), real sub-directories are
    recursed into and then rmdir-ed. This is the deliberate replacement for
    ``shutil.rmtree`` (which would follow a planted symlink out of ``.jp``). Returns
    True iff the directory was removed/processed.
    """
    d = Path(directory)
    if d.is_symlink():
        # A symlink standing in for a versioning dir: remove the link, not its
        # target. We refuse to walk THROUGH it.
        _safe_unlink_file(d)
        return True
    if not d.exists():
        return False

    # Bottom-up walk; followlinks defaults to False so os.walk never descends a
    # symlinked subdir, but we ALSO defensively unlink any symlink we encounter.
    for current, subdirs, files in os.walk(str(d), topdown=False, followlinks=False):
        cur = Path(current)
        for name in files:
            _safe_unlink_file(cur / name)
        for name in subdirs:
            sub = cur / name
            if sub.is_symlink():
                # A symlinked subdir os.walk reported but did not descend: unlink
                # the link name only.
                _safe_unlink_file(sub)
            else:
                with contextlib.suppress(OSError):
                    os.rmdir(str(sub))
    with contextlib.suppress(OSError):
        os.rmdir(str(d))
    return True

"""Per-OS native WebDAV mount/unmount driver.

The OS's built-in WebDAV client mounts the local DavServer (127.0.0.1) as a
folder -- no FUSE driver, no third-party deps. This module builds the platform
commands (pure, unit-tested) and runs them (subprocess, exercised manually).

macOS  : mount_webdav (built in)        -> umount
Linux  : gio mount dav:// (GVfs, no root, GNOME/most desktops) -> gio mount -u
Windows: net use (WebClient redirector) -> net use /delete
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class MountError(Exception):
    pass


@dataclass
class MountPlan:
    """How to mount on this platform (pure data; built without side effects)."""

    argv: list[str]
    note: str  # one-line human hint about what will happen
    needs_existing_dir: bool


def _dav_url_to_scheme(url: str, scheme: str) -> str:
    """Rewrite http://host:port/ to <scheme>://host:port/ (for gio dav://)."""
    if url.startswith("http://"):
        return scheme + url[len("http://") :]
    if url.startswith("https://"):
        return scheme + url[len("https://") :]
    return url


def build_mount_plan(url: str, mountpoint: str, platform: str) -> MountPlan:
    """Build the mount command for ``platform`` ('darwin'|'linux'|'win32'...)."""
    if platform == "darwin":
        return MountPlan(
            ["mount_webdav", "-S", url, mountpoint],
            f"macOS mount_webdav -> {mountpoint}",
            needs_existing_dir=True,
        )
    if platform.startswith("win"):
        return MountPlan(
            ["net", "use", "*", url],
            "Windows net use (assigns a drive letter)",
            needs_existing_dir=False,
        )
    # linux / other unix: GVfs (no root)
    dav = _dav_url_to_scheme(url, "dav://")
    return MountPlan(
        ["gio", "mount", dav],
        "Linux gio mount (GVfs; userspace, no root)",
        needs_existing_dir=False,
    )


def build_unmount_plan(url: str, mountpoint: str, platform: str) -> list[str]:
    if platform == "darwin":
        return ["umount", mountpoint]
    if platform.startswith("win"):
        return ["net", "use", mountpoint, "/delete", "/y"]
    return ["gio", "mount", "-u", _dav_url_to_scheme(url, "dav://")]


def mount(url: str, mountpoint: str, *, platform: str | None = None) -> MountPlan:
    """Run the mount command. Returns the MountPlan used. Raises MountError on failure.

    NOTE: not covered by automated tests (does real I/O); exercised manually.
    """
    plat = platform or sys.platform
    plan = build_mount_plan(url, mountpoint, plat)
    try:
        subprocess.run(plan.argv, check=True, capture_output=True, timeout=30)
    except FileNotFoundError as exc:
        raise MountError(f"mount tool not found: {plan.argv[0]!r}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        raise MountError(f"mount failed: {detail or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MountError("mount timed out") from exc
    return plan


def unmount(url: str, mountpoint: str, *, platform: str | None = None) -> None:
    """Run the unmount command (best-effort; never raises)."""
    plat = platform or sys.platform
    with contextlib.suppress(Exception):
        subprocess.run(build_unmount_plan(url, mountpoint, plat), capture_output=True, timeout=30)


# --------------------------------------------------------------------------- #
# Auto mount handle (per-OS): one friendly handle named after the prefix leaf.
#
# The new ``jp live <URL>`` flow mounts the remote under a single auto-managed
# local handle (a folder on macOS/Linux, a drive letter on Windows). Selecting
# that target and the Windows free-letter logic are PURE functions (unit-tested);
# the actual mounting still happens through the subprocess primitives above.
# --------------------------------------------------------------------------- #
@dataclass
class MountHandle:
    """A mounted remote folder plus how to tear it down.

    ``display``     -- what to show the user / where edits land (folder path, or
                       a drive letter like ``Z:`` on Windows).
    ``open_target`` -- the path/handle to hand to the VS Code launcher.
    ``unmount``     -- release the OS mount (best-effort; never raises).
    ``cleanup``     -- remove only what ``jp`` created (an empty dir or a symlink);
                       never deletes user data.
    """

    display: str
    open_target: str
    unmount: Callable[[], None]
    cleanup: Callable[[], None]


def gvfs_dav_path(host: str, port: int, *, uid: int, ssl: bool = False) -> str:
    """Compute the GVfs mount path for a ``dav://host:port`` mount (pure).

    GNOME's gio mounts a WebDAV share into the per-user GVfs namespace under
    ``/run/user/<uid>/gvfs/`` using a directory name that encodes the share, e.g.
    ``dav:host=127.0.0.1,port=8080,ssl=false``. We compute it so the caller can
    symlink a friendly ``./<leaf>/`` handle to it without parsing ``gio mount -l``.
    """
    spec = f"dav:host={host},port={int(port)},ssl={'true' if ssl else 'false'}"
    return f"/run/user/{int(uid)}/gvfs/{spec}"


def _split_host_port(url: str, default_port: int) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port if parts.port is not None else default_port
    return host, port


_DRIVE_LETTERS = "CDEFGHIJKLMNOPQRSTUVWXYZ"


def first_free_drive_letter(used: set[str]) -> str:
    """Return the first free Windows drive letter (``C:``..``Z:``) not in ``used``.

    ``used`` is a set of single upper-case letters (e.g. ``{"C", "D"}``). Pure and
    unit-testable; the live wrapper feeds it the letters read via ctypes. Raises
    :class:`MountError` if every letter is taken.
    """
    used_upper = {str(s).strip().rstrip(":").upper() for s in used}
    for letter in _DRIVE_LETTERS:
        if letter not in used_upper:
            return f"{letter}:"
    raise MountError("no free drive letter available to map the mount")


def _used_drive_letters_windows() -> set[str]:
    """Read the set of in-use drive letters on Windows via ``GetLogicalDrives``.

    Guarded so the ctypes/WinDLL access never imports or runs off-Windows (the
    ``windll`` attribute only exists there), keeping tests fully cross-platform.
    """
    import ctypes  # local import: only touched on Windows

    bitmask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    used: set[str] = set()
    for i in range(26):
        if bitmask & (1 << i):
            used.add(chr(ord("A") + i))
    return used


def auto_mount_target(
    leaf: str,
    cwd: str,
    platform: str,
    *,
    used_drive_letters: set[str] | None = None,
) -> str:
    """Decide the local mount target for ``leaf`` under ``cwd`` on ``platform``.

    Pure (no I/O, no real mount). Returns the folder path on macOS/Linux or a
    drive letter (``Z:``) on Windows. The Windows branch takes the in-use letter
    set as a parameter so it stays testable without ctypes; ``None`` means "read
    it from the OS" (only valid when actually running on Windows).
    """
    if platform.startswith("win"):
        used = (
            used_drive_letters if used_drive_letters is not None else _used_drive_letters_windows()
        )
        return first_free_drive_letter(used)
    return str(Path(cwd) / leaf)


def _prepare_dir_target(target: str) -> None:
    """Create ``target`` if absent, reuse if empty, REFUSE if non-empty.

    Safety invariant: never shadow or delete user data. A pre-existing non-empty
    folder is refused (``MountError``); an empty one is reused; a missing one is
    created.
    """
    path = Path(target)
    if path.exists():
        if not path.is_dir():
            raise MountError(f"mount target exists and is not a directory: {target}")
        if any(path.iterdir()):
            raise MountError(
                f"refusing to mount over a non-empty folder: {target}. "
                "Move/rename it or pass --mount with a different location."
            )
        return
    path.mkdir(parents=True, exist_ok=False)


def _rmdir_if_empty(target: str, *, attempts: int = 12, delay: float = 0.1) -> None:
    """Remove ``target`` once it is an empty directory, retrying briefly.

    A macOS WebDAV ``umount`` can return before the kernel finishes detaching,
    so the mountpoint is transiently still "mounted"/busy and an immediate
    ``rmdir`` fails (EBUSY / ENOTEMPTY) -- which previously left the empty folder
    behind. ``rmdir`` only ever removes an EMPTY directory, so retrying can never
    delete user data; it just waits out the unmount.
    """
    import time

    for i in range(max(1, attempts)):
        try:
            Path(target).rmdir()
            return
        except FileNotFoundError:
            return  # already gone
        except OSError:
            if i < attempts - 1:
                time.sleep(delay)


def build_auto_mount_handle(
    url: str,
    leaf: str,
    cwd: str,
    platform: str,
    *,
    used_drive_letters: set[str] | None = None,
    uid: int | None = None,
) -> MountHandle:
    """Mount ``url`` under an auto handle named after ``leaf`` and return it.

    Per-OS behaviour (see the design spec, C3):

    - darwin: mountpoint ``<cwd>/<leaf>/`` (created/reused-if-empty/refused-if-
      non-empty); ``mount_webdav -S``; cleanup = umount then rmdir-if-empty.
    - linux: ``gio mount dav://...``; symlink ``<cwd>/<leaf>/`` -> the computed
      GVfs path; cleanup = ``gio mount -u`` then remove the symlink.
    - win32: first free drive letter; ``net use <L:> <url>``; display/open =
      ``L:\\``; cleanup = ``net use <L:> /delete /y`` (no local folder).

    Does real I/O (mkdir/symlink/subprocess); not covered by automated tests.
    Raises :class:`MountError` on failure (after best-effort cleanup of anything
    it created).
    """
    if platform.startswith("win"):
        drive = auto_mount_target(leaf, cwd, platform, used_drive_letters=used_drive_letters)
        mount(url, drive, platform=platform)
        display = drive if drive.endswith(":") else drive + ":"
        open_target = display + "\\"

        def _win_unmount() -> None:
            unmount(url, drive, platform=platform)

        return MountHandle(
            display=display,
            open_target=open_target,
            unmount=_win_unmount,
            cleanup=lambda: None,
        )

    if platform == "darwin":
        target = auto_mount_target(leaf, cwd, platform)
        _prepare_dir_target(target)
        try:
            mount(url, target, platform=platform)
        except MountError:
            _rmdir_if_empty(target)
            raise

        def _darwin_unmount() -> None:
            unmount(url, target, platform=platform)

        return MountHandle(
            display=target,
            open_target=target,
            unmount=_darwin_unmount,
            cleanup=lambda: _rmdir_if_empty(target),
        )

    # linux / other unix: gio mount into GVfs, then symlink a friendly handle.
    link = auto_mount_target(leaf, cwd, platform)
    host, port = _split_host_port(url, default_port=80)
    real_uid = uid if uid is not None else os.getuid()
    gvfs = gvfs_dav_path(host, port, uid=real_uid, ssl=url.startswith("https://"))
    mount(url, link, platform=platform)
    link_path = Path(link)
    made_link = False
    try:
        if link_path.exists() or link_path.is_symlink():
            if not (link_path.is_symlink() and os.readlink(link) == gvfs):
                raise MountError(
                    f"refusing to replace existing path: {link}. "
                    "Move/rename it or pass --mount with a different location."
                )
        else:
            link_path.symlink_to(gvfs)
            made_link = True
    except OSError as exc:
        unmount(url, link, platform=platform)
        raise MountError(f"could not create mount symlink {link}: {exc}") from exc

    def _linux_unmount() -> None:
        unmount(url, link, platform=platform)

    def _linux_cleanup() -> None:
        if made_link and link_path.is_symlink():
            with contextlib.suppress(OSError):
                link_path.unlink()

    return MountHandle(
        display=link,
        open_target=link,
        unmount=_linux_unmount,
        cleanup=_linux_cleanup,
    )

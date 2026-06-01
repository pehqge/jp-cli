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
import subprocess
import sys
from dataclasses import dataclass


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

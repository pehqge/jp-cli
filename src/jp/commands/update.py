"""``jp update`` -- self-update jp to the latest version.

Works across the install methods jp supports, by detecting which tool owns the
running ``jp`` and delegating its upgrade:

  * pipx   -> ``pipx upgrade jp-cli``       (or reinstall from GitHub)
  * uv     -> ``uv tool upgrade jp-cli``
  * pip    -> ``python -m pip install --upgrade jp-cli``
  * a standalone binary / zipapp -> we can't self-replace safely, so we print
    the one-line reinstall command for the user's OS.

``jp update --check`` only compares the installed version against the latest
GitHub release and reports, without changing anything.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from .. import __version__, ui
from ..errors import EXIT_GENERIC, EXIT_NETWORK, EXIT_OK

_REPO = "pehqge/jp-cli"
_PYPI_NAME = "jp-cli"
_GIT_SPEC = f"git+https://github.com/{_REPO}"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("update", help="update jp to the latest version")
    p.add_argument("--check", action="store_true", help="only check; do not install")
    p.add_argument(
        "--source",
        choices=["auto", "pypi", "git"],
        default="auto",
        help="install source for the upgrade (default: auto)",
    )
    p.set_defaults(func=run)


def _latest_release_tag() -> str | None:
    """Return the latest GitHub release tag (e.g. 'v0.2.0'), or None on failure."""
    url = f"https://api.github.com/repos/{_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        tag = str(data.get("tag_name") or "").strip()
        return tag or None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _norm(v: str) -> tuple[int, ...]:
    parts = v.lstrip("vV").split(".")
    out: list[int] = []
    for p in parts:
        num = "".join(ch for ch in p if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out) or (0,)


def _running_as_binary() -> bool:
    """True if running from a PyInstaller binary (no real Python env to pip into)."""
    return bool(getattr(sys, "frozen", False))


def _detect_manager() -> str | None:
    """Best-effort detection of the tool that installed this jp."""
    exe = (shutil.which("jp") or sys.argv[0] or "").replace("\\", "/").lower()
    if "/pipx/" in exe or os.path.sep + "pipx" + os.path.sep in (shutil.which("jp") or ""):
        return "pipx"
    if "/uv/" in exe or "uv/tools" in exe:
        return "uv"
    # Fall back to whichever manager is available, preferring isolation tools.
    if shutil.which("pipx"):
        return "pipx"
    if shutil.which("uv"):
        return "uv"
    return "pip"


def _run(cmd: list[str]) -> int:
    ui.detail("  $ " + " ".join(cmd))
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        ui.error(f"could not run {cmd[0]}: {exc}")
        return EXIT_GENERIC


def _reinstall_hint() -> None:
    ui.info("Reinstall with one of:")
    ui.detail(f"  pipx install --force {_PYPI_NAME}")
    ui.detail(f"  uv tool install --force {_PYPI_NAME}")
    if os.name == "nt":
        ui.detail(
            "  powershell -ExecutionPolicy ByPass -c "
            f'"irm https://raw.githubusercontent.com/{_REPO}/main/scripts/install.ps1 | iex"'
        )
    else:
        ui.detail(
            f"  curl -fsSL https://raw.githubusercontent.com/{_REPO}/main/scripts/install.sh | sh"
        )


def run(args: argparse.Namespace) -> int:
    ui.info(f"jp {__version__} (installed)")
    latest = _latest_release_tag()
    if latest is None:
        ui.warn("could not reach GitHub to check for the latest release.")
        if args.check:
            return EXIT_NETWORK
    else:
        if _norm(latest) <= _norm(__version__):
            ui.success(f"already up to date (latest release: {latest})")
            return EXIT_OK
        ui.info(f"a newer version is available: {latest}")

    if args.check:
        return EXIT_OK

    if _running_as_binary():
        ui.warn("running from a standalone binary; jp cannot replace itself.")
        _reinstall_hint()
        return EXIT_OK

    # Choose the package spec.
    if args.source == "git":
        spec = _GIT_SPEC
    elif args.source == "pypi":
        spec = _PYPI_NAME
    else:
        spec = _PYPI_NAME  # auto: prefer PyPI; fall back to git below on failure

    mgr = _detect_manager()
    ui.info(f"updating via {mgr} ...")
    rc: int
    if mgr == "pipx":
        rc = _run(["pipx", "upgrade", _PYPI_NAME]) if args.source != "git" else 1
        if rc != 0:
            rc = _run(["pipx", "install", "--force", spec if args.source != "auto" else _GIT_SPEC])
    elif mgr == "uv":
        rc = _run(["uv", "tool", "upgrade", _PYPI_NAME]) if args.source != "git" else 1
        if rc != 0:
            rc = _run(["uv", "tool", "install", "--force", _GIT_SPEC])
    else:
        rc = _run([sys.executable, "-m", "pip", "install", "--upgrade", spec])
        if rc != 0 and args.source == "auto":
            rc = _run([sys.executable, "-m", "pip", "install", "--upgrade", _GIT_SPEC])

    if rc == 0:
        ui.success("update complete. Run 'jp --version' to confirm.")
        return EXIT_OK
    ui.error("automatic update failed.")
    _reinstall_hint()
    return EXIT_GENERIC

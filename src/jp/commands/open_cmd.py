"""``jp open`` -- open this workspace's folder in the Jupyter web UI.

Builds the JupyterLab folder URL for the current workspace (or the subfolder you
are standing in) and opens it in your browser. The URL is just the ``/lab/tree/``
view of the mapped remote path -- it carries **no token**, so there is no secret
to leak; the browser handles authentication on its own.

Runs only inside an initialized jp workspace (a directory with a ``.jp/``, found
by walking up from the cwd). From a subfolder, the URL points at that subfolder
-- unless that folder is one jp never syncs (``.jp/`` metadata, a hidden
dot-name, or a ``.jpignore`` match), in which case it is refused offline because
no such folder can exist on the remote. ``-f``/``--force`` overrides the refusal
and opens it anyway; forcing always re-asks (it bypasses a remembered choice) so
the person consciously decides for that unusual folder.

In the prompt, ``r`` picks the highlighted action *and* remembers it globally
(``~/.config/jp/prefs.json``), so future runs skip the prompt. ``jp open --ask``
forgets that choice and asks again.

Cross-platform: ``webbrowser`` (stdlib) and the clipboard helper both work on
macOS, Linux and Windows. The interactive picker uses the platform reader in
``tui``.
"""

from __future__ import annotations

import argparse
import urllib.parse
import webbrowser
from pathlib import Path, PurePosixPath

from .. import clipboard, prefs, tui, ui
from .. import config as config_mod
from ..errors import EXIT_OK, ConfigError, UsageError
from ..ignore import IgnoreSet
from ..paths import DOT_DIR, find_root, is_hidden

# Global preference key (see jp.prefs): the remembered action for `jp open`,
# one of "" (ask), "browser", or "copy".
_PREF_KEY = "open_action"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "open",
        help="open this workspace's folder in the Jupyter web UI (browser)",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="open the browser without the confirmation prompt",
    )
    p.add_argument(
        "--copy",
        action="store_true",
        help="copy the URL to the clipboard instead of opening the browser",
    )
    p.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print the URL only (do not open the browser or copy)",
    )
    p.add_argument(
        "--ask",
        action="store_true",
        help="forget the remembered choice and show the prompt again",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="open even a folder jp never syncs (.jp, hidden, .jpignore); asks every time",
    )
    p.set_defaults(func=run)


# --------------------------------------------------------------------------- #
# Pure URL builder (directly tested)
# --------------------------------------------------------------------------- #
def folder_url(base_url: str, prefix: str, subpath: str = "") -> str:
    """Build the JupyterLab ``/lab/tree/<path>`` URL for a workspace folder.

    ``prefix`` is the workspace's mapped remote path; ``subpath`` is the path of
    the current directory relative to the workspace root (empty at the root).
    Every path segment is percent-encoded so spaces and accents survive. The URL
    round-trips with :func:`urls.parse_clone_url`.
    """
    # Defensive: a trailing ``/api`` would point at the Contents API, not the UI.
    server = base_url[:-4] if base_url.endswith("/api") else base_url
    server = server.rstrip("/")

    segments = [s for s in PurePosixPath(prefix).parts if s not in ("", "/")]
    segments += [s for s in PurePosixPath(subpath).parts if s not in ("", "/")]
    encoded = "/".join(urllib.parse.quote(s, safe="") for s in segments)
    return f"{server}/lab/tree/{encoded}"


def _unsynced_reason(subpath: str, ignore: IgnoreSet) -> str | None:
    """Why ``subpath`` is never on the remote, or ``None`` if jp would sync it.

    Mirrors the rules jp applies when pushing, so the check stays offline:
      * the ``.jp/`` metadata dir is local-only state, never uploaded;
      * a hidden (dot-name) segment is rejected by the server (``allow_hidden``
        is off), so it never lands there under any name the URL could address;
      * a path matching ``.jpignore`` is deliberately never uploaded.
    The empty subpath (the workspace root) is always syncable.
    """
    if not subpath:
        return None
    if subpath == DOT_DIR or subpath.startswith(DOT_DIR + "/"):
        return f"the {DOT_DIR}/ directory is local jp metadata"
    if is_hidden(subpath):
        return "this is a hidden (dot-name) path, which jp never uploads"
    if ignore.is_ignored(subpath, is_dir=True):
        return "this path matches .jpignore, which jp never uploads"
    return None


def _subpath_from_cwd(root: Path) -> str:
    """Return the cwd relative to the workspace ``root`` as a POSIX path.

    Empty when the cwd is the root itself. ``find_root`` walked up from the cwd,
    so the cwd is always at or below ``root`` -- the ``relative_to`` cannot fail.
    """
    rel = Path.cwd().resolve().relative_to(root)
    return rel.as_posix() if rel != Path(".") else ""


# --------------------------------------------------------------------------- #
# Command entry point
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    root = find_root()
    if root is None:
        raise ConfigError(
            "not inside a jp repository (no .jp directory found). "
            "Run 'jp init' or 'jp clone' first."
        )
    cfg = config_mod.load(root)
    ui.set_color_mode(cfg.color)

    subpath = _subpath_from_cwd(root)
    # Refuse folders jp never syncs: a URL into them could not resolve on the
    # remote. This is decided offline from jp's own rules (no network). --force
    # overrides the refusal (the URL may still 404), but because that is an
    # unusual, deliberate act we make the person choose every time: a remembered
    # choice is bypassed so the prompt is shown again.
    reason = _unsynced_reason(subpath, IgnoreSet.from_root(root))
    forced = False
    if reason is not None:
        if not args.force:
            raise UsageError(
                f"{reason}, so it is not on the remote. "
                "Run 'jp open' from the workspace or a folder that jp syncs, "
                "or pass --force to open it anyway."
            )
        forced = True
        ui.warn(f"{reason}; opening anyway (--force). The page may not exist on the remote.")

    url = folder_url(cfg.base_url, cfg.prefix, subpath)

    # --ask resets the remembered choice up front, so we fall through to the
    # prompt below and re-learn (or stay un-set) from this run.
    if args.ask:
        prefs.clear(_PREF_KEY)

    # Explicit flags always win and never change what is remembered.
    if args.print_only:
        ui.out(url)
        return EXIT_OK
    if args.copy:
        return _copy(url)
    if args.yes:
        return _open(url)

    # A remembered choice skips the prompt -- except when forcing a normally
    # refused folder, where the person must choose deliberately every time.
    if not args.ask and not forced:
        remembered = prefs.get(_PREF_KEY)
        if remembered == "browser":
            ui.detail("Using your saved choice (open). Run `jp open --ask` to change it.")
            return _open(url)
        if remembered == "copy":
            ui.detail("Using your saved choice (copy). Run `jp open --ask` to change it.")
            return _copy(url)

    # Interactive: show the URL and the three-way picker.
    if not tui.interactive():
        raise UsageError(
            "jp open needs an interactive terminal. "
            "Use --print to show the URL, --copy to copy it, or -y to open it."
        )

    ui.heading("Open this folder in your browser?")
    ui.out(url)
    ui.out("")
    choice, remember = tui.select_one_remember(
        [
            "Yes, open in the browser",
            "Copy the URL to the clipboard only",
            "No, cancel",
        ],
        title="What would you like to do?",
    )
    if choice == 0:
        if remember:
            prefs.set(_PREF_KEY, "browser")
            ui.detail("Saved. Run `jp open --ask` to change it.")
        return _open(url)
    if choice == 1:
        if remember:
            prefs.set(_PREF_KEY, "copy")
            ui.detail("Saved. Run `jp open --ask` to change it.")
        return _copy(url)
    ui.info("aborted")
    return EXIT_OK


def _open(url: str) -> int:
    if webbrowser.open(url):
        ui.success("Opened in your browser.")
    else:
        ui.warn("Could not open a browser automatically. Copy the URL above, or run with --copy.")
    return EXIT_OK


def _copy(url: str) -> int:
    tool = clipboard.copy(url)
    if tool is not None:
        ui.success(f"Copied the URL to the clipboard (via {tool}).")
    else:
        ui.info("Couldn't copy automatically -- run `jp open --print` to print the URL.")
        ui.out(url)
    return EXIT_OK

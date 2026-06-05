"""``jp kernel`` -- print + copy a one-time snippet that fixes the working

directory of a VS Code remote Jupyter kernel.

Background: when VS Code runs a *local* ``.ipynb`` against a *remote* kernel,
the kernel's working directory is the server's home, not the notebook's folder,
so relative paths (``pd.read_excel("dataset/x.xlsx")``) raise ``FileNotFoundError``.
VS Code's ``notebookFileRoot`` setting does not apply to remote kernels.

This command does NOT touch the remote. It only builds a small IPython startup
snippet -- pre-filled with this workspace's local root and prefix -- and copies
it to the clipboard with step-by-step instructions. The user pastes it into one
cell, runs it once, and every notebook in the workspace then starts in the right
directory. See ``docs/vscode-remote-cwd.md`` for the full explanation.
"""

from __future__ import annotations

import argparse
import sys

from .. import clipboard, ui
from .. import config as config_mod
from ..errors import EXIT_OK, SafetyError
from ._context import load_repo

# Full walkthrough (connecting VS Code + how it works), linked from the command
# output so the user can click straight through on GitHub.
_GUIDE_URL = "https://github.com/pehqge/jpsync/blob/main/docs/vscode-remote-cwd.md"

# The file written on the remote. The two ``{...!r}`` slots are filled with the
# workspace's local root and prefix so the mapping works for ANY prefix (including
# nested ones like ``users/alice``) and any local folder name -- we never guess
# from the folder name. ``__vsc_ipynb_file__`` is the local notebook path that
# VS Code injects into the kernel namespace; we translate it to the mirrored
# remote directory and ``chdir`` there before each cell runs.
_STARTUP_FILE = """import os
from pathlib import Path

JP_LOCAL_ROOT = {local_root!r}   # your local jp workspace root
JP_PREFIX = {prefix!r}           # jp prefix (the folder that holds your notebooks on the remote)


def _jp_autocwd(info=None):
    try:
        p = get_ipython().user_ns.get("__vsc_ipynb_file__")  # local .ipynb path injected by VS Code
        if not p:
            return
        p = p.replace("\\\\", "/")
        root = JP_LOCAL_ROOT.replace("\\\\", "/").rstrip("/")
        if root and not p.casefold().startswith(root.casefold() + "/"):
            return
        rel = p[len(root):].lstrip("/")              # e.g. "boxe.ml/test.ipynb"
        rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for base in (os.getcwd(), os.path.expanduser("~")):
            for cand in (Path(base) / JP_PREFIX / rel_dir, Path(base) / rel_dir):
                if cand.is_dir():
                    if Path.cwd() != cand:
                        os.chdir(cand)
                    return
    except Exception:
        pass


get_ipython().events.register("pre_run_cell", _jp_autocwd)
"""

# The snippet the user pastes into a notebook cell: it writes ``_STARTUP_FILE``
# into the IPython startup directory, where it runs automatically for every
# kernel from then on.
_INSTALLER = """from pathlib import Path

SCRIPT = {script!r}

d = Path.home() / ".ipython" / "profile_default" / "startup"
d.mkdir(parents=True, exist_ok=True)
(d / "50-jp-autocwd.py").write_text(SCRIPT)
print("installed:", d / "50-jp-autocwd.py")
print("Now restart the kernel; it runs automatically for every notebook.")
"""


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "kernel",
        help="set up a VS Code remote kernel to use the notebook's directory",
    )
    p.add_argument(
        "--script",
        action="store_true",
        help="print the full setup snippet (otherwise it is only copied to the clipboard)",
    )
    p.add_argument(
        "--link",
        action="store_true",
        help="show + copy the server connection URL (with your token) for VS Code",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the confirmation prompt for --link",
    )
    p.add_argument(
        "--no-clipboard",
        action="store_true",
        help="do not copy to the clipboard",
    )
    p.set_defaults(func=run)


def build_snippet(local_root: str, prefix: str) -> str:
    """Build the cell snippet for a workspace (pure; used by tests)."""
    startup = _STARTUP_FILE.format(local_root=local_root, prefix=prefix)
    return _INSTALLER.format(script=startup)


def connection_url(base_url: str, token: str) -> str:
    """Build the Jupyter-server connection URL VS Code expects (pure; tested).

    Strips a trailing ``/api`` so the result is the server root, then appends the
    token as a query parameter.
    """
    server = base_url[:-4] if base_url.endswith("/api") else base_url
    return server.rstrip("/") + "/?token=" + token


def _copy_to_clipboard(text: str) -> str | None:
    """Best-effort, dependency-free clipboard copy (see :mod:`jp.clipboard`)."""
    return clipboard.copy(text)


def run(args: argparse.Namespace) -> int:
    # load_repo() raises ConfigError (EXIT_CONFIG) outside a jp workspace, telling
    # the user to run inside an initialized folder -- exactly what we want here.
    ctx = load_repo()

    # `--link`: show + copy the server URL (with token) to paste into VS Code.
    if args.link:
        return _run_link(args, ctx)

    snippet = build_snippet(str(ctx.root), ctx.cfg.prefix)

    # `--script`: print the raw snippet only, so it can be inspected or piped.
    if args.script:
        ui.out(snippet)
        return EXIT_OK

    ui.heading("Auto-cwd for a VS Code remote kernel")
    ui.out("")
    ui.out(f"Prefix '{ctx.cfg.prefix}'. The setup snippet is ready -- paste it into one cell")
    ui.out("on your remote kernel, run it once, then restart the kernel. Done once, it")
    ui.out("works for every notebook here.")
    ui.out("")

    if not args.no_clipboard:
        tool = _copy_to_clipboard(snippet)
        if tool is not None:
            ui.success(f"Copied to clipboard (via {tool}).")
        else:
            ui.info("Couldn't copy automatically -- run `jp kernel --script` to print it.")
    else:
        ui.info("Run `jp kernel --script` to print the snippet.")

    ui.out("")
    ui.heading("Steps:")
    ui.bullets(
        [
            "1. In VS Code, open a notebook connected to your remote kernel"
            " (new to this? see the full guide below).",
            "2. Add a new cell, paste (Cmd/Ctrl+V), and run it.",
            "3. Restart the kernel (Restart button).",
            "4. Check: a cell with `from pathlib import Path; print(Path.cwd())`",
            "   should now show your workspace folder.",
        ],
        indent="  ",
    )
    ui.out("")
    ui.detail("Full script: jp kernel --script")
    ui.detail("Connecting VS Code to the remote kernel + how it works (full guide):")
    ui.detail(f"  {_GUIDE_URL}")
    return EXIT_OK


def _run_link(args: argparse.Namespace, ctx) -> int:  # noqa: ANN001 - RepoContext
    """Show + copy the server connection URL (with token) for VS Code.

    This is the ONE place jp prints the token, and it is strictly opt-in (the
    user must pass --link and confirm). It exists because pasting the URL into
    VS Code's kernel picker is otherwise a manual token hunt. SECURITY.md
    documents this exception. The URL is written with the builtin ``print`` so it
    deliberately bypasses ``ui.redact`` -- otherwise the token would be masked,
    defeating the purpose.
    """
    token = config_mod.load_token(ctx.cfg)
    link = connection_url(ctx.cfg.base_url, token)

    if not args.yes:
        ui.warn("This prints your API token to the screen -- anyone watching it can read it.")
        if not sys.stdin.isatty():
            raise SafetyError("refusing to print the token without confirmation; pass --yes")
        ans = input("Show the connection URL with your token? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            ui.info("aborted")
            return EXIT_OK

    tool = _copy_to_clipboard(link) if not args.no_clipboard else None

    print(link)  # intentional raw, unredacted output (see docstring)

    if tool is not None:
        ui.success(f"Also copied to clipboard (via {tool}).")
    ui.detail("Paste it where VS Code asks for the server URL (kernel picker).")
    return EXIT_OK

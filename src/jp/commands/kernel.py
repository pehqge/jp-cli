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
import shutil
import subprocess
import sys

from .. import ui
from ..errors import EXIT_OK
from ._context import load_repo

# Full walkthrough (connecting VS Code + how it works), linked from the command
# output so the user can click straight through on GitHub.
_GUIDE_URL = "https://github.com/pehqge/jp-cli/blob/main/docs/vscode-remote-cwd.md"

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
        "--no-clipboard",
        action="store_true",
        help="do not copy to the clipboard",
    )
    p.set_defaults(func=run)


def build_snippet(local_root: str, prefix: str) -> str:
    """Build the cell snippet for a workspace (pure; used by tests)."""
    startup = _STARTUP_FILE.format(local_root=local_root, prefix=prefix)
    return _INSTALLER.format(script=startup)


def _copy_to_clipboard(text: str) -> str | None:
    """Best-effort, dependency-free clipboard copy.

    Returns the tool name used, or ``None`` when no clipboard tool is available
    (in which case the printed snippet is the fallback). Never raises.
    """
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform.startswith("win"):
        candidates = [["clip"]]
    else:  # Linux / *BSD: Wayland or X11, whichever is installed
        candidates = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return cmd[0]
        except Exception:
            continue
    return None


def run(args: argparse.Namespace) -> int:
    # load_repo() raises ConfigError (EXIT_CONFIG) outside a jp workspace, telling
    # the user to run inside an initialized folder -- exactly what we want here.
    ctx = load_repo()
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

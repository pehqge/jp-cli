"""Best-effort, dependency-free clipboard copy, shared across commands.

Picks the right native tool per platform and never raises: a failed or missing
clipboard tool simply returns ``None`` so the caller can fall back to printing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def copy(text: str) -> str | None:
    """Copy ``text`` to the system clipboard.

    Returns the tool name used (``pbcopy``/``clip``/``wl-copy``/...), or ``None``
    when no clipboard tool is available or the copy failed. Never raises.
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

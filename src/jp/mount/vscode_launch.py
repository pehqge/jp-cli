"""Generate a local ``.code-workspace`` and the per-OS VS Code launcher argv.

Both functions here are PURE (no I/O, no PATH probing, no subprocess): they just
build data. ``jp live --code`` writes the workspace JSON next to the launch dir
(never inside the mount -- the server rejects dotfiles) and runs the launcher.

The workspace opens the mounted folder and, on ``folderOpen``, runs
``jp terminal "<URL>" --credential <name>`` in an integrated terminal so the user
gets a remote shell with one click (after VS Code's one-time "Allow Automatic
Tasks" prompt).
"""

from __future__ import annotations

from typing import Any


def build_code_workspace(folder_open_target: str, url: str, credential: str) -> dict[str, Any]:
    """Return the ``.code-workspace`` JSON structure (pure).

    - ``folder_open_target`` is the mounted folder/handle VS Code should open.
    - The ``folderOpen`` task runs ``jp terminal "<url>" --credential <name>``;
      when ``credential`` is empty the ``--credential`` flag is omitted.

    The command is assembled as a single shell string with the URL quoted, so a
    URL is never split on whitespace and the saved credential name is passed
    through verbatim (credential names are already restricted to a safe alphabet
    by ``credentials.validate_name``).
    """
    command = f'jp terminal "{url}"'
    if credential:
        command += f" --credential {credential}"
    return {
        "folders": [{"path": folder_open_target}],
        "tasks": {
            "version": "2.0.0",
            "tasks": [
                {
                    "label": "jp remote terminal",
                    "type": "shell",
                    "command": command,
                    "runOptions": {"runOn": "folderOpen"},
                    "presentation": {"reveal": "always", "panel": "dedicated"},
                }
            ],
        },
    }


def launcher_argv(workspace_file: str, platform: str, code_on_path: bool) -> list[str] | None:
    """Return the argv to open ``workspace_file`` in VS Code, or ``None``.

    Detection order (per the design spec, C5):

    1. macOS: ``open -a "Visual Studio Code" <file>`` (no ``code`` CLI needed).
    2. else if ``code`` is on PATH: ``code <file>``.
    3. else open ``vscode://file/<abs>`` via the OS opener (``open`` / ``xdg-open``
       / ``start``).
    4. else ``None`` -- the caller prints manual instructions.

    ``code_on_path`` is taken as a parameter (not probed here) so the function is
    pure and unit-testable on any host.
    """
    if platform == "darwin":
        return ["open", "-a", "Visual Studio Code", workspace_file]

    if code_on_path:
        return ["code", workspace_file]

    uri = f"vscode://file/{workspace_file}"
    if platform.startswith("win"):
        # ``start`` is a cmd builtin; the empty "" is the (ignored) window title.
        return ["cmd", "/c", "start", "", uri]
    # linux / other unix
    return ["xdg-open", uri]

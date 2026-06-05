"""Unit tests for the pure VS Code launch helpers (C5). No I/O, no subprocess."""

from __future__ import annotations

from jp.mount.vscode_launch import build_code_workspace, launcher_argv

URL = "https://host/user/alice/lab/tree/privado/jp-live-test"


# ---------------------------------------------------------------------------
# build_code_workspace
# ---------------------------------------------------------------------------


def test_workspace_folder_path():
    ws = build_code_workspace("/home/me/jp-live-test", URL, "myserver")
    assert ws["folders"] == [{"path": "/home/me/jp-live-test"}]


def test_workspace_folder_open_task():
    ws = build_code_workspace("/x", URL, "myserver")
    tasks = ws["tasks"]["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["runOptions"] == {"runOn": "folderOpen"}
    assert task["type"] == "shell"
    assert task["label"] == "jp remote terminal"


def test_workspace_command_includes_url_and_credential():
    ws = build_code_workspace("/x", URL, "myserver")
    cmd = ws["tasks"]["tasks"][0]["command"]
    assert cmd == f'jp terminal "{URL}" --credential myserver'


def test_workspace_command_quotes_url():
    # The URL must be quoted so it never splits on whitespace in the shell.
    ws = build_code_workspace("/x", URL, "")
    cmd = ws["tasks"]["tasks"][0]["command"]
    assert cmd == f'jp terminal "{URL}"'
    assert "--credential" not in cmd


def test_workspace_version():
    ws = build_code_workspace("/x", URL, "c")
    assert ws["tasks"]["version"] == "2.0.0"


# ---------------------------------------------------------------------------
# launcher_argv
# ---------------------------------------------------------------------------


def test_launcher_macos_uses_open_a():
    argv = launcher_argv("/x/p.code-workspace", "darwin", code_on_path=False)
    assert argv == ["open", "-a", "Visual Studio Code", "/x/p.code-workspace"]


def test_launcher_macos_ignores_code_on_path():
    # macOS always uses `open -a`, even when `code` is on PATH.
    argv = launcher_argv("/x/p.code-workspace", "darwin", code_on_path=True)
    assert argv[0] == "open"


def test_launcher_code_on_path():
    argv = launcher_argv("/x/p.code-workspace", "linux", code_on_path=True)
    assert argv == ["code", "/x/p.code-workspace"]


def test_launcher_linux_uri_fallback():
    argv = launcher_argv("/x/p.code-workspace", "linux", code_on_path=False)
    assert argv == ["xdg-open", "vscode://file//x/p.code-workspace"]


def test_launcher_windows_code_on_path():
    argv = launcher_argv("C:\\x\\p.code-workspace", "win32", code_on_path=True)
    assert argv == ["code", "C:\\x\\p.code-workspace"]


def test_launcher_windows_uri_fallback():
    argv = launcher_argv("C:\\x\\p.code-workspace", "win32", code_on_path=False)
    assert argv == ["cmd", "/c", "start", "", "vscode://file/C:\\x\\p.code-workspace"]

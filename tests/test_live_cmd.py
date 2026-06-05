"""Tests for the ``jp live`` command: the new one-shot URL mount, the workspace
guard, the writable confirmation, ``--code``, and the transparency modes.

Everything here is network-free and mount-free: we monkeypatch the command
module's references (build_api / connect_live / config_from_url / mount helpers)
so a test can never reach a real server or perform a real mount, even if a gate
were to (wrongly) fall through.
"""

from __future__ import annotations

import argparse
import json

import pytest

from jp.commands import live
from jp.errors import EXIT_OK, SafetyError, UsageError

_URL = "https://hub.example/user/alice/lab/tree/privado/jp-live-test"


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path, monkeypatch):
    """Keep every test off the real ~/.config/jp (user_settings + live_state)
    and never install a real SIGTERM handler in the pytest process."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(live, "_install_signal_stop", lambda: None)


def _args(**over):
    base = {
        "url": None,
        "read_only": False,
        "credential": None,
        "code": False,
        "yes": False,
        "mount": None,
        "dry_run": False,
        "root": None,
        "stats": False,
        "print_agent": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeApi:
    def __init__(self, mode: str = "up") -> None:
        self._mode = mode

    def status_probe(self):
        from jp.api import StatusResult

        if self._mode == "up":
            return StatusResult(up=True, detail="ok")
        from jp.errors import ServerDownError

        raise ServerDownError()


class _FakeRFS:
    def ping(self) -> bool:
        return True

    def listdir(self, path: str):
        from jp.remote_fs import Entry

        return [Entry(name="dummy.txt", type="file", size=3)]


def _fake_cfg(prefix="privado/jp-live-test", credential="alice"):
    from jp.config import Config

    return Config(base_url="https://hub.example/user/alice", prefix=prefix, credential=credential)


def _patch_network(monkeypatch, *, prefix="privado/jp-live-test", credential="alice"):
    """Wire config_from_url / build_api / connect_live to fakes (no network)."""
    monkeypatch.setattr(
        live._context, "config_from_url", lambda url, **k: _fake_cfg(prefix, credential)
    )
    monkeypatch.setattr(live._context, "build_api", lambda cfg: _FakeApi("up"))
    monkeypatch.setattr(live.config_mod, "load_token", lambda cfg: "secret-token")

    captured = {}

    def _fake_connect(api, *, prefix, token, writable):
        captured["prefix"] = prefix
        captured["writable"] = writable
        return _FakeRFS(), lambda: captured.setdefault("released", True)

    monkeypatch.setattr(live, "connect_live", _fake_connect)
    return captured


def _guard_no_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("connect_live must not be called in this test")

    monkeypatch.setattr(live, "connect_live", _boom)


# --------------------------------------------------------------------------- #
# C2: workspace-tree guard
# --------------------------------------------------------------------------- #
def test_refuses_inside_jp_workspace(tmp_path, monkeypatch):
    (tmp_path / ".jp").mkdir()
    monkeypatch.chdir(tmp_path)
    _guard_no_network(monkeypatch)
    with pytest.raises(SafetyError) as exc:
        live.run(_args(url=_URL))
    assert "workspace" in str(exc.value).lower()


def test_refuses_inside_jp_subfolder(tmp_path, monkeypatch):
    (tmp_path / ".jp").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    _guard_no_network(monkeypatch)
    with pytest.raises(SafetyError):
        live.run(_args(url=_URL))


# --------------------------------------------------------------------------- #
# URL required
# --------------------------------------------------------------------------- #
def test_no_url_raises_usage_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _guard_no_network(monkeypatch)
    with pytest.raises(UsageError):
        live.run(_args(url=None))


# --------------------------------------------------------------------------- #
# C4: confirmation default logic
# --------------------------------------------------------------------------- #
def _stub_mount(monkeypatch, captured_mount=None):
    """Replace the auto-mount + serve loop so nothing real mounts or loops."""

    class _Handle:
        display = "./jp-live-test"
        open_target = "./jp-live-test"

        def unmount(self):
            pass

        def cleanup(self):
            pass

    monkeypatch.setattr(live, "_planned_display", lambda args, leaf: "./jp-live-test")

    def _fake_serve(args, rfs, *, prefix, writable, leaf, url, cfg):
        if captured_mount is not None:
            captured_mount["writable"] = writable
            captured_mount["leaf"] = leaf
        return EXIT_OK

    monkeypatch.setattr(live, "_serve", _fake_serve)


def test_enter_means_writable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cap = _patch_network(monkeypatch)
    mounted = {}
    _stub_mount(monkeypatch, mounted)
    monkeypatch.setattr(live.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(live.user_settings, "get_live_access", lambda: "ask")
    # writable, no remember
    monkeypatch.setattr(live.tui, "select_access", lambda *a, **k: (True, False))

    rc = live.run(_args(url=_URL))
    assert rc == EXIT_OK
    assert mounted["writable"] is True
    assert mounted["leaf"] == "jp-live-test"
    assert cap.get("released") is True


def test_r_means_read_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_network(monkeypatch)
    mounted = {}
    _stub_mount(monkeypatch, mounted)
    monkeypatch.setattr(live.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(live.user_settings, "get_live_access", lambda: "ask")
    monkeypatch.setattr(live.tui, "select_access", lambda *a, **k: (False, False))  # read-only

    rc = live.run(_args(url=_URL))
    assert rc == EXIT_OK
    assert mounted["writable"] is False


def test_cancel_aborts_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cap = _patch_network(monkeypatch)
    served = {"called": False}

    def _serve(*a, **k):
        served["called"] = True
        return EXIT_OK

    monkeypatch.setattr(live, "_serve", _serve)
    monkeypatch.setattr(live, "_planned_display", lambda args, leaf: "./jp-live-test")
    monkeypatch.setattr(live.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(live.user_settings, "get_live_access", lambda: "ask")
    monkeypatch.setattr(live.tui, "select_access", lambda *a, **k: None)  # cancel

    rc = live.run(_args(url=_URL))
    assert rc == EXIT_OK
    assert served["called"] is False
    assert cap.get("released") is True  # kernel still released on abort


def test_read_only_flag_forces_read_only_and_skips_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_network(monkeypatch)
    mounted = {}
    _stub_mount(monkeypatch, mounted)

    def _no_input(*a):
        raise AssertionError("--read-only must not prompt")

    monkeypatch.setattr("builtins.input", _no_input)

    rc = live.run(_args(url=_URL, read_only=True))
    assert rc == EXIT_OK
    assert mounted["writable"] is False


def test_yes_skips_prompt_and_assumes_writable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_network(monkeypatch)
    mounted = {}
    _stub_mount(monkeypatch, mounted)

    def _no_input(*a):
        raise AssertionError("--yes must not prompt")

    monkeypatch.setattr("builtins.input", _no_input)

    rc = live.run(_args(url=_URL, yes=True))
    assert rc == EXIT_OK
    assert mounted["writable"] is True


def test_non_tty_without_yes_refuses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_network(monkeypatch)
    monkeypatch.setattr(live, "_serve", lambda *a, **k: EXIT_OK)
    monkeypatch.setattr(live, "_planned_display", lambda args, leaf: "./jp-live-test")
    monkeypatch.setattr(live.sys.stdin, "isatty", lambda: False)

    with pytest.raises(SafetyError):
        live.run(_args(url=_URL))


# --------------------------------------------------------------------------- #
# --code writes a LOCAL .code-workspace (not inside the mount) and cleans it up
# --------------------------------------------------------------------------- #
def test_code_writes_and_cleans_up_workspace_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_network(monkeypatch)

    # A real auto-mount handle replacement (no subprocess), pointing the open
    # target at a folder UNDER cwd to prove the workspace file is written OUTSIDE it.
    mount_folder = tmp_path / "jp-live-test"

    class _Handle:
        display = str(mount_folder)
        open_target = str(mount_folder)

        def unmount(self):
            pass

        def cleanup(self):
            pass

    from jp.mount import os_mount

    monkeypatch.setattr(os_mount, "build_auto_mount_handle", lambda url, leaf, cwd, plat: _Handle())
    # Never launch a real editor: force the "print instructions" path.
    monkeypatch.setattr(live.shutil, "which", lambda name: None)

    seen = {"existed_during_serve": None}

    def _fake_keepalive(cached):
        # While serving, the local workspace file must exist OUTSIDE the mount.
        wf = tmp_path / "jp-live-test.code-workspace"
        seen["existed_during_serve"] = wf.is_file()
        if wf.is_file():
            data = json.loads(wf.read_text())
            seen["folder"] = data["folders"][0]["path"]
            task = data["tasks"]["tasks"][0]
            # type: process with an args array -- no shell string is built.
            seen["task_type"] = task["type"]
            seen["task_args"] = task["args"]

    monkeypatch.setattr(live, "_keepalive_loop", _fake_keepalive)
    # launcher_argv returns None for the vscode URI path? Force None by making the
    # subprocess path fail cleanly: with which()->None and non-darwin we still get
    # an argv (xdg-open). Stub launcher to None so we hit the print branch.
    from jp.mount import vscode_launch

    monkeypatch.setattr(vscode_launch, "launcher_argv", lambda *a, **k: None)

    rc = live.run(_args(url=_URL, yes=True, code=True))
    assert rc == EXIT_OK
    assert seen["existed_during_serve"] is True
    assert seen["folder"] == str(mount_folder)
    assert seen["task_type"] == "process"
    assert _URL in seen["task_args"]  # the URL is one argv element, not a shell string
    # The workspace file lived in cwd, not inside the mount folder.
    assert not (mount_folder / "jp-live-test.code-workspace").exists()
    # Cleaned up after serve returns.
    assert not (tmp_path / "jp-live-test.code-workspace").exists()


# --------------------------------------------------------------------------- #
# Arg parsing of the new flags
# --------------------------------------------------------------------------- #
def test_arg_parsing_new_flags():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    live.add_parser(sub)

    ns = parser.parse_args(
        ["live", _URL, "--read-only", "--credential", "work", "--code", "--yes", "--mount", "Z:"]
    )
    assert ns.url == _URL
    assert ns.read_only is True
    assert ns.credential == "work"
    assert ns.code is True
    assert ns.yes is True
    assert ns.mount == "Z:"
    # Transparency/offline flags survive.
    ns2 = parser.parse_args(["live", "--dry-run", "--root", "/tmp/x", "--stats"])
    assert ns2.dry_run is True and ns2.root == "/tmp/x" and ns2.stats is True
    ns3 = parser.parse_args(["live", "--print-agent"])
    assert ns3.print_agent is True
    # --writable is gone.
    with pytest.raises(SystemExit):
        parser.parse_args(["live", _URL, "--writable"])


def test_url_is_optional_for_dry_run():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    live.add_parser(sub)
    ns = parser.parse_args(["live", "--dry-run", "--root", "/tmp/x"])
    assert ns.url is None


# --------------------------------------------------------------------------- #
# --print-agent
# --------------------------------------------------------------------------- #
def test_print_agent_from_url(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        live._context, "config_from_url", lambda url, **k: _fake_cfg("privado/jp-live-test")
    )
    rc = live.run(_args(url=_URL, print_agent=True))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "class Agent" in out
    assert "register_target" in out


def test_print_agent_read_only_reflected(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        live._context, "config_from_url", lambda url, **k: _fake_cfg("privado/jp-live-test")
    )
    rc = live.run(_args(url=_URL, print_agent=True, read_only=True))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "writable=False" in out


# --------------------------------------------------------------------------- #
# Saved global defaults (user_settings) skip the picker
# --------------------------------------------------------------------------- #
def test_saved_writable_default_skips_picker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_network(monkeypatch)
    mounted = {}
    _stub_mount(monkeypatch, mounted)
    monkeypatch.setattr(live.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(live.user_settings, "get_live_access", lambda: "writable")
    # The picker must NOT be consulted when a default is saved.
    monkeypatch.setattr(
        live.tui, "select_access", lambda *a, **k: pytest.fail("picker should be skipped")
    )
    rc = live.run(_args(url=_URL))
    assert rc == EXIT_OK
    assert mounted["writable"] is True


def test_saved_read_only_default_skips_picker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_network(monkeypatch)
    mounted = {}
    _stub_mount(monkeypatch, mounted)
    monkeypatch.setattr(live.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(live.user_settings, "get_live_access", lambda: "read-only")
    monkeypatch.setattr(
        live.tui, "select_access", lambda *a, **k: pytest.fail("picker should be skipped")
    )
    rc = live.run(_args(url=_URL))
    assert rc == EXIT_OK
    assert mounted["writable"] is False


def test_remember_persists_choice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_network(monkeypatch)
    _stub_mount(monkeypatch, {})
    monkeypatch.setattr(live.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(live.user_settings, "get_live_access", lambda: "ask")
    saved = {}
    monkeypatch.setattr(live.user_settings, "set_live_access", lambda v: saved.update(v=v))
    # picker returns (read-only, remember=True)
    monkeypatch.setattr(live.tui, "select_access", lambda *a, **k: (False, True))
    rc = live.run(_args(url=_URL))
    assert rc == EXIT_OK
    assert saved == {"v": "read-only"}


# --------------------------------------------------------------------------- #
# jp live unmount
# --------------------------------------------------------------------------- #
def test_unmount_here_signals_owner(tmp_path, monkeypatch):
    from jp.mount import live_state

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    mp = str(tmp_path / "mnt")
    live_state.record(mp, pid=4242, url=_URL, display=mp, created=1.0)
    monkeypatch.chdir(tmp_path / "mnt" if (tmp_path / "mnt").exists() else tmp_path)
    # find_for_path uses cwd; point cwd at the recorded mountpoint
    import os as _os

    _os.makedirs(mp, exist_ok=True)
    monkeypatch.chdir(mp)
    killed = {}
    monkeypatch.setattr(live.os, "kill", lambda pid, sig: killed.update(pid=pid, sig=sig))
    rc = live.run(_args(url="unmount"))
    assert rc == EXIT_OK
    assert killed["pid"] == 4242


def test_unmount_here_no_mount_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SafetyError):
        live.run(_args(url="unmount"))


def test_unmount_here_stale_record_cleared(tmp_path, monkeypatch):
    from jp.mount import live_state

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    mp = str(tmp_path / "mnt")
    import os as _os

    _os.makedirs(mp, exist_ok=True)
    live_state.record(mp, pid=999999, url=_URL, display=mp, created=1.0)
    monkeypatch.chdir(mp)

    def _kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(live.os, "kill", _kill)
    rc = live.run(_args(url="unmount"))
    assert rc == EXIT_OK
    assert live_state.find_for_path(mp) is None  # stale record removed


# --------------------------------------------------------------------------- #
# --no-terminal and --defaults
# --------------------------------------------------------------------------- #
def test_no_terminal_omits_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import os as _os
    from pathlib import Path as _Path

    class _Handle:
        display = str(tmp_path / "jp-live-test")
        open_target = str(tmp_path / "jp-live-test")

        def unmount(self):
            pass

        def cleanup(self):
            pass

    # Never launch a real editor.
    monkeypatch.setattr(live.shutil, "which", lambda name: None)
    from jp.mount import vscode_launch

    monkeypatch.setattr(vscode_launch, "launcher_argv", lambda *a, **k: None)

    # with_terminal=False -> the workspace has NO auto-start task.
    wf = live._open_in_code(
        _Handle(), url=_URL, credential="c", leaf="jp-live-test", with_terminal=False
    )
    assert "tasks" not in json.loads(_Path(wf).read_text())
    _os.unlink(wf)

    # with_terminal=True -> the task is present.
    wf2 = live._open_in_code(
        _Handle(), url=_URL, credential="c", leaf="jp-live-test", with_terminal=True
    )
    assert "tasks" in json.loads(_Path(wf2).read_text())
    _os.unlink(wf2)


def test_defaults_menu_non_interactive(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(live.tui, "interactive", lambda: False)
    rc = live.run(_args(url=None, defaults=True))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "access=" in out and "code_terminal=" in out

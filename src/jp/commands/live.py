"""``jp live`` -- mount a remote folder as a local folder over the kernel websocket.

Primary use: ``jp live <URL>`` -- one self-contained command that connects to the
server the URL points at, starts a kernel, injects the small file agent, serves
the remote folder over loopback WebDAV, and mounts it under a single
auto-managed local handle (a folder on macOS/Linux, a drive letter on Windows).
Writable is the default; a single confirmation (showing the folder's top
entries) lets the user pick read-only instead. ``--code`` additionally opens the
mounted folder in VS Code with a remote shell wired up.

Two transparency/offline modes survive unchanged:

  * ``--dry-run``    : exercise the full transport against a LOCAL folder, no
                       network. A safe offline self-test.
  * ``--print-agent``: print the EXACT bootstrap code that would be injected,
                       for auditing, and exit without touching the network.

Safety invariants (see docs/superpowers/specs/2026-06-05-jp-live-redesign-design.md):
  * Refuses to run inside a ``.jp`` workspace tree (root or any subfolder).
  * Never deletes user data: the auto handle is created only if absent / reused
    if empty / refused if non-empty; cleanup removes only the empty dir, the
    symlink, and the ``.code-workspace`` file ``jp`` itself created.
  * Writable is gated: a single confirmation precedes the mount; ``--read-only``
    forces read-only, ``--yes`` assumes writable, and a non-tty without ``--yes``
    is refused.
  * Remote interaction is identical to today's writable live mount: never a
    recursive remote delete, never an escape of the prefix (double jail).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import sys
import time

from .. import agent_loader, paths, tui, ui, user_settings
from .. import config as config_mod
from ..errors import EXIT_OK, SafetyError, UsageError
from ..live_session import connect_live
from . import _context
from ._context import load_repo

# How often we ping the remote to defeat the server's idle-culling, and a cap on
# the per-iteration sleep so KeyboardInterrupt stays responsive.
_KEEPALIVE_INTERVAL = 30.0
_SLEEP_STEP = 1.0

# The C2 refusal shown when jp live is run inside a .jp workspace tree.
_WORKSPACE_GUARD_MSG = (
    "this folder is a jp workspace (pull/push). 'jp live' mounts the remote as a "
    "separate live folder and must run OUTSIDE a workspace. cd to a plain "
    "directory and run: jp live <URL>."
)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("live", help="mount a remote folder as a local folder")
    p.add_argument(
        "url",
        nargs="?",
        help="the Jupyter URL of the remote folder to mount (as copied from your browser)",
    )
    p.add_argument(
        "--read-only",
        action="store_true",
        help="mount read-only (writable is the default; this opts out)",
    )
    p.add_argument(
        "--credential",
        metavar="NAME",
        help="use a specific saved credential (else picked interactively)",
    )
    p.add_argument(
        "--code",
        action="store_true",
        help="open the mounted folder in VS Code with a remote shell running",
    )
    p.add_argument(
        "--no-terminal",
        action="store_true",
        help="with --code, do NOT auto-start the remote terminal (override the saved default)",
    )
    p.add_argument(
        "--defaults",
        action="store_true",
        help="view/edit the saved jp live defaults (access, auto-terminal), then exit",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the writable confirmation (required for unattended/non-tty use)",
    )
    p.add_argument(
        "--mount",
        metavar="POINT",
        help="override the auto mount target (a folder, or a drive letter like Z: on Windows)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="safe offline self-test: exercise the full transport against a LOCAL folder",
    )
    p.add_argument("--root", help="local folder to use as the simulated remote (with --dry-run)")
    p.add_argument(
        "--stats",
        action="store_true",
        help="(with --dry-run) also print the remote machine stats dashboard",
    )
    p.add_argument(
        "--print-agent",
        action="store_true",
        help="print the EXACT agent code that would be injected into the kernel, then exit",
    )
    p.set_defaults(func=run)


class _Aborted(Exception):
    """Internal: user cancelled at the confirmation; mapped to a clean EXIT_OK."""


def run(args: argparse.Namespace) -> int:
    if getattr(args, "print_agent", False):
        return _print_agent(args)
    if getattr(args, "defaults", False):
        return _defaults_menu()
    if getattr(args, "dry_run", False):
        return _dry_run(
            args.root,
            getattr(args, "mount", None),
            show_stats=getattr(args, "stats", False),
            writable=not getattr(args, "read_only", False),
        )
    # `jp live unmount`: stop the live mount that owns the current directory.
    if (getattr(args, "url", None) or "") == "unmount":
        return _unmount_here()
    try:
        return _live(args)
    except _Aborted:
        return EXIT_OK


# --------------------------------------------------------------------------- #
# --print-agent: transparency. Prints exactly what --live injects; no network.
# --------------------------------------------------------------------------- #
def _print_agent(args: argparse.Namespace) -> int:
    writable = not getattr(args, "read_only", False)
    url = getattr(args, "url", None)
    if url:
        cfg = _context.config_from_url(url, credential=getattr(args, "credential", "") or "")
        prefix = cfg.prefix
    else:
        # No URL: fall back to a workspace config (the legacy transparency path).
        ctx = load_repo()
        prefix = ctx.cfg.prefix
    code = agent_loader.build_bootstrap(root=prefix, writable=writable)
    ui.out(code)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# jp live <URL>: the real one-shot mount.
# --------------------------------------------------------------------------- #
def _live(args: argparse.Namespace) -> int:
    # C2 GUARD: refuse to run inside a .jp workspace tree (root or any subfolder),
    # so a live mount can never be nested inside something a `push` would walk.
    if paths.find_root() is not None:
        raise SafetyError(_WORKSPACE_GUARD_MSG)

    url = getattr(args, "url", None)
    if not url:
        raise UsageError(
            "jp live needs a URL: jp live <URL>. Copy the address bar URL of your "
            "Jupyter folder, e.g. https://host/user/<name>/lab/tree/<folder>."
        )

    cfg = _context.config_from_url(url, credential=getattr(args, "credential", "") or "")
    # SAFETY GATE: the prefix was already validated inside config_from_url.
    prefix = cfg.prefix

    api = _context.build_api(cfg)
    # Surface a stopped server / bad token with the friendly messages; the CLI
    # boundary redacts and prints, so we just let these propagate.
    api.status_probe()

    writable = not getattr(args, "read_only", False)

    token = config_mod.load_token(cfg)
    rfs, release = connect_live(api, prefix=prefix, token=token, writable=writable)

    try:
        if not rfs.ping():
            raise SafetyError("the remote file agent did not respond to ping; aborting.")

        # C4: the write/read confirmation (the selector names the exact target).
        # --read-only forces read-only and skips; --yes assumes writable and
        # skips; a saved default skips; a non-tty without --yes is refused.
        leaf = _prefix_leaf(prefix)
        display = _planned_display(args, leaf)
        writable = _confirm_writable(args, prefix=prefix, display=display, writable=writable)
        return _serve(args, rfs, prefix=prefix, writable=writable, leaf=leaf, url=url, cfg=cfg)
    finally:
        with _swallow():
            release()
        ui.info("kernel released.")


def _prefix_leaf(prefix: str) -> str:
    """Last path segment of the prefix (``privado/jp-live-test`` -> ``jp-live-test``)."""
    return prefix.rstrip("/").split("/")[-1] or prefix


def _planned_display(args: argparse.Namespace, leaf: str) -> str:
    """The display handle the confirmation/warning text should reference.

    Mirrors what the mount will produce so the prompt is accurate BEFORE we
    mount: the explicit ``--mount POINT`` if given, else the auto target.
    """
    explicit = getattr(args, "mount", None)
    if explicit:
        return explicit
    from ..mount import os_mount

    used = None if not sys.platform.startswith("win") else _used_drive_letters()
    return os_mount.auto_mount_target(leaf, os.getcwd(), sys.platform, used_drive_letters=used)


def _used_drive_letters() -> set[str] | None:
    """Best-effort in-use Windows drive letters; None off-Windows or on failure."""
    if not sys.platform.startswith("win"):
        return None
    try:
        from ..mount import os_mount

        return os_mount._used_drive_letters_windows()
    except Exception:
        return None


def _confirm_writable(
    args: argparse.Namespace,
    *,
    prefix: str,
    display: str,
    writable: bool,
) -> bool:
    """C4 confirmation. Returns the resolved writable flag (or aborts/raises).

    The single prompt uses the per-OS display handle, never a hardcoded path.
    The selector title already names the exact prefix + target, so we do not
    pre-list the folder contents (the mount itself shows them).
    """
    # Explicit per-run flags win over everything.
    if getattr(args, "read_only", False):
        return False
    if getattr(args, "yes", False):
        return writable

    # A saved global default (set via the picker's "remember" or `jp live
    # --defaults`) skips the prompt -- this also makes unattended runs work.
    saved = user_settings.get_live_access()
    if saved == "writable":
        ui.detail("using saved default: writable  (change with: jp live --defaults)")
        return True
    if saved == "read-only":
        ui.detail("using saved default: read-only  (change with: jp live --defaults)")
        return False

    # Otherwise ask interactively with the pretty selector.
    if not sys.stdin.isatty():
        raise SafetyError(
            "refusing to start a live mount unattended. Re-run with --yes (writable) "
            "or --read-only, or set a default with: jp live --defaults."
        )

    result = tui.select_access(f"{prefix} -> {display}", default_writable=writable)
    if result is None:
        ui.info("aborted")
        raise _Aborted()
    chosen, remember = result
    if remember:
        user_settings.set_live_access("writable" if chosen else "read-only")
        ui.detail("saved as your default  (change with: jp live --defaults)")
    return chosen


def _serve(
    args: argparse.Namespace,
    rfs,
    *,
    prefix: str,
    writable: bool,
    leaf: str,
    url: str,
    cfg,
) -> int:
    """Wrap in the cache, start WebDAV, mount under the auto handle, run keep-alive."""
    from ..mount import live_state, os_mount
    from ..mount.webdav_server import DavServer
    from ..vfs_cache import CachingFS

    cached = CachingFS(rfs)
    dav = DavServer(cached, writable=writable).start()
    handle = None
    workspace_file = None
    recorded = False
    try:
        explicit = getattr(args, "mount", None)
        if explicit:
            handle = _explicit_mount_handle(os_mount, dav.url, explicit)
        else:
            handle = os_mount.build_auto_mount_handle(dav.url, leaf, os.getcwd(), sys.platform)

        if getattr(args, "code", False):
            # Auto-terminal follows the saved default; --no-terminal forces it off.
            with_terminal = user_settings.get_live_code_terminal() and not getattr(
                args, "no_terminal", False
            )
            workspace_file = _open_in_code(
                handle, url=url, credential=cfg.credential, leaf=leaf, with_terminal=with_terminal
            )

        # Register this running mount so `jp live unmount` can find + stop it,
        # and route SIGTERM (sent by that command) into a clean Ctrl-C-style stop.
        with _swallow():
            live_state.record(
                handle.display,
                pid=os.getpid(),
                url=url,
                display=handle.display,
                created=time.time(),
            )
            recorded = True
        _install_signal_stop()

        _print_live_banner(prefix=prefix, writable=writable, display=handle.display, url=url)
        _keepalive_loop(cached)
        return EXIT_OK
    except os_mount.MountError as exc:
        # Mount failure: cleanup happens in finally; surface the manual command.
        ui.warn(f"mount failed ({exc}).")
        point = getattr(args, "mount", None) or leaf
        plan = os_mount.build_mount_plan(dav.url, point, sys.platform)
        ui.out(f"  mount it manually: {' '.join(plan.argv)}")
        return EXIT_OK
    finally:
        if recorded and handle is not None:
            with _swallow():
                live_state.remove(handle.display)
        if handle is not None:
            with _swallow():
                handle.unmount()
            with _swallow():
                handle.cleanup()
        if workspace_file is not None:
            with _swallow():
                os.unlink(workspace_file)
        with _swallow():
            dav.stop()


def _explicit_mount_handle(os_mount, url: str, point: str):
    """A MountHandle for an explicit --mount POINT: mount there, no-op cleanup.

    The user owns the target, so cleanup never removes it (only unmount runs).
    """
    os_mount.mount(url, point, platform=sys.platform)

    def _unmount() -> None:
        os_mount.unmount(url, point, platform=sys.platform)

    return os_mount.MountHandle(
        display=point,
        open_target=point,
        unmount=_unmount,
        cleanup=lambda: None,
    )


def _open_in_code(
    handle, *, url: str, credential: str, leaf: str, with_terminal: bool = True
) -> str | None:
    """Write a LOCAL ``<leaf>.code-workspace`` and open it in VS Code.

    The workspace file is written in the cwd -- NEVER inside the mounted folder
    (the server rejects dotfiles). When ``with_terminal`` is False the workspace
    omits the auto-start remote-terminal task. Returns the path written (for
    cleanup), or ``None`` if it could not be written.
    """
    from ..mount import vscode_launch

    # Use the SAME program name the user invoked (jp, jpw, ...), so the VS Code
    # auto-terminal task calls the same binary that has this standalone form.
    program = os.path.basename(sys.argv[0] or "jp") or "jp"
    workspace = vscode_launch.build_code_workspace(
        handle.open_target, url, credential, with_terminal=with_terminal, program=program
    )
    path = os.path.abspath(os.path.join(os.getcwd(), f"{leaf}.code-workspace"))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(workspace, fh, indent=2)
    except OSError as exc:
        ui.warn(f"could not write {path}: {exc}")
        return None

    code_on_path = shutil.which("code") is not None
    argv = vscode_launch.launcher_argv(path, sys.platform, code_on_path)
    launched = False
    if argv is not None:
        import subprocess

        try:
            subprocess.run(argv, check=True, timeout=30)
            launched = True
        except (OSError, subprocess.SubprocessError) as exc:
            ui.warn(f"could not launch VS Code ({exc}).")

    if not launched:
        ui.info(f"open this workspace in VS Code: {path}")
        ui.info(f'then run a remote shell with: {program} terminal "{url}"')
    return path


def _print_live_banner(*, prefix: str, writable: bool, display: str, url: str) -> None:
    """The clean, scannable post-mount status block (no secret URL in scrollback)."""
    access = "writable" if writable else "read-only"
    ui.out("")
    ui.success(f"{prefix}  ·  {access}")
    ui.detail(f"  mounted   {display}")
    ui.out("")
    ui.detail("  server-side changes appear when a file is re-read (no live push)")
    ui.detail(f'  live output   jp terminal "{url}"')
    if writable:
        ui.warn("writable -- edits & deletes reach the server, no undo. keep a backup.")
    ui.out("")
    ui.detail("  stop   Ctrl-C   ·   or  jp live unmount  (from the mounted folder)")
    ui.out("")


def _install_signal_stop() -> None:
    """Route SIGTERM into a clean Ctrl-C-style stop so the finally blocks run.

    `jp live unmount` sends SIGTERM to this process; translating it to
    KeyboardInterrupt reuses the keep-alive loop's existing clean-shutdown path
    (unmount + cleanup + kernel release). Best-effort: signal handling differs on
    Windows, so any failure is swallowed.
    """

    def _stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    with contextlib.suppress(Exception):
        signal.signal(signal.SIGTERM, _stop)


def _unmount_here() -> int:
    """`jp live unmount`: stop the live mount that owns the current directory."""
    from ..mount import live_state

    rec = live_state.find_for_path(os.getcwd())
    if rec is None:
        raise SafetyError(
            "no live mount found for this directory. Run `jp live unmount` from "
            "inside a folder mounted by `jp live`."
        )
    pid = int(rec.get("pid", 0) or 0)
    display = rec.get("display") or rec.get("mountpoint", "")
    if pid <= 0:
        live_state.remove(rec.get("mountpoint", ""))
        raise SafetyError("the live mount record is incomplete; cleared it. Try remounting.")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # The owning process is already gone -- clear the stale record.
        live_state.remove(rec.get("mountpoint", ""))
        ui.info(f"the live mount at {display} was already stopped; cleared its record.")
        return EXIT_OK
    except OSError as exc:
        raise SafetyError(f"could not signal the live mount (pid {pid}): {exc}") from exc

    ui.success(f"stopping live mount at {display} (pid {pid}).")
    ui.detail("  it will unmount and release the kernel.")
    return EXIT_OK


def _defaults_menu() -> int:
    """`jp live --defaults`: view/edit the saved global jp live defaults."""
    access = tui.Setting(
        key="access",
        label="Default access",
        value=user_settings.get_live_access(),
        options=("ask", "writable", "read-only"),
        help_text="What jp live does at mount time: 'ask' shows the picker; "
        "'writable'/'read-only' skip it and mount that way.",
    )
    code_term = tui.Setting(
        key="code_terminal",
        label="--code auto-terminal",
        value=user_settings.get_live_code_terminal(),
        options=(True, False),
        help_text="Whether --code auto-starts the remote terminal in VS Code. "
        "Per run, --no-terminal overrides this.",
    )
    if not tui.interactive():
        ui.info(
            f"live defaults (edit interactively with a tty): access={access.value}, "
            f"code_terminal={str(code_term.value).lower()}"
        )
        ui.info(f"stored at {user_settings.settings_path()}")
        return EXIT_OK

    result = tui.settings_menu([access, code_term], title="jp live defaults")
    if result is None:
        ui.info("unchanged")
        return EXIT_OK
    if access.changed:
        user_settings.set_live_access(str(access.value))
    if code_term.changed:
        user_settings.set_live_code_terminal(bool(code_term.value))
    ui.success(f"saved to {user_settings.settings_path()}")
    return EXIT_OK


def _keepalive_loop(cached) -> None:
    """Ping the remote every ~30s (defeats idle-culling) until Ctrl-C.

    The ping flows through the cache to the KernelConn, which reconnects if the
    kernel was culled. Sleeps in short steps so Ctrl-C stays responsive.
    """
    last = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - last >= _KEEPALIVE_INTERVAL:
                cached.ping()
                last = now
            time.sleep(_SLEEP_STEP)
    except KeyboardInterrupt:
        ui.out("")
        ui.info("stopping")


def _swallow():
    """Best-effort context manager: never let a cleanup step raise."""
    return contextlib.suppress(Exception)


# --------------------------------------------------------------------------- #
# --dry-run: offline transport self-test (unchanged behaviour).
# --------------------------------------------------------------------------- #
def _dry_run(
    root: str | None,
    mountpoint: str | None = None,
    show_stats: bool = False,
    writable: bool = False,
) -> int:
    if not root:
        raise SafetyError("--dry-run requires --root <folder>")

    from .. import stats
    from .._sim import FakeKernelWS
    from ..kernel_conn import KernelConn
    from ..remote_fs import RemoteFS

    ws = FakeKernelWS(root=root, writable=writable)
    ws.open_comm(comm_id="c1", target="jp.fs")
    rfs = RemoteFS(KernelConn(ws, comm_id="c1", session="dryrun"))

    if writable:
        warning = (
            "WRITABLE mode: writes through this mount WILL reach the (simulated) "
            "remote -- creating, overwriting, renaming and deleting files. Removals "
            "are NEVER recursive, but there is NO automatic server-side undo -- keep "
            "your own backup. Read-only is available with --read-only."
        )
        ui.out("!!! " + warning)
        ui.warn(warning)
        if sys.stdin.isatty():
            ui.out("Type 'yes' to proceed with a writable dry-run mount: ")
            if input().strip().lower() not in ("y", "yes"):
                ui.out("aborted (writable mount not confirmed)")
                return EXIT_OK

    ui.heading("jp live --dry-run (transport self-test, no network)")
    ui.out(f"ping: {'ok' if rfs.ping() else 'FAILED'}")

    verified = 0
    for entry in _walk(rfs, ""):
        ui.out(f"  {entry}")
        st = rfs.stat(entry)
        if st.type == "file" and st.size:
            head = rfs.read(entry, 0, min(st.size, 64))
            verified += len(head)
    ui.success(f"{verified} bytes verified over the binary comm transport")

    if show_stats:
        ui.out("")
        ui.out(stats.render_machine(rfs.statmachine()))

    if mountpoint:
        from ..mount import os_mount
        from ..mount.webdav_server import DavServer

        srv = DavServer(rfs, writable=writable).start()
        mounted = False
        try:
            url = srv.url
            plan = os_mount.build_mount_plan(url, mountpoint, sys.platform)
            ui.out(f"\nWebDAV server: {url}")
            ui.out(f"Mount note   : {plan.note}")
            # Auto-mount, mirroring `--live --mount`, so testing the mount is a
            # single command -- no second terminal, no copy-paste of the secret
            # URL, no orphaned mount if the manual umount is forgotten.
            try:
                os_mount.mount(url, mountpoint, platform=sys.platform)
                mounted = True
                ui.success(f"mounted at {mountpoint}")
            except os_mount.MountError as exc:
                ui.warn(f"automatic mount failed ({exc}); mount it manually:")
                ui.out(f"  {' '.join(plan.argv)}")
            msg = f"\nServer running at {url}. Press Enter here to stop and unmount."
            if sys.stdin.isatty():
                ui.out(msg)
                input()
            else:
                ui.out(msg)
        finally:
            if mounted:
                os_mount.unmount(srv.url, mountpoint, platform=sys.platform)
            srv.stop()

    return EXIT_OK


def _walk(rfs, path: str):
    """Yield relative paths depth-first (for the report)."""
    for e in rfs.listdir(path):
        rel = f"{path}/{e.name}" if path else e.name
        yield rel
        if e.type == "directory":
            yield from _walk(rfs, rel)

"""``jp live`` -- live remote-as-local mount over the kernel websocket.

Three modes, gated by an explicit flag:

  * ``--dry-run``  : exercise the full transport against a LOCAL folder, no
                     network. A safe offline self-test.
  * ``--live``     : connect to your CONFIGURED server (read-only). Starts a
                     kernel, injects the small file agent, opens the comm, and
                     serves the remote folder over loopback WebDAV.
  * ``--live --writable`` : as above, but local edits WRITE to the remote (still
                     never recursive-deletes; a server-side checkpoint is taken
                     before each overwrite).

This command NEVER auto-connects: the user must pass ``--live`` themselves, and
unattended (non-tty) use is refused unless ``--yes`` is given. The shared/too-
broad prefix guard (``paths.validate_prefix``) blocks the live path before any
connection. ``--print-agent`` prints the exact bootstrap code that would be
injected, for auditing, and exits without touching the network.
"""

from __future__ import annotations

import argparse
import sys
import time

from .. import agent_loader, paths, ui
from .. import config as config_mod
from ..errors import EXIT_OK, SafetyError, UsageError
from ..live_session import connect_live
from . import _context
from ._context import load_repo

# How often we ping the remote to defeat the server's idle-culling, and a cap on
# the per-iteration sleep so KeyboardInterrupt stays responsive.
_KEEPALIVE_INTERVAL = 30.0
_SLEEP_STEP = 1.0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("live", help="live mount of a remote folder over the kernel")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="safe offline self-test: exercise the full transport against a LOCAL folder",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="connect to your configured server and mount the remote folder (read-only)",
    )
    p.add_argument(
        "--print-agent",
        action="store_true",
        help="print the EXACT agent code that --live would inject into the kernel, then exit",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip confirmation prompts (required for unattended/non-tty --live)",
    )
    p.add_argument("--root", help="local folder to use as the simulated remote (with --dry-run)")
    p.add_argument(
        "--stats",
        action="store_true",
        help="(with --dry-run) also print the remote machine stats dashboard",
    )
    p.add_argument(
        "--mount",
        metavar="POINT",
        help="serve over WebDAV and mount natively at POINT (falls back to printing the command)",
    )
    p.add_argument(
        "--writable",
        action="store_true",
        help=(
            "allow writes through the mount to reach the remote. OFF by default; "
            "read-only is always the default."
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if getattr(args, "print_agent", False):
        return _print_agent(args)
    if getattr(args, "dry_run", False):
        return _dry_run(
            args.root,
            getattr(args, "mount", None),
            show_stats=getattr(args, "stats", False),
            writable=getattr(args, "writable", False),
        )
    if getattr(args, "live", False):
        return _live(args)
    raise UsageError("specify --dry-run (safe local self-test) or --live (connect to your server)")


# --------------------------------------------------------------------------- #
# --print-agent: transparency. Prints exactly what --live injects; no network.
# --------------------------------------------------------------------------- #
def _print_agent(args: argparse.Namespace) -> int:
    ctx = load_repo()
    code = agent_loader.build_bootstrap(
        root=ctx.cfg.prefix, writable=getattr(args, "writable", False)
    )
    ui.out(code)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# --live: the gated real path.
# --------------------------------------------------------------------------- #
def _live(args: argparse.Namespace) -> int:
    ctx = load_repo()
    # SAFETY GATE 1: refuse shared/too-broad prefixes BEFORE any connection.
    prefix = paths.validate_prefix(ctx.cfg.prefix)

    api = _context.build_api(ctx.cfg)
    # Surface a stopped server / bad token with the friendly messages. The CLI
    # boundary redacts and prints, so we just let these propagate.
    api.status_probe()

    writable = getattr(args, "writable", False)

    # SAFETY GATE 2: confirmation. Refuse unattended (non-tty) use unless --yes.
    if not getattr(args, "yes", False):
        server = ctx.cfg.base_url
        ui.warn(
            f"jp live will start a kernel on {server}, inject a small read-only "
            f"file agent, and mount {prefix!r} locally."
        )
        if writable:
            ui.warn(
                "WRITABLE mode: local edits will WRITE to the remote. Removals are "
                "NEVER recursive, and a server-side checkpoint is taken before each "
                "overwrite. Read-only is the default; this is OFF unless --writable."
            )
        if not sys.stdin.isatty():
            raise SafetyError(
                "refusing to start a live mount unattended. Re-run with --yes if you "
                "are sure, or run it in an interactive terminal."
            )
        if input("Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
            ui.info("aborted")
            return EXIT_OK

    token = config_mod.load_token(ctx.cfg)
    rfs, cleanup = connect_live(api, prefix=prefix, token=token, writable=writable)

    try:
        # SAFETY PRE-FLIGHT: a read-only probe + show the top-level entries so the
        # user can confirm the mount points at the folder they expect BEFORE any
        # mount or write. Catches a wrong prefix/root early.
        if not rfs.ping():
            raise SafetyError("the remote file agent did not respond to ping; aborting.")
        entries = rfs.listdir("")
        ui.heading(f"top of {prefix!r}:")
        if entries:
            ui.bullets(
                f"{e.name}{'/' if getattr(e, 'type', '') == 'directory' else ''}" for e in entries
            )
        else:
            ui.out("  (empty)")
        if not getattr(args, "yes", False) and input(
            "Does this match the folder you expect? [y/N]: "
        ).strip().lower() not in ("y", "yes"):
            ui.info("aborted")
            return EXIT_OK

        return _serve(args, rfs, prefix=prefix, writable=writable)
    finally:
        cleanup()
        ui.info("kernel released.")


def _serve(args: argparse.Namespace, rfs, *, prefix: str, writable: bool) -> int:
    """Wrap in the cache, start WebDAV, optionally mount, and run the keep-alive loop."""
    from ..mount import os_mount
    from ..mount.webdav_server import DavServer
    from ..vfs_cache import CachingFS

    cached = CachingFS(rfs)
    dav = DavServer(cached, writable=writable).start()
    mountpoint = getattr(args, "mount", None)
    mounted = False
    try:
        ui.success(f"WebDAV server: {dav.url}")
        if mountpoint:
            plan = os_mount.build_mount_plan(dav.url, mountpoint, sys.platform)
            try:
                os_mount.mount(dav.url, mountpoint, platform=sys.platform)
                mounted = True
                ui.success(f"mounted at {mountpoint}")
            except os_mount.MountError as exc:
                ui.warn(f"automatic mount failed ({exc}); mount it manually:")
                ui.out(f"  {' '.join(plan.argv)}")
        else:
            ui.info("mount it natively (see 'docs/jp-live.md'), then press Ctrl-C to stop.")

        _keepalive_loop(cached)
        return EXIT_OK
    finally:
        if mounted and mountpoint:
            os_mount.unmount(dav.url, mountpoint, platform=sys.platform)
        dav.stop()


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
            "remote -- creating, overwriting, renaming and deleting files. "
            "Read-only is the default; this is OFF unless --writable is given."
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
        from ..mount.os_mount import build_mount_plan, build_unmount_plan
        from ..mount.webdav_server import DavServer

        srv = DavServer(rfs, writable=writable).start()
        try:
            url = srv.url
            plan = build_mount_plan(url, mountpoint, sys.platform)
            unmount_argv = build_unmount_plan(url, mountpoint, sys.platform)
            ui.out(f"\nWebDAV server: {url}")
            ui.out(f"Mount note   : {plan.note}")
            ui.out(f"Mount command: {' '.join(plan.argv)}")
            ui.out(f"Umount cmd   : {' '.join(unmount_argv)}")
            msg = (
                "\nServer running at "
                + url
                + ". Mount with the command above, then press Enter here to stop."
            )
            if sys.stdin.isatty():
                ui.out(msg)
                input()
            else:
                ui.out(msg)
        finally:
            srv.stop()

    return EXIT_OK


def _walk(rfs, path: str):
    """Yield relative paths depth-first (for the report)."""
    for e in rfs.listdir(path):
        rel = f"{path}/{e.name}" if path else e.name
        yield rel
        if e.type == "directory":
            yield from _walk(rfs, rel)

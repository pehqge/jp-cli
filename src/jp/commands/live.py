"""``jp live`` -- live remote-as-local mount (PHASE 1: dry-run only).

Phase 1 ships ONLY ``jp live --dry-run``: it wires the entire transport
(RemoteFS -> KernelConn -> simulator running the real agent) against a local
directory and prints a verification report. The real-server path is deliberately
blocked until the live-fire certification (Phase 6); attempting ``--live`` raises
SafetyError. This guarantees nothing in Phase 1 can touch a real JupyterHub.
"""

from __future__ import annotations

import argparse
import sys

from .. import ui
from ..errors import EXIT_OK, SafetyError


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("live", help="(experimental) live mount of a remote folder")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="exercise the full transport against a LOCAL folder (no network)",
    )
    p.add_argument("--root", help="local folder to use as the simulated remote (with --dry-run)")
    p.add_argument(
        "--live", action="store_true", help="(blocked until certified) connect to the real server"
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="(with --dry-run) also print the remote machine stats dashboard",
    )
    p.add_argument(
        "--mount",
        metavar="POINT",
        help=(
            "(with --dry-run) serve the simulated remote over WebDAV and print the "
            "native-mount command for POINT"
        ),
    )
    p.add_argument(
        "--writable",
        action="store_true",
        help=(
            "(with --dry-run) allow writes through the mount to reach the "
            "(simulated) remote. OFF by default; read-only is always the default."
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if getattr(args, "live", False) or not getattr(args, "dry_run", False):
        raise SafetyError(
            "jp live is in development: only '--dry-run --root <folder>' is enabled. "
            "The real-server path is disabled until the safety certification is complete."
        )
    return _dry_run(
        args.root,
        getattr(args, "mount", None),
        show_stats=getattr(args, "stats", False),
        writable=getattr(args, "writable", False),
    )


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

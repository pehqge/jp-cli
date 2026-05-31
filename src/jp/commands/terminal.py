"""``jp terminal`` -- turn this terminal into the remote machine's shell.

One command: it creates an ephemeral Jupyter ``terminado`` terminal on the
server, connects to its websocket, and proxies your local PTY to it, starting in
the workspace's mapped folder. No SSH, no configuration.

Safety (this command is paranoid by design -- the server is shared):
  * The ONLY remote calls are ``POST``/``DELETE /api/terminals`` -- ephemeral PTY
    sessions. It NEVER touches the Contents API, never reads/writes/moves/deletes
    a file, and is never recursive. There is no file operation to gate, so the
    path-jail is not even reachable here.
  * ``DELETE`` targets ONLY the session name THIS process created (held in a
    local), in a ``finally`` -- so we never leak our terminal and never affect
    another user's or another session's terminal.
  * The token travels only in the ``Authorization`` header on the websocket
    handshake -- never in a URL.

POSIX only: it needs ``termios``/``tty`` for raw mode. On Windows (no termios) it
falls back to opening the Jupyter web UI so the user can use New -> Terminal.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import select
import shlex
import signal
import sys

from .. import config as config_mod
from .. import paths, ui
from .._ws import WebSocket, WebSocketError
from ..errors import EXIT_OK, NetworkError, UsageError
from . import _context
from ._context import RepoContext, load_repo

# Local escape: Ctrl-] (telnet convention, 0x1d). Forces a disconnect even if the
# remote shell is wedged. Chosen over ":q" (collides with vim/less) because it is
# essentially never typed into a shell.
_ESCAPE = 0x1D

# Raw-mode availability. termios/tty are POSIX-only; on Windows we fall back to
# the browser. Imported lazily so the module loads everywhere.
try:
    import termios
    import tty

    _HAS_PTY = True
except ImportError:  # pragma: no cover - exercised only on Windows
    _HAS_PTY = False


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "terminal",
        help="open the remote machine's shell in this terminal",
    )
    p.add_argument(
        "--no-cd",
        action="store_true",
        help="start in the server's default directory instead of the workspace folder",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the confirmation prompt",
    )
    p.set_defaults(func=run)


# --------------------------------------------------------------------------- #
# Pure protocol helpers (terminado JSON message format) -- directly tested
# --------------------------------------------------------------------------- #
def stdin_message(data: bytes) -> str:
    """Build a terminado ``["stdin", <text>]`` message from raw keystrokes."""
    return json.dumps(["stdin", data.decode("utf-8", "replace")])


def setsize_message(rows: int, cols: int) -> str:
    """Build a terminado ``["set_size", rows, cols]`` resize message."""
    return json.dumps(["set_size", int(rows), int(cols)])


def parse_server_message(text: str) -> tuple[str, object]:
    """Parse a server message into ``(kind, payload)``.

    terminado sends JSON arrays: ``["setup", {}]``, ``["stdout", "<text>"]``,
    ``["disconnect", 1]``. Returns ``("", None)`` for anything unparseable so the
    caller can simply ignore noise.
    """
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return ("", None)
    if isinstance(msg, list) and msg:
        payload = msg[1] if len(msg) > 1 else None
        return (str(msg[0]), payload)
    return ("", None)


def cd_command(prefix: str) -> str:
    """A silent ``cd`` into the workspace folder, used only as a fallback.

    Sent as the first stdin when the server did not honour the native ``cwd``.
    ``shlex.quote`` defends against odd prefixes (the prefix is already validated,
    but this is cheap defense in depth); ``clear`` hides the command itself.
    """
    return f"cd -- {shlex.quote(prefix)} 2>/dev/null && clear\n"


# --------------------------------------------------------------------------- #
# Command entry point
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    ctx = load_repo()

    if not _HAS_PTY:
        return _browser_fallback(args, ctx)

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise UsageError("jp terminal needs an interactive terminal (a tty).")

    api = _context.build_api(ctx.cfg)
    prefix = paths.validate_prefix(ctx.cfg.prefix)

    if not args.yes and not _confirm():
        ui.info("aborted")
        return EXIT_OK

    cwd = None if args.no_cd else prefix

    # Create the terminal FIRST (outside the try): if it fails -- e.g. terminals
    # are disabled (404) or forbidden (403) -- nothing was created, so there is
    # nothing to clean up and the error propagates to the CLI boundary.
    session = api.create_terminal(cwd=cwd)
    name = session.name

    try:
        token = config_mod.load_token(ctx.cfg)
        url = api.terminal_ws_url(name)
        try:
            ws = WebSocket.connect(
                url,
                headers={"Authorization": f"token {token}"},
                timeout=ctx.cfg.timeout,
            )
        except WebSocketError as exc:
            raise NetworkError(f"could not open the terminal websocket: {exc}") from exc
        try:
            do_cd = cwd is not None and not session.cwd_applied
            _run_pty(ws, prefix=prefix, do_cd=do_cd)
        finally:
            ws.close()
    finally:
        # ALWAYS delete only the session we created -- never leak it, never touch
        # any other terminal.
        api.delete_terminal(name)

    ui.info("terminal closed.")
    return EXIT_OK


def _confirm() -> bool:
    ui.warn("This opens an interactive shell on the remote server.")
    ans = input("Open a remote shell now? [y/N]: ").strip().lower()
    return ans in ("y", "yes")


# --------------------------------------------------------------------------- #
# PTY proxy (POSIX)
# --------------------------------------------------------------------------- #
def _run_pty(ws: WebSocket, *, prefix: str, do_cd: bool) -> None:
    """Put the local tty in raw mode and proxy it to the websocket.

    Restores the terminal and all signal handlers in ``finally`` no matter how we
    exit, so the user's shell is never left in raw mode.
    """
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    saved = termios.tcgetattr(stdin_fd)

    # SIGWINCH wakeup pipe: PEP 475 auto-retries select on EINTR, so we cannot
    # rely on the signal interrupting select. Instead route the signal to a pipe
    # we also select on, and recompute the window size when it fires.
    pipe_r, pipe_w = os.pipe()
    os.set_blocking(pipe_w, False)
    old_wakeup = signal.set_wakeup_fd(pipe_w)
    old_winch = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGWINCH, lambda *_: None)

    try:
        tty.setraw(stdin_fd)
        _send_winsize(ws, stdout_fd)
        if do_cd:
            ws.send_text(stdin_message(cd_command(prefix).encode("utf-8")))
        # Flush anything already buffered from the handshake (the setup message
        # and any early prompt) before blocking in select.
        if not _pump_output(ws, stdout_fd):
            _loop(ws, stdin_fd, stdout_fd, pipe_r)
    finally:
        signal.signal(signal.SIGWINCH, old_winch)
        signal.set_wakeup_fd(old_wakeup)
        os.close(pipe_r)
        os.close(pipe_w)
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)


def _loop(ws: WebSocket, stdin_fd: int, stdout_fd: int, wakeup_fd: int) -> None:
    while not ws.closed:
        try:
            readable, _, _ = select.select([stdin_fd, ws.fileno(), wakeup_fd], [], [])
        except OSError:
            break

        if wakeup_fd in readable:
            with contextlib.suppress(OSError):
                os.read(wakeup_fd, 4096)
            _send_winsize(ws, stdout_fd)

        if stdin_fd in readable:
            try:
                data = os.read(stdin_fd, 65536)
            except OSError:
                data = b""
            if not data or _ESCAPE in data:
                break
            ws.send_text(stdin_message(data))

        if ws.fileno() in readable and _pump_output(ws, stdout_fd):
            break


def _pump_output(ws: WebSocket, stdout_fd: int) -> bool:
    """Write any pending server stdout to the terminal.

    Returns True when the session should end (a ``disconnect`` message or the
    socket closed).
    """
    for raw in ws.read_messages():
        kind, payload = parse_server_message(raw.decode("utf-8", "replace"))
        if kind == "stdout" and isinstance(payload, str):
            _write_all(stdout_fd, payload.encode("utf-8"))
        elif kind == "disconnect":
            return True
    return ws.closed


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte: ``os.write`` may accept fewer bytes than given."""
    while data:
        written = os.write(fd, data)
        data = data[written:]


def _send_winsize(ws: WebSocket, fd: int) -> None:
    try:
        size = os.get_terminal_size(fd)
        rows, cols = size.lines, size.columns
    except OSError:
        rows, cols = 24, 80
    ws.send_text(setsize_message(rows, cols))


# --------------------------------------------------------------------------- #
# Windows fallback: open the Jupyter web UI (no termios -> no raw PTY)
# --------------------------------------------------------------------------- #
def _browser_fallback(args: argparse.Namespace, ctx: RepoContext) -> int:
    import webbrowser

    from . import kernel

    ui.warn("jp terminal needs a POSIX terminal (termios); it is not supported on this platform.")
    ui.info("Opening the Jupyter web UI instead -- use New -> Terminal there.")

    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("refusing to print the token without confirmation; pass --yes")
        ui.warn("This opens a URL containing your API token; treat it like a password.")
        if input("Open the browser now? [y/N]: ").strip().lower() not in ("y", "yes"):
            ui.info("aborted")
            return EXIT_OK

    token = config_mod.load_token(ctx.cfg)
    url = kernel.connection_url(ctx.cfg.base_url, token)
    webbrowser.open(url)
    return EXIT_OK

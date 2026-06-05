"""``jp run`` -- run a LOCAL script on the remote, exactly like a local run.

It must look indistinguishable from running the script on your own machine: no
shell prompt, no echoed command, no remote Linux chrome -- just the program's
output streaming live, working ``input()``, and the real exit code.

How it works:
  * The script source is uploaded to a random-named temp file *in the mapped
    folder you are standing in* (via the Contents API, out of band -- it never
    appears on your screen and never goes through jp's sync). Being in that
    folder gives the script the same cwd / ``sys.path`` / ``__file__`` /
    relative-``open()`` behaviour as a local run.
  * It is executed inside a one-shot terminado terminal whose command brackets
    the program output with two unique marker control-sequences (the FinalTerm /
    iTerm2 shell-integration technique). The client swallows the shell prompt and
    the echoed command (everything before the start marker) and stops at the end
    marker, which carries the exit code. ``exit`` then closes the session, so no
    trailing prompt is shown.
  * The remote PTY stays in cooked mode (echo on) so typed input shows up exactly
    like a local terminal; the local terminal is in raw mode so it does not
    double-echo. Ctrl-C is forwarded to the program just like locally.

Only that one temp file is ever created and removed -- no directory is touched --
so concurrent ``jp run`` invocations never clash and your files are never
overwritten (128-bit random name + a remote existence pre-check).

Workspace-only (no URL mode). Works on macOS, Linux and Windows: the raw local
proxy uses termios on POSIX and the virtual-terminal console modes on Windows
(both via :mod:`jp.pty`). A non-tty stdout (piped output) streams without raw
mode on every platform.
"""

from __future__ import annotations

import argparse
import contextlib
import secrets
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import config as config_mod
from .. import paths, pty, ui
from .._ws import WebSocket, WebSocketError
from ..errors import EXIT_OK, NetworkError, SafetyError, UsageError
from . import _context
from ._context import load_repo

EXT_INTERP = {
    ".py": "python3",
    ".sh": "bash",
    ".bash": "bash",
    ".js": "node",
    ".mjs": "node",
    ".rb": "ruby",
    ".pl": "perl",
    ".r": "Rscript",
    ".lua": "lua",
    ".php": "php",
}


@dataclass(frozen=True)
class Runner:
    use_shebang: bool
    interp: str  # "" when use_shebang is True


# --------------------------------------------------------------------------- #
# Pure helpers (directly tested)
# --------------------------------------------------------------------------- #
def resolve_runner(filename: str, first_line: str, as_override: str) -> Runner:
    """Decide how to run the file: ``--as`` wins, then a shebang, then extension."""
    if as_override:
        return Runner(False, as_override)
    if first_line.startswith("#!"):
        return Runner(True, "")
    ext = PurePosixPath(filename).suffix.lower()
    interp = EXT_INTERP.get(ext)
    if not interp:
        raise UsageError(
            f"don't know how to run {filename!r}; pass --as <interpreter> (e.g. --as python3)"
        )
    return Runner(False, interp)


# Python name-faking bootstrap: run the temp file's source under the REAL name so
# sys.argv[0], __file__, and tracebacks all show it (not the temp name) -- exactly
# like a local run. The temp path and real name arrive via env vars (JPF/JPNAME)
# so the source/name need no shell quoting. On an uncaught exception we print the
# traceback skipping the one bootstrap frame (``tb_next`` drops the ``exec`` call)
# so it is identical to a local run; SystemExit is re-raised so the real exit code
# is preserved. ``python -c`` sets sys.path[0] to '' (the cwd = the mapped
# folder), so sibling imports still work.
_PY_BOOTSTRAP = (
    "import os,sys,traceback\n"
    'real=os.environ.pop("JPNAME")\n'
    'src=open(os.environ.pop("JPF")).read()\n'
    "sys.argv[0]=real\n"
    "try:\n"
    '    exec(compile(src,real,"exec"),{"__name__":"__main__","__file__":real})\n'
    "except SystemExit:\n"
    "    raise\n"
    "except BaseException as e:\n"
    "    traceback.print_exception(type(e),e,e.__traceback__.tb_next)\n"
    "    sys.exit(1)"
)


def temp_name(real_name: str, rand_hex: str) -> str:
    """A random temp name derived from the real file.

    ``train.py`` -> ``__temp__.train.<rand_hex>.py``. NOT hidden (no leading dot):
    the Jupyter Contents API refuses to create dotfiles by default
    (``allow_hidden=False``), so a leading dot would 400. The ``__temp__.`` prefix
    makes it obviously transient and lets the default ignore rule skip it; the
    ``rand_hex`` (128 bits in production) makes a collision with a real file
    impossible.
    """
    p = PurePosixPath(real_name)
    return f"__temp__.{p.stem}.{rand_hex}{p.suffix}"


def _is_python(interp: str) -> bool:
    return interp.rsplit("/", 1)[-1].startswith("python")


def normalize_source(text: str) -> str:
    """CRLF/CR -> LF, with a guaranteed trailing newline."""
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    return out if out.endswith("\n") else out + "\n"


def build_remote_command(
    tmp_name: str,
    runner: Runner,
    args: list[str],
    token: str,
    *,
    remote_cwd: str,
    cwd_applied: bool,
    real_name: str,
) -> str:
    """The single POSIX-sh command line run inside the terminado terminal.

    It brackets the program's output with the start/end markers, propagates the
    exit code, removes the temp file, and ``exit``s so the session closes with no
    trailing prompt. The markers are written with backslash *notation* so the
    echoed command shows literal text; only the executed ``printf`` emits the
    real control bytes the client matches.

    A Python file (by extension/``--as``, not a shebang) runs through a tiny
    bootstrap so ``sys.argv[0]``/``__file__``/tracebacks show ``real_name`` instead
    of the temp name. Shebang scripts and non-Python interpreters run the temp
    file directly (they show the temp name -- a minor cosmetic difference).
    """
    q_args = " ".join(shlex.quote(a) for a in args)
    tail = f" {q_args}" if q_args else ""
    if runner.use_shebang:
        run_line = f'chmod +x "$f"; "./$f"{tail}'
    elif _is_python(runner.interp):
        run_line = (
            f'JPF="$f" JPNAME={shlex.quote(real_name)} '
            f"{shlex.quote(runner.interp)} -c {shlex.quote(_PY_BOOTSTRAP)}{tail}"
        )
    else:
        run_line = f'{shlex.quote(runner.interp)} "$f"{tail}'
    cd = "" if cwd_applied else f"cd -- {shlex.quote(remote_cwd)} 2>/dev/null; "
    start = "printf '" + "\\033_jp" + token + "C\\033\\\\" + "'"
    end = "printf '" + "\\033_jp" + token + "D%d\\033\\\\" + '\' "$__rc"'
    return (
        f'{cd}f={shlex.quote(tmp_name)}; {start}; {run_line}; __rc=$?; rm -f -- "$f"; {end}; exit'
    )


def _remote_cwd(root: Path, prefix: str) -> str:
    """``prefix`` + (current dir relative to the workspace root), POSIX-joined."""
    rel = Path.cwd().resolve().relative_to(Path(root).resolve())
    rel_str = "" if str(rel) == "." else PurePosixPath(rel.as_posix()).as_posix()
    return prefix if not rel_str else f"{prefix}/{rel_str}"


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #
def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "run",
        help="run a local script on the remote in the current mapped folder",
    )
    p.add_argument("file", help="local file to run (relative to the current folder)")
    p.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to the script")
    p.add_argument(
        "--as",
        dest="as_interp",
        default="",
        metavar="INTERP",
        help="force the interpreter (e.g. python3, bash, node)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show the target folder, interpreter and source without running",
    )
    p.set_defaults(func=run)


def _status(text: str, *, show: bool) -> None:
    if show:
        sys.stderr.write("\r\x1b[K" + text)
        sys.stderr.flush()


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    prefix = paths.validate_prefix(ctx.cfg.prefix)

    local = Path(args.file)
    if not local.is_file():
        raise UsageError(f"no such file: {args.file!r}")
    source = normalize_source(local.read_text(encoding="utf-8", errors="replace"))
    runner = resolve_runner(local.name, source.split("\n", 1)[0], args.as_interp)
    remote_cwd = _remote_cwd(ctx.root, prefix)

    if args.dry_run:
        how = "shebang" if runner.use_shebang else runner.interp
        ui.heading(f"jp run (dry-run): {args.file}")
        ui.info(f"remote folder : {remote_cwd}")
        ui.info(f"interpreter   : {how}")
        ui.info(f"args          : {args.args}")
        ui.out(source)
        return EXIT_OK

    # Interactive (raw local input for the program's input()) needs a real
    # terminal: POSIX termios, or a Windows console new enough for VT modes.
    # Without it we still stream output (pump_noninteractive); input() just has
    # no local stdin (e.g. piped output, or a pre-Win10-1511 console).
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    interactive = is_tty and (pty.HAS_PTY or pty.windows_console_supported())

    api = _context.build_api(ctx.cfg)
    if api.stat(remote_cwd) is None:
        raise UsageError(f"the remote folder {remote_cwd!r} does not exist -- run 'jp push' first.")

    # Upload the source to a random temp file in the mapped folder. The 128-bit
    # name plus a remote existence pre-check guarantee we never clobber an
    # existing file.
    src_bytes = source.encode("utf-8")
    api_path = ""
    tmp_name = ""
    for _ in range(8):
        tmp_name = temp_name(local.name, secrets.token_hex(16))
        api_path = f"{remote_cwd}/{tmp_name}"
        paths.assert_within_prefix(api_path, prefix)
        if api.stat(api_path) is None:
            break
    else:  # pragma: no cover - astronomically unlikely
        raise SafetyError("could not allocate a free temp filename on the remote")

    token = secrets.token_hex(8)
    show_status = sys.stderr.isatty()
    clear_seq = b"\r\x1b[K" if show_status else b""

    try:
        api.put_file_bytes(api_path, src_bytes)
        _status("· connecting…", show=show_status)
        session = api.create_terminal(cwd=remote_cwd)
        try:
            command = build_remote_command(
                tmp_name,
                runner,
                args.args,
                token,
                remote_cwd=remote_cwd,
                cwd_applied=session.cwd_applied,
                real_name=local.name,
            )
            auth = config_mod.load_token(ctx.cfg)
            url = api.terminal_ws_url(session.name)
            try:
                ws = WebSocket.connect(
                    url, headers={"Authorization": f"token {auth}"}, timeout=ctx.cfg.timeout
                )
            except WebSocketError as exc:
                raise NetworkError(f"could not open the run websocket: {exc}") from exc
            try:
                _status("· running…", show=show_status)
                scanner = pty.RunScanner(token, clear_seq=clear_seq)
                initial = [pty.stdin_message((command + "\n").encode("utf-8"))]
                if interactive:
                    rc = pty.drive(ws, initial=initial, scanner=scanner)
                else:
                    rc = pty.pump_noninteractive(ws, initial=initial, scanner=scanner)
            finally:
                ws.close()
        finally:
            api.delete_terminal(session.name)
    finally:
        # Guaranteed temp cleanup (the remote command also removes it).
        with contextlib.suppress(Exception):
            paths.assert_within_prefix(api_path, prefix)
            api.delete(api_path)

    return rc if rc is not None else EXIT_OK

"""jp command-line entry point: argparse subcommands + safe error handling.

Every command's ``run`` returns an exit code or raises a ``JpError``. This
dispatcher translates exceptions into the the design docsexit codes and passes EVERY
error message through ``ui.redact`` so a token can never leak via an error path.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence

from . import __version__, ui, update_notify
from .commands import ALL
from .errors import EXIT_GENERIC, EXIT_OK, JpError


def build_parser() -> argparse.ArgumentParser:
    # Parent parser holds global flags shared by all subcommands.
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("-q", "--quiet", action="store_true", help="suppress non-essential output")
    parent.add_argument("--no-color", action="store_true", help="disable colored output")

    parser = argparse.ArgumentParser(
        prog="jp",
        description="git-like safe sync between a local folder and a remote JupyterHub.",
        parents=[parent],
    )
    parser.add_argument("-V", "--version", action="version", version=f"jp {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = False

    # Each command registers its own subparser, inheriting the global flags.
    for mod in ALL:
        # add_parser implementations create the subparser; we re-attach the
        # parent flags so -q/--no-color work after the subcommand too.
        mod.add_parser(_SubparsersWithParent(subparsers, parent))

    return parser


class _SubparsersWithParent:
    """Wrap add_subparsers so each command's parser inherits global flags."""

    def __init__(self, subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser):
        self._subparsers = subparsers
        self._parent = parent

    def add_parser(self, name: str, **kwargs):
        parents = list(kwargs.pop("parents", []))
        parents.append(self._parent)
        return self._subparsers.add_parser(name, parents=parents, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    # Hidden internal command: the detached update-check worker. Intercepted
    # before argparse so it is never a visible/registered subcommand.
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw == ["_update-check"]:
        from .commands import update_check

        return update_check.run(argparse.Namespace())

    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "no_color", False):
        import os

        os.environ["JP_NO_COLOR"] = "1"
    if getattr(args, "quiet", False):
        ui.set_quiet(True)

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return EXIT_OK

    try:
        rc = func(args)
        rc = int(rc) if rc is not None else EXIT_OK
    except JpError as exc:
        # Central redaction at the error boundary.
        ui.error(exc.message)
        rc = exc.exit_code
    except KeyboardInterrupt:
        ui.error("interrupted")
        rc = EXIT_GENERIC
    except BrokenPipeError:  # pragma: no cover
        return EXIT_OK
    except Exception as exc:  # last-resort: never leak a token in a traceback line
        ui.error(f"unexpected error: {exc}")
        rc = EXIT_GENERIC

    # Passive 'update available' notice -- never affects rc, never raises.
    with contextlib.suppress(Exception):
        update_notify.maybe_notify(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())

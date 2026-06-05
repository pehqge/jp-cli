"""Tiny interactive terminal UI primitives -- pure standard library.

This module powers the interactive ``jp config`` settings screen and the
keep/delete confirmation selector used by mirror-mode sync. It is deliberately
dependency-free: raw key reading uses ``termios``/``tty`` on POSIX and
``msvcrt`` on Windows, and rendering uses plain ANSI escapes.

Everything degrades gracefully: if stdin/stdout is not an interactive terminal
(a pipe, a CI job, a redirect), :func:`interactive` returns False and callers
fall back to a non-interactive path instead of blocking on a prompt.

Keys are normalized to logical names so callers never touch escape sequences:
``"up" "down" "left" "right" "enter" "space" "esc" "tab" "backspace"``, or a
single printable character, or ``""`` at end-of-input.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence

# --------------------------------------------------------------------------- #
# Capability detection
# --------------------------------------------------------------------------- #


def interactive(stream: object | None = None) -> bool:
    """True only if we have a real interactive terminal on stdin AND stdout.

    Respects ``JP_NO_TUI`` (set it to anything to force the non-interactive
    path, e.g. in tests or scripts).
    """
    if os.environ.get("JP_NO_TUI"):
        return False
    out = stream if stream is not None else sys.stdout
    try:
        return bool(sys.stdin.isatty() and out.isatty())
    except (ValueError, AttributeError):
        return False


# --------------------------------------------------------------------------- #
# ANSI helpers
# --------------------------------------------------------------------------- #
_CSI = "\x1b["
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
REVERSE = "\x1b[7m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"


def _w(text: str) -> None:
    sys.stdout.write(text)


def _hide_cursor() -> None:
    _w("\x1b[?25l")


def _show_cursor() -> None:
    _w("\x1b[?25h")


def _clear_lines(n: int) -> None:
    """Move the cursor up ``n`` lines and clear from there to end of screen."""
    if n > 0:
        _w(f"{_CSI}{n}A")
    _w("\r")  # also return to column 0 (raw mode leaves it where it was)
    _w(f"{_CSI}0J")  # clear from cursor to end of screen


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _visible_len(s: str) -> int:
    """Length of ``s`` ignoring ANSI escape sequences (zero display width)."""
    return len(_ANSI_RE.sub("", s))


def _term_cols() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _wrap_ansi(line: str, width: int) -> list[str]:
    """Split ``line`` into physical rows of at most ``width`` visible columns,
    keeping ANSI escape sequences intact (they take zero columns)."""
    if width <= 0 or _visible_len(line) <= width:
        return [line]
    out: list[str] = []
    cur = ""
    col = 0
    i = 0
    n = len(line)
    while i < n:
        m = _ANSI_RE.match(line, i)
        if m:
            cur += m.group()
            i = m.end()
            continue
        cur += line[i]
        col += 1
        i += 1
        if col >= width:
            out.append(cur)
            cur = ""
            col = 0
    if cur or not out:
        out.append(cur)
    return out


def _render_block(lines: list[str], prev_lines: int) -> int:
    """Repaint a block of logical ``lines`` in place, returning the number of
    physical terminal rows written (pass it back as ``prev_lines`` next time).

    Two rendering hazards are handled here, both caused by raw terminal mode
    (``tty.setraw`` clears ``OPOST``, so the terminal does no output
    post-processing): a bare ``\\n`` moves the cursor down but NOT to column 0,
    and a logical line longer than the terminal wraps onto extra physical rows.
    We therefore emit explicit ``\\r\\n`` between rows and pre-wrap each logical
    line to the terminal width so the physical-row count stays exact -- which is
    what ``_clear_lines`` relies on to erase the previous frame cleanly.
    """
    cols = _term_cols()
    phys: list[str] = []
    for ln in lines:
        phys.extend(_wrap_ansi(ln, cols))
    _clear_lines(prev_lines)
    _w("\r" + "\r\n".join(phys) + "\r\n")
    sys.stdout.flush()
    return len(phys)


# --------------------------------------------------------------------------- #
# Raw key reader
# --------------------------------------------------------------------------- #


class _PosixReader:
    """Read single logical keypresses on POSIX using termios raw mode."""

    def __init__(self) -> None:
        import termios
        import tty

        self._termios = termios
        self._tty = tty
        self._fd = sys.stdin.fileno()
        self._saved = None

    def __enter__(self) -> _PosixReader:
        self._saved = self._termios.tcgetattr(self._fd)
        self._tty.setraw(self._fd)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._saved is not None:
            self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._saved)

    def read_key(self) -> str:
        ch = os.read(self._fd, 1)
        if not ch:
            return ""
        b = ch[0]
        if b == 0x1B:  # ESC -- could be a lone Esc or a CSI sequence
            seq = os.read(self._fd, 2)
            if seq == b"[A":
                return "up"
            if seq == b"[B":
                return "down"
            if seq == b"[C":
                return "right"
            if seq == b"[D":
                return "left"
            return "esc"
        if b in (0x0D, 0x0A):
            return "enter"
        if b == 0x20:
            return "space"
        if b == 0x09:
            return "tab"
        if b in (0x7F, 0x08):
            return "backspace"
        if b == 0x03:  # Ctrl-C
            raise KeyboardInterrupt
        try:
            return ch.decode("utf-8", "replace")
        except Exception:
            return ""


class _WindowsReader:
    """Read single logical keypresses on Windows using msvcrt."""

    def __init__(self) -> None:
        import msvcrt

        self._msvcrt = msvcrt

    def __enter__(self) -> _WindowsReader:
        # Best-effort: enable ANSI escape processing on the console.
        try:
            import ctypes

            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read_key(self) -> str:
        ch = self._msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # special key prefix; the next char is the code
            code = self._msvcrt.getwch()
            return {"H": "up", "P": "down", "M": "right", "K": "left"}.get(code, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\t":
            return "tab"
        if ch in ("\x08", "\x7f"):
            return "backspace"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch


def _make_reader():  # noqa: ANN202 -- returns a platform reader context manager
    if os.name == "nt":
        return _WindowsReader()
    return _PosixReader()


# --------------------------------------------------------------------------- #
# Settings menu (Claude-Code-style: arrows, Space to change, / search, i info)
# --------------------------------------------------------------------------- #


class Setting:
    """One editable row in the settings menu.

    ``options`` is the list of allowed values (for bool use ``[True, False]``);
    Space cycles to the next option. ``fmt`` renders a value for display.
    """

    def __init__(
        self,
        key: str,
        label: str,
        value: object,
        options: Sequence[object],
        help_text: str = "",
        fmt: Callable[[object], str] | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.options = list(options)
        self.help_text = help_text
        self._fmt = fmt or (lambda v: str(v).lower() if isinstance(v, bool) else str(v))
        self.value = value
        self.original = value

    @property
    def changed(self) -> bool:
        return self.value != self.original

    def display(self) -> str:
        return self._fmt(self.value)

    def cycle(self, step: int = 1) -> None:
        if not self.options:
            return
        try:
            i = self.options.index(self.value)
        except ValueError:
            i = -1
        self.value = self.options[(i + step) % len(self.options)]


def settings_menu(
    settings: Sequence[Setting], title: str = "", _reader: object | None = None
) -> list[Setting] | None:
    """Render an interactive settings editor. Returns the settings on save
    (Enter) or None on cancel (Esc). Mutates ``Setting.value`` in place.

    Controls: Up/Down move · Space change · i info · / search · Enter save ·
    Esc cancel.

    ``_reader`` is a test seam: a context manager yielding an object with a
    ``read_key()`` method. When provided, the interactive-terminal check is
    skipped (tests drive scripted key streams).
    """
    if _reader is None and not interactive():
        raise RuntimeError("settings_menu requires an interactive terminal")
    rows = list(settings)
    if not rows:
        return rows

    idx = 0
    query = ""
    searching = False
    show_help = False
    prev_lines = 0
    label_w = min(40, max((len(s.label) for s in rows), default=10) + 2)

    def visible() -> list[int]:
        if not query:
            return list(range(len(rows)))
        q = query.lower()
        return [i for i, s in enumerate(rows) if q in s.label.lower() or q in s.key.lower()]

    def render() -> int:
        nonlocal prev_lines
        vis = visible()
        lines: list[str] = []
        if title:
            lines.append(f"{BOLD}{title}{RESET}")
            lines.append("")
        for vi, i in enumerate(vis):
            s = rows[i]
            cursor = f"{CYAN}>{RESET} " if vi == idx else "  "
            name = s.label.ljust(label_w)
            val = s.display()
            mark = f" {YELLOW}*{RESET}" if s.changed else ""
            color = CYAN if vi == idx else ""
            end = RESET if vi == idx else ""
            lines.append(f"{cursor}{color}{name}{end}{val}{mark}")
        if not vis:
            lines.append(f"  {DIM}(no settings match {query!r}){RESET}")
        lines.append("")
        if show_help and vis:
            s = rows[vis[idx]]
            lines.append(f"{DIM}{s.help_text or '(no description)'}{RESET}")
            lines.append("")
            lines.append(
                f"{DIM}i hide info · Up/Down move · Space change · / search · "
                f"Enter save · Esc cancel{RESET}"
            )
        elif searching:
            lines.append(f"{CYAN}/{query}{RESET}{DIM}  (type to filter){RESET}")
            lines.append("")
            lines.append(f"{DIM}Enter apply · Esc cancel search{RESET}")
        else:
            lines.append(
                f"{DIM}Up/Down move · Space change · i info · / search · "
                f"Enter save · Esc cancel{RESET}"
            )
        prev_lines = _render_block(lines, prev_lines)
        return len(vis)

    _hide_cursor()
    try:
        with _reader if _reader is not None else _make_reader() as reader:
            while True:
                nvis = render()
                key = reader.read_key()
                if searching:
                    if key == "enter":
                        searching = False
                    elif key == "esc":
                        searching = False
                        query = ""
                    elif key == "backspace":
                        query = query[:-1]
                    elif len(key) == 1 and key.isprintable():
                        query += key
                    idx = 0
                    continue
                if key in ("up", "k"):
                    idx = (idx - 1) % max(nvis, 1)
                    show_help = False
                elif key in ("down", "j"):
                    idx = (idx + 1) % max(nvis, 1)
                    show_help = False
                elif key in ("space", "right"):
                    vis = visible()
                    if vis:
                        rows[vis[idx]].cycle(1)
                elif key == "left":
                    vis = visible()
                    if vis:
                        rows[vis[idx]].cycle(-1)
                elif key == "i":
                    show_help = not show_help
                elif key == "/":
                    searching = True
                    show_help = False
                elif key == "enter":
                    return rows
                elif key in ("esc", "q", ""):
                    return None
    finally:
        _show_cursor()


# --------------------------------------------------------------------------- #
# Single-choice selector (e.g. pick a credential at clone/init time)
# --------------------------------------------------------------------------- #


def select_one(labels: Sequence[str], title: str = "", _reader: object | None = None) -> int | None:
    """Let the user pick one item from ``labels``. Returns the chosen index, or
    None on cancel (Esc).

    Controls: Up/Down move · Enter select · Esc cancel.

    ``_reader`` is a test seam (see :func:`settings_menu`).
    """
    items = list(labels)
    if not items:
        return None
    if _reader is None and not interactive():
        raise RuntimeError("select_one requires an interactive terminal")

    idx = 0
    prev_lines = 0

    def render() -> None:
        nonlocal prev_lines
        lines: list[str] = []
        if title:
            lines.append(f"{BOLD}{title}{RESET}")
            lines.append("")
        for i, label in enumerate(items):
            cursor = f"{CYAN}>{RESET} " if i == idx else "  "
            color = CYAN if i == idx else ""
            end = RESET if i == idx else ""
            lines.append(f"{cursor}{color}{label}{end}")
        lines.append("")
        lines.append(f"{DIM}Up/Down move · Enter select · Esc cancel{RESET}")
        prev_lines = _render_block(lines, prev_lines)

    _hide_cursor()
    try:
        with _reader if _reader is not None else _make_reader() as reader:
            while True:
                render()
                key = reader.read_key()
                if key in ("up", "k"):
                    idx = (idx - 1) % len(items)
                elif key in ("down", "j"):
                    idx = (idx + 1) % len(items)
                elif key == "enter":
                    return idx
                elif key in ("esc", "q", ""):
                    return None
    finally:
        _show_cursor()


# --------------------------------------------------------------------------- #
# Credential selector (site-aware: filter by origin, set site inline)
# --------------------------------------------------------------------------- #


def select_credential(
    creds: Sequence[object],
    target_site: str = "",
    on_set_site: Callable[[object, str], str] | None = None,
    title: str = "",
    _reader: object | None = None,
) -> object | None:
    """Pick one credential, returns the chosen object or None (Esc/cancel).

    ``creds`` is a list of objects each exposing ``.name`` (str), ``.scope``
    (str), and ``.site`` (str). Two view modes:

    - ``'site'`` (default when ``target_site`` is set AND at least one cred
      matches the filter): show only creds where ``site == ""`` (legacy
      wildcard) or ``site.lower() == target_site.lower()``.
    - ``'all'``: show every cred.

    Key ``a`` toggles between the two modes (only meaningful when
    ``target_site`` is set; otherwise always show all and ``a`` is a no-op).

    Each row renders ``> name  (scope)  <site or '(no site)'>`` with the
    highlighted row in CYAN like :func:`select_one`.

    Key ``s`` on a highlighted cred whose ``.site == ""`` opens an inline text
    prompt to paste a URL; the typed string is passed to
    ``on_set_site(cred, raw) -> str`` which returns the stored origin (or ``""``
    on failure). On non-empty return, ``cred.site`` is set in place and the view
    re-filtered.

    Up/Down (and ``k``/``j``) move, Enter selects (returns the highlighted
    cred), Esc/``q`` cancels (returns None). ``_reader`` is the test seam (see
    :func:`select_one`).
    """
    if _reader is None and not interactive():
        raise RuntimeError("select_credential requires an interactive terminal")
    rows = list(creds)
    if not rows:
        return None

    def matches_site(c: object) -> bool:
        site = getattr(c, "site", "") or ""
        return site == "" or site.lower() == target_site.lower()

    # Default to the filtered view only when target_site is set and it actually
    # narrows things (at least one cred matches); otherwise show everything.
    mode = "site" if target_site and any(matches_site(c) for c in rows) else "all"

    idx = 0
    prev_lines = 0

    def visible() -> list[int]:
        if mode == "site" and target_site:
            return [i for i, c in enumerate(rows) if matches_site(c)]
        return list(range(len(rows)))

    def render() -> None:
        nonlocal prev_lines
        vis = visible()
        lines: list[str] = []
        if title:
            lines.append(f"{BOLD}{title}{RESET}")
            lines.append("")
        for vi, i in enumerate(vis):
            c = rows[i]
            cursor = f"{CYAN}>{RESET} " if vi == idx else "  "
            color = CYAN if vi == idx else ""
            end = RESET if vi == idx else ""
            site = getattr(c, "site", "") or ""
            site_disp = site if site else f"{DIM}(no site){RESET}"
            scope = getattr(c, "scope", "")
            lines.append(f"{cursor}{color}{c.name}{end}  ({scope})  {site_disp}")
        if not vis:
            lines.append(f"  {DIM}(no credentials){RESET}")
        lines.append("")
        footer = f"{DIM}Up/Down move · Enter select · Esc cancel"
        if target_site:
            footer += " · a all/site"
        if on_set_site is not None:
            footer += " · s set site"
        footer += RESET
        lines.append(footer)
        lines.append(f"{DIM}Manage saved credentials with: jp credentials{RESET}")
        prev_lines = _render_block(lines, prev_lines)

    def edit_site(reader: object, cred: object) -> None:
        """Inline URL prompt; on commit, push through ``on_set_site``."""
        nonlocal prev_lines
        typed = ""
        while True:
            lines = [
                "",
                f"{CYAN}Paste a Jupyter URL to link this credential to its server:{RESET} {typed}",
                "",
                f"{DIM}Enter save · Esc cancel{RESET}",
            ]
            prev_lines = _render_block(lines, prev_lines)
            key = reader.read_key()
            if key == "enter":
                origin = on_set_site(cred, typed)
                if origin:
                    cred.site = origin
                return
            if key in ("esc", ""):
                return
            if key == "backspace":
                typed = typed[:-1]
            elif len(key) == 1 and key.isprintable():
                typed += key

    _hide_cursor()
    try:
        with _reader if _reader is not None else _make_reader() as reader:
            while True:
                render()
                vis = visible()
                nvis = len(vis)
                key = reader.read_key()
                if key in ("up", "k"):
                    idx = (idx - 1) % max(nvis, 1)
                elif key in ("down", "j"):
                    idx = (idx + 1) % max(nvis, 1)
                elif key == "a" and target_site:
                    mode = "all" if mode == "site" else "site"
                    idx = 0
                elif key == "s" and on_set_site is not None and vis:
                    # Available for ANY highlighted cred: add a site to a legacy
                    # cred, or overwrite an existing one.
                    cred = rows[vis[idx]]
                    edit_site(reader, cred)
                    # Re-filter and clamp the cursor after a possible change.
                    nvis = len(visible())
                    idx = min(idx, nvis - 1) if nvis else 0
                elif key == "enter":
                    if vis:
                        return rows[vis[idx]]
                elif key in ("esc", "q", ""):
                    return None
    finally:
        _show_cursor()


# --------------------------------------------------------------------------- #
# Credential manager (delete / set site / rename saved credentials)
# --------------------------------------------------------------------------- #


def credential_manager(
    creds,
    *,
    on_delete,
    on_set_site,
    on_rename,
    title: str = "",
    _reader: object | None = None,
) -> None:
    """Interactive manager for saved credentials. Returns None.

    ``creds`` is a mutable list of objects exposing ``.name`` (str), ``.scope``
    (str), and ``.site`` (str). Rows render as ``name  (scope)  <site or
    '(no site)'>`` with the highlighted row in CYAN.

    Keys:
      - Up/Down (``k``/``j``) move;
      - ``d`` delete the highlighted cred via an inline ``Delete '<name>'?
        [y/N]`` confirm; on ``y`` call ``on_delete(cred) -> bool`` and, if True,
        remove it from ``creds`` (clamping the cursor);
      - ``s`` set/replace the site via an inline URL prompt pushed through
        ``on_set_site(cred, raw) -> str``; on a non-empty return set
        ``cred.site``;
      - ``r`` rename via an inline name prompt pushed through
        ``on_rename(cred, newname) -> bool``; on True set ``cred.name``;
      - ``q``/Esc quit.

    ``_reader`` is the test seam (see :func:`select_one`).
    """
    if _reader is None and not interactive():
        raise RuntimeError("credential_manager requires an interactive terminal")

    idx = 0
    prev_lines = 0

    def render(extra: str | None = None) -> None:
        nonlocal prev_lines
        lines: list[str] = []
        if title:
            lines.append(f"{BOLD}{title}{RESET}")
            lines.append("")
        if not creds:
            lines.append(f"  {DIM}(no saved credentials){RESET}")
        else:
            for i, c in enumerate(creds):
                cursor = f"{CYAN}>{RESET} " if i == idx else "  "
                color = CYAN if i == idx else ""
                end = RESET if i == idx else ""
                site = getattr(c, "site", "") or ""
                site_disp = site if site else f"{DIM}(no site){RESET}"
                scope = getattr(c, "scope", "")
                lines.append(f"{cursor}{color}{c.name}{end}  ({scope})  {site_disp}")
        lines.append("")
        if extra is not None:
            lines.append(extra)
            lines.append("")
        lines.append(f"{DIM}Up/Down move · d delete · s set site · r rename · q quit{RESET}")
        prev_lines = _render_block(lines, prev_lines)

    def prompt(reader: object, label: str) -> str | None:
        """Inline text prompt. Returns the typed string on Enter, None on Esc."""
        typed = ""
        while True:
            render(f"{CYAN}{label}{RESET} {typed}")
            key = reader.read_key()
            if key == "enter":
                return typed
            if key in ("esc", ""):
                return None
            if key == "backspace":
                typed = typed[:-1]
            elif len(key) == 1 and key.isprintable():
                typed += key

    def confirm_delete(reader: object, cred: object) -> None:
        nonlocal idx
        while True:
            render(f"{CYAN}Delete {cred.name!r}? [y/N]{RESET}")
            key = reader.read_key()
            if key in ("y", "Y"):
                if on_delete(cred):
                    creds.remove(cred)
                    if idx >= len(creds):
                        idx = max(len(creds) - 1, 0)
                return
            if key in ("n", "N", "enter", "esc", ""):
                return

    _hide_cursor()
    try:
        with _reader if _reader is not None else _make_reader() as reader:
            while True:
                render()
                key = reader.read_key()
                if not creds:
                    if key in ("esc", "q", ""):
                        return None
                    continue
                if key in ("up", "k"):
                    idx = (idx - 1) % len(creds)
                elif key in ("down", "j"):
                    idx = (idx + 1) % len(creds)
                elif key == "d":
                    confirm_delete(reader, creds[idx])
                elif key == "s":
                    cred = creds[idx]
                    raw = prompt(reader, "Paste a Jupyter URL for this credential:")
                    if raw:
                        origin = on_set_site(cred, raw)
                        if origin:
                            cred.site = origin
                elif key == "r":
                    cred = creds[idx]
                    newname = prompt(reader, "New name:")
                    if newname and on_rename(cred, newname):
                        cred.name = newname
                elif key in ("esc", "q", ""):
                    return None
    finally:
        _show_cursor()


# --------------------------------------------------------------------------- #
# Keep/Delete confirmation selector (mirror-mode deletions)
# --------------------------------------------------------------------------- #


def confirm_deletions(paths: Iterable[str], where: str, _reader: object | None = None) -> list[str]:
    """Interactively choose which ``paths`` to DELETE on ``where`` ("remote"
    or "local"). Every item defaults to KEEP (the safe choice). Returns the
    list of paths the user marked for deletion (possibly empty).

    Controls: Up/Down move · Space toggle keep/delete · a all-delete ·
    n none · Enter confirm · Esc cancel (keep everything).

    ``_reader`` is a test seam (see :func:`settings_menu`).
    """
    items = list(paths)
    if not items:
        return []
    if _reader is None and not interactive():
        raise RuntimeError("confirm_deletions requires an interactive terminal")

    idx = 0
    mark = dict.fromkeys(items, False)  # path -> delete?
    prev_lines = 0

    def render() -> None:
        nonlocal prev_lines
        lines = [
            f"{BOLD}{RED}Mirror mode:{RESET} {len(items)} file(s) exist on {where} "
            f"but not on the other side.",
            f"{DIM}Choose which to DELETE on {where}. Default is KEEP (nothing is "
            f"deleted unless you mark it).{RESET}",
            "",
        ]
        for i, p in enumerate(items):
            cursor = f"{CYAN}>{RESET} " if i == idx else "  "
            state = f"{RED}[DELETE]{RESET}" if mark[p] else f"{GREEN}[keep]  {RESET}"
            lines.append(f"{cursor}{state} {p}")
        ndel = sum(mark.values())
        lines.append("")
        lines.append(
            f"{DIM}Up/Down move · Space toggle · a delete-all · n keep-all · "
            f"Enter confirm ({ndel} to delete) · Esc cancel{RESET}"
        )
        prev_lines = _render_block(lines, prev_lines)

    _hide_cursor()
    try:
        with _reader if _reader is not None else _make_reader() as reader:
            while True:
                render()
                key = reader.read_key()
                if key in ("up", "k"):
                    idx = (idx - 1) % len(items)
                elif key in ("down", "j"):
                    idx = (idx + 1) % len(items)
                elif key == "space":
                    mark[items[idx]] = not mark[items[idx]]
                elif key == "a":
                    mark = dict.fromkeys(items, True)
                elif key == "n":
                    mark = dict.fromkeys(items, False)
                elif key == "enter":
                    return [p for p in items if mark[p]]
                elif key in ("esc", "q", ""):
                    return []
    finally:
        _show_cursor()

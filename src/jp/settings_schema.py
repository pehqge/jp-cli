"""Declarative registry of the user-editable workspace settings.

Drives both the interactive ``jp config`` screen and the scriptable
``jp config get/set`` path, so the two never drift. Connection fields
(``base_url``, ``prefix``, ``token_path``) are intentionally NOT here: they are
set once at clone/init time and editing them by accident would point a workspace
at the wrong place. They remain reachable via ``jp config set`` for power users.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    options: tuple[Any, ...]
    help_text: str
    coerce: Callable[[str], Any]

    def fmt(self, value: object) -> str:
        return str(value).lower() if isinstance(value, bool) else str(value)


def _coerce_bool(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "on")


# The editable settings, in display order.
SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="mirror",
        label="Mirror mode (allow deletes)",
        options=(False, True),
        help_text=(
            "When ON, push/pull may DELETE files that exist on one side but not "
            "the other (push removes remote-only files; pull removes local-only "
            "files). Deletion is NEVER silent: jp always lists the files and asks "
            "you, one by one, to keep or delete. Default is keep. Leave OFF for the "
            "safest, additive-only behavior on a shared machine."
        ),
        coerce=_coerce_bool,
    ),
    SettingSpec(
        key="dotfiles",
        label="Dotfile policy",
        options=("skip",),
        help_text=(
            "How to handle hidden files (names starting with a dot). The UFSC "
            "server rejects hidden uploads (allow_hidden=False), so 'skip' is the "
            "only supported policy: dotfiles are reported and never uploaded."
        ),
        coerce=lambda s: str(s).strip() or "skip",
    ),
    SettingSpec(
        key="color",
        label="Colored output",
        options=("auto", "always", "never"),
        help_text=(
            "When to use ANSI colors. 'auto' colors only when writing to a "
            "terminal; 'always' forces color (e.g. through a pager); 'never' "
            "disables it. NO_COLOR in the environment always wins."
        ),
        coerce=lambda s: str(s).strip().lower(),
    ),
    SettingSpec(
        key="timeout",
        label="Network timeout (s)",
        options=(15.0, 30.0, 60.0, 120.0, 300.0),
        help_text=(
            "How long to wait on each API call before giving up. The shared box "
            "can be slow under load and large uploads have no chunking, so raise "
            "this if you see timeouts on big files."
        ),
        coerce=lambda s: float(s),
    ),
)

BY_KEY = {s.key: s for s in SPECS}

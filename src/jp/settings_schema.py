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
    # The Python attribute name on Config. Defaults to ``key`` (correct for the
    # plain settings whose display key IS their attribute name); the versioning
    # specs override it because their display key is DOTTED (e.g.
    # ``versioning.push_prompt``) while the attribute is ``versioning_push_prompt``.
    # This explicit mapping is what lets Config stay a plain dataclass (no
    # ``__getattr__`` magic that would blind mypy to typo'd attribute access).
    attr: str = ""

    @property
    def attr_name(self) -> str:
        """The Config attribute this spec edits (``attr`` if set, else ``key``)."""
        return self.attr or self.key

    def fmt(self, value: object) -> str:
        return str(value).lower() if isinstance(value, bool) else str(value)


def _coerce_bool(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "on")


def _coerce_enum(s: str) -> str:
    """Normalize a free-typed enum value (lower/strip) for option matching.

    Validation against the allowed set is done by config_cmd against ``options``;
    this only canonicalizes so ``Always`` matches ``always``.
    """
    return str(s).strip().lower()


def _coerce_positive_int(s: str) -> int:
    """Coerce to a strictly-positive int, raising on a non-positive/non-int.

    config_cmd surfaces the ValueError as a clear ``invalid value`` UsageError, so
    ``jp config set versioning.max_blob_mb 0`` is rejected instead of stored.
    """
    value = int(str(s).strip())
    if value <= 0:
        raise ValueError("must be a positive integer")
    return value


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
        options=("skip", "protect"),
        help_text=(
            "How to handle hidden files (names starting with a dot). The Jupyter "
            "server runs with allow_hidden=False and rejects hidden uploads, so a "
            "dotfile cannot be stored under its real name. 'skip' (default, safest) "
            "reports dotfiles and never uploads them. 'protect' uploads them under "
            "a reversible alias (e.g. '.gitignore' -> '__jpdot__1_gitignore') and "
            "restores the real name on pull, so hidden files round-trip."
        ),
        coerce=lambda s: str(s).strip().lower() or "skip",
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
    # --- versioning (opt-in) ---------------------------------------------- #
    SettingSpec(
        key="versioning.push_prompt",
        attr="versioning_push_prompt",
        label="Versioning: prompt to push commits",
        options=("ask", "never", "always"),
        help_text=(
            "After you commit, whether 'jp push' offers to push the commit history "
            "too. 'ask' (default) prompts; 'always' pushes history without asking; "
            "'never' pushes only working files and leaves history local."
        ),
        coerce=_coerce_enum,
    ),
    SettingSpec(
        key="versioning.mirror_history",
        attr="versioning_mirror_history",
        label="Versioning: mirror history to remote",
        options=("ask", "always", "never"),
        help_text=(
            "Whether the commit history (objects + refs) is mirrored to the remote "
            "so a teammate can fetch it. 'ask' (default) prompts; 'always' mirrors; "
            "'never' keeps history purely local."
        ),
        coerce=_coerce_enum,
    ),
    SettingSpec(
        key="versioning.notebook_outputs",
        attr="versioning_notebook_outputs",
        label="Versioning: notebook outputs",
        options=("hybrid", "full"),
        help_text=(
            "How notebooks are versioned. 'hybrid' (default) stores the original "
            ".ipynb but detects changes on CODE only, so a pure re-run (same code, "
            "new outputs) is NOT a new version. 'full' versions every byte change, "
            "outputs included, so each re-run is a new version. ('strip' is not "
            "supported -- it would discard outputs irreversibly.)"
        ),
        coerce=_coerce_enum,
    ),
    SettingSpec(
        key="versioning.max_blob_mb",
        attr="versioning_max_blob_mb",
        label="Versioning: max blob size (MiB)",
        options=(50, 100, 250, 500, 1000),
        help_text=(
            "Files larger than this (in MiB) are refused by the remote history "
            "mirror, so a huge artifact does not bloat the shared object store. "
            "The local working-file sync is unaffected."
        ),
        coerce=_coerce_positive_int,
    ),
    SettingSpec(
        key="versioning.author",
        attr="versioning_author",
        label="Versioning: commit author",
        # Freeform: no fixed options. config_cmd skips the option-membership check
        # when options is empty, so any "Name <email>" string is accepted.
        options=(),
        help_text=(
            "The identity recorded on each commit, e.g. 'Ada Lovelace "
            "<ada@example.com>'. Leave empty to fall back to your $USER name."
        ),
        coerce=str,
    ),
)

BY_KEY = {s.key: s for s in SPECS}

"""Per-repo configuration (``.jp/config.json``) and credential loading.

Security rules (see docs/architecture.md):
  * The config stores only the *path* to the token file, never the token value.
  * The token is read at call time from that file, registered with
    ``ui.redact`` immediately, and never written back anywhere.
  * We refuse ``http://`` URLs whenever a token would be sent (no token over
    cleartext). TLS verification is always on (handled in api.py).
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ui
from .errors import AuthError, ConfigError
from .paths import DOT_DIR, validate_prefix

CONFIG_NAME = "config.json"

# Maps a DOTTED versioning display/JSON key -> the Python attribute on Config.
# Single source of truth for serialization, from_json, and the dotted-attr
# routing on Config so the three can never drift.
_VERSIONING_KEY_MAP: dict[str, str] = {
    "versioning.push_prompt": "versioning_push_prompt",
    "versioning.mirror_history": "versioning_mirror_history",
    "versioning.notebook_outputs": "versioning_notebook_outputs",
    "versioning.max_blob_mb": "versioning_max_blob_mb",
    "versioning.author": "versioning_author",
}

# Allowed enum values for the enum-typed versioning settings. An unknown value is
# sanitized back to the default on load (mirroring how ``color``/``dotfiles`` are
# handled) so a hand-edited / tampered config can never select an unsupported mode
# (notably the deliberately-unsupported, lossy notebook "strip").
_VERSIONING_ENUMS: dict[str, tuple[str, ...]] = {
    "versioning_push_prompt": ("ask", "never", "always"),
    "versioning_mirror_history": ("ask", "always", "never"),
    "versioning_notebook_outputs": ("hybrid", "full"),
}

# Defaults for every versioning attribute (only-if-non-default serialization).
_VERSIONING_DEFAULTS: dict[str, object] = {
    "versioning_push_prompt": "ask",
    "versioning_mirror_history": "ask",
    "versioning_notebook_outputs": "hybrid",
    "versioning_max_blob_mb": 100,
    "versioning_author": "",
}


def _sanitize_enum(raw: object, attr: str) -> str:
    """Coerce ``raw`` to one of ``attr``'s allowed enum values, else its default.

    Mirrors the color/dotfiles handling in :meth:`Config.from_json`: a missing,
    unknown, or unsupported value (e.g. the deliberately-unsupported notebook
    "strip") falls back to the attribute's default rather than raising, so a
    hand-edited config can never select an invalid mode.
    """
    allowed = _VERSIONING_ENUMS[attr]
    default = str(_VERSIONING_DEFAULTS[attr])
    value = str(raw).strip().lower() if raw is not None else ""
    return value if value in allowed else default


def _sanitize_positive_int(raw: object, default: int) -> int:
    """Coerce ``raw`` to a POSITIVE int, else ``default`` (bad/<=0 -> default).

    Rejects non-ints and non-positive values (and a float like ``1.5`` that does
    not represent a whole number) so ``versioning.max_blob_mb`` is always a sane,
    positive threshold even after a tampered config. Accepts only the JSON scalar
    types a config can legitimately carry (int / str / a whole-number float).
    """
    # A real float that is not a whole number (e.g. 1.5) is rejected outright;
    # int() would silently truncate it, hiding a malformed config value.
    if isinstance(raw, float) and not raw.is_integer():
        return default
    if not isinstance(raw, (int, float, str)):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass
class Config:
    """Resolved per-repo configuration."""

    base_url: str
    prefix: str
    token_path: str = ""
    # Name of the saved credential (see credentials.py) this workspace uses.
    # Resolved to a token file at call time; the token value is never stored here.
    credential: str = ""
    # Default dotfile policy: skip (server rejects hidden uploads).
    dotfiles: str = "skip"
    # Network timeout (seconds) for API calls; generous because the shared box
    # can be slow and there is no chunking for large uploads (see docs/architecture.md).
    timeout: float = 30.0
    # Mirror mode: when True, push/pull may DELETE files that exist on one side
    # and not the other -- but ALWAYS interactively, file by file, defaulting to
    # keep. Off by default; deletion is never silent. See docs/architecture.md.
    mirror: bool = False
    # Colored output: auto (tty only) | always | never. Defaults to "always"
    # so jp is colorful out of the box; NO_COLOR / --no-color still override it.
    color: str = "always"
    # --- versioning (OPT-IN) ----------------------------------------------- #
    # These five fields drive the git-like versioning feature. They are written
    # to config.json ONLY when changed from their default (see ``to_json``), so a
    # non-adopter's config never grows a single versioning key -- the opt-in
    # invariant. The Python attribute names use underscores; the on-disk / display
    # keys are the dotted ``versioning.*`` names (see ``_VERSIONING_KEY_MAP``).
    # ``jp config`` resolves the dotted display key to the underscore attribute via
    # the SettingSpec's ``attr`` -- Config stays a PLAIN dataclass (no attribute
    # magic), so mypy still catches a typo'd attribute access on this core class.
    #
    # When (on push, after committing) to prompt to push the commit history.
    versioning_push_prompt: str = "ask"  # ask | never | always
    # Whether to also mirror the versioning history to the remote on push.
    versioning_mirror_history: str = "ask"  # ask | always | never
    # Notebook storage policy. "hybrid" stores the original bytes but change-detects
    # outputs-free (a pure re-run is suppressed); "full" versions every byte change
    # (outputs included). "strip" is intentionally unsupported in v1 (lossy).
    versioning_notebook_outputs: str = "hybrid"  # hybrid | full
    # Refuse-to-version blob size threshold in MiB (used by the remote mirror).
    versioning_max_blob_mb: int = 100
    # Freeform commit identity ("Name <email>"); "" falls back to $USER.
    versioning_author: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    def __post_init__(self) -> None:
        self._config_dir: Path = Path(".")

    # --- serialization -----------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "base_url": self.base_url,
            "prefix": self.prefix,
            "dotfiles": self.dotfiles,
            "timeout": self.timeout,
            "mirror": self.mirror,
            "color": self.color,
        }
        if self.token_path:
            data["token_path"] = self.token_path
        if self.credential:
            data["credential"] = self.credential
        # Versioning keys are written ONLY when they differ from their default, so
        # a non-adopter's config.json never grows a single versioning key. The
        # DOTTED display key is used on disk (e.g. "versioning.push_prompt").
        for dotted, attr in _VERSIONING_KEY_MAP.items():
            value = getattr(self, attr)
            if value != _VERSIONING_DEFAULTS[attr]:
                data[dotted] = value
        data.update(self.extra)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Config:
        known = {
            "base_url",
            "prefix",
            "token_path",
            "credential",
            "dotfiles",
            "timeout",
            "mirror",
            "color",
        }
        # The dotted versioning keys are KNOWN too, so they never leak into the
        # opaque ``extra`` dict (which would otherwise round-trip them verbatim and
        # double-write them). The split treats "versioning.*" as recognized.
        known |= set(_VERSIONING_KEY_MAP)
        extra = {k: v for k, v in data.items() if k not in known}
        base_url = str(data.get("base_url", "")).strip()
        prefix = str(data.get("prefix", "")).strip()
        if not base_url:
            raise ConfigError("config is missing 'base_url'")
        if not prefix:
            raise ConfigError("config is missing 'prefix'")
        # Re-validate the prefix on load: a tampered config must not bypass the
        # shared-prefix refusal.
        prefix = validate_prefix(prefix)
        try:
            timeout = float(data.get("timeout", 30.0))
        except (TypeError, ValueError):
            timeout = 30.0
        color = str(data.get("color", "always"))
        if color not in ("auto", "always", "never"):
            color = "always"
        dotfiles = str(data.get("dotfiles", "skip")).strip().lower() or "skip"
        if dotfiles not in ("skip", "protect"):
            dotfiles = "skip"
        # Versioning enums: read the dotted key, sanitize an unknown value back to
        # the default exactly like color/dotfiles above (so a tampered config or a
        # "strip" notebook mode silently degrades to the safe default, never errors).
        v_push = _sanitize_enum(data.get("versioning.push_prompt"), "versioning_push_prompt")
        v_mirror = _sanitize_enum(
            data.get("versioning.mirror_history"), "versioning_mirror_history"
        )
        v_nb = _sanitize_enum(
            data.get("versioning.notebook_outputs"), "versioning_notebook_outputs"
        )
        v_blob = _sanitize_positive_int(data.get("versioning.max_blob_mb"), 100)
        v_author = str(data.get("versioning.author", "") or "")
        cfg = cls(
            base_url=base_url.rstrip("/"),
            prefix=prefix,
            token_path=str(data.get("token_path", "")),
            credential=str(data.get("credential", "")),
            dotfiles=dotfiles,
            timeout=timeout,
            mirror=bool(data.get("mirror", False)),
            color=color,
            versioning_push_prompt=v_push,
            versioning_mirror_history=v_mirror,
            versioning_notebook_outputs=v_nb,
            versioning_max_blob_mb=v_blob,
            versioning_author=v_author,
            extra=extra,
        )
        return cfg


def config_path(root: Path) -> Path:
    return Path(root) / DOT_DIR / CONFIG_NAME


def load(root: Path) -> Config:
    """Load and validate ``.jp/config.json`` from ``root``."""
    path = config_path(root)
    if not path.is_file():
        raise ConfigError(f"no jp config found at {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"could not read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"malformed config (expected an object): {path}")
    cfg = Config.from_json(raw)
    cfg._config_dir = Path(root) / DOT_DIR
    return cfg


def save(root: Path, cfg: Config) -> None:
    """Write the config atomically with private (0600) permissions."""
    dot = Path(root) / DOT_DIR
    dot.mkdir(parents=True, exist_ok=True)
    ensure_dot_gitignore(root)
    path = config_path(root)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg.to_json(), indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(str(tmp), str(path))


# Content of the guard .gitignore dropped inside every ``.jp/``. ``*`` makes git
# ignore the entire metadata dir (local config, index, AND any local token
# files), so a workspace that also happens to be a git repo can never commit a
# credential by accident. ``.jp/`` is per-clone local state, like ``.git/``.
_DOT_GITIGNORE = "# jp workspace metadata -- never commit (local state + token files)\n*\n"


def ensure_dot_gitignore(root: Path) -> None:
    """Make sure ``<root>/.jp/.gitignore`` exists so git ignores all of ``.jp/``.

    Defense in depth against accidentally committing a local credential when the
    workspace is also a git repository. Idempotent and best-effort: never raises.
    """
    dot = Path(root) / DOT_DIR
    gi = dot / ".gitignore"
    try:
        dot.mkdir(parents=True, exist_ok=True)
        if not gi.exists():
            gi.write_text(_DOT_GITIGNORE, encoding="utf-8")
    except OSError:
        # Worst case git protection is missing; never block the real operation.
        ui.warn(f"could not write {gi}; add '.jp/' to your .gitignore manually")


# --------------------------------------------------------------------------- #
# Token loading
# --------------------------------------------------------------------------- #
def _default_token_candidates(cfg: Config | None) -> list[Path]:
    candidates: list[Path] = []
    # 1) Env var override (path): JP_TOKEN_FILE points at a token file. (The
    #    JP_TOKEN *value* is handled separately and wins in load_token.)
    env_file = os.environ.get("JP_TOKEN_FILE")
    if env_file:
        candidates.append(Path(os.path.expanduser(env_file)))
    # 2) The named credential recorded in this workspace's config (local first,
    #    then global -- see credentials.resolve).
    if cfg and cfg.credential:
        from . import credentials

        root: Path | None = None
        config_dir = cfg.config_dir
        if str(config_dir) not in (".", ""):
            root = config_dir.parent
        cred = credentials.resolve(cfg.credential, root)
        if cred is not None:
            candidates.append(Path(os.path.expanduser(cred.token_path)))
    # 3) Back-compat: a direct token path stored in the config.
    if cfg and cfg.token_path:
        candidates.append(Path(os.path.expanduser(cfg.token_path)))
    # 4) Back-compat: the legacy global token file.
    candidates.append(Path(os.path.expanduser("~/.config/jp/token")))
    return candidates


def load_token(cfg: Config | None) -> str:
    """Load the token *value* from the configured path (or JP_TOKEN env).

    The value is registered with ``ui.redact`` immediately so it can never be
    printed. We never return the token in any structured output.
    """
    # Direct value via env (useful for CI). Still redacted.
    env_val = os.environ.get("JP_TOKEN")
    if env_val:
        token = env_val.strip()
        if not token:
            raise AuthError("JP_TOKEN is set but empty")
        ui.register_secret(token)
        return token

    for cand in _default_token_candidates(cfg):
        if cand.is_file():
            _warn_if_world_readable(cand)
            try:
                token = cand.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise AuthError(f"could not read token file {cand}: {exc}") from exc
            if not token:
                raise AuthError(f"token file is empty: {cand}")
            ui.register_secret(token)
            return token

    raise AuthError("no token found. Run 'jp login' or set JP_TOKEN / JP_TOKEN_FILE.")


def _warn_if_world_readable(path: Path) -> None:
    """On POSIX, warn if the token file is group/other-readable (shared box)."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        ui.warn(f"token file {path} is accessible to other users; run: chmod 600 {path}")

"""Per-repo configuration (``.jp/config.json``) and credential loading.

Security rules (DESIGN §5):
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


@dataclass
class Config:
    """Resolved per-repo configuration."""

    base_url: str
    prefix: str
    token_path: str = ""
    # Default dotfile policy: skip (server rejects hidden uploads).
    dotfiles: str = "skip"
    # Network timeout (seconds) for API calls; generous because the shared box
    # can be slow and there is no chunking for large uploads (DESIGN §1, §10).
    timeout: float = 30.0
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
        }
        if self.token_path:
            data["token_path"] = self.token_path
        data.update(self.extra)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Config:
        known = {"base_url", "prefix", "token_path", "dotfiles", "timeout"}
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
        cfg = cls(
            base_url=base_url.rstrip("/"),
            prefix=prefix,
            token_path=str(data.get("token_path", "")),
            dotfiles=str(data.get("dotfiles", "skip")) or "skip",
            timeout=timeout,
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
    path = config_path(root)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg.to_json(), indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(str(tmp), str(path))


# --------------------------------------------------------------------------- #
# Token loading
# --------------------------------------------------------------------------- #
def _default_token_candidates(cfg: Config | None) -> list[Path]:
    candidates: list[Path] = []
    if cfg and cfg.token_path:
        candidates.append(Path(os.path.expanduser(cfg.token_path)))
    # Env var override (path OR value): JP_TOKEN is the value, JP_TOKEN_FILE the path.
    env_file = os.environ.get("JP_TOKEN_FILE")
    if env_file:
        candidates.append(Path(os.path.expanduser(env_file)))
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

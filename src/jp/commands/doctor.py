"""``jp doctor`` -- diagnose the repo/config/credentials and connectivity.

Read-only. Reports problems with actionable hints. Never prints the token value
(token presence is shown only as "present"/"missing"), and all output is passed
through the redaction filter by the ui helpers.
"""

from __future__ import annotations

import argparse
import ssl

from .. import config as config_mod
from .. import ui
from ..errors import EXIT_OK, EXIT_PARTIAL, AuthError, JpError, ServerDownError
from ..paths import find_root, validate_prefix


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("doctor", help="diagnose config, credentials and connectivity")
    p.add_argument("--no-network", action="store_true", help="skip the connectivity check")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    problems = 0

    root = find_root()
    if root is None:
        ui.error("not inside a jp repo (no .jp dir). Run 'jp init' or 'jp clone'.")
        return EXIT_PARTIAL
    ui.success(f"repo root: {root}")

    try:
        cfg = config_mod.load(root)
    except JpError as exc:
        ui.error(f"config: {exc.message}")
        return EXIT_PARTIAL

    # base_url scheme.
    if cfg.base_url.startswith("https://"):
        ui.success(f"base_url uses TLS: {cfg.base_url}")
    elif cfg.base_url.startswith("http://"):
        ui.warn(f"base_url is plain http (token will be refused over http): {cfg.base_url}")
        problems += 1
    else:
        ui.error(f"base_url has no http(s) scheme: {cfg.base_url}")
        problems += 1

    # prefix safety.
    try:
        validate_prefix(cfg.prefix)
        ui.success(f"prefix is safe: {cfg.prefix}")
    except JpError as exc:
        ui.error(f"prefix: {exc.message}")
        problems += 1

    # token presence (value never shown).
    try:
        config_mod.load_token(cfg)
        ui.success("token: present and readable")
    except JpError as exc:
        ui.warn(f"token: {exc.message}")
        problems += 1

    # TLS / connectivity + JupyterHub server-running health probe.
    if not args.no_network:
        try:
            from ._context import build_api

            api = build_api(cfg)
            # Health probe FIRST: GET /api/status WITHOUT following redirects.
            # This distinguishes "server up" (200) from "token bad" (403 ->
            # AuthError) from "server stopped" (3xx -> /hub -> ServerDownError)
            # before we attempt any Contents API read (research §8).
            result = api.status_probe()
            if result.up:
                ui.success("server: up (GET /api/status -> 200)")
            else:
                ui.warn(f"server: unexpected status probe result ({result.detail})")
                problems += 1
            # Now a harmless authenticated read; verifies prefix existence.
            api.list_dir(validate_prefix(cfg.prefix))
            ui.success("connectivity: reached the Contents API")
        except ssl.SSLError as exc:
            ui.error(f"TLS verification failed: {exc}")
            problems += 1
        except ServerDownError as exc:
            # Actionable: the single-user server is not running.
            ui.error(f"server: {exc.message}")
            problems += 1
        except AuthError as exc:
            ui.error(f"token: {exc.message}")
            problems += 1
        except JpError as exc:
            ui.warn(f"connectivity: {exc.message}")
            problems += 1

    if problems:
        ui.info(f"doctor found {problems} issue(s)")
        return EXIT_PARTIAL
    ui.success("all checks passed")
    return EXIT_OK

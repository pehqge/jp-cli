# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0](https://github.com/pehqge/jp-cli/compare/v0.2.0...v0.3.0) (2026-05-31)


### Features

* **kernel:** add --link, fix docs, print privacy verdict ([13d42ca](https://github.com/pehqge/jp-cli/commit/13d42cab82df2463d444c5387da59609e8a84e12))
* **kernel:** add `jp kernel` to fix VS Code remote-kernel cwd ([7ba887c](https://github.com/pehqge/jp-cli/commit/7ba887c782d7cdf341e262fbd398609d8e7d0864))


### Bug Fixes

* **credentials:** skip world-readable warning on Windows ([44fa06a](https://github.com/pehqge/jp-cli/commit/44fa06a4af1b9f3983bfaad12e1a9380257f36c9))
* **readme:** use a static release badge auto-bumped by release-please ([1176d07](https://github.com/pehqge/jp-cli/commit/1176d07d4885562db9dd2f4fb9ccfab9128a97ee))
* **tests:** isolate Windows home dir and simplify release badge ([9eeb6ac](https://github.com/pehqge/jp-cli/commit/9eeb6acbd7931ee30384b6c74dd9c3214805e425))
* **tests:** keep USERPROFILE comment within ruff line-length ([86f3d68](https://github.com/pehqge/jp-cli/commit/86f3d68fea3614e930a1d06278ba11017f8d02bb))


### Documentation

* fix jp login flow to match actual behavior ([c378c4e](https://github.com/pehqge/jp-cli/commit/c378c4e7538632155a17fd1d70a28901fe7fac40))
* fix two more stale README details ([1fefd67](https://github.com/pehqge/jp-cli/commit/1fefd6719a35457f374ffc33d7ec3ad244910781))
* **readme:** add FAQ entry for the VS Code remote-kernel workflow ([d4e0436](https://github.com/pehqge/jp-cli/commit/d4e043644df21357d39156964954a164c68d3cfa))

## [0.2.0](https://github.com/pehqge/jp-cli/compare/v0.1.0...v0.2.0) (2026-05-31)


### Features

* **config:** activate color setting and add dotfile "protect" policy ([38c9ac7](https://github.com/pehqge/jp-cli/commit/38c9ac7ebe963156e0eb92015ec98c147d617a00))
* named credentials with interactive jp login ([ec290db](https://github.com/pehqge/jp-cli/commit/ec290db9233c427a7150ecebb3cfad836d4b0e68))


### Bug Fixes

* add top-level release-type to release-please config ([695d456](https://github.com/pehqge/jp-cli/commit/695d456d42511a371a33bad1d3e327af61c45b88))
* tag releases as vX.Y.Z without component prefix ([c2e9794](https://github.com/pehqge/jp-cli/commit/c2e9794065feb93f13afa24a947f5db26bae820c))
* **tui:** repair interactive settings render and add navigation hints ([0e6b673](https://github.com/pehqge/jp-cli/commit/0e6b673ab1bab1ad4e396679b1874b1284b93cf7))


### Documentation

* replace real JupyterHub URL with generic example placeholder ([54612b7](https://github.com/pehqge/jp-cli/commit/54612b7e269d8e8f2c1e4757c6427aeaec625314))

## [Unreleased]

### Added

- Named credentials: `jp login` is now interactive — it explains how to get a
  JupyterHub API token, reads it with hidden input, asks for a name, and saves it
  **globally** (`~/.config/jp/`) or **locally** (a workspace's `.jp/`). Token
  values are written to private `600` files and never leave the machine; only the
  credential name is recorded in config. Scriptable via `--name`,
  `--global`/`--local`, `--token-stdin`, `--token-path`, `--force`.
- `jp clone` / `jp init` select a saved credential automatically when only one
  exists, or prompt to choose when several do (`--credential <name>` to skip the
  prompt); the choice is recorded in the workspace config.
- New `credential` config key and a `credentials.json` registry per scope.
- Every `.jp/` workspace now gets a `.jp/.gitignore` (`*`) so a workspace that is
  also a git repo can never commit its local state or a local token file.

### Changed

- Token resolution order is now `$JP_TOKEN` → `$JP_TOKEN_FILE` → the workspace's
  saved credential → legacy `token_path` / `~/.config/jp/token` (the legacy paths
  remain supported).

## [0.1.0] - 2026-05-30

### Added

- Initial release of `jp` (PyPI package `jp-cli`, command `jp`).
- Git-like commands: `clone`, `pull`, `push`, `status`, `diff`, `init`,
  `config`, `auth`, `version`.
- Three-way sync model (local / remote / base) with conflict detection.
- Conflict-safe synchronization: conflicting files are never overwritten
  silently (exit code `6`).
- Token-based authentication resolved as `--token-file` > `$JP_TOKEN_FILE` /
  `$JUPYTER_TOKEN` > repo config > global config > `~/.jupyter_token`;
  only the token path is stored in config, and the token file is expected to be
  mode `0600`.
- Path-jail confinement: remote paths are resolved inside the clone root, with
  `..` traversal, absolute paths, and boundary-crossing symlinks rejected.
- `.jpignore` support plus built-in defaults (`.jp/`, dotfiles, `.git/`,
  `__pycache__/`, `*.pyc`); hidden files opt-in via `allow_hidden`.
- Pure standard-library HTTP client (`urllib`) for the Jupyter Contents API,
  with retry/backoff on network errors.
- Distribution via PyPI, a single-file `jp.pyz` zipapp, standalone per-OS
  PyInstaller binaries, and `install.sh` / `install.ps1` installers.

[Unreleased]: https://github.com/pehqge/jp-cli/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pehqge/jp-cli/releases/tag/v0.1.0

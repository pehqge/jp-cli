# Contributing to jp

Thanks for your interest in improving `jp`! This guide covers how to set up a
development environment, run the checks, and submit changes.

By participating, you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rules

- `jp` has **zero runtime dependencies** — it uses only the Python standard
  library. Please do not add third-party runtime dependencies; if you think one
  is unavoidable, open an issue to discuss it first.
- Keep the **safety guarantees** intact: never delete remote data by default,
  never silently overwrite on conflict, stay inside the path-jail, never log or
  commit tokens. See [SECURITY.md](SECURITY.md).
- Target **Python 3.8+** and keep the tool cross-platform (macOS, Windows,
  Linux).

## Development setup

`jp` uses a `src/` layout. Create a virtual environment and install in editable
mode with the dev extras:

```bash
git clone https://github.com/pehqge/jp-cli
cd jp

python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
# .venv\Scripts\Activate.ps1

pip install -e ".[dev]"
```

This installs `pytest`, `ruff`, and `mypy`.

## Running the checks

Run the same checks CI runs, against the whole repo:

```bash
ruff check .            # lint
ruff format --check .   # formatting
pytest                  # tests
mypy                    # type check (advisory / non-blocking in CI)
```

Auto-fix what's fixable:

```bash
ruff check --fix .
ruff format .
```

### Pre-commit hooks (optional but recommended)

```bash
pip install pre-commit
pre-commit install
```

Now `ruff` and `ruff-format` run automatically on every commit. Run on the
whole repo at once with:

```bash
pre-commit run --all-files
```

## Tests

- Tests live in `tests/`, one file per module (`test_config.py`, `test_api.py`,
  …), matching `src/jp/`.
- The HTTP layer is **mocked** — tests never hit a live server.
- New behavior should come with tests. Bug fixes should come with a regression
  test that fails before the fix.

## Pull requests

1. Fork the repo and create a topic branch off `main`.
2. Make your change, with tests and (if user-facing) a `CHANGELOG.md` entry
   under `[Unreleased]`.
3. Make sure `ruff check`, `ruff format --check`, and `pytest` all pass.
4. Open a PR using the template. Describe **what** changed and **why**, and link
   any related issue.

Keep PRs focused — one logical change per PR is easier to review and merge.

## Reporting bugs and requesting features

Use the GitHub issue templates:

- **Bug report** — what you did, what happened, what you expected, your OS and
  Python version.
- **Feature request** — the problem you're trying to solve, not just a proposed
  solution.

Please **never** paste an API token, server URL with embedded credentials, or
other secrets into an issue.

## Releasing (maintainers)

1. Bump `__version__` in `src/jp/__init__.py`.
2. Move the `[Unreleased]` changes in `CHANGELOG.md` under a new version
   heading with the date.
3. Tag `vX.Y.Z` and push the tag. The release workflow builds the wheel, sdist,
   `jp.pyz`, and per-OS binaries, publishes to PyPI via Trusted Publishing, and
   creates a GitHub Release with the artifacts and `SHA256SUMS`.

Thanks again for contributing!

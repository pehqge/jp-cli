# Design — Update notifier, opt-in auto-update, `jp changelog`, AI release notes

Date: 2026-06-05
Branch: `feat/update-notifier`
Status: approved design → ready for implementation plan

## Problem

`jp` ships as a PyPI package (`jpsync`) installed via pipx/uv/pip. Users have no
passive signal that a newer version exists — they only find out if they happen
to run `jp update --check`. We want a non-intrusive, npm/gh-style update notice
to surface automatically during normal use, plus three related capabilities the
notice unlocks: opt-in auto-update, a `jp changelog` surface, and AI-authored,
user-facing release highlights so people understand *what changed and how to use
it*.

## Goals

1. **Passive notifier** — a colored notice during normal use when a newer release
   exists, with **zero added latency** on the command's hot path.
2. **Opt-in auto-update** (default OFF) — when enabled, update in the background
   between commands and announce it on the next session.
3. **`jp changelog`** — three surfaces to read release notes.
4. **AI release highlights** — a CI step that writes a professional, no-emoji
   "what changed / how to use it" section into each minor/major GitHub Release,
   so `jp changelog` is engaging without anyone hand-writing it.

## Non-goals

- No per-repo configuration of notifier/auto-update — these are user-global
  machine preferences, not workspace settings.
- No mutation of the committed `CHANGELOG.md` by the AI step (avoids clobber/loop
  with release-please). The deterministic conventional-commit changelog stands.
- No new runtime dependency for the shipped `jpsync` package — it stays
  stdlib-only. The AI step's provider client lives only in CI.

## Standard / prior art (research)

- **Auto-update default OFF** is the dev-CLI norm: `pip` only notifies and never
  self-updates; `npm` uses `update-notifier` (notify, don't update); `gh`,
  `deno`, `rustup` require an explicit `upgrade` command. CLIs that auto-update by
  default (Salesforce/Heroku oclif) are criticized for breaking CI/scripts and
  always ship an env opt-out. Consensus: opt-in, reversible, never surprise.
- **Release notes after update** is standard (Deno prints them); release notes
  follow keepachangelog conventions, which release-please already generates.
- **Never replace the running process mid-command** — apply updates between
  commands / on the next session.

## Existing building blocks (reused, not rebuilt)

- `src/jp/commands/update.py`
  - `_latest_release_tag()` — GitHub releases API, returns latest tag or None.
  - `_norm()` — version tuple compare.
  - `_editable_source()` — detects editable/dev installs (update no-ops there).
  - `_running_as_binary()` — PyInstaller/frozen detection.
  - `run()` — the working self-updater (pipx/uv/pip).
- `src/jp/credentials.py` → `global_dir()` = `~/.config/jp` (per-user state dir).
- `src/jp/ui.py` — color (`_Style`, `_color_enabled`), `redact`, quiet mode.
- `src/jp/cli.py` → `main()` — central dispatch and error boundary.
- `src/jp/paths.py` → `atomic_write()` — temp-then-rename, symlink-safe.
- release-please (`release-please-config.json`, `.github/workflows/release*.yml`).

## Architecture overview

Five components, foundation first. Components 1–2 are the core notifier; 3 adds
auto-update; 4 adds changelog surfaces; 5 is the CI enrichment.

### Shared cache — `~/.config/jp/update-check.json`

```json
{
  "last_check": 1733400000.0,
  "latest": "v1.2.0",
  "checked_version": "1.1.1",
  "pending_announcement": {"from": "1.1.1", "to": "1.2.0"}
}
```

- `last_check` (epoch seconds) gates the **refresh** (TTL = 24h).
- `latest` (last-known release tag) drives the **notice** vs installed
  `__version__` via `update._norm()`.
- `pending_announcement` set by the auto-update worker; consumed + cleared by the
  next `maybe_notify`.
- Atomic write (reuse `paths.atomic_write`), mode 0600. Missing or corrupt file →
  treated as "stale, no data" → never raises.

### Component 1 — version cache + background worker

**New `src/jp/update_notify.py`** (the only module hooked into the hot path):

- `maybe_notify(args) -> None` — called at the end of `main()`, after the command
  runs, wrapped so it can never change the exit code or raise. Reads the cache
  (one small JSON read, no network). Behavior:
  1. Cheap gates first (below). If any trip → return silently.
  2. If `pending_announcement` present → print it, clear it, return.
  3. If cache stale (`now - last_check > TTL`) → spawn the detached worker
     (fire-and-forget), then continue to step 4 with whatever is cached.
  4. If cached `latest` is newer than installed → print the notice.
- Cache read/write helpers (`_load_cache`, `_save_cache`) — corruption-safe.
- `_spawn_worker()` — `subprocess.Popen([jp_exe, "_update-check"], stdin/stdout/
  stderr=DEVNULL, start_new_session=True)`. `jp_exe` resolved as
  `sys.argv[0]`/`shutil.which("jp")`/`sys.executable -m jp` fallback so it works
  under pipx/uv/pip and a frozen binary. Never waits on it.

**New hidden subcommand `src/jp/commands/update_check.py`** (`jp _update-check`,
registered with `help=argparse.SUPPRESS`):

- `run()` — the background worker. Calls `update._latest_release_tag()`, writes
  `{last_check: now, latest, checked_version}` to the cache. If auto-update is
  enabled (Component 3), performs the update and records `pending_announcement`.
  Always exits 0; swallows all errors (it runs detached, output goes nowhere).

Registered in `src/jp/commands/__init__.py` `ALL`.

### Component 2 — passive notifier

`maybe_notify` gates (silent when any is true):
- `-q/--quiet` (from `args`);
- stderr is not a TTY (output piped/redirected);
- CI env present (`CI`, `GITHUB_ACTIONS`, `BUILD_NUMBER`, …);
- opt-out env `JP_NO_UPDATE_NOTIFIER`;
- global pref `update_notifier=false` (Component 3);
- meta/excluded command (`update`, `version`, `_update-check`, no-command/help);
- editable/dev or frozen install (reuse `update._editable_source()` /
  `_running_as_binary()`).

**Notice** — to **stderr** (keeps stdout pipe-clean), colored via `ui._Style`,
respecting `ui._color_enabled(sys.stderr)`. Two lines, ASCII-safe, degrades to
plain text when color is off:

```
jp 1.1.1 → 1.2.0  (update available)
run `jp changelog` to see what's new · `jp update` to upgrade
```

**Hook** — in `cli.py` `main()`, after `rc = func(args)` is computed and before
returning, call `update_notify.maybe_notify(args)` inside a `try/except
Exception: pass`. It must never affect `rc`.

### Component 3 — global prefs + opt-in auto-update

**New `src/jp/global_prefs.py`** — user-global preferences, stored at
`~/.config/jp/config.json` (distinct from the per-repo `.jp/config.json`):

```json
{ "auto_update": false, "update_notifier": true }
```

- `load() -> dict`, `get(key, default)`, `set(key, value)` — small typed
  accessors; corruption-safe; atomic write 0600.
- Defaults: `auto_update=False` (opt-in), `update_notifier=True`.

**`jp config` routing** — `config_cmd.py` currently requires a workspace
(`load_repo()`). Add a small set of **global keys** (`auto_update`,
`update_notifier`) handled *before* `load_repo()`:
- `jp config get auto_update` / `set auto_update true` / and `list` includes them
  under a "machine settings" group — works **without** being inside a workspace.
- Values coerced as bool (reuse the `_coerce_bool` pattern from
  `settings_schema.py`).
- Interactive TUI inclusion of global keys is a nice-to-have, not required for v1.

**Auto-update execution (when `auto_update=true`)** — never replaces the running
process. The detached worker (`jp _update-check`), after finding a newer version:
1. Skips if CI / editable / frozen (same guards as the notifier).
2. Runs the existing `update` machinery non-interactively (reuse `update.run`'s
   manager-detection + install path; no prompts).
3. On success, writes `pending_announcement={from: <old>, to: <new>}` to the
   cache.

Next session, `maybe_notify` prints and clears it:

```
✓ jp auto-updated 1.1.1 → 1.2.0 — run `jp changelog` to see what's new
```

### Component 4 — `jp changelog` (three surfaces)

**New `src/jp/changelog.py`** — fetch + render release notes from the GitHub
releases API (stdlib `urllib`, same pattern as `update._latest_release_tag()`):
- `latest_release() -> Release|None` — `releases/latest`.
- `releases_since(version) -> list[Release]` — page `releases`, keep those with a
  tag newer than `version` (via `update._norm()`).
- `release_for(tag) -> Release|None` — `releases/tags/{tag}`.
- A small terminal renderer for the release body (markdown → lightly styled
  text: bold headings via `ui._Style`, bullets preserved). No markdown lib;
  keep it simple and safe.
- All network failures degrade to a friendly "couldn't reach GitHub" message and
  `EXIT_NETWORK`, never a traceback.

**New command `src/jp/commands/changelog.py`** — `jp changelog [VERSION] [--all]`:
- no arg → notes for releases **newer than installed** (or the latest release if
  already up to date);
- `VERSION` → notes for that tag;
- `--all` → recent releases.
Registered in `commands/__init__.py`.

**`jp version --changelog`** — add a `--changelog` flag to `version.py` that
prints the current version's release notes (delegates to `changelog`).

**Auto after `jp update`** — on a successful upgrade, `update.run()` prints a
"What's new" block for the new version (delegates to `changelog.release_for`).
Best-effort; a fetch failure does not fail the update.

### Component 5 — AI release highlights (CI, Gemini)

Goal: each **minor/major** GitHub Release gets a professional, no-emoji
"Highlights — what changed and how to use it" section appended to its body, so
`jp changelog` reads engaging notes. The deterministic release-please changelog
(the factual commit list) stays as-is; the AI section is **additive**, which
keeps facts grounded and limits hallucination to the prose layer.

- **Provider: Gemini** (Pedro has a Gemini API key; Claude Max ≠ API credits, and
  the Max OAuth path is not usable headless in CI). Called via stdlib `urllib`
  REST — no SDK, no added project dependency. Secret: `GEMINI_API_KEY`. Model via
  `GEMINI_MODEL` env (default a current `gemini-*-flash`; Pedro confirms the exact
  id).
- **New `scripts/release_notes_ai.py`** (CI-only, not shipped in the wheel):
  1. Inputs: the new tag and the previous tag.
  2. Gather context: `git log` subjects + `git diff --stat` (and a bounded slice
     of the actual diff) between the two tags, plus `README.md` for usage context.
  3. Prompt Gemini to produce a concise, professional, **no-emoji** highlights
     section, grounded strictly in the diff/commits, explaining each feature and
     how to use it. Output is markdown.
  4. Append the section to the GitHub Release body via `gh release edit <tag>
     --notes "<existing>\n\n## Highlights\n<ai>"` (read existing body first).
  - Bail cleanly (exit 0, log a notice) if `GEMINI_API_KEY` is unset or the call
    fails — the release must never be blocked by this step.
- **Workflow wiring** — in `.github/workflows/release.yml` (the
  `release_created == true` path), add a step after the release exists that:
  - computes previous vs new tag;
  - runs only when the bump is **minor or major** (compare semver components of
    the two tags; skip patch-only);
  - runs `scripts/release_notes_ai.py`.
- **`release-please-config.json`** — set professional, **no-emoji**
  `changelog-sections` (Features, Bug Fixes, Performance Improvements,
  Documentation, …) so the deterministic section is clean too.

## Files

New:
- `src/jp/update_notify.py`
- `src/jp/commands/update_check.py` (hidden `_update-check`)
- `src/jp/global_prefs.py`
- `src/jp/changelog.py`
- `src/jp/commands/changelog.py`
- `scripts/release_notes_ai.py` (CI-only)
- `tests/test_update_notify.py`
- `tests/test_global_prefs.py`
- `tests/test_changelog.py`

Edited:
- `src/jp/cli.py` — call `maybe_notify(args)` at end of `main()` (guarded).
- `src/jp/commands/__init__.py` — register `update_check`, `changelog`.
- `src/jp/commands/update.py` — print "What's new" after a successful update.
- `src/jp/commands/version.py` — add `--changelog`.
- `src/jp/commands/config_cmd.py` — route global keys without a workspace.
- `release-please-config.json` — professional no-emoji `changelog-sections`.
- `README.md` — document the notifier, `JP_NO_UPDATE_NOTIFIER`, `auto_update`,
  `jp changelog`.
- `.github/workflows/release.yml` — AI highlights step (minor/major only).

## Testing (offline, mocked — no live network, no real credentials)

- **Gates**: each of quiet / non-TTY / CI env / `JP_NO_UPDATE_NOTIFIER` /
  `update_notifier=false` / excluded command / editable install → no output and
  no worker spawn (monkeypatch `Popen`, assert not called).
- **Notice**: cache fresh + newer → prints to stderr; same/older → silent;
  color on/off formatting.
- **Refresh**: cache stale → worker spawned (mock `Popen`); fresh → not spawned.
- **Cache**: roundtrip; corrupt/missing file → treated as stale, no crash.
- **Worker** (`_update-check`): mocked `_latest_release_tag` writes the cache;
  with `auto_update=true` (mock `update.run` → success) writes
  `pending_announcement`; CI/editable guards skip the update.
- **Announcement**: `pending_announcement` present → printed once and cleared.
- **global_prefs**: roundtrip, defaults, corruption-safe; `jp config get/set/list`
  of global keys works without a workspace.
- **changelog**: `releases_since` filtering by `_norm`; renderer output;
  `jp changelog` / `--all` / `VERSION`; `version --changelog`; network failure →
  friendly message + `EXIT_NETWORK`.
- `scripts/release_notes_ai.py`: pure functions (tag-diff, semver bump
  classification, prompt assembly) unit-tested with fixtures; the network call is
  mocked. CI smoke only; never calls Gemini in tests.

## Phasing

1. Components 1 + 2 (cache, worker, passive notifier) — the core.
2. Component 3 (global prefs + opt-in auto-update).
3. Component 4 (`jp changelog`, `version --changelog`, post-update notes).
4. Component 5 (Gemini release-notes CI step + changelog-sections).

One spec; PRs may be stacked per phase.

## Open items for Pedro

- Confirm the exact `GEMINI_MODEL` id to default to.
- Add the `GEMINI_API_KEY` GitHub Actions secret before Component 5 runs live.

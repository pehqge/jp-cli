# Update Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a passive "update available" notifier to `jp`, plus opt-in auto-update, a `jp changelog` command, and an AI-authored release-notes CI step.

**Architecture:** The hot path reads a small JSON cache only (zero network latency); when the cache is stale it fires a detached `jp _update-check` worker that does the network call and rewrites the cache, so the notice appears on the *next* command. Auto-update (opt-in, default OFF) runs in that worker and is announced next session. `jp changelog` fetches GitHub release notes. A CI step calls Gemini to append user-facing highlights to minor/major releases.

**Tech Stack:** Python 3.9+, stdlib only (urllib, subprocess, json, argparse), pytest. The shipped `jpsync` package gains no runtime dependency; the Gemini call lives only in a CI-only script.

**Spec:** `docs/superpowers/specs/2026-06-05-update-notifier-design.md`

**Deviation from spec:** global prefs file is `~/.config/jp/prefs.json` (not `config.json`) to avoid confusion with the per-repo `.jp/config.json` and the existing `credentials.json` in the same dir.

**Cross-module import rule (important):** `update_notify`, `update_check`, and `changelog` reference each other and `commands/update`. To avoid import cycles through `commands/__init__.py`, **all cross-module references are lazy imports inside functions**, except light leaf imports (`__version__`, `ui`, `errors`). Follow the import placement shown in each task exactly.

---

## Phase 1 — Version cache + background worker

### Task 1: `update_notify` cache helpers

**Files:**
- Create: `src/jp/update_notify.py`
- Test: `tests/test_update_notify.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_update_notify.py
from __future__ import annotations

import json

import jp.update_notify as un
from jp import credentials


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "global_dir", lambda: tmp_path)


def test_cache_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    un._save_cache({"latest": "v1.2.0", "last_check": 123.0})
    assert un._load_cache() == {"latest": "v1.2.0", "last_check": 123.0}


def test_load_cache_missing_returns_empty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert un._load_cache() == {}


def test_load_cache_corrupt_returns_empty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "update-check.json").write_text("{not json", encoding="utf-8")
    assert un._load_cache() == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_update_notify.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jp.update_notify'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/jp/update_notify.py
"""Passive 'update available' notifier (npm/gh-style).

The hot path reads a small JSON cache only -- never the network -- so it adds no
latency. When the cache is stale, a detached ``jp _update-check`` worker does the
network call (and, if enabled, the auto-update) and rewrites the cache; the
notice therefore appears on the *next* command, never blocking the current one.

Cross-module references (``global_prefs``, ``commands.update``) are imported
lazily inside functions to avoid an import cycle through ``commands/__init__``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, ui

_TTL_SECONDS = 24 * 60 * 60
_CACHE_NAME = "update-check.json"
_EXCLUDED_COMMANDS = frozenset({"update", "version", "_update-check"})
_CI_ENV_VARS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "BUILDKITE",
    "JENKINS_URL",
    "TEAMCITY_VERSION",
)


def _cache_path() -> Path:
    from .credentials import global_dir

    return global_dir() / _CACHE_NAME


def _load_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(data: dict) -> None:
    from .paths import atomic_write

    atomic_write(_cache_path(), json.dumps(data).encode("utf-8"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_update_notify.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/jp/update_notify.py tests/test_update_notify.py
git commit -m "feat(update-notify): cache read/write helpers"
```

---

### Task 2: suppression gates + worker spawn

**Files:**
- Modify: `src/jp/update_notify.py`
- Test: `tests/test_update_notify.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_update_notify.py`:

```python
import argparse


def _args(command="status", quiet=False):
    return argparse.Namespace(command=command, quiet=quiet)


def _force_tty(monkeypatch):
    monkeypatch.setattr(un.sys.stderr, "isatty", lambda: True, raising=False)


def _no_ci(monkeypatch):
    for v in un._CI_ENV_VARS + ("JP_NO_UPDATE_NOTIFIER",):
        monkeypatch.delenv(v, raising=False)


def test_suppressed_when_quiet(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    assert un._suppressed(_args(quiet=True)) is True


def test_suppressed_when_not_tty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(un.sys.stderr, "isatty", lambda: False, raising=False)
    _no_ci(monkeypatch)
    assert un._suppressed(_args()) is True


def test_suppressed_in_ci(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    monkeypatch.setenv("CI", "1")
    assert un._suppressed(_args()) is True


def test_suppressed_for_excluded_command(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    monkeypatch.setattr(un, "_install_is_special", lambda: False)
    monkeypatch.setattr(un, "_notifier_pref_on", lambda: True)
    assert un._suppressed(_args(command="update")) is True


def test_not_suppressed_for_normal_command(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    monkeypatch.setattr(un, "_install_is_special", lambda: False)
    monkeypatch.setattr(un, "_notifier_pref_on", lambda: True)
    assert un._suppressed(_args(command="status")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_update_notify.py -q`
Expected: FAIL with `AttributeError: module 'jp.update_notify' has no attribute '_suppressed'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/jp/update_notify.py`:

```python
def _is_ci() -> bool:
    return any(os.environ.get(v) for v in _CI_ENV_VARS)


def _notifier_pref_on() -> bool:
    from . import global_prefs

    return bool(global_prefs.get("update_notifier", True))


def _install_is_special() -> bool:
    """True for editable/dev or frozen installs, where notifying is noise."""
    from .commands import update as _update

    return bool(_update._editable_source() or _update._running_as_binary())


def _suppressed(args) -> bool:
    if getattr(args, "quiet", False):
        return True
    if os.environ.get("JP_NO_UPDATE_NOTIFIER"):
        return True
    if _is_ci():
        return True
    if not getattr(sys.stderr, "isatty", lambda: False)():
        return True
    if getattr(args, "command", None) in _EXCLUDED_COMMANDS:
        return True
    if not _notifier_pref_on():
        return True
    if _install_is_special():
        return True
    return False


def _jp_executable() -> list[str]:
    exe = shutil.which("jp") or sys.argv[0] or ""
    name = Path(exe).name.lower()
    if exe and not name.startswith("python") and not exe.endswith(".py"):
        return [exe]
    return [sys.executable, "-m", "jp"]


def _spawn_worker() -> None:
    cmd = _jp_executable() + ["_update-check"]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_update_notify.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/jp/update_notify.py tests/test_update_notify.py
git commit -m "feat(update-notify): suppression gates and detached worker spawn"
```

---

### Task 3: `maybe_notify` — notice, refresh trigger, announcement

**Files:**
- Modify: `src/jp/update_notify.py`
- Test: `tests/test_update_notify.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_update_notify.py`:

```python
def _ready(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    monkeypatch.setattr(un, "_install_is_special", lambda: False)
    monkeypatch.setattr(un, "_notifier_pref_on", lambda: True)
    monkeypatch.setattr(un, "__version__", "1.1.1")


def test_notice_printed_when_newer(monkeypatch, tmp_path, capsys):
    _ready(monkeypatch, tmp_path)
    spawned = []
    monkeypatch.setattr(un, "_spawn_worker", lambda: spawned.append(True))
    un._save_cache({"latest": "v1.2.0", "last_check": un.time.time()})
    un.maybe_notify(_args())
    err = capsys.readouterr().err
    assert "1.1.1" in err and "1.2.0" in err
    assert spawned == []  # cache fresh -> no refresh


def test_no_notice_when_same_version(monkeypatch, tmp_path, capsys):
    _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(un, "_spawn_worker", lambda: None)
    un._save_cache({"latest": "v1.1.1", "last_check": un.time.time()})
    un.maybe_notify(_args())
    assert capsys.readouterr().err == ""


def test_stale_cache_triggers_refresh(monkeypatch, tmp_path):
    _ready(monkeypatch, tmp_path)
    spawned = []
    monkeypatch.setattr(un, "_spawn_worker", lambda: spawned.append(True))
    un._save_cache({"latest": "v1.1.1", "last_check": 0.0})
    un.maybe_notify(_args())
    assert spawned == [True]


def test_pending_announcement_printed_and_cleared(monkeypatch, tmp_path, capsys):
    _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(un, "_spawn_worker", lambda: None)
    un._save_cache({"pending_announcement": {"from": "1.1.1", "to": "1.2.0"}})
    un.maybe_notify(_args())
    err = capsys.readouterr().err
    assert "auto-updated" in err and "1.2.0" in err
    assert "pending_announcement" not in un._load_cache()


def test_maybe_notify_never_raises(monkeypatch, tmp_path):
    _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(un, "_load_cache", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    un.maybe_notify(_args())  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_update_notify.py -q`
Expected: FAIL with `AttributeError: ... has no attribute 'maybe_notify'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/jp/update_notify.py`:

```python
def _version_is_newer(latest: str) -> bool:
    from .commands import update as _update

    return _update._norm(latest) > _update._norm(__version__)


def _print_notice(latest: str) -> None:
    err = sys.stderr
    latest_clean = latest.lstrip("vV")
    head = ui._wrap(f"jp {__version__} → {latest_clean}", ui._Style.BOLD, err)
    tag = ui._wrap("(update available)", ui._Style.YELLOW, err)
    hint = ui._wrap(
        "run `jp changelog` to see what's new · `jp update` to upgrade",
        ui._Style.DIM,
        err,
    )
    print(f"{head}  {tag}", file=err)
    print(hint, file=err)


def _print_announcement(ann: dict) -> None:
    msg = (
        f"✓ jp auto-updated {ann.get('from', '?')} → {ann.get('to', '?')}"
        " — run `jp changelog` to see what's new"
    )
    print(ui._wrap(msg, ui._Style.GREEN, sys.stderr), file=sys.stderr)


def maybe_notify(args) -> None:
    """Show the notice if warranted; spawn a refresh if the cache is stale.

    Wrapped so it can never raise into ``main()`` or change the exit code.
    """
    try:
        _maybe_notify(args)
    except Exception:
        pass


def _maybe_notify(args) -> None:
    if _suppressed(args):
        return
    cache = _load_cache()
    ann = cache.get("pending_announcement")
    if ann:
        _print_announcement(ann)
        cache.pop("pending_announcement", None)
        _save_cache(cache)
        return
    last = float(cache.get("last_check") or 0)
    if (time.time() - last) > _TTL_SECONDS:
        cache["last_check"] = time.time()  # debounce: avoid a spawn storm
        _save_cache(cache)
        _spawn_worker()
    latest = cache.get("latest")
    if latest and _version_is_newer(latest):
        _print_notice(latest)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_update_notify.py -q`
Expected: PASS (13 passed).

- [ ] **Step 5: Commit**

```bash
git add src/jp/update_notify.py tests/test_update_notify.py
git commit -m "feat(update-notify): maybe_notify with notice, refresh, announcement"
```

---

### Task 4: hidden `jp _update-check` worker command

**Files:**
- Create: `src/jp/commands/update_check.py`
- Modify: `src/jp/commands/__init__.py`
- Test: `tests/test_update_check_cmd.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_update_check_cmd.py
from __future__ import annotations

import argparse

import jp.update_notify as un
from jp import credentials
from jp.commands import update as upd
from jp.commands import update_check as uc


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "global_dir", lambda: tmp_path)


def test_worker_writes_cache(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "__version__", "1.1.1")
    monkeypatch.setattr(upd, "_latest_release_tag", lambda: "v1.3.0")
    uc.run(argparse.Namespace())
    cache = un._load_cache()
    assert cache["latest"] == "v1.3.0"
    assert cache["checked_version"] == "1.1.1"
    assert "last_check" in cache


def test_worker_no_autoupdate_when_disabled(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "__version__", "1.1.1")
    monkeypatch.setattr(upd, "_latest_release_tag", lambda: "v1.3.0")
    from jp import global_prefs
    monkeypatch.setattr(global_prefs, "get", lambda k, d=None: False)
    ran = []
    monkeypatch.setattr(uc, "_perform_auto_update", lambda: ran.append(True) or True)
    uc.run(argparse.Namespace())
    assert ran == []
    assert "pending_announcement" not in un._load_cache()


def test_worker_autoupdate_records_announcement(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "__version__", "1.1.1")
    monkeypatch.setattr(upd, "_latest_release_tag", lambda: "v1.3.0")
    from jp import global_prefs
    monkeypatch.setattr(global_prefs, "get", lambda k, d=None: True)
    monkeypatch.setattr(upd, "_editable_source", lambda: None)
    monkeypatch.setattr(upd, "_running_as_binary", lambda: False)
    monkeypatch.setattr(un, "_is_ci", lambda: False)
    monkeypatch.setattr(uc, "_perform_auto_update", lambda: True)
    uc.run(argparse.Namespace())
    ann = un._load_cache()["pending_announcement"]
    assert ann == {"from": "1.1.1", "to": "1.3.0"}


def test_worker_never_raises(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(upd, "_latest_release_tag", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert uc.run(argparse.Namespace()) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_update_check_cmd.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jp.commands.update_check'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/jp/commands/update_check.py
"""Hidden ``jp _update-check`` -- the background worker for the update notifier.

Spawned detached by ``update_notify``. Does the network check, rewrites the
cache, and -- when ``auto_update`` is enabled and the install is upgradeable --
performs the upgrade and records an announcement for the next session. Always
exits 0 and never prints to the user (it runs with stdout/stderr to DEVNULL).
"""

from __future__ import annotations

import argparse

from .. import __version__
from ..errors import EXIT_OK


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("_update-check", help=argparse.SUPPRESS)
    p.set_defaults(func=run)


def _perform_auto_update() -> bool:
    """Run the upgrade non-interactively. Returns True on success."""
    from . import update as _update

    args = argparse.Namespace(check=False, source="auto", quiet=True)
    return _update.run(args) == EXIT_OK


def run(args: argparse.Namespace) -> int:
    try:
        _run()
    except Exception:
        pass
    return EXIT_OK


def _run() -> None:
    import time

    from .. import global_prefs, update_notify
    from . import update as _update

    latest = _update._latest_release_tag()
    cache = update_notify._load_cache()
    cache["last_check"] = time.time()
    cache["checked_version"] = __version__
    if latest:
        cache["latest"] = latest
    update_notify._save_cache(cache)

    if not latest or _update._norm(latest) <= _update._norm(__version__):
        return
    if not global_prefs.get("auto_update", False):
        return
    if _update._editable_source() or _update._running_as_binary() or update_notify._is_ci():
        return
    if _perform_auto_update():
        cache = update_notify._load_cache()
        cache["pending_announcement"] = {"from": __version__, "to": latest.lstrip("vV")}
        update_notify._save_cache(cache)
```

- [ ] **Step 4: Register the command**

Edit `src/jp/commands/__init__.py` — add `update_check` to the import block and to `ALL` (place it right after `update`):

```python
from . import (
    clone,
    config_cmd,
    diff,
    doctor,
    ignore_cmd,
    init,
    kernel,
    login,
    ls,
    pull,
    push,
    rm,
    status,
    terminal,
    update,
    update_check,
    version,
)

ALL = [
    clone,
    init,
    login,
    pull,
    push,
    status,
    ls,
    diff,
    config_cmd,
    ignore_cmd,
    rm,
    kernel,
    terminal,
    doctor,
    update,
    update_check,
    version,
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_update_check_cmd.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add src/jp/commands/update_check.py src/jp/commands/__init__.py tests/test_update_check_cmd.py
git commit -m "feat(update-notify): hidden _update-check worker command"
```

---

### Task 5: hook `maybe_notify` into `main()`

**Files:**
- Modify: `src/jp/cli.py`
- Test: `tests/test_cli_notify_hook.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_notify_hook.py
from __future__ import annotations

import jp.cli as cli


def test_main_calls_maybe_notify(monkeypatch, capsys):
    called = {}
    monkeypatch.setattr(cli.update_notify, "maybe_notify", lambda args: called.setdefault("ok", args.command))
    rc = cli.main(["version"])
    assert rc == 0
    assert called.get("ok") == "version"


def test_notify_failure_does_not_break_command(monkeypatch):
    def boom(_):
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(cli.update_notify, "maybe_notify", boom)
    # maybe_notify is wrapped internally, but cli must also be defensive.
    rc = cli.main(["version"])
    assert rc == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_notify_hook.py -q`
Expected: FAIL with `AttributeError: module 'jp.cli' has no attribute 'update_notify'`.

- [ ] **Step 3: Write minimal implementation**

In `src/jp/cli.py`, add the import near the top (after `from . import __version__, ui`):

```python
from . import __version__, ui, update_notify
```

Then in `main()`, wrap the dispatch so the notice runs after the command and can never change `rc`. Replace the existing `try:` block that runs `func(args)` with:

```python
    try:
        rc = func(args)
        rc = int(rc) if rc is not None else EXIT_OK
    except JpError as exc:
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

    try:
        update_notify.maybe_notify(args)
    except Exception:
        pass
    return rc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_notify_hook.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the whole suite + lint**

Run: `python -m pytest -q && ruff check src/jp && ruff format --check src/jp`
Expected: all pass. (Lint gate is required before any commit — see project rule.)

- [ ] **Step 6: Commit**

```bash
git add src/jp/cli.py tests/test_cli_notify_hook.py
git commit -m "feat(update-notify): surface the notice at the end of main()"
```

---

## Phase 2 — Global prefs + opt-in auto-update

### Task 6: `global_prefs` module

**Files:**
- Create: `src/jp/global_prefs.py`
- Test: `tests/test_global_prefs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_global_prefs.py
from __future__ import annotations

from jp import credentials, global_prefs as gp


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "global_dir", lambda: tmp_path)


def test_defaults(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert gp.get("auto_update") is False
    assert gp.get("update_notifier") is True


def test_set_get_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    gp.set("auto_update", True)
    assert gp.get("auto_update") is True
    assert gp.load() == {"auto_update": True}


def test_corrupt_file_falls_back_to_defaults(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "prefs.json").write_text("nonsense", encoding="utf-8")
    assert gp.get("update_notifier") is True


def test_coerce_bool():
    assert gp.coerce_bool("true") is True
    assert gp.coerce_bool("0") is False
    assert gp.coerce_bool("on") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_global_prefs.py -q`
Expected: FAIL with `ImportError: cannot import name 'global_prefs'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/jp/global_prefs.py
"""User-global jp preferences (machine-wide), stored at ``~/.config/jp/prefs.json``.

Distinct from the per-repo ``.jp/config.json``: these govern jp itself (the
update notifier and opt-in auto-update), not a workspace. Corruption-safe;
written atomically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PREFS_NAME = "prefs.json"
_DEFAULTS: dict[str, Any] = {"auto_update": False, "update_notifier": True}
GLOBAL_KEYS = frozenset(_DEFAULTS)


def _path() -> Path:
    from .credentials import global_dir

    return global_dir() / _PREFS_NAME


def load() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get(key: str, default: Any = None) -> Any:
    if default is None and key in _DEFAULTS:
        default = _DEFAULTS[key]
    return load().get(key, default)


def set(key: str, value: Any) -> None:  # noqa: A001 - intentional public name
    from .paths import atomic_write

    data = load()
    data[key] = value
    atomic_write(_path(), (json.dumps(data, indent=2) + "\n").encode("utf-8"))


def coerce_bool(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "on")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_global_prefs.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/jp/global_prefs.py tests/test_global_prefs.py
git commit -m "feat(prefs): user-global preferences store"
```

---

### Task 7: route global keys through `jp config` without a workspace

**Files:**
- Modify: `src/jp/commands/config_cmd.py`
- Test: `tests/test_config_global_keys.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_global_keys.py
from __future__ import annotations

import argparse

from jp import credentials, global_prefs as gp
from jp.commands import config_cmd


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "global_dir", lambda: tmp_path)


def test_set_global_key_without_workspace(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    # load_repo would raise outside a workspace; assert it is never called.
    monkeypatch.setattr(
        config_cmd, "load_repo", lambda: (_ for _ in ()).throw(AssertionError("should not load repo"))
    )
    rc = config_cmd.run(argparse.Namespace(action="set", key="auto_update", value="true"))
    assert rc == 0
    assert gp.get("auto_update") is True
    assert "auto_update" in capsys.readouterr().out


def test_get_global_key_without_workspace(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        config_cmd, "load_repo", lambda: (_ for _ in ()).throw(AssertionError("should not load repo"))
    )
    gp.set("update_notifier", False)
    rc = config_cmd.run(argparse.Namespace(action="get", key="update_notifier", value=None))
    assert rc == 0
    assert "False" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_global_keys.py -q`
Expected: FAIL (it falls through to `load_repo`, raising the AssertionError).

- [ ] **Step 3: Write minimal implementation**

In `src/jp/commands/config_cmd.py`, add the import at the top alongside the others:

```python
from .. import global_prefs
```

Then at the very start of `run()` (before `ctx = load_repo()`), insert the global-key fast path:

```python
def run(args: argparse.Namespace) -> int:
    # Machine-wide preferences (notifier / auto-update) are handled before
    # workspace resolution, so they work from any directory.
    if args.action in ("get", "set") and args.key in global_prefs.GLOBAL_KEYS:
        return _run_global(args)

    ctx = load_repo()
    cfg = ctx.cfg
    ...  # unchanged from here
```

Add this helper near `_print_list`:

```python
def _run_global(args: argparse.Namespace) -> int:
    if args.action == "get":
        ui.info(f"{global_prefs.get(args.key)}")
        return EXIT_OK
    if args.value is None:
        raise UsageError("config set requires a key and a value")
    value = global_prefs.coerce_bool(args.value)
    global_prefs.set(args.key, value)
    ui.success(f"set {args.key} = {str(value).lower()}")
    return EXIT_OK
```

Also extend the `list` branch so global prefs are visible. After the existing `_print_list(cfg)` call in the `args.action == "list"` branch, add:

```python
        ui.info("")
        ui.heading("machine settings (global):")
        for key in sorted(global_prefs.GLOBAL_KEYS):
            ui.info(f"{key} = {str(global_prefs.get(key)).lower()}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config_global_keys.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Wire the notifier pref + auto-update into the worker (integration check)**

The worker already reads `global_prefs.get("auto_update", ...)` (Task 4) and the
notifier reads `global_prefs.get("update_notifier", ...)` (Task 2). Add an
integration test:

```python
# append to tests/test_update_notify.py
def test_notifier_off_pref_suppresses(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    monkeypatch.setattr(un, "_install_is_special", lambda: False)
    from jp import global_prefs
    monkeypatch.setattr(global_prefs, "get", lambda k, d=None: False if k == "update_notifier" else d)
    assert un._suppressed(_args(command="status")) is True
```

Run: `python -m pytest tests/test_update_notify.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/jp/commands/config_cmd.py tests/test_config_global_keys.py tests/test_update_notify.py
git commit -m "feat(config): manage global notifier/auto-update prefs without a workspace"
```

---

## Phase 3 — `jp changelog`

### Task 8: `changelog` fetch + render module

**Files:**
- Create: `src/jp/changelog.py`
- Test: `tests/test_changelog.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_changelog.py
from __future__ import annotations

from jp import changelog as cl


def test_releases_since_filters_by_version(monkeypatch):
    data = [
        {"tag_name": "v1.3.0", "name": "1.3.0", "body": "x"},
        {"tag_name": "v1.2.0", "name": "1.2.0", "body": "y"},
        {"tag_name": "v1.1.0", "name": "1.1.0", "body": "z"},
    ]
    monkeypatch.setattr(cl, "_api", lambda path: data)
    rels = cl.releases_since("1.2.0")
    assert [r.tag for r in rels] == ["v1.3.0"]


def test_releases_since_network_failure_returns_empty(monkeypatch):
    def boom(path):
        raise OSError("no network")

    monkeypatch.setattr(cl, "_api", boom)
    assert cl.releases_since("1.0.0") == []


def test_render_outputs_body(monkeypatch, capsys):
    cl.render(cl.Release(tag="v1.2.0", name="1.2.0", body="line one\nline two"))
    out = capsys.readouterr().out
    assert "1.2.0" in out
    assert "line one" in out and "line two" in out


def test_release_for_normalizes_tag(monkeypatch):
    seen = {}
    monkeypatch.setattr(cl, "_api", lambda path: seen.setdefault("path", path) or {"tag_name": "v1.2.0", "name": "1.2.0", "body": "b"})
    cl.release_for("1.2.0")
    assert seen["path"] == "releases/tags/v1.2.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_changelog.py -q`
Expected: FAIL with `ImportError: cannot import name 'changelog'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/jp/changelog.py
"""Fetch and render GitHub release notes for ``jp changelog``.

Stdlib-only (urllib), mirroring ``commands/update._latest_release_tag``. The
version compare is reused from ``commands/update`` via a lazy import to avoid an
import cycle through ``commands/__init__``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import ui

_REPO = "pehqge/jpsync"


@dataclass
class Release:
    tag: str
    name: str
    body: str


def _api(path: str):
    url = f"https://api.github.com/repos/{_REPO}/{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _to_release(d: dict) -> Release:
    return Release(
        tag=str(d.get("tag_name") or ""),
        name=str(d.get("name") or ""),
        body=str(d.get("body") or ""),
    )


def latest_release() -> Release | None:
    try:
        return _to_release(_api("releases/latest"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def release_for(tag: str) -> Release | None:
    norm = tag if tag.lower().startswith("v") else f"v{tag.lstrip('vV')}"
    try:
        return _to_release(_api(f"releases/tags/{norm}"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def releases_since(version: str) -> list[Release]:
    from .commands.update import _norm

    try:
        data = _api("releases?per_page=30")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return []
    base = _norm(version)
    out: list[Release] = []
    for d in data if isinstance(data, list) else []:
        rel = _to_release(d)
        if rel.tag and _norm(rel.tag) > base:
            out.append(rel)
    return out


def render(release: Release) -> None:
    ui.heading(release.name or release.tag)
    for line in release.body.splitlines():
        ui.out(line)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_changelog.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/jp/changelog.py tests/test_changelog.py
git commit -m "feat(changelog): fetch and render GitHub release notes"
```

---

### Task 9: `jp changelog` command

**Files:**
- Create: `src/jp/commands/changelog.py`
- Modify: `src/jp/commands/__init__.py`
- Test: `tests/test_changelog_cmd.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_changelog_cmd.py
from __future__ import annotations

import argparse

from jp import changelog as cl
from jp.commands import changelog as cmd
from jp.errors import EXIT_NETWORK, EXIT_OK


def _ns(version=None, all=False):
    return argparse.Namespace(version=version, all=all)


def test_changelog_specific_version(monkeypatch, capsys):
    monkeypatch.setattr(cl, "release_for", lambda t: cl.Release("v1.2.0", "1.2.0", "notes here"))
    assert cmd.run(_ns(version="1.2.0")) == EXIT_OK
    assert "notes here" in capsys.readouterr().out


def test_changelog_specific_version_network_error(monkeypatch):
    monkeypatch.setattr(cl, "release_for", lambda t: None)
    assert cmd.run(_ns(version="9.9.9")) == EXIT_NETWORK


def test_changelog_up_to_date(monkeypatch, capsys):
    monkeypatch.setattr(cmd, "__version__", "1.2.0")
    monkeypatch.setattr(cl, "releases_since", lambda v: [])
    monkeypatch.setattr(cl, "latest_release", lambda: cl.Release("v1.2.0", "1.2.0", "current"))
    assert cmd.run(_ns()) == EXIT_OK
    assert "up to date" in capsys.readouterr().out


def test_changelog_newer_available(monkeypatch, capsys):
    monkeypatch.setattr(cmd, "__version__", "1.1.0")
    monkeypatch.setattr(cl, "releases_since", lambda v: [cl.Release("v1.2.0", "1.2.0", "new stuff")])
    assert cmd.run(_ns()) == EXIT_OK
    assert "new stuff" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_changelog_cmd.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jp.commands.changelog'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/jp/commands/changelog.py
"""``jp changelog`` -- show release notes from GitHub."""

from __future__ import annotations

import argparse

from .. import __version__, changelog as _cl, ui
from ..errors import EXIT_NETWORK, EXIT_OK


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("changelog", help="show jp release notes")
    p.add_argument("version", nargs="?", help="show notes for a specific version (e.g. 1.2.0)")
    p.add_argument("--all", action="store_true", help="show recent releases")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.version:
        rel = _cl.release_for(args.version)
        if rel is None:
            ui.warn("could not fetch that release from GitHub.")
            return EXIT_NETWORK
        _cl.render(rel)
        return EXIT_OK

    if args.all:
        rels = _cl.releases_since("0")
        if not rels:
            ui.warn("could not fetch releases from GitHub.")
            return EXIT_NETWORK
        for rel in rels:
            _cl.render(rel)
            ui.out("")
        return EXIT_OK

    rels = _cl.releases_since(__version__)
    if rels:
        ui.info(f"jp {__version__} -- newer releases available:\n")
        for rel in rels:
            _cl.render(rel)
            ui.out("")
        return EXIT_OK

    rel = _cl.latest_release()
    if rel is None:
        ui.warn("could not fetch releases from GitHub.")
        return EXIT_NETWORK
    ui.success(f"jp {__version__} is up to date. Latest release:")
    _cl.render(rel)
    return EXIT_OK
```

- [ ] **Step 4: Register the command**

Edit `src/jp/commands/__init__.py` — add `changelog` to the import block and to
`ALL` (place it right after `version`):

```python
from . import (
    changelog,
    clone,
    config_cmd,
    diff,
    doctor,
    ignore_cmd,
    init,
    kernel,
    login,
    ls,
    pull,
    push,
    rm,
    status,
    terminal,
    update,
    update_check,
    version,
)

ALL = [
    clone,
    init,
    login,
    pull,
    push,
    status,
    ls,
    diff,
    config_cmd,
    ignore_cmd,
    rm,
    kernel,
    terminal,
    doctor,
    update,
    update_check,
    version,
    changelog,
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_changelog_cmd.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add src/jp/commands/changelog.py src/jp/commands/__init__.py tests/test_changelog_cmd.py
git commit -m "feat(changelog): add jp changelog command"
```

---

### Task 10: `jp version --changelog` + "What's new" after `jp update`

**Files:**
- Modify: `src/jp/commands/version.py`
- Modify: `src/jp/commands/update.py`
- Test: `tests/test_version_changelog.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_version_changelog.py
from __future__ import annotations

import argparse

from jp import changelog as cl
from jp.commands import version as ver
from jp.errors import EXIT_OK


def test_version_plain(capsys):
    assert ver.run(argparse.Namespace(changelog=False)) == EXIT_OK
    assert "jp " in capsys.readouterr().out


def test_version_with_changelog(monkeypatch, capsys):
    monkeypatch.setattr(ver, "__version__", "1.2.0")
    monkeypatch.setattr(cl, "release_for", lambda t: cl.Release("v1.2.0", "1.2.0", "release body"))
    assert ver.run(argparse.Namespace(changelog=True)) == EXIT_OK
    out = capsys.readouterr().out
    assert "jp 1.2.0" in out
    assert "release body" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_version_changelog.py -q`
Expected: FAIL — `version.run` does not yet accept `changelog`, and `add_parser` lacks the flag.

- [ ] **Step 3: Implement `version --changelog`**

Replace the body of `src/jp/commands/version.py` with:

```python
"""``jp version`` -- print the jp version (optionally with release notes)."""

from __future__ import annotations

import argparse

from .. import __version__, ui
from ..errors import EXIT_NETWORK, EXIT_OK


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("version", help="print the jp version")
    p.add_argument("--changelog", action="store_true", help="also show this version's release notes")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ui.out(f"jp {__version__}")
    if getattr(args, "changelog", False):
        from .. import changelog as _cl

        rel = _cl.release_for(__version__) or _cl.latest_release()
        if rel is None:
            ui.warn("could not fetch release notes from GitHub.")
            return EXIT_NETWORK
        ui.out("")
        _cl.render(rel)
    return EXIT_OK
```

- [ ] **Step 4: Add the post-update "What's new" block**

In `src/jp/commands/update.py`, find the success branch in `run()`:

```python
    if rc == 0:
        ui.success("update complete. Run 'jp --version' to confirm.")
        return EXIT_OK
```

Replace it with:

```python
    if rc == 0:
        ui.success("update complete. Run 'jp --version' to confirm.")
        if latest:
            from .. import changelog as _cl

            rel = _cl.release_for(latest)
            if rel is not None:
                ui.info("")
                ui.heading("What's new:")
                _cl.render(rel)
        return EXIT_OK
```

(`latest` is already in scope — it is the tag returned by `_latest_release_tag()`
earlier in `run()`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_version_changelog.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Full suite + lint**

Run: `python -m pytest -q && ruff check src/jp && ruff format --check src/jp`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/jp/commands/version.py src/jp/commands/update.py tests/test_version_changelog.py
git commit -m "feat(changelog): version --changelog flag and post-update notes"
```

---

## Phase 4 — AI release highlights (CI, Gemini)

### Task 11: `release_notes_ai.py` pure helpers

**Files:**
- Create: `scripts/release_notes_ai.py`
- Test: `tests/test_release_notes_ai.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_release_notes_ai.py
from __future__ import annotations

import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "release_notes_ai",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "release_notes_ai.py",
)
rna = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rna)


def test_semver_parse():
    assert rna._semver("v1.2.3") == (1, 2, 3)
    assert rna._semver("1.2.3") == (1, 2, 3)


def test_is_minor_or_major():
    assert rna.is_minor_or_major("v1.1.0", "v1.2.0") is True   # minor
    assert rna.is_minor_or_major("v1.1.0", "v2.0.0") is True   # major
    assert rna.is_minor_or_major("v1.1.0", "v1.1.1") is False  # patch
    assert rna.is_minor_or_major("v1.1.0", "v1.1.0") is False  # same


def test_build_prompt_includes_context():
    prompt = rna.build_prompt(commits="feat: add live mount", diffstat="1 file changed", readme="# jp")
    assert "feat: add live mount" in prompt
    assert "no emoji" in prompt.lower()


def test_call_gemini_parses_response(monkeypatch):
    fake = {"candidates": [{"content": {"parts": [{"text": "## Highlights\nStuff."}]}}]}
    monkeypatch.setattr(rna, "_http_post_json", lambda url, body: fake)
    out = rna.call_gemini("prompt", api_key="k", model="gemini-x")
    assert "Highlights" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_release_notes_ai.py -q`
Expected: FAIL with `FileNotFoundError` / `ModuleNotFoundError` for the script.

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/release_notes_ai.py
"""CI-only: generate professional, no-emoji release highlights with Gemini.

Runs in the release workflow after a minor/major GitHub Release is created. Reads
the commit log + diffstat between the previous and new tags, asks Gemini to write
a "Highlights -- what changed and how to use it" section, and appends it to the
GitHub Release body via ``gh release edit``. This is NOT part of the shipped
``jpsync`` package; it calls Gemini over stdlib urllib (no SDK, no project dep).

Env:
  GEMINI_API_KEY   (required) -- Gemini API key
  GEMINI_MODEL     (optional) -- model id, default 'gemini-flash-latest'
Args:
  <previous_tag> <new_tag>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_MODEL = "gemini-flash-latest"


def _semver(tag: str) -> tuple[int, int, int]:
    parts = tag.lstrip("vV").split(".")
    nums = []
    for p in parts[:3]:
        digits = "".join(c for c in p if c.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def is_minor_or_major(prev_tag: str, new_tag: str) -> bool:
    p = _semver(prev_tag)
    n = _semver(new_tag)
    return n[0] > p[0] or (n[0] == p[0] and n[1] > p[1])


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def build_prompt(commits: str, diffstat: str, readme: str) -> str:
    return (
        "You are writing release highlights for the open-source CLI 'jp' (PyPI: jpsync), "
        "a git-like tool that syncs local folders with a remote JupyterHub.\n\n"
        "Write a concise, PROFESSIONAL release-notes section titled exactly '## Highlights'. "
        "Use no emoji. For each notable user-facing change, explain in one or two sentences "
        "WHAT changed and HOW a user uses it (commands/flags). Ground every claim strictly in "
        "the commits and diff below -- do not invent features. Skip internal refactors and CI-only "
        "changes. Output Markdown only, no preamble.\n\n"
        f"## Commits\n{commits}\n\n## Diffstat\n{diffstat}\n\n"
        f"## README (for usage context)\n{readme[:6000]}\n"
    )


def _http_post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def call_gemini(prompt: str, api_key: str, model: str) -> str:
    url = f"{_GEMINI_BASE}/{model}:generateContent?key={api_key}"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    data = _http_post_json(url, body)
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_notes_ai.py <previous_tag> <new_tag>", file=sys.stderr)
        return 2
    prev_tag, new_tag = argv
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY unset; skipping AI highlights.", file=sys.stderr)
        return 0
    if not is_minor_or_major(prev_tag, new_tag):
        print(f"{prev_tag} -> {new_tag} is a patch release; skipping AI highlights.")
        return 0

    model = os.environ.get("GEMINI_MODEL", _DEFAULT_MODEL)
    rng = f"{prev_tag}..{new_tag}"
    commits = _git("log", "--no-merges", "--pretty=format:- %s", rng)
    diffstat = _git("diff", "--stat", rng)
    try:
        readme = open("README.md", encoding="utf-8").read()
    except OSError:
        readme = ""

    try:
        highlights = call_gemini(build_prompt(commits, diffstat, readme), api_key, model)
    except Exception as exc:  # never block the release
        print(f"Gemini call failed ({exc}); skipping AI highlights.", file=sys.stderr)
        return 0

    existing = _git("release", "view", new_tag, "--json", "body", "-q", ".body") if _has_gh() else ""
    new_body = f"{existing}\n\n{highlights}".strip()
    subprocess.run(["gh", "release", "edit", new_tag, "--notes", new_body], check=True)
    print(f"Appended AI highlights to release {new_tag}.")
    return 0


def _has_gh() -> bool:
    from shutil import which

    return which("gh") is not None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_release_notes_ai.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/release_notes_ai.py tests/test_release_notes_ai.py
git commit -m "feat(ci): Gemini release-highlights generator script"
```

---

### Task 12: wire the AI step into the release workflow

**Files:**
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Inspect the release job**

Run: `sed -n '1,200p' .github/workflows/release.yml`
Identify the `release` job that runs on the `workflow_call` path (the one that
builds + publishes after the tag exists). The new step must run there, after the
tag/release exist, and must have `contents: write` + a checkout with full history.

- [ ] **Step 2: Add the AI-highlights step**

In the job that runs after the release is created, ensure the checkout fetches
all tags/history, then add a step at the end:

```yaml
      - name: Checkout (full history for tag diff)
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.x"

      - name: Generate AI release highlights (minor/major only)
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          GEMINI_MODEL: ${{ vars.GEMINI_MODEL }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          set -euo pipefail
          NEW_TAG="${{ inputs.tag_name }}"
          PREV_TAG="$(git describe --tags --abbrev=0 "${NEW_TAG}^" 2>/dev/null || echo '')"
          if [ -z "$PREV_TAG" ]; then
            echo "No previous tag; skipping AI highlights."
            exit 0
          fi
          python scripts/release_notes_ai.py "$PREV_TAG" "$NEW_TAG"
```

If the existing `release.yml` already checks out the repo, reuse that checkout
(add `fetch-depth: 0` to it) instead of adding a second one. The step must be
non-fatal: the script already exits 0 when `GEMINI_API_KEY` is unset or the call
fails, so a missing secret will not break publishing.

- [ ] **Step 3: Validate the workflow YAML**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/release.yml')); print('yaml ok')"`
Expected: `yaml ok`. (If `pyyaml` is unavailable, run `gh workflow view release.yml` after pushing, or skip — the syntax mirrors the existing `release-please.yml` patterns.)

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci(release): append AI-generated highlights to minor/major releases"
```

---

### Task 13: professional changelog sections + docs

**Files:**
- Modify: `release-please-config.json`
- Modify: `README.md`

- [ ] **Step 1: Add no-emoji changelog sections**

In `release-please-config.json`, inside the `"."` package object, add:

```json
      "changelog-sections": [
        { "type": "feat", "section": "Features" },
        { "type": "fix", "section": "Bug Fixes" },
        { "type": "perf", "section": "Performance Improvements" },
        { "type": "revert", "section": "Reverts" },
        { "type": "docs", "section": "Documentation" },
        { "type": "refactor", "section": "Code Refactoring" },
        { "type": "build", "section": "Build System" },
        { "type": "ci", "section": "Continuous Integration", "hidden": true },
        { "type": "test", "section": "Tests", "hidden": true },
        { "type": "chore", "section": "Miscellaneous", "hidden": true }
      ]
```

- [ ] **Step 2: Validate JSON**

Run: `python -c "import json; json.load(open('release-please-config.json')); print('json ok')"`
Expected: `json ok`.

- [ ] **Step 3: Document the new behavior in README**

Add a short section to `README.md` (near the existing `jp update` docs) covering:
- the passive notifier (appears once/24h, only in an interactive terminal);
- opt-out: `export JP_NO_UPDATE_NOTIFIER=1`, or `jp config set update_notifier false`;
- opt-in auto-update: `jp config set auto_update true` (default off; updates in the background and announces on the next run);
- `jp changelog`, `jp changelog <version>`, `jp changelog --all`, `jp version --changelog`.

Use this exact block:

```markdown
## Staying up to date

`jp` checks for a newer release at most once a day, in the background, and shows a
one-line notice next time you run a command (only in an interactive terminal —
never in scripts, pipes, or CI):

```
jp 1.1.1 → 1.2.0  (update available)
run `jp changelog` to see what's new · `jp update` to upgrade
```

- Turn the notice off: `export JP_NO_UPDATE_NOTIFIER=1` or `jp config set update_notifier false`.
- Opt in to automatic updates: `jp config set auto_update true`. When on, `jp`
  updates itself in the background between commands and tells you on the next run.
  It never updates the running command mid-flight, and never in CI or dev installs.
- See what changed: `jp changelog` (newer releases), `jp changelog 1.2.0` (a
  specific version), `jp changelog --all`, or `jp version --changelog`.
```

- [ ] **Step 4: Full suite + lint**

Run: `python -m pytest -q && ruff check src/jp && ruff format --check src/jp`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add release-please-config.json README.md
git commit -m "docs+ci: professional changelog sections and update/notifier docs"
```

---

## Final verification

- [ ] **Run the whole suite, lint, and format check (repo-wide)**

Run: `python -m pytest -q && ruff check . && ruff format --check .`
Expected: all green. (Matches the CI lint gate — required before opening a PR.)

- [ ] **Manual smoke (optional, offline)**

```bash
# Notice path with a faked cache (forces a TTY-only message; safe, no network):
python - <<'PY'
import jp.update_notify as un, jp.credentials as cr, pathlib, tempfile, argparse, types
d = pathlib.Path(tempfile.mkdtemp()); cr.global_dir = lambda: d
un.__version__ = "0.0.1"
un._save_cache({"latest": "v9.9.9", "last_check": un.time.time()})
un._suppressed = lambda a: False  # bypass TTY gate for the demo
un.maybe_notify(argparse.Namespace(command="status", quiet=False))
PY
```

Expected: prints the two-line update notice to stderr.

- [ ] **Open the PR** (only on explicit user approval — do not push without it).

---

## Spec coverage self-check

- Passive notifier (zero-latency, npm-style) → Tasks 1–3, 5.
- Detached worker / GitHub source reuse → Task 4 (`_latest_release_tag`, `_norm`).
- Gates (quiet/TTY/CI/env/pref/excluded/editable) → Task 2, Task 7 (pref).
- Cache file + corruption safety + atomic write → Task 1.
- Global prefs (`auto_update`, `update_notifier`) → Task 6; `jp config` routing → Task 7.
- Opt-in auto-update + next-session announcement → Task 4 (worker) + Task 3 (announcement).
- `jp changelog` (3 surfaces) → Tasks 8, 9, 10.
- Post-`jp update` "What's new" → Task 10.
- AI highlights (Gemini, minor/major, release-body only) → Tasks 11, 12.
- Professional no-emoji changelog sections → Task 13.
- Docs + opt-out → Task 13.

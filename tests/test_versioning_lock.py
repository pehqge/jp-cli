"""Tests for the cross-platform advisory process lock (jp.versioning.lock).

The lock serializes versioning mutations within a repo across processes. It is
EXCLUSIVE + NON-BLOCKING: a second acquire while held must fail fast (rather
than hang), and the lock must auto-release when the context exits or the holding
process dies (so a crash never leaves a stale, unrecoverable lock).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from jp.versioning import lock
from jp.versioning.objects import VersioningError


def _make_repo(tmp_path):
    root = tmp_path / "work"
    (root / ".jp").mkdir(parents=True)
    return root


def test_module_imports_on_any_platform():
    # The fcntl/msvcrt guard must let the module import even where one backend
    # is missing; merely importing above already exercised this, assert presence.
    assert hasattr(lock, "versioning_lock")
    assert hasattr(lock, "lock_path")


def test_lock_path(tmp_path):
    root = _make_repo(tmp_path)
    assert lock.lock_path(root) == root / ".jp" / "versioning.lock"


def test_acquire_and_release(tmp_path):
    root = _make_repo(tmp_path)
    with lock.versioning_lock(root):
        assert lock.lock_path(root).exists()
    # After the context exits the lock auto-releases; we can re-acquire.
    with lock.versioning_lock(root):
        pass


def test_reacquire_after_context_exit(tmp_path):
    root = _make_repo(tmp_path)
    for _ in range(3):
        with lock.versioning_lock(root):
            pass  # must succeed every time -- lock released on exit


def test_lock_released_on_exception(tmp_path):
    root = _make_repo(tmp_path)
    with pytest.raises(RuntimeError), lock.versioning_lock(root):
        raise RuntimeError("boom")
    # Lock must have been released despite the exception.
    with lock.versioning_lock(root):
        pass


def test_lock_file_created_0600(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX perms only")
    root = _make_repo(tmp_path)
    with lock.versioning_lock(root):
        mode = lock.lock_path(root).stat().st_mode & 0o777
    assert mode == 0o600


# A separate PROCESS holding the lock must make our acquire fail fast. We use a
# subprocess (not a second fd in-process) because POSIX flock semantics are
# per-process: two fds in one process can both "hold" an flock, but a different
# process is reliably blocked. This matches the real cross-process threat model.
_CHILD = """
import sys, time
sys.path.insert(0, {src!r})
from jp.versioning import lock
root = {root!r}
import pathlib
with lock.versioning_lock(pathlib.Path(root)):
    print("LOCKED", flush=True)
    time.sleep({hold})
"""


def test_second_process_acquire_raises_while_held(tmp_path):
    root = _make_repo(tmp_path)
    src = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src")
    code = _CHILD.format(src=src, root=str(root), hold=5)
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait until the child confirms it holds the lock.
        line = proc.stdout.readline().strip()
        assert line == "LOCKED", f"child failed to lock: {proc.stderr.read()}"
        # Now OUR acquire must fail fast (non-blocking), not hang.
        with pytest.raises(VersioningError), lock.versioning_lock(root):
            pass
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_lock_available_again_after_holder_exits(tmp_path):
    root = _make_repo(tmp_path)
    src = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src")
    code = _CHILD.format(src=src, root=str(root), hold=0.2)
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline().strip()
    assert line == "LOCKED", f"child failed to lock: {proc.stderr.read()}"
    proc.wait(timeout=10)  # child finishes its short hold and exits
    # The lock auto-released on the child's exit; we can take it now.
    with lock.versioning_lock(root):
        pass

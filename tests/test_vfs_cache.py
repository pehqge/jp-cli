"""Tests for the VFS caching layer (:mod:`jp.vfs_cache`)."""

from __future__ import annotations

import os
import threading

import pytest

from jp.remote_fs import RemoteNotFound, Stat
from jp.vfs_cache import BLOCK, CachingFS


class CountingFS:
    """Deterministic in-memory backend that records every call."""

    def __init__(self, files):  # files: {path: bytes}
        self.files = dict(files)
        self.read_calls = []  # list of (path, off, length)
        self.stat_calls = 0
        self.mtimes = dict.fromkeys(files, 1.0)

    def ping(self):
        return True

    def stat(self, path):
        self.stat_calls += 1
        if path in self.files:
            return Stat(type="file", size=len(self.files[path]), mtime=self.mtimes[path])
        raise RemoteNotFound(path)

    def listdir(self, path):
        return []

    def read(self, path, off, length):
        self.read_calls.append((path, off, length))
        if path not in self.files:
            raise RemoteNotFound(path)
        return self.files[path][off : off + length]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


def test_read_returns_exact_bytes():
    data = os.urandom(BLOCK * 3 + 1234)
    backend = CountingFS({"/f": data})
    fs = CachingFS(backend, time_fn=FakeClock())
    import random

    rng = random.Random(42)
    for _ in range(200):
        off = rng.randint(0, len(data))
        length = rng.randint(0, len(data) - off + 50)
        assert fs.read("/f", off, length) == data[off : off + length]


def test_repeat_read_is_served_from_cache():
    data = os.urandom(BLOCK * 2)
    backend = CountingFS({"/f": data})
    fs = CachingFS(backend, time_fn=FakeClock())

    fs.read("/f", 0, 100)
    n_after_first = len(backend.read_calls)
    fs.read("/f", 0, 100)
    assert len(backend.read_calls) == n_after_first
    assert fs.metrics.block_hits > 0


def test_sequential_read_triggers_prefetch():
    data = os.urandom(BLOCK * 10)
    backend = CountingFS({"/f": data})
    fs = CachingFS(backend, readahead_blocks=8, time_fn=FakeClock())

    # Read block 0, then block 1 -> sequential -> prefetch kicks in.
    fs.read("/f", 0, BLOCK)
    fs.read("/f", BLOCK, BLOCK)
    assert fs.metrics.prefetches > 0

    # Block 2 should have been prefetched: reading it must not call backend.
    n_before = len(backend.read_calls)
    out = fs.read("/f", 2 * BLOCK, BLOCK)
    assert out == data[2 * BLOCK : 3 * BLOCK]
    assert len(backend.read_calls) == n_before


def test_stat_cached_within_ttl():
    backend = CountingFS({"/f": b"hello"})
    clock = FakeClock()
    fs = CachingFS(backend, attr_ttl=1.0, time_fn=clock)

    fs.stat("/f")
    fs.stat("/f")
    assert backend.stat_calls == 1

    clock.advance(1.5)
    fs.stat("/f")
    assert backend.stat_calls == 2


def test_negative_lookup_cached():
    backend = CountingFS({"/f": b"hello"})
    fs = CachingFS(backend, attr_ttl=1.0, time_fn=FakeClock())

    with pytest.raises(RemoteNotFound):
        fs.stat("/missing")
    calls = backend.stat_calls
    with pytest.raises(RemoteNotFound):
        fs.stat("/missing")
    assert backend.stat_calls == calls
    assert fs.metrics.stat_hits >= 1


def test_mtime_change_invalidates_blocks():
    data = b"A" * (BLOCK + 10)
    backend = CountingFS({"/f": data})
    fs = CachingFS(backend, time_fn=FakeClock())

    assert fs.read("/f", 0, 10) == b"A" * 10

    new = b"B" * (BLOCK + 10)
    backend.files["/f"] = new
    backend.mtimes["/f"] = 99.0

    # stat sees the new mtime -> blocks for /f invalidated.
    fs.stat("/f")
    n_before = len(backend.read_calls)
    assert fs.read("/f", 0, 10) == b"B" * 10
    assert len(backend.read_calls) > n_before


def test_lru_eviction_respects_max_bytes():
    # 5 blocks of distinct data; cache only holds ~3 blocks.
    data = os.urandom(BLOCK * 5)
    backend = CountingFS({"/f": data})
    fs = CachingFS(backend, max_bytes=BLOCK * 3, readahead_blocks=0, time_fn=FakeClock())

    # Touch blocks 0,1,2,3,4 in order (non-sequential trick: read just 1 byte each
    # at block boundaries so prefetch is off anyway).
    for b in range(5):
        fs.read("/f", b * BLOCK, 1)

    assert fs._cached_bytes <= BLOCK * 3

    # Block 0 should have been evicted (oldest); re-reading it hits the backend.
    n_before = len(backend.read_calls)
    fs.read("/f", 0, 1)
    assert len(backend.read_calls) > n_before


def test_eof_short_read():
    data = b"short"
    backend = CountingFS({"/f": data})
    fs = CachingFS(backend, time_fn=FakeClock())

    assert fs.read("/f", 0, 1000) == b"short"
    assert fs.read("/f", 2, 1000) == b"ort"
    assert fs.read("/f", 100, 10) == b""


def test_ping_passthrough():
    backend = CountingFS({})
    fs = CachingFS(backend, time_fn=FakeClock())
    assert fs.ping() is True


def test_listdir_cached_within_ttl():
    backend = CountingFS({"/f": b"x"})
    clock = FakeClock()
    fs = CachingFS(backend, attr_ttl=1.0, time_fn=clock)

    calls = {"n": 0}
    orig = backend.listdir

    def counting_listdir(path):
        calls["n"] += 1
        return orig(path)

    backend.listdir = counting_listdir

    fs.listdir("/d")
    fs.listdir("/d")
    assert calls["n"] == 1
    clock.advance(2.0)
    fs.listdir("/d")
    assert calls["n"] == 2


def test_invalidate_drops_caches():
    data = os.urandom(BLOCK)
    backend = CountingFS({"/f": data})
    fs = CachingFS(backend, time_fn=FakeClock())

    fs.read("/f", 0, 100)
    fs.invalidate("/f")
    n_before = len(backend.read_calls)
    fs.read("/f", 0, 100)
    assert len(backend.read_calls) > n_before


class WritableCountingFS(CountingFS):
    """CountingFS plus the mutating surface, recording writes."""

    def __init__(self, files):
        super().__init__(files)
        self.writes = []  # list of (path, bytes)

    def write(self, path, data):
        self.writes.append((path, data))
        self.files[path] = bytes(data)
        self.mtimes[path] = self.mtimes.get(path, 1.0) + 1.0
        return len(data)


class ThreadSafeCountingFS:
    """Backend whose own bookkeeping is locked, so the test isolates the
    cache's locking rather than racing the backend's counters.

    ``read``/``stat`` are deterministic and depend only on their arguments, so
    any correct return value is independent of concurrency -- the cache is the
    only place where shared mutable state could be corrupted.
    """

    def __init__(self, files):
        self.files = dict(files)
        self.mtimes = dict.fromkeys(files, 1.0)
        self._lock = threading.Lock()
        self.read_calls = 0
        self.stat_calls = 0

    def ping(self):
        return True

    def stat(self, path):
        with self._lock:
            self.stat_calls += 1
        if path in self.files:
            return Stat(type="file", size=len(self.files[path]), mtime=self.mtimes[path])
        raise RemoteNotFound(path)

    def listdir(self, path):
        return []

    def read(self, path, off, length):
        with self._lock:
            self.read_calls += 1
        if path not in self.files:
            raise RemoteNotFound(path)
        return self.files[path][off : off + length]


def test_concurrent_reads_threadsafe():
    n_files = 6
    files = {}
    for i in range(n_files):
        files[f"/f{i}"] = os.urandom(BLOCK * 3 + 17 * (i + 1))
    backend = ThreadSafeCountingFS(files)
    # A cache far smaller than the working set forces constant LRU eviction and
    # block-dropping, so the eviction/drop loops (which iterate the block dict)
    # run concurrently with inserts -- the classic "dict mutated during
    # iteration" / lost-byte-counter corruption the cache lock must prevent.
    fs = CachingFS(backend, max_bytes=BLOCK * 4, time_fn=FakeClock())

    n_threads = 16
    iters = 60
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []
    errors_lock = threading.Lock()

    def worker(tid: int) -> None:
        import random

        rng = random.Random(tid)
        try:
            barrier.wait()
            for _ in range(iters):
                # Mix of distinct and overlapping ranges across all files.
                name = f"/f{rng.randint(0, n_files - 1)}"
                data = files[name]
                off = rng.randint(0, len(data))
                length = rng.randint(0, len(data) - off + 50)
                got = fs.read(name, off, length)
                assert got == data[off : off + length], (name, off, length)
                if rng.random() < 0.2:
                    st = fs.stat(name)
                    assert st.size == len(data)
                if rng.random() < 0.1:
                    # Concurrent invalidate iterates+mutates the block/attr dicts.
                    fs.invalidate(name)
        except Exception as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors[:5]

    # Metrics must be internally consistent and non-negative after the storm.
    m = fs.metrics
    for field in (
        m.block_hits,
        m.block_misses,
        m.backend_reads,
        m.bytes_from_cache,
        m.bytes_from_backend,
        m.prefetches,
        m.stat_hits,
        m.stat_misses,
    ):
        assert field >= 0, m
    # Every distinct block touched produced at least one miss; total accesses
    # account for both hits and misses with no lost counts corrupting the sum.
    assert m.block_hits + m.block_misses >= m.block_misses >= 1
    # Cached-byte bookkeeping never goes negative and respects the cap.
    assert fs._cached_bytes >= 0
    assert fs._cached_bytes <= fs._max_bytes


def test_write_through_invalidates():
    backend = WritableCountingFS({"/f": b"old-bytes"})
    fs = CachingFS(backend, time_fn=FakeClock())

    # Read once to populate the block cache.
    assert fs.read("/f", 0, 9) == b"old-bytes"
    reads_before = len(backend.read_calls)

    # Write new bytes through the cache.
    fs.write("/f", b"new!")
    assert backend.writes == [("/f", b"new!")]
    assert backend.files["/f"] == b"new!"

    # A subsequent read must return the NEW bytes (cache was invalidated),
    # which forces a fresh backend read.
    assert fs.read("/f", 0, 4) == b"new!"
    assert len(backend.read_calls) > reads_before

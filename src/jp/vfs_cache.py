"""A caching VFS layer that fronts a remote read-only filesystem.

:class:`CachingFS` wraps any object exposing the ``RemoteFS`` surface
(``stat`` / ``listdir`` / ``read`` / ``ping``, raising :class:`RemoteNotFound`
and :class:`RemoteAccessDenied` from :mod:`jp.remote_fs`) and makes the mount
feel native by adding:

* a fixed block (1 MiB) cache evicted LRU by total bytes,
* sequential read-ahead prefetch,
* short-TTL attribute and directory caches,
* a negative-lookup cache,
* mtime-based and explicit cache invalidation, and
* metrics for a stats dashboard.

It exposes the same surface, so it drops in transparently in front of the
WebDAV server.
"""

from __future__ import annotations

import contextlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from .remote_fs import Entry, RemoteNotFound, Stat

BLOCK = 1024 * 1024  # 1 MiB


@dataclass
class CacheMetrics:
    block_hits: int = 0
    block_misses: int = 0
    backend_reads: int = 0
    bytes_from_cache: int = 0
    bytes_from_backend: int = 0
    prefetches: int = 0
    stat_hits: int = 0
    stat_misses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.block_hits + self.block_misses
        if total == 0:
            return 0.0
        return self.block_hits / total


class CachingFS:
    """Caching wrapper around a ``RemoteFS``-like backend."""

    def __init__(
        self,
        fs,
        *,
        max_bytes: int = 256 * 1024 * 1024,
        attr_ttl: float = 1.0,
        readahead_blocks: int = 8,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._fs = fs
        self._max_bytes = max_bytes
        self._attr_ttl = attr_ttl
        self._readahead = readahead_blocks
        self._time = time_fn or time.monotonic

        # Block cache: (path, blockno) -> bytes. The OrderedDict doubles as the
        # LRU ordering (oldest first).
        self._blocks: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._cached_bytes = 0
        # Per-path mtime stamp captured when blocks were cached; a changed mtime
        # drops that path's blocks.
        self._block_mtime: dict[str, float] = {}
        # Per-path end offset of the previous read, for sequential detection.
        self._last_read_end: dict[str, int] = {}

        # Attribute cache: path -> (stat, expiry). A negative entry stores None.
        self._attr: dict[str, tuple[Stat | None, float]] = {}
        # Directory cache: path -> (entries, expiry).
        self._dirs: dict[str, tuple[list, float]] = {}

        self.metrics = CacheMetrics()

    # -- internal block helpers ------------------------------------------

    def _drop_path_blocks(self, path: str) -> None:
        keys = [k for k in self._blocks if k[0] == path]
        for k in keys:
            self._cached_bytes -= len(self._blocks.pop(k))
        self._block_mtime.pop(path, None)
        self._last_read_end.pop(path, None)

    def _evict(self) -> None:
        while self._cached_bytes > self._max_bytes and self._blocks:
            _, data = self._blocks.popitem(last=False)
            self._cached_bytes -= len(data)

    def _store_block(self, path: str, blockno: int, data: bytes) -> None:
        key = (path, blockno)
        if key in self._blocks:
            self._cached_bytes -= len(self._blocks.pop(key))
        self._blocks[key] = data
        self._cached_bytes += len(data)
        self._evict()

    def _fetch_block(self, path: str, blockno: int) -> bytes:
        self.metrics.backend_reads += 1
        data = self._fs.read(path, blockno * BLOCK, BLOCK)
        self.metrics.bytes_from_backend += len(data)
        self._store_block(path, blockno, data)
        return data

    def _get_block(self, path: str, blockno: int) -> bytes:
        """Return a cached block (touch LRU) or fetch it (miss)."""
        key = (path, blockno)
        cached = self._blocks.get(key)
        if cached is not None:
            self._blocks.move_to_end(key)
            self.metrics.block_hits += 1
            return cached
        self.metrics.block_misses += 1
        return self._fetch_block(path, blockno)

    # -- public surface --------------------------------------------------

    def read(self, path: str, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""

        # Establish a baseline mtime stamp for this path the first time we cache
        # any of its blocks, so a later stat() that sees a different mtime can
        # invalidate them. Prefer an already-cached attr to avoid a round-trip;
        # otherwise read mtime straight from the backend without populating the
        # TTL attr cache (so a subsequent stat() still observes a real change).
        if path not in self._block_mtime:
            attr = self._attr.get(path)
            if attr is not None and attr[0] is not None:
                self._block_mtime[path] = attr[0].mtime
            else:
                with contextlib.suppress(RemoteNotFound):
                    self._block_mtime[path] = self._fs.stat(path).mtime

        first = offset // BLOCK
        last = (offset + length - 1) // BLOCK

        out = bytearray()
        eof = False
        had_miss = False
        for blockno in range(first, last + 1):
            if (path, blockno) not in self._blocks:
                had_miss = True
            data = self._get_block(path, blockno)

            # Slice the portion of this block that falls inside the request.
            block_start = blockno * BLOCK
            lo = max(offset, block_start) - block_start
            hi = min(offset + length, block_start + BLOCK) - block_start
            piece = data[lo:hi]
            out += piece
            self.metrics.bytes_from_cache += len(piece)
            if len(data) < BLOCK:
                eof = True
                break

        # Sequential read-ahead: if this read starts exactly where the previous
        # read for this path ended AND we actually had to fetch something,
        # prefetch the next blocks. Skipping prefetch on an all-hit read keeps a
        # re-read of already-prefetched blocks from re-touching the backend.
        prev_end = self._last_read_end.get(path)
        end = offset + len(out)
        if (
            not eof
            and had_miss
            and self._readahead > 0
            and prev_end is not None
            and prev_end == offset
        ):
            for blockno in range(last + 1, last + 1 + self._readahead):
                if (path, blockno) in self._blocks:
                    continue
                data = self._fetch_block(path, blockno)
                self.metrics.prefetches += 1
                if len(data) < BLOCK:
                    break  # hit EOF, stop prefetching
        self._last_read_end[path] = end

        return bytes(out)

    def stat(self, path: str) -> Stat:
        now = self._time()
        entry = self._attr.get(path)
        if entry is not None and entry[1] > now:
            self.metrics.stat_hits += 1
            st = entry[0]
            if st is None:
                raise RemoteNotFound(path)
            return st

        self.metrics.stat_misses += 1
        try:
            st = self._fs.stat(path)
        except RemoteNotFound:
            self._attr[path] = (None, now + self._attr_ttl)
            raise

        # If mtime changed for a file we have cached blocks for, invalidate them.
        old = self._block_mtime.get(path)
        if old is not None and old != st.mtime:
            self._drop_path_blocks(path)
        self._block_mtime[path] = st.mtime

        self._attr[path] = (st, now + self._attr_ttl)
        return st

    def listdir(self, path: str) -> list:
        now = self._time()
        entry = self._dirs.get(path)
        if entry is not None and entry[1] > now:
            return entry[0]
        entries = self._fs.listdir(path)
        self._dirs[path] = (entries, now + self._attr_ttl)
        return entries

    def invalidate(self, path: str) -> None:
        """Drop all cached state for ``path`` (and, if a dir, its children)."""
        self._drop_path_blocks(path)
        self._attr.pop(path, None)
        self._dirs.pop(path, None)

        prefix = path.rstrip("/") + "/"
        for bk in [bk for bk in self._blocks if bk[0].startswith(prefix)]:
            self._cached_bytes -= len(self._blocks.pop(bk))
        for d in (self._attr, self._dirs):
            for sk in [sk for sk in d if sk.startswith(prefix)]:
                d.pop(sk, None)
        for m in (self._block_mtime, self._last_read_end):
            for mk in [mk for mk in m if mk == path or mk.startswith(prefix)]:
                m.pop(mk, None)

    def ping(self) -> bool:
        return self._fs.ping()


__all__ = ["BLOCK", "CacheMetrics", "CachingFS", "Entry"]

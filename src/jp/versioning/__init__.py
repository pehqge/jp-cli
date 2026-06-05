"""Git-like versioning primitives for jp (content store, refs, commits, ...).

This package builds a local, content-addressed history under ``.jp/`` so a
working tree can be snapshotted and restored without a remote round-trip. The
foundational layer is :mod:`jp.versioning.objects` -- an immutable,
write-once, sha256-addressed object store with mandatory read-time integrity
verification. Everything else (refs, trees, commits, staging) is layered on
top of that primitive.

All modules here inherit the project's paranoid threat model: a SHARED,
multi-user machine with a possibly hostile filesystem. Writes are atomic and
refuse to follow symlinks; reads re-verify content hashes; and any value used
to compose a filesystem path is validated before use (see paths.py).
"""

from __future__ import annotations

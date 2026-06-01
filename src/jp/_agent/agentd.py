"""jp remote agent -- runs INSIDE a Jupyter kernel. PURE STDLIB ONLY.

This module is imported by tests (so its logic is unit-tested directly) AND its
source text is injected into a kernel at runtime (see jp.agent_loader). It must
therefore import nothing from ``jp`` and nothing third-party.

PHASE 1: READ-ONLY. It can ``ping``/``stat``/``readdir``/``read`` under a single
configured root, and it refuses -- via an independent realpath jail -- any path
that escapes that root, is absolute, hidden, or reached through an escaping
symlink. There is deliberately NO write/delete/rename code path (Safety Charter
rule 2; enforced by tests/test_agentd.py::test_agent_source_has_no_write_syscalls).
"""

from __future__ import annotations

import os
import posixpath

# Mirror jp.fsrpc constants so the agent needs no jp import when injected.
OP_PING, OP_STAT, OP_READDIR, OP_READ = "ping", "stat", "readdir", "read"
E_NOENT, E_NOTDIR, E_ISDIR, E_ACCES, E_IO = "ENOENT", "ENOTDIR", "EISDIR", "EACCES", "EIO"

# Read in capped chunks so a single op can never balloon memory (defense in
# depth; the client also bounds ``length``).
MAX_READ = 8 * 1024 * 1024


class JailError(Exception):
    pass


class Agent:
    def __init__(self, root: str) -> None:
        # The one directory the agent may ever touch. Resolve it once.
        self.root = os.path.realpath(root)

    # --- jail ---------------------------------------------------------------
    def _resolve(self, rel: str) -> str:
        """Resolve a client-relative path to an absolute path INSIDE root.

        Rejects absolute input, hidden components, ``..`` escape, and symlinks
        whose real target leaves root. Raises :class:`JailError` on any refusal.
        """
        rel = str(rel).replace("\\", "/")
        if rel.startswith("/"):
            raise JailError("absolute path")
        norm = posixpath.normpath(rel) if rel not in ("", ".") else ""
        if norm.startswith("..") or "/.." in norm:
            raise JailError("path escapes root")
        for part in norm.split("/"):
            if part.startswith("."):
                raise JailError("hidden component")
        target = os.path.realpath(os.path.join(self.root, norm))
        if target != self.root and not target.startswith(self.root + os.sep):
            raise JailError("resolved path escapes root")
        return target

    # --- dispatch -----------------------------------------------------------
    def handle(self, req: dict) -> tuple[dict, list[bytes]]:
        """Return (response_dict, buffers). Never raises; errors become responses."""
        rid = req.get("rid")
        op = req.get("op")
        try:
            if op == OP_PING:
                return {"rid": rid, "ok": True}, []
            if op == OP_STAT:
                return self._stat(rid, req["path"]), []
            if op == OP_READDIR:
                return self._readdir(rid, req["path"]), []
            if op == OP_READ:
                return self._read(rid, req["path"], int(req["offset"]), int(req["length"]))
            return self._err(rid, E_IO, f"unknown op {op!r}"), []
        except JailError as exc:
            return self._err(rid, E_ACCES, str(exc)), []
        except FileNotFoundError:
            return self._err(rid, E_NOENT, "no such file or directory"), []
        except NotADirectoryError:
            return self._err(rid, E_NOTDIR, "not a directory"), []
        except IsADirectoryError:
            return self._err(rid, E_ISDIR, "is a directory"), []
        except Exception as exc:  # never leak a traceback to the wire
            return self._err(rid, E_IO, type(exc).__name__), []

    # --- ops ----------------------------------------------------------------
    def _stat(self, rid, path) -> dict:
        target = self._resolve(path)
        st = os.lstat(target)  # lstat: a symlink itself is reported, not followed
        is_dir = os.path.isdir(target) and not os.path.islink(target)
        return {
            "rid": rid,
            "ok": True,
            "type": "directory" if is_dir else "file",
            "size": 0 if is_dir else st.st_size,
            "mtime": st.st_mtime,
        }

    def _readdir(self, rid, path) -> dict:
        target = self._resolve(path)
        if not os.path.isdir(target):
            return self._err(rid, E_NOTDIR, "not a directory")
        entries = []
        with os.scandir(target) as it:
            for de in it:
                if de.name.startswith("."):
                    continue  # hidden: the server hides them anyway (research §2.1)
                entries.append(
                    {
                        "name": de.name,
                        "type": "directory" if de.is_dir(follow_symlinks=False) else "file",
                        "size": 0
                        if de.is_dir(follow_symlinks=False)
                        else de.stat(follow_symlinks=False).st_size,
                    }
                )
        return {"rid": rid, "ok": True, "entries": entries}

    def _read(self, rid, path, offset, length) -> tuple[dict, list[bytes]]:
        target = self._resolve(path)
        if os.path.isdir(target):
            return self._err(rid, E_ISDIR, "is a directory"), []
        length = max(0, min(int(length), MAX_READ))
        fd = os.open(target, os.O_RDONLY)
        try:
            data = os.pread(fd, length, max(0, int(offset)))
        finally:
            os.close(fd)
        return {"rid": rid, "ok": True, "size": len(data)}, [data]

    @staticmethod
    def _err(rid, code, message) -> dict:
        return {"rid": rid, "ok": False, "code": code, "message": message}

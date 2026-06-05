"""jp remote agent -- runs INSIDE a Jupyter kernel. PURE STDLIB ONLY.

This module is imported by tests (so its logic is unit-tested directly) AND its
source text is injected into a kernel at runtime (see jp.agent_loader). It must
therefore import nothing from ``jp`` and nothing third-party.

It can ``ping``/``stat``/``readdir``/``read`` under a single configured root, and
it refuses -- via an independent realpath jail -- any path that escapes that
root, is absolute, hidden, or reached through an escaping symlink.

WRITE ops exist in this source but are GATED BEHAVIORALLY: an Agent is read-only
unless constructed with ``writable=True``. A read-only agent provably refuses
EVERY write op with EROFS (tests/test_agentd.py::test_readonly_agent_refuses_all_writes).
Writes are whole-file and atomic (temp file in the same dir + fsync + os.replace),
jailed (same realpath jail), and refuse to write through a symlink. Removals use
os.remove/os.rmdir -- never a server-side Contents API DELETE.
"""

from __future__ import annotations

import contextlib
import errno
import os
import posixpath
import subprocess

# Mirror jp.fsrpc constants so the agent needs no jp import when injected.
OP_PING, OP_STAT, OP_READDIR, OP_READ = "ping", "stat", "readdir", "read"
OP_STATMACHINE = "statmachine"
OP_WRITE, OP_MKDIR, OP_RENAME, OP_UNLINK, OP_RMDIR = (
    "write",
    "mkdir",
    "rename",
    "unlink",
    "rmdir",
)
E_NOENT, E_NOTDIR, E_ISDIR, E_ACCES, E_IO = "ENOENT", "ENOTDIR", "EISDIR", "EACCES", "EIO"
E_EXIST, E_NOTEMPTY, E_ROFS = "EEXIST", "ENOTEMPTY", "EROFS"

# Read in capped chunks so a single op can never balloon memory (defense in
# depth; the client also bounds ``length``).
MAX_READ = 8 * 1024 * 1024


def parse_meminfo(text: str) -> dict:
    """Parse /proc/meminfo -> {'mem_total_kb', 'mem_available_kb'} (missing keys omitted)."""
    out = {}
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            out["mem_total_kb"] = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            out["mem_available_kb"] = int(line.split()[1])
    return out


def parse_nvidia_smi(text: str) -> list:
    """Parse nvidia-smi csv (name,memory.used,memory.total,utilization.gpu) -> list of dicts."""
    gpus = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 4:
            continue
        name, used, total, util = cols[0], cols[1], cols[2], cols[3]
        gpu: dict = {"name": name}
        try:
            gpu["mem_used_mb"] = int(float(used))
            gpu["mem_total_mb"] = int(float(total))
            gpu["util_pct"] = int(float(util))
        except ValueError:
            pass
        gpus.append(gpu)
    return gpus


class JailError(Exception):
    pass


class ReadOnlyError(Exception):
    """Raised when a write op is attempted on a read-only agent."""


class Agent:
    def __init__(self, root: str, *, writable: bool = False) -> None:
        # The one directory the agent may ever touch. Resolve it once.
        self.root = os.path.realpath(root)
        # Writes are refused unless explicitly enabled (default read-only).
        self.writable = bool(writable)

    def _require_writable(self) -> None:
        if not self.writable:
            raise ReadOnlyError("agent is read-only")

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

    def _resolve_mutable(self, rel: str) -> str:
        """Resolve for a MUTATING op, refusing the jail root itself.

        Reads of the root (stat/readdir/read) stay legitimate via ``_resolve``;
        but mutating the root -- rmdir("") would delete the configured prefix,
        write("") would create a temp file in ``dirname(root)`` OUTSIDE the jail
        -- is always refused.
        """
        target = self._resolve(rel)
        if target == self.root:
            raise JailError("refusing to mutate the jail root itself")
        return target

    # --- dispatch -----------------------------------------------------------
    def handle(self, req: dict, buffers: list[bytes] | None = None) -> tuple[dict, list[bytes]]:
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
            if op == OP_STATMACHINE:
                return self._statmachine(rid), []
            if op == OP_WRITE:
                data = (buffers or [b""])[0]
                return self._write(rid, req["path"], data), []
            if op == OP_MKDIR:
                return self._mkdir(rid, req["path"]), []
            if op == OP_RENAME:
                return self._rename(rid, req["src"], req["dst"]), []
            if op == OP_UNLINK:
                return self._unlink(rid, req["path"]), []
            if op == OP_RMDIR:
                return self._rmdir(rid, req["path"]), []
            return self._err(rid, E_IO, f"unknown op {op!r}"), []
        except ReadOnlyError as exc:
            return self._err(rid, E_ROFS, str(exc)), []
        except JailError as exc:
            return self._err(rid, E_ACCES, str(exc)), []
        except FileExistsError:
            return self._err(rid, E_EXIST, "file exists"), []
        except FileNotFoundError:
            return self._err(rid, E_NOENT, "no such file or directory"), []
        except NotADirectoryError:
            return self._err(rid, E_NOTDIR, "not a directory"), []
        except IsADirectoryError:
            return self._err(rid, E_ISDIR, "is a directory"), []
        except OSError as exc:
            if exc.errno == errno.ENOTEMPTY:
                return self._err(rid, E_NOTEMPTY, "directory not empty"), []
            return self._err(rid, E_IO, type(exc).__name__), []
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
            # os.pread is POSIX-only; lseek+read is portable (the agent normally
            # runs on a Linux server, but the test suite exercises it on Windows).
            # os.read may return fewer bytes than asked, so loop until we have the
            # full length or hit EOF -- matching pread's regular-file behavior.
            os.lseek(fd, max(0, int(offset)), os.SEEK_SET)
            parts: list[bytes] = []
            remaining = length
            while remaining > 0:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                parts.append(chunk)
                remaining -= len(chunk)
            data = b"".join(parts)
        finally:
            os.close(fd)
        return {"rid": rid, "ok": True, "size": len(data)}, [data]

    # --- write ops (gated by ``writable``) ----------------------------------
    def _write(self, rid, path, data: bytes) -> dict:
        """Atomic whole-file write into an EXISTING parent dir, never via a symlink."""
        self._require_writable()
        # _resolve realpaths the existing prefix; a not-yet-existing leaf is fine.
        # The guard runs BEFORE any open(), so write("") never leaks a temp file.
        target = self._resolve_mutable(path)
        # Refuse to clobber a symlink (would write through to its target).
        if os.path.islink(target):
            raise JailError("refusing to write through a symlink")
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            raise FileNotFoundError(parent)
        tmp = os.path.join(parent, f".jp-tmp-{rid}-{os.getpid()}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            written = 0
            view = memoryview(data)
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise
        return {"rid": rid, "ok": True, "size": len(data)}

    def _mkdir(self, rid, path) -> dict:
        self._require_writable()
        target = self._resolve_mutable(path)
        os.mkdir(target)  # parent must exist; FileExistsError -> EEXIST
        return {"rid": rid, "ok": True}

    def _rename(self, rid, src, dst) -> dict:
        self._require_writable()
        # BOTH endpoints must resolve inside root (and neither may BE the root).
        src_t = self._resolve_mutable(src)
        dst_t = self._resolve_mutable(dst)
        if os.path.islink(dst_t):
            raise JailError("refusing to rename onto a symlink")
        os.rename(src_t, dst_t)
        return {"rid": rid, "ok": True}

    def _unlink(self, rid, path) -> dict:
        self._require_writable()
        target = self._resolve_mutable(path)
        if os.path.isdir(target) and not os.path.islink(target):
            return self._err(rid, E_ISDIR, "is a directory")
        os.remove(target)  # removes a file, or the symlink entry (not its target)
        return {"rid": rid, "ok": True}

    def _rmdir(self, rid, path) -> dict:
        self._require_writable()
        target = self._resolve_mutable(path)
        os.rmdir(target)  # never recursive; ENOTEMPTY -> E_NOTEMPTY
        return {"rid": rid, "ok": True}

    def _statmachine(self, rid) -> dict:
        """Read-only snapshot of host stats. Every source degrades gracefully."""
        machine: dict = {}

        machine["cpu_count"] = os.cpu_count()

        try:
            with open("/proc/meminfo") as fh:
                machine.update(parse_meminfo(fh.read()))
        except OSError:
            pass

        try:
            st = os.statvfs(self.root)
            machine["disk_total_bytes"] = st.f_frsize * st.f_blocks
            machine["disk_free_bytes"] = st.f_frsize * st.f_bavail
        except (OSError, AttributeError):  # statvfs absent on Windows
            pass

        gpus: list = []
        try:
            proc = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                gpus = parse_nvidia_smi(proc.stdout)
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            gpus = []
        machine["gpus"] = gpus

        return {"rid": rid, "ok": True, "machine": machine}

    @staticmethod
    def _err(rid, code, message) -> dict:
        return {"rid": rid, "ok": False, "code": code, "message": message}

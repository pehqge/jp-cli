"""Client-side filesystem view over a :class:`jp.kernel_conn.KernelConn`.

Translates fsrpc replies into dataclasses and error codes into exceptions, and
stitches large reads from multiple agent chunks (the agent caps each at
``MAX_READ``). This is the API the local mount daemon calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import fsrpc
from .kernel_conn import KernelConn

# Must match jp._agent.agentd.MAX_READ.
CHUNK = 8 * 1024 * 1024


class RemoteFsError(Exception):
    pass


class RemoteNotFound(RemoteFsError):
    pass


class RemoteAccessDenied(RemoteFsError):
    pass


class RemoteReadOnly(RemoteFsError):
    pass


class RemoteExists(RemoteFsError):
    pass


class RemoteNotEmpty(RemoteFsError):
    pass


@dataclass
class Stat:
    type: str  # "file" | "directory"
    size: int
    mtime: float


@dataclass
class Entry:
    name: str
    type: str
    size: int


_EXC = {
    fsrpc.E_NOENT: RemoteNotFound,
    fsrpc.E_ACCES: RemoteAccessDenied,
    fsrpc.E_ROFS: RemoteReadOnly,
    fsrpc.E_EXIST: RemoteExists,
    fsrpc.E_NOTEMPTY: RemoteNotEmpty,
}


def _check(resp: dict) -> dict:
    if resp.get("ok"):
        return resp
    code = str(resp.get("code") or "")
    message = str(resp.get("message") or code or "error")
    raise _EXC.get(code, RemoteFsError)(message)


class RemoteFS:
    def __init__(self, conn: KernelConn) -> None:
        self._conn = conn

    def ping(self) -> bool:
        resp, _ = self._conn.call(fsrpc.OP_PING)
        return bool(resp.get("ok"))

    def stat(self, path: str) -> Stat:
        resp, _ = self._conn.call(fsrpc.OP_STAT, path=path)
        _check(resp)
        return Stat(type=resp["type"], size=resp["size"], mtime=resp.get("mtime", 0.0))

    def listdir(self, path: str) -> list[Entry]:
        resp, _ = self._conn.call(fsrpc.OP_READDIR, path=path)
        _check(resp)
        return [
            Entry(name=e["name"], type=e["type"], size=e.get("size", 0)) for e in resp["entries"]
        ]

    def statmachine(self) -> dict:
        resp, _ = self._conn.call(fsrpc.OP_STATMACHINE)
        _check(resp)
        return resp.get("machine", {})

    def read(self, path: str, offset: int, length: int) -> bytes:
        out = bytearray()
        remaining = length
        pos = offset
        while remaining > 0:
            want = min(remaining, CHUNK)
            resp, buffers = self._conn.call(fsrpc.OP_READ, path=path, offset=pos, length=want)
            _check(resp)
            chunk = buffers[0] if buffers else b""
            if not chunk:
                break  # EOF
            out += chunk
            pos += len(chunk)
            remaining -= len(chunk)
        return bytes(out)

    # --- write ops (refused with RemoteReadOnly by a read-only agent) --------
    def write(self, path: str, data: bytes) -> int:
        resp, _ = self._conn.call(fsrpc.OP_WRITE, path=path, buffers=[data])
        _check(resp)
        return int(resp.get("size", 0))

    def mkdir(self, path: str) -> None:
        resp, _ = self._conn.call(fsrpc.OP_MKDIR, path=path)
        _check(resp)

    def rename(self, src: str, dst: str) -> None:
        resp, _ = self._conn.call(fsrpc.OP_RENAME, src=src, dst=dst)
        _check(resp)

    def unlink(self, path: str) -> None:
        resp, _ = self._conn.call(fsrpc.OP_UNLINK, path=path)
        _check(resp)

    def rmdir(self, path: str) -> None:
        resp, _ = self._conn.call(fsrpc.OP_RMDIR, path=path)
        _check(resp)

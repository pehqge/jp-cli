"""The filesystem-RPC wire contract shared by the local client and the remote
agent. Pure data shapes -- no behavior -- so both ends provably agree.

Read ops are always available. Write ops (Phase 4) also exist here but are gated
BEHAVIORALLY by the agent: a read-only agent refuses every write with EROFS.
"""

from __future__ import annotations

from typing import Any

# Read-only ops.
OP_PING = "ping"
OP_STAT = "stat"
OP_READDIR = "readdir"
OP_READ = "read"
OP_STATMACHINE = "statmachine"

# Write ops (gated behaviorally by the agent's ``writable`` flag).
OP_WRITE = "write"
OP_MKDIR = "mkdir"
OP_RENAME = "rename"
OP_UNLINK = "unlink"
OP_RMDIR = "rmdir"

# Error codes (POSIX-flavoured, transport-neutral).
E_NOENT = "ENOENT"  # no such file/dir
E_NOTDIR = "ENOTDIR"  # readdir on a file
E_ISDIR = "EISDIR"  # read on a directory
E_ACCES = "EACCES"  # jail/permission refusal
E_IO = "EIO"  # unexpected server-side error
E_EXIST = "EEXIST"  # target already exists (mkdir)
E_NOTEMPTY = "ENOTEMPTY"  # rmdir on a non-empty directory
E_ROFS = "EROFS"  # write attempted on a read-only agent


def request(op: str, *, rid: int, **fields: Any) -> dict[str, Any]:
    return {"rid": rid, "op": op, **fields}


def ok(*, rid: int, **fields: Any) -> dict[str, Any]:
    return {"rid": rid, "ok": True, **fields}


def error(*, rid: int, code: str, message: str) -> dict[str, Any]:
    return {"rid": rid, "ok": False, "code": code, "message": message}

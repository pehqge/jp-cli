"""The filesystem-RPC wire contract shared by the local client and the remote
agent. Pure data shapes -- no behavior -- so both ends provably agree.

PHASE 1 IS READ-ONLY. No write/delete/rename op exists here yet (Safety Charter
rule 2). Write ops are added in Phase 4 behind explicit gates.
"""

from __future__ import annotations

from typing import Any

# Read-only ops (Phase 1).
OP_PING = "ping"
OP_STAT = "stat"
OP_READDIR = "readdir"
OP_READ = "read"
OP_STATMACHINE = "statmachine"

# Error codes (POSIX-flavoured, transport-neutral).
E_NOENT = "ENOENT"  # no such file/dir
E_NOTDIR = "ENOTDIR"  # readdir on a file
E_ISDIR = "EISDIR"  # read on a directory
E_ACCES = "EACCES"  # jail/permission refusal
E_IO = "EIO"  # unexpected server-side error


def request(op: str, *, rid: int, **fields: Any) -> dict[str, Any]:
    return {"rid": rid, "op": op, **fields}


def ok(*, rid: int, **fields: Any) -> dict[str, Any]:
    return {"rid": rid, "ok": True, **fields}


def error(*, rid: int, code: str, message: str) -> dict[str, Any]:
    return {"rid": rid, "ok": False, "code": code, "message": message}

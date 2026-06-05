from jp import fsrpc


def test_op_constants_present():
    # Read-only ops still equal their strings.
    assert fsrpc.OP_PING == "ping"
    assert fsrpc.OP_STAT == "stat"
    assert fsrpc.OP_READDIR == "readdir"
    assert fsrpc.OP_READ == "read"
    # Write ops now exist (gated behaviorally by the agent, not by absence).
    assert fsrpc.OP_WRITE == "write"
    assert fsrpc.OP_MKDIR == "mkdir"
    assert fsrpc.OP_RENAME == "rename"
    assert fsrpc.OP_UNLINK == "unlink"
    assert fsrpc.OP_RMDIR == "rmdir"
    # New error codes exist.
    assert fsrpc.E_EXIST == "EEXIST"
    assert fsrpc.E_NOTEMPTY == "ENOTEMPTY"
    assert fsrpc.E_ROFS == "EROFS"


def test_request_and_ok_error_envelopes():
    req = fsrpc.request(fsrpc.OP_READ, rid=7, path="a/b", offset=0, length=10)
    assert req == {"rid": 7, "op": "read", "path": "a/b", "offset": 0, "length": 10}
    assert fsrpc.ok(rid=7, size=10) == {"rid": 7, "ok": True, "size": 10}
    err = fsrpc.error(rid=7, code=fsrpc.E_NOENT, message="missing")
    assert err == {"rid": 7, "ok": False, "code": "ENOENT", "message": "missing"}

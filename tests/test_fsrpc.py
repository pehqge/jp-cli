from jp import fsrpc


def test_op_constants_are_read_only_set_in_phase1():
    assert fsrpc.OP_PING == "ping"
    assert fsrpc.OP_STAT == "stat"
    assert fsrpc.OP_READDIR == "readdir"
    assert fsrpc.OP_READ == "read"
    # Phase 1 is read-only: write ops must NOT exist yet.
    assert not hasattr(fsrpc, "OP_WRITE")


def test_request_and_ok_error_envelopes():
    req = fsrpc.request(fsrpc.OP_READ, rid=7, path="a/b", offset=0, length=10)
    assert req == {"rid": 7, "op": "read", "path": "a/b", "offset": 0, "length": 10}
    assert fsrpc.ok(rid=7, size=10) == {"rid": 7, "ok": True, "size": 10}
    err = fsrpc.error(rid=7, code=fsrpc.E_NOENT, message="missing")
    assert err == {"rid": 7, "ok": False, "code": "ENOENT", "message": "missing"}

import os

import pytest

from jp import fsrpc
from jp._agent import agentd


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "hello.txt").write_bytes(b"hello world")
    (root / "sub" / "data.bin").write_bytes(bytes(range(256)) * 10)
    return agentd.Agent(str(root)), root


def test_ping(agent):
    a, _ = agent
    resp, buffers = a.handle(fsrpc.request(fsrpc.OP_PING, rid=1))
    assert resp["ok"] is True and resp["rid"] == 1
    assert buffers == []


def test_stat_file_and_missing(agent):
    a, _ = agent
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_STAT, rid=2, path="hello.txt"))
    assert resp["ok"] is True and resp["type"] == "file" and resp["size"] == 11
    miss, _ = a.handle(fsrpc.request(fsrpc.OP_STAT, rid=3, path="nope"))
    assert miss["ok"] is False and miss["code"] == fsrpc.E_NOENT


def test_readdir_lists_children(agent):
    a, _ = agent
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_READDIR, rid=4, path=""))
    names = {e["name"]: e["type"] for e in resp["entries"]}
    assert names == {"hello.txt": "file", "sub": "directory"}


def test_read_returns_exact_bytes_in_buffer(agent):
    a, _ = agent
    resp, buffers = a.handle(
        fsrpc.request(fsrpc.OP_READ, rid=5, path="hello.txt", offset=6, length=5)
    )
    assert resp["ok"] is True
    assert len(buffers) == 1 and buffers[0] == b"world"


def test_read_partial_random_access(agent):
    a, root = agent
    resp, buffers = a.handle(
        fsrpc.request(fsrpc.OP_READ, rid=6, path="sub/data.bin", offset=300, length=8)
    )
    expected = (root / "sub" / "data.bin").read_bytes()[300:308]
    assert buffers[0] == expected  # proves os.pread random access, no whole-file load


def test_jail_blocks_parent_traversal(agent):
    a, _ = agent
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_READ, rid=7, path="../secret", offset=0, length=1))
    assert resp["ok"] is False and resp["code"] == fsrpc.E_ACCES


def test_jail_blocks_absolute_path(agent):
    a, _ = agent
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_STAT, rid=8, path="/etc/passwd"))
    assert resp["ok"] is False and resp["code"] == fsrpc.E_ACCES


def test_jail_blocks_symlink_escape(agent, tmp_path):
    a, root = agent
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"SECRET")
    os.symlink(str(outside), str(root / "link.txt"))
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_READ, rid=9, path="link.txt", offset=0, length=6))
    assert resp["ok"] is False and resp["code"] == fsrpc.E_ACCES


def test_read_on_directory_is_eisdir(agent):
    a, _ = agent
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_READ, rid=10, path="sub", offset=0, length=1))
    assert resp["ok"] is False and resp["code"] == fsrpc.E_ISDIR


def test_agent_source_has_no_write_syscalls():
    """Safety Charter rule 2: the Phase 1 agent must contain NO write/delete path."""
    if hasattr(agentd, "SOURCE"):
        src = agentd.SOURCE
    else:
        with open(agentd.__file__) as fh:
            src = fh.read()
    for forbidden in (
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "shutil.rmtree",
        "os.pwrite",
        '"wb"',
        "'wb'",
        "os.rename",
        "send2trash",
    ):
        assert forbidden not in src, f"forbidden write primitive present: {forbidden}"

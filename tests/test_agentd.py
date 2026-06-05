import glob
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


def test_parse_meminfo():
    text = "MemTotal: 32896744 kB\nMemAvailable: 20000000 kB\n"
    assert agentd.parse_meminfo(text) == {
        "mem_total_kb": 32896744,
        "mem_available_kb": 20000000,
    }


def test_parse_nvidia_smi():
    text = "NVIDIA H100, 1024, 81920, 37\nNVIDIA H100, 0, 81920, 0"
    gpus = agentd.parse_nvidia_smi(text)
    assert len(gpus) == 2
    assert gpus[0]["util_pct"] == 37
    assert gpus[1]["util_pct"] == 0
    assert gpus[0]["name"] == "NVIDIA H100"


def test_statmachine_op_returns_machine_dict(tmp_path):
    a = agentd.Agent(str(tmp_path))
    resp, buffers = a.handle(fsrpc.request(fsrpc.OP_STATMACHINE, rid=1))
    assert resp["ok"] is True
    assert "machine" in resp
    assert "cpu_count" in resp["machine"]
    assert buffers == []


def test_readonly_agent_refuses_all_writes(tmp_path):
    """THE safety guard: a read-only agent provably refuses EVERY write with EROFS."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "f.txt").write_bytes(b"keep")
    a = agentd.Agent(str(root))  # writable defaults False
    cases = [
        (fsrpc.request(fsrpc.OP_WRITE, rid=1, path="new.txt"), [b"x"]),
        (fsrpc.request(fsrpc.OP_MKDIR, rid=2, path="newdir"), None),
        (fsrpc.request(fsrpc.OP_RENAME, rid=3, src="f.txt", dst="g.txt"), None),
        (fsrpc.request(fsrpc.OP_UNLINK, rid=4, path="f.txt"), None),
        (fsrpc.request(fsrpc.OP_RMDIR, rid=5, path="sub"), None),
    ]
    for req, bufs in cases:
        resp, _ = a.handle(req, bufs)
        assert resp["ok"] is False
        assert resp["code"] == fsrpc.E_ROFS, req["op"]
    # Nothing was touched.
    assert (root / "f.txt").read_bytes() == b"keep"
    assert (root / "sub").is_dir()
    assert not (root / "new.txt").exists()
    assert not (root / "newdir").exists()


def test_writable_agent_writes_atomically(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    a = agentd.Agent(str(root), writable=True)
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_WRITE, rid=1, path="new.txt"), [b"hello"])
    assert resp["ok"] is True and resp["size"] == 5
    assert (root / "new.txt").read_bytes() == b"hello"


def test_writable_write_refuses_symlink_dest(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"SECRET")
    os.symlink(str(outside), str(root / "link.txt"))
    a = agentd.Agent(str(root), writable=True)
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_WRITE, rid=1, path="link.txt"), [b"x"])
    assert resp["ok"] is False and resp["code"] == fsrpc.E_ACCES
    assert outside.read_bytes() == b"SECRET"  # target untouched


def test_writable_write_jails_parent_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    a = agentd.Agent(str(root), writable=True)
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_WRITE, rid=1, path="../evil.txt"), [b"x"])
    assert resp["ok"] is False and resp["code"] == fsrpc.E_ACCES
    assert not (tmp_path / "evil.txt").exists()


def test_writable_mkdir_rename_unlink_rmdir(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "file.txt").write_bytes(b"data")
    (root / "filled").mkdir()
    (root / "filled" / "inner.txt").write_bytes(b"x")
    a = agentd.Agent(str(root), writable=True)

    # mkdir happy path
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_MKDIR, rid=1, path="newdir"))
    assert resp["ok"] is True and (root / "newdir").is_dir()

    # rename happy path
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_RENAME, rid=2, src="file.txt", dst="renamed.txt"))
    assert resp["ok"] is True
    assert (root / "renamed.txt").read_bytes() == b"data" and not (root / "file.txt").exists()

    # unlink happy path
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_UNLINK, rid=3, path="renamed.txt"))
    assert resp["ok"] is True and not (root / "renamed.txt").exists()

    # rmdir happy path
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_RMDIR, rid=4, path="sub"))
    assert resp["ok"] is True and not (root / "sub").exists()

    # rmdir on non-empty dir -> ENOTEMPTY
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_RMDIR, rid=5, path="filled"))
    assert resp["ok"] is False and resp["code"] == fsrpc.E_NOTEMPTY
    assert (root / "filled").is_dir()

    # unlink on a directory -> EISDIR
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_UNLINK, rid=6, path="filled"))
    assert resp["ok"] is False and resp["code"] == fsrpc.E_ISDIR
    assert (root / "filled").is_dir()


def test_writable_refuses_mutating_root(tmp_path):
    """Issues #1 + #2: mutating the jail root itself is refused with EACCES, and
    no temp file leaks into the root's PARENT (outside the jail)."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.txt").write_bytes(b"data")
    a = agentd.Agent(str(root), writable=True)

    cases = [
        (fsrpc.request(fsrpc.OP_WRITE, rid=1, path=""), [b"x"]),
        (fsrpc.request(fsrpc.OP_MKDIR, rid=2, path=""), None),
        (fsrpc.request(fsrpc.OP_RMDIR, rid=3, path=""), None),
        (fsrpc.request(fsrpc.OP_UNLINK, rid=4, path=""), None),
        (fsrpc.request(fsrpc.OP_RENAME, rid=5, src="real.txt", dst=""), None),
    ]
    for req, bufs in cases:
        resp, _ = a.handle(req, bufs)
        assert resp["ok"] is False, req["op"]
        assert resp["code"] == fsrpc.E_ACCES, req["op"]

    # The root dir still exists and was not mutated.
    assert root.is_dir()
    assert (root / "real.txt").read_bytes() == b"data"
    # No temp file was ever created in the root's PARENT (outside the jail).
    assert glob.glob(os.path.join(str(tmp_path), ".jp-tmp*")) == []

    # Reads of the root itself stay legitimate.
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_STAT, rid=6, path=""))
    assert resp["ok"] is True and resp["type"] == "directory"
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_READDIR, rid=7, path=""))
    assert resp["ok"] is True


def test_rename_dst_must_be_in_jail(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.txt").write_bytes(b"data")
    a = agentd.Agent(str(root), writable=True)
    resp, _ = a.handle(fsrpc.request(fsrpc.OP_RENAME, rid=1, src="real.txt", dst="../escape"))
    assert resp["ok"] is False and resp["code"] == fsrpc.E_ACCES
    assert (root / "real.txt").read_bytes() == b"data"  # original untouched
    assert not (tmp_path / "escape").exists()

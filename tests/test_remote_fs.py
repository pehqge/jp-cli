import pytest

from jp._sim import FakeKernelWS
from jp.kernel_conn import KernelConn
from jp.remote_fs import RemoteAccessDenied, RemoteFS, RemoteNotFound, RemoteReadOnly


@pytest.fixture
def rfs(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "hello.txt").write_bytes(b"hello world")
    (tmp_path / "sub" / "big.bin").write_bytes(bytes(range(256)) * 100000)  # ~25 MiB
    ws = FakeKernelWS(root=str(tmp_path))
    ws.open_comm(comm_id="c1", target="jp.fs")
    return RemoteFS(KernelConn(ws, comm_id="c1", session="s"))


@pytest.fixture
def wrfs(tmp_path):
    ws = FakeKernelWS(root=str(tmp_path), writable=True)
    ws.open_comm(comm_id="c1", target="jp.fs")
    return RemoteFS(KernelConn(ws, comm_id="c1", session="s")), tmp_path


def test_stat(rfs):
    st = rfs.stat("hello.txt")
    assert st.type == "file" and st.size == 11


def test_listdir(rfs):
    names = sorted(e.name for e in rfs.listdir(""))
    assert names == ["hello.txt", "sub"]


def test_read_small(rfs):
    assert rfs.read("hello.txt", 0, 11) == b"hello world"


def test_read_spans_multiple_chunks(rfs):
    # ~25 MiB > MAX_READ (8 MiB): RemoteFS must stitch multiple agent reads.
    data = rfs.read("sub/big.bin", 0, 25_600_000)
    assert len(data) == 25_600_000
    assert data[:4] == bytes([0, 1, 2, 3])


def test_missing_raises(rfs):
    with pytest.raises(RemoteNotFound):
        rfs.stat("nope")


def test_traversal_raises_access_denied(rfs):
    with pytest.raises(RemoteAccessDenied):
        rfs.read("../escape", 0, 1)


def test_write_round_trip(wrfs):
    fs, root = wrfs
    fs.write("note.txt", b"contents")
    assert fs.read("note.txt", 0, 8) == b"contents"
    assert (root / "note.txt").read_bytes() == b"contents"


def test_mkdir_then_listdir(wrfs):
    fs, _ = wrfs
    fs.mkdir("created")
    assert "created" in [e.name for e in fs.listdir("")]


def test_rename(wrfs):
    fs, root = wrfs
    fs.write("a.txt", b"x")
    fs.rename("a.txt", "b.txt")
    assert (root / "b.txt").read_bytes() == b"x" and not (root / "a.txt").exists()


def test_unlink_removes(wrfs):
    fs, root = wrfs
    fs.write("gone.txt", b"x")
    fs.unlink("gone.txt")
    assert not (root / "gone.txt").exists()


def test_readonly_write_raises(rfs):
    with pytest.raises(RemoteReadOnly):
        rfs.write("nope.txt", b"x")

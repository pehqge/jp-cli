"""Tests for the read-only WebDAV server (jp.mount.webdav_server).

Drives a real :class:`DavServer` over loopback HTTP with ``http.client`` --
no mount, no network, no real Jupyter. The fs is a real
:class:`jp.remote_fs.RemoteFS` over the in-process simulator, so the agent's
jail is exercised end to end.

The hardest safety property: every mutating WebDAV verb MUST be refused 403.
"""

from __future__ import annotations

import http.client
import os
import xml.etree.ElementTree as ET

import pytest

from jp._sim import FakeKernelWS
from jp.kernel_conn import KernelConn
from jp.mount.webdav_server import DavServer
from jp.remote_fs import RemoteFS

DAV_NS = "{DAV:}"


def _make_fs(root):
    ws = FakeKernelWS(root=str(root))
    ws.open_comm(comm_id="c1", target="jp.fs")
    return RemoteFS(KernelConn(ws, comm_id="c1", session="s"))


@pytest.fixture
def server(tmp_path):
    # Seed a small tree: a.txt, a 1000-byte big.bin, and a sub/ directory.
    (tmp_path / "a.txt").write_bytes(b"hello world")
    (tmp_path / "big.bin").write_bytes(bytes(i % 256 for i in range(1000)))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("nested")

    srv = DavServer(_make_fs(tmp_path))
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _request(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(server.host, server.port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def test_options_advertises_dav(server):
    status, headers, _ = _request(server, "OPTIONS", "/")
    assert status == 200
    assert "1" in headers.get("DAV", "")
    assert "PROPFIND" in headers.get("Allow", "")


def test_propfind_root_lists_children(server):
    status, headers, body = _request(server, "PROPFIND", "/", headers={"Depth": "1"})
    assert status == 207
    assert "xml" in headers.get("Content-Type", "")
    text = body.decode("utf-8")
    assert "a.txt" in text
    assert "sub" in text

    # Harden against XXE / billion-laughs without a third-party dep
    # (defusedxml; jp ships zero deps): reject any DOCTYPE before parsing.
    # XXE and entity-expansion attacks both require a DTD/entity declaration,
    # so refusing DOCTYPE neutralizes them while stdlib expat (no network
    # entity fetching by default) handles the rest.
    assert b"<!DOCTYPE" not in body.upper()
    root = ET.fromstring(body)
    responses = root.findall(f"{DAV_NS}response")
    # self + 3 children (a.txt, big.bin, sub)
    assert len(responses) >= 4

    # Find the response for sub and assert it is a collection.
    sub_is_collection = False
    for r in responses:
        href = r.find(f"{DAV_NS}href")
        assert href is not None and href.text is not None
        if href.text.rstrip("/").endswith("sub"):
            rtype = r.find(f".//{DAV_NS}resourcetype/{DAV_NS}collection")
            if rtype is not None:
                sub_is_collection = True
    assert sub_is_collection


def test_get_file_returns_bytes(server):
    status, _, body = _request(server, "GET", "/a.txt")
    assert status == 200
    assert body == b"hello world"


def test_get_range(server):
    status, headers, body = _request(server, "GET", "/big.bin", headers={"Range": "bytes=100-199"})
    assert status == 206
    assert "bytes 100-199/1000" in headers.get("Content-Range", "")
    expected = bytes(i % 256 for i in range(1000))[100:200]
    assert body == expected
    assert len(body) == 100


def test_get_missing_404(server):
    status, _, _ = _request(server, "GET", "/nope")
    assert status == 404


def test_get_directory_not_a_file(server):
    status, _, body = _request(server, "GET", "/sub")
    assert status == 403
    assert body != b""
    assert b"Traceback" not in body


@pytest.mark.parametrize(
    "method",
    ["PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK"],
)
def test_mutating_verb_is_forbidden(server, method):
    body = b"data" if method == "PUT" else None
    status, _, resp = _request(server, method, "/x", body=body)
    assert status == 403
    assert resp == b"read-only mount"


def test_put_is_forbidden(server):
    status, _, _ = _request(server, "PUT", "/x", body=b"data")
    assert status == 403


def test_delete_is_forbidden(server):
    status, _, _ = _request(server, "DELETE", "/a.txt")
    assert status == 403


def test_mkcol_is_forbidden(server):
    status, _, _ = _request(server, "MKCOL", "/newdir")
    assert status == 403


def test_move_is_forbidden(server):
    status, _, _ = _request(server, "MOVE", "/a.txt")
    assert status == 403


def test_traversal_is_denied(server):
    status, _, body = _request(server, "GET", "/../escape")
    assert status in (403, 404)
    assert b"Traceback" not in body


def test_traversal_propfind_is_denied(server):
    status, _, body = _request(server, "PROPFIND", "/../escape", headers={"Depth": "0"})
    assert status in (403, 404)
    assert b"Traceback" not in body


def test_context_manager(tmp_path):
    (tmp_path / "x.txt").write_text("x")
    with DavServer(_make_fs(tmp_path)) as srv:
        assert srv.url.startswith("http://127.0.0.1:")
        status, _, body = _request(srv, "GET", "/x.txt")
        assert status == 200
        assert body == b"x"


def test_head_no_body(server):
    conn = http.client.HTTPConnection(server.host, server.port, timeout=5)
    try:
        conn.request("HEAD", "/a.txt")
        resp = conn.getresponse()
        data = resp.read()
        assert resp.status == 200
        assert int(resp.getheader("Content-Length")) == len(b"hello world")
        assert data == b""
    finally:
        conn.close()


def test_url_is_loopback(server):
    assert server.host == "127.0.0.1"
    assert server.port != 0
    assert server.url == f"http://127.0.0.1:{server.port}/"
    assert os.path is not None  # keep os import meaningful even if env changes

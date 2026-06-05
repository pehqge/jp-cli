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
    # The server now serves only under a per-session secret segment; a real
    # mount client requests under server.url. Prepend the secret so the existing
    # behavioural tests keep using bare paths like "/a.txt".
    prefix = f"/{server.secret}" if server.secret else ""
    conn = http.client.HTTPConnection(server.host, server.port, timeout=5)
    try:
        conn.request(method, prefix + path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def _raw_request(server, method, path, headers=None):
    """Hit a literal path WITHOUT the secret prefix (for the capability tests)."""
    conn = http.client.HTTPConnection(server.host, server.port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def _make_writable_fs(root):
    ws = FakeKernelWS(root=str(root), writable=True)
    ws.open_comm(comm_id="c1", target="jp.fs")
    return RemoteFS(KernelConn(ws, comm_id="c1", session="s"))


@pytest.fixture
def wserver(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello world")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "child.txt").write_text("c")

    srv = DavServer(_make_writable_fs(tmp_path), writable=True)
    srv.start()
    try:
        yield srv, tmp_path
    finally:
        srv.stop()


def test_put_creates_file(wserver):
    srv, root = wserver
    status, _, _ = _request(srv, "PUT", "/new.txt", body=b"hello", headers={"Content-Length": "5"})
    assert status == 201
    assert (root / "new.txt").read_bytes() == b"hello"


def test_put_overwrite_returns_204(wserver):
    srv, root = wserver
    status, _, _ = _request(srv, "PUT", "/a.txt", body=b"updated", headers={"Content-Length": "7"})
    assert status == 204
    assert (root / "a.txt").read_bytes() == b"updated"


def test_mkcol_creates_dir(wserver):
    srv, root = wserver
    status, _, _ = _request(srv, "MKCOL", "/d")
    assert status == 201
    assert (root / "d").is_dir()


def test_move_renames(wserver):
    srv, root = wserver
    (root / "src.txt").write_text("x")
    # Destination echoes the secret the client received in our hrefs.
    status, _, _ = _request(srv, "MOVE", "/src.txt", headers={"Destination": f"{srv.url}b.txt"})
    assert status in (201, 204)
    assert not (root / "src.txt").exists()
    assert (root / "b.txt").exists()


def test_delete_file(wserver):
    srv, root = wserver
    status, _, _ = _request(srv, "DELETE", "/a.txt")
    assert status == 204
    assert not (root / "a.txt").exists()


def test_delete_nonempty_dir_refused(wserver):
    srv, root = wserver
    status, _, body = _request(srv, "DELETE", "/dir")
    assert status == 403
    assert body == b"refusing recursive remote delete"
    assert (root / "dir").is_dir()
    assert (root / "dir" / "child.txt").exists()


def test_readonly_server_still_forbids_put(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hi")
    srv = DavServer(_make_writable_fs(tmp_path))  # writable defaults False
    srv.start()
    try:
        status, _, body = _request(
            srv, "PUT", "/x.txt", body=b"data", headers={"Content-Length": "4"}
        )
        assert status == 403
        assert body == b"read-only mount"
        assert not (tmp_path / "x.txt").exists()
    finally:
        srv.stop()


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
        conn.request("HEAD", f"/{server.secret}/a.txt")
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
    # url now carries the per-session secret segment.
    assert server.url == f"http://127.0.0.1:{server.port}/{server.secret}/"
    assert os.path is not None  # keep os import meaningful even if env changes


# --- capability secret: another local process that finds the port but not the
# secret must not be able to read or write the mounted files ----------------


def test_url_contains_random_secret(server):
    import re

    # 128-bit url-safe secret: long enough, and url-path-safe charset.
    assert server.secret and len(server.secret) >= 20
    assert re.fullmatch(r"[A-Za-z0-9_-]+", server.secret)


def test_connection_reset_is_swallowed(server, capsys):
    # The macOS WebDAV client RST-closes idle pooled connections; that benign
    # churn must NOT spew a traceback from the server's worker threads.
    httpd = server._httpd
    try:
        raise ConnectionResetError(54, "Connection reset by peer")
    except ConnectionResetError:
        httpd.handle_error(None, ("127.0.0.1", 12345))
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_real_handler_error_still_reported(server, capsys):
    # A non-connection error must still surface (we only swallow socket churn).
    httpd = server._httpd
    try:
        raise ValueError("boom in a handler")
    except ValueError:
        httpd.handle_error(None, ("127.0.0.1", 12345))
    assert "Traceback" in capsys.readouterr().err


def test_secret_survives_redaction(server):
    # Regression: the mount URL must stay copy-pasteable. ui.redact() masks
    # 32+ hex blobs as likely Jupyter tokens; the capability secret is
    # deliberately url-safe (non-hex) so the printed mount command is usable.
    from jp import ui

    assert ui.redact(server.url) == server.url
    assert "REDACTED" not in ui.redact(f"mount_webdav -S {server.url} /tmp/x")


def test_get_without_secret_is_404(server):
    # Bare path (no secret) -- simulates a local port-scanner -- must not read.
    status, _, body = _raw_request(server, "GET", "/a.txt")
    assert status == 404
    assert b"hello world" not in body


def test_propfind_without_secret_is_404(server):
    status, _, body = _raw_request(server, "PROPFIND", "/", headers={"Depth": "1"})
    assert status == 404
    assert b"a.txt" not in body


def test_wrong_secret_is_404(server):
    wrong = "0" * 32
    status, _, body = _raw_request(server, "GET", f"/{wrong}/a.txt")
    assert status == 404
    assert b"hello world" not in body


def test_write_without_secret_is_blocked(wserver):
    srv, root = wserver
    status, _, _ = _raw_request(srv, "PUT", "/owned.txt", headers={"Content-Length": "0"})
    assert status in (403, 404)  # writable gate or secret gate -- never written
    assert not (root / "owned.txt").exists()


def test_options_without_secret_still_ok(server):
    # OPTIONS is exempt so the OS client can probe capabilities before mounting;
    # it exposes nothing the open port did not already reveal.
    status, headers, _ = _raw_request(server, "OPTIONS", "/")
    assert status == 200
    assert "PROPFIND" in headers.get("Allow", "")


def test_propfind_hrefs_carry_secret(server):
    # Children hrefs must include the secret so the client stays in the jail.
    status, _, body = _request(server, "PROPFIND", "/", headers={"Depth": "1"})
    assert status == 207
    assert f"/{server.secret}/".encode() in body


# --- macOS read-write mount handshake (writable mode only) ---------------
#
# macOS's built-in WebDAV client decides read-only vs read-write AT MOUNT
# TIME from the server's OPTIONS response. To mount read-WRITE it requires
# (a) DAV: 1, 2 (class 2 == locking), (b) the write verbs in Allow, and
# (c) working LOCK/UNLOCK. Without these it mounts read-only and blocks the
# save locally with "Read-only file system (os error 30)" before any write
# reaches us. These tests pin the protocol the macOS client needs.


def test_options_writable_advertises_dav_class_2(wserver):
    srv, _ = wserver
    status, headers, _ = _request(srv, "OPTIONS", "/")
    assert status == 200
    dav = headers.get("DAV", "")
    assert "2" in dav
    assert "1" in dav
    allow = headers.get("Allow", "")
    for verb in ("PUT", "DELETE", "MKCOL", "MOVE", "LOCK", "UNLOCK", "PROPPATCH"):
        assert verb in allow, f"{verb} missing from Allow: {allow!r}"


def test_options_readonly_is_class_1_only(server):
    # Regression guard: read-only mounts MUST stay class 1, read verbs only.
    status, headers, _ = _request(server, "OPTIONS", "/")
    assert status == 200
    dav = headers.get("DAV", "")
    assert dav == "1"
    assert "2" not in dav
    allow = headers.get("Allow", "")
    assert "PUT" not in allow
    assert "LOCK" not in allow


def test_lock_returns_token_when_writable(wserver):
    srv, _ = wserver
    status, headers, body = _request(srv, "LOCK", "/test.py")
    assert status == 200
    assert "opaquelocktoken:" in headers.get("Lock-Token", "")
    text = body.decode("utf-8")
    assert "<D:activelock>" in text


def test_lock_forbidden_when_readonly(server):
    status, _, body = _request(server, "LOCK", "/a.txt")
    assert status == 403
    assert body == b"read-only mount"


def test_unlock_when_writable(wserver):
    srv, _ = wserver
    status, _, _ = _request(
        srv, "UNLOCK", "/test.py", headers={"Lock-Token": "<opaquelocktoken:abc>"}
    )
    assert status == 204


def test_proppatch_when_writable_returns_207(wserver):
    srv, _ = wserver
    body = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<D:propertyupdate xmlns:D="DAV:" '
        b'xmlns:Z="urn:schemas-microsoft-com:">'
        b"<D:set><D:prop>"
        b"<D:getlastmodified>Tue, 01 Jun 2026 00:00:00 GMT</D:getlastmodified>"
        b"<Z:Win32LastModifiedTime>Tue, 01 Jun 2026 00:00:00 GMT</Z:Win32LastModifiedTime>"
        b"</D:prop></D:set>"
        b"</D:propertyupdate>"
    )
    status, headers, resp = _request(
        srv,
        "PROPPATCH",
        "/test.py",
        body=body,
        headers={"Content-Length": str(len(body))},
    )
    assert status == 207
    assert "xml" in headers.get("Content-Type", "")
    # Body must be valid XML multistatus reporting a 200 OK.
    root = ET.fromstring(resp)
    assert root.tag == f"{DAV_NS}multistatus"
    assert b"200 OK" in resp


def test_macos_save_sequence_writes_file(wserver):
    # End-to-end reproduction of the real-world macOS save handshake that
    # used to fail with "Read-only file system (os error 30)".
    srv, root = wserver

    # 1. OPTIONS -> mount must see class 2 to mount read-write.
    status, headers, _ = _request(srv, "OPTIONS", "/")
    assert status == 200
    assert "2" in headers.get("DAV", "")

    # 2. LOCK the target -> macOS needs a lock token before writing.
    status, lock_headers, _ = _request(srv, "LOCK", "/new.txt")
    assert status == 200
    token = lock_headers.get("Lock-Token", "")
    assert "opaquelocktoken:" in token

    # 3. PUT the real body.
    status, _, _ = _request(
        srv,
        "PUT",
        "/new.txt",
        body=b"hello world",
        headers={"Content-Length": "11", "If": f"({token})"},
    )
    assert status == 201

    # 4. UNLOCK.
    status, _, _ = _request(srv, "UNLOCK", "/new.txt", headers={"Lock-Token": token})
    assert status == 204

    # 5. PROPPATCH (macOS sets timestamps after the save).
    pp_body = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
        b"<D:getlastmodified>Tue, 01 Jun 2026 00:00:00 GMT</D:getlastmodified>"
        b"</D:prop></D:set></D:propertyupdate>"
    )
    status, _, _ = _request(
        srv,
        "PROPPATCH",
        "/new.txt",
        body=pp_body,
        headers={"Content-Length": str(len(pp_body))},
    )
    assert status == 207

    # The save actually landed on disk.
    assert (root / "new.txt").read_bytes() == b"hello world"

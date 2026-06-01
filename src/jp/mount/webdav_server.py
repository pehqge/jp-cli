"""A WebDAV server backed by a RemoteFS-like object (read-only by default).

This exposes the remote filesystem (any object with the
:class:`jp.remote_fs.RemoteFS` surface) over loopback HTTP so the OS's built-in
WebDAV client can mount it natively from ``127.0.0.1`` -- no FUSE, no third-party
deps, stdlib only.

Safety Charter: a mount is READ-ONLY by default. Every mutating WebDAV method
(PUT/DELETE/MKCOL/MOVE/COPY/PROPPATCH/LOCK/UNLOCK) is refused with HTTP 403
unless the server was constructed with ``writable=True`` (Phase 4 guarded write
path). Even when writable, COPY stays 403 (out of scope), and a DELETE of a
non-empty directory is refused: this server NEVER recursive-deletes the remote.
The agent's jail (enforced remote-side) is forwarded verbatim; this server
never resolves paths itself.

macOS read-write mounts: macOS's built-in WebDAV client decides read-only vs
read-write AT MOUNT TIME from the OPTIONS response. To mount read-WRITE it
requires the server to advertise ``DAV: 1, 2`` (class 2 == locking), list the
write verbs in ``Allow``, and answer ``LOCK``/``UNLOCK`` successfully. In
writable mode we therefore speak just enough of WebDAV class 2 -- including a
FAKE-but-valid lock (we do NOT enforce real locking; this is a single-user
mount) and a no-op ``PROPPATCH`` -- so the client mounts read-write and the
save actually reaches us. None of this changes read-only behavior.
"""

from __future__ import annotations

import threading
import uuid
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

from ..remote_fs import (
    RemoteAccessDenied,
    RemoteExists,
    RemoteNotEmpty,
    RemoteNotFound,
    RemoteReadOnly,
)

# Verbs that could mutate the remote. PUT/DELETE/MKCOL/MOVE are honored only in
# writable mode; LOCK/UNLOCK/PROPPATCH are answered (no-op/fake) in writable mode
# so macOS will mount read-write; COPY stays refused 403 even when writable.
_MUTATING = ("PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK")
# Verbs that remain 403 in BOTH modes (writable does not enable these).
_ALWAYS_FORBIDDEN = ("COPY",)

# ElementTree namespace prefix for the DAV: namespace.
_DAV_NS = "{DAV:}"


class _DavRequestHandler(BaseHTTPRequestHandler):
    """Handles one WebDAV request against ``self.server.fs``.

    Non-standard verbs (PROPFIND, MKCOL, ...) dispatch fine because
    ``BaseHTTPRequestHandler`` calls ``do_<METHOD>`` by name.
    """

    server_version = "jp-dav/1.0"
    protocol_version = "HTTP/1.1"

    # --- internals -----------------------------------------------------

    @property
    def _fs(self) -> Any:
        return self.server.fs  # type: ignore[attr-defined]

    @property
    def _writable(self) -> bool:
        return bool(getattr(self.server, "writable", False))

    def log_message(self, *args: Any) -> None:  # silence stderr access logs
        return

    def _resolve_path(self) -> str:
        """Map the request URL to a remote-relative path.

        Drops the query string, percent-decodes, and strips the leading "/".
        The empty string denotes the fs root. No local resolution / jailing is
        done here -- the agent enforces the jail and refuses traversal.
        """
        raw = urlsplit(self.path).path
        return unquote(raw).lstrip("/")

    def _read_body(self) -> bytes:
        """Read exactly ``Content-Length`` bytes (or b"" if absent/zero).

        Reading to EOF is unsafe on a keep-alive connection, so a missing
        Content-Length is treated as an empty body rather than a blocking read.
        """
        clen = self.headers.get("Content-Length")
        if clen is None:
            return b""
        try:
            length = int(clen)
        except ValueError:
            return b""
        return self.rfile.read(length) if length > 0 else b""

    def _drain_body(self) -> None:
        """Consume any request body so keep-alive connections stay in sync."""
        self._read_body()

    def _send_error(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _forbidden(self) -> None:
        self._send_error(403, b"read-only mount")

    # --- read-only verbs ----------------------------------------------

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        if self._writable:
            # Class 2 (locking) + write verbs: required for macOS to mount
            # read-write. The lock is faked (see do_LOCK) but the protocol
            # advertisement must be honest enough for the client to proceed.
            self.send_header("DAV", "1, 2")
            self.send_header(
                "Allow",
                "OPTIONS, GET, HEAD, POST, PROPFIND, PROPPATCH, "
                "PUT, DELETE, MKCOL, MOVE, COPY, LOCK, UNLOCK",
            )
        else:
            self.send_header("DAV", "1")
            self.send_header("Allow", "OPTIONS, GET, HEAD, PROPFIND")
        self.send_header("MS-Author-Via", "DAV")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PROPFIND(self) -> None:
        path = self._resolve_path()
        depth = self.headers.get("Depth", "1")
        if depth == "infinity":
            depth = "1"
        try:
            st = self._fs.stat(path)
        except RemoteNotFound:
            self._send_error(404, b"not found")
            return
        except RemoteAccessDenied:
            self._forbidden()
            return

        href_self = self._href_for(path, st.type == "directory")
        responses = [self._response_xml(href_self, st.type, st.size, st.mtime, path)]

        if st.type == "directory" and depth == "1":
            try:
                entries = self._fs.listdir(path)
            except RemoteNotFound:
                self._send_error(404, b"not found")
                return
            except RemoteAccessDenied:
                self._forbidden()
                return
            for e in entries:
                child = (path + "/" + e.name) if path else e.name
                is_dir = e.type == "directory"
                href = self._href_for(child, is_dir)
                mtime = 0.0
                # Children's mtime is not in the listing; getlastmodified is
                # advisory for WebDAV, so 0.0 (epoch) is acceptable here.
                responses.append(self._response_xml(href, e.type, e.size, mtime, e.name))

        xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>"
        )
        payload = xml.encode("utf-8")
        self.send_response(207, "Multi-Status")
        self.send_header("Content-Type", 'application/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _href_for(self, path: str, is_dir: bool) -> str:
        href = "/" + quote(path)
        if is_dir and not href.endswith("/"):
            href += "/"
        return href

    def _response_xml(self, href: str, rtype: str, size: int, mtime: float, name: str) -> str:
        if rtype == "directory":
            resourcetype = "<D:resourcetype><D:collection/></D:resourcetype>"
            contentlength = ""
        else:
            resourcetype = "<D:resourcetype/>"
            contentlength = f"<D:getcontentlength>{size}</D:getcontentlength>"
        lastmod = formatdate(mtime, usegmt=True)
        display = escape(name or "/")
        return (
            "<D:response>"
            f"<D:href>{escape(href)}</D:href>"
            "<D:propstat>"
            "<D:prop>"
            f"{resourcetype}"
            f"{contentlength}"
            f"<D:getlastmodified>{escape(lastmod)}</D:getlastmodified>"
            f"<D:displayname>{display}</D:displayname>"
            "</D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status>"
            "</D:propstat>"
            "</D:response>"
        )

    def _parse_range(self, total: int) -> tuple[int, int] | None:
        """Parse a single ``Range: bytes=START-END`` header.

        Returns ``(offset, length)`` or ``None`` when no usable range is given.
        """
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes="):
            return None
        spec = header[len("bytes=") :].split(",")[0].strip()
        if "-" not in spec:
            return None
        start_s, _, end_s = spec.partition("-")
        try:
            if start_s == "":
                # suffix range: last N bytes
                n = int(end_s)
                start = max(0, total - n)
                end = total - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else total - 1
        except ValueError:
            return None
        if start > end or start >= total:
            return None
        end = min(end, total - 1)
        return start, end - start + 1

    def _serve_get(self, head_only: bool) -> None:
        path = self._resolve_path()
        try:
            st = self._fs.stat(path)
        except RemoteNotFound:
            self._send_error(404, b"not found")
            return
        except RemoteAccessDenied:
            self._forbidden()
            return

        if st.type == "directory":
            # Directories are not files; refuse to keep the mount simple/safe.
            self._send_error(403, b"is a directory")
            return

        total = st.size
        rng = self._parse_range(total)
        if rng is None:
            offset, length, status = 0, total, 200
        else:
            offset, length = rng
            status = 206

        body = b""
        if not head_only:
            try:
                body = self._fs.read(path, offset, length)
            except RemoteNotFound:
                self._send_error(404, b"not found")
                return
            except RemoteAccessDenied:
                self._forbidden()
                return

        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {offset}-{offset + length - 1}/{total}")
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._serve_get(head_only=False)

    def do_HEAD(self) -> None:
        self._serve_get(head_only=True)

    # --- mutating verbs (only honored in writable mode) ----------------

    def do_PUT(self) -> None:
        if not self._writable:
            self._forbidden()
            return
        clen = self.headers.get("Content-Length")
        if clen is None:
            # We refuse to guess the body length: reading to EOF is unsafe on a
            # persistent (keep-alive) connection. Require an explicit length.
            self._send_error(411, b"length required")
            return
        try:
            length = int(clen)
        except ValueError:
            self._send_error(400, b"bad content-length")
            return
        body = self.rfile.read(length) if length > 0 else b""

        path = self._resolve_path()
        # Decide created (201) vs updated (204) by probing existence first.
        try:
            self._fs.stat(path)
            existed = True
        except RemoteNotFound:
            existed = False
        except RemoteAccessDenied:
            self._forbidden()
            return

        try:
            self._fs.write(path, body)
        except RemoteReadOnly:
            self._forbidden()
            return
        except RemoteAccessDenied:
            self._forbidden()
            return
        except RemoteNotFound:
            # Parent directory missing -> conflict per WebDAV.
            self._send_error(409, b"conflict")
            return
        self._send_error(204 if existed else 201)

    def do_DELETE(self) -> None:
        if not self._writable:
            self._forbidden()
            return
        path = self._resolve_path()
        try:
            st = self._fs.stat(path)
        except RemoteNotFound:
            self._send_error(404, b"not found")
            return
        except RemoteAccessDenied:
            self._forbidden()
            return

        try:
            if st.type == "directory":
                # SAFETY: we never recursively delete the remote. rmdir only
                # succeeds on an empty directory; a non-empty one is refused.
                try:
                    self._fs.rmdir(path)
                except RemoteNotEmpty:
                    self._send_error(403, b"refusing recursive remote delete")
                    return
            else:
                self._fs.unlink(path)
        except RemoteReadOnly:
            self._forbidden()
            return
        except RemoteAccessDenied:
            self._forbidden()
            return
        except RemoteNotFound:
            self._send_error(404, b"not found")
            return
        self._send_error(204)

    def do_MKCOL(self) -> None:
        if not self._writable:
            self._forbidden()
            return
        path = self._resolve_path()
        try:
            self._fs.mkdir(path)
        except RemoteExists:
            # MKCOL on an existing resource -> Method Not Allowed (RFC 4918).
            self._send_error(405, b"already exists")
            return
        except RemoteReadOnly:
            self._forbidden()
            return
        except RemoteAccessDenied:
            self._forbidden()
            return
        except RemoteNotFound:
            self._send_error(409, b"conflict")
            return
        self._send_error(201)

    def do_MOVE(self) -> None:
        if not self._writable:
            self._forbidden()
            return
        dest_header = self.headers.get("Destination")
        if not dest_header:
            self._send_error(400, b"missing destination")
            return
        # Destination is an absolute URL or path; take the path component, decode
        # it, and strip the leading "/" to match _resolve_path's convention. We do
        # not jail it here on purpose: the agent re-jails BOTH rename endpoints
        # remote-side (Agent._resolve_mutable), so this is forwarded verbatim.
        dest_path = unquote(urlsplit(dest_header).path).lstrip("/")
        path = self._resolve_path()
        # Did the destination already exist? (created 201 vs overwritten 204)
        try:
            self._fs.stat(dest_path)
            dest_existed = True
        except (RemoteNotFound, RemoteAccessDenied):
            dest_existed = False

        try:
            self._fs.rename(path, dest_path)
        except RemoteReadOnly:
            self._forbidden()
            return
        except RemoteAccessDenied:
            self._forbidden()
            return
        except RemoteNotFound:
            self._send_error(404, b"not found")
            return
        except RemoteExists:
            # Overwrite refused by the remote -> Precondition Failed (WebDAV).
            self._send_error(412, b"destination exists")
            return
        self._send_error(204 if dest_existed else 201)

    # --- WebDAV class 2 (writable mode only): faked locking + no-op props ---
    #
    # We do NOT implement real locking -- a jp mount is single-user, so there
    # is no contention to arbitrate. These handlers exist purely to satisfy
    # the macOS client's mount-time protocol checks so it mounts read-write.

    def do_LOCK(self) -> None:
        if not self._writable:
            self._forbidden()
            return
        # Drain any request body (LOCK carries a lockinfo doc) so the next
        # request on a keep-alive connection parses cleanly.
        self._drain_body()
        token = f"opaquelocktoken:{uuid.uuid4()}"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>\n'
            "<D:locktype><D:write/></D:locktype>\n"
            "<D:lockscope><D:exclusive/></D:lockscope>\n"
            "<D:depth>infinity</D:depth>\n"
            "<D:timeout>Second-3600</D:timeout>\n"
            f"<D:locktoken><D:href>{escape(token)}</D:href></D:locktoken>\n"
            "</D:activelock></D:lockdiscovery></D:prop>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", 'application/xml; charset="utf-8"')
        self.send_header("Lock-Token", f"<{token}>")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_UNLOCK(self) -> None:
        if not self._writable:
            self._forbidden()
            return
        self._drain_body()
        self._send_error(204)

    def do_PROPPATCH(self) -> None:
        if not self._writable:
            self._forbidden()
            return
        # macOS issues PROPPATCH after PUT to set timestamps (getlastmodified,
        # Win32 props). We do not persist arbitrary props, but must report
        # success or the save is treated as failed. Report each requested prop
        # as 200 OK (no-op). Fall back to a generic empty propstat 200 OK.
        path = self._resolve_path()
        body = self._read_body()
        propstats = self._proppatch_ok_propstats(body)
        href = self._href_for(path, is_dir=False)
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<D:multistatus xmlns:D="DAV:"><D:response>'
            f"<D:href>{escape(href)}</D:href>"
            f"{propstats}"
            "</D:response></D:multistatus>"
        )
        payload = xml.encode("utf-8")
        self.send_response(207, "Multi-Status")
        self.send_header("Content-Type", 'application/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _proppatch_ok_propstats(self, body: bytes) -> str:
        """Build a propstat block marking every requested prop as 200 OK.

        Parses ``<D:set><D:prop>...`` children out of the PROPPATCH body. If
        parsing yields nothing (unexpected shape / unparseable), returns a
        single generic 200 OK propstat so the save still succeeds.
        """
        names = self._parse_proppatch_prop_names(body)
        if not names:
            return "<D:propstat><D:prop/><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
        props = "".join(f"<{escape(n)}/>" for n in names)
        return (
            f"<D:propstat><D:prop>{props}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
        )

    @staticmethod
    def _parse_proppatch_prop_names(body: bytes) -> list[str]:
        """Extract qualified prop element names under ``<set><prop>``.

        Refuses DOCTYPE (XXE / billion-laughs guard, matching PROPFIND), and
        swallows parse errors -- a malformed body just yields the generic 200.
        """
        if not body or b"<!DOCTYPE" in body.upper():
            return []
        import xml.etree.ElementTree as ET  # stdlib, lazy import

        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return []
        names: list[str] = []
        # Find every <D:prop> under a <D:set> and collect its children's tags.
        for setel in root.iter(f"{_DAV_NS}set"):
            for propel in setel.iter(f"{_DAV_NS}prop"):
                for child in propel:
                    names.append(_qualify_tag(child.tag))
        return names


def _qualify_tag(tag: str) -> str:
    """Turn a ``{namespace}local`` ElementTree tag into a ``D:local`` name.

    DAV-namespaced tags become ``D:local``; anything else falls back to the
    local name only (without a prefix) since we don't track foreign prefixes.
    """
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        if ns == "DAV:":
            return f"D:{local}"
        return local
    return tag


def _make_mutating_handler(method: str) -> Any:
    def handler(self: _DavRequestHandler) -> None:
        self._forbidden()

    handler.__name__ = f"do_{method}"
    return handler


# COPY/PROPPATCH/LOCK/UNLOCK are always 403 (even in writable mode). PUT/DELETE/
# MKCOL/MOVE have explicit handlers above that gate on ``self._writable``.
for _m in _ALWAYS_FORBIDDEN:
    setattr(_DavRequestHandler, f"do_{_m}", _make_mutating_handler(_m))


class _DavHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], fs: Any, writable: bool = False) -> None:
        super().__init__(address, _DavRequestHandler)
        self.fs = fs
        self.writable = writable


class DavServer:
    """WebDAV server over a RemoteFS-like ``fs``, bound to loopback.

    Read-only by default. Pass ``writable=True`` to enable the guarded write
    path (PUT/DELETE/MKCOL/MOVE); see the module docstring for the safety rules.
    """

    def __init__(self, fs: Any, *, writable: bool = False) -> None:
        self._fs = fs
        self.writable = writable
        self._httpd: _DavHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> DavServer:
        if self._httpd is not None:
            return self
        self._httpd = _DavHTTPServer(("127.0.0.1", 0), self._fs, self.writable)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="jp-dav", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def host(self) -> str:
        if self._httpd is None:
            raise RuntimeError("server not started")
        host = self._httpd.server_address[0]
        return host if isinstance(host, str) else host.decode()

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("server not started")
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def __enter__(self) -> DavServer:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

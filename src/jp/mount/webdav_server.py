"""A strictly read-only WebDAV server backed by a RemoteFS-like object.

This exposes the remote filesystem (any object with the
:class:`jp.remote_fs.RemoteFS` surface) over loopback HTTP so the OS's built-in
WebDAV client can mount it natively from ``127.0.0.1`` -- no FUSE, no third-party
deps, stdlib only.

Safety Charter, Phase 2: a mount can NEVER alter the remote. Every mutating
WebDAV method (PUT/DELETE/MKCOL/MOVE/COPY/PROPPATCH/LOCK/UNLOCK) is refused with
HTTP 403. Only OPTIONS/GET/HEAD/PROPFIND are honored. The agent's jail (enforced
remote-side) is forwarded verbatim; this server never resolves paths itself.
"""

from __future__ import annotations

import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

from ..remote_fs import RemoteAccessDenied, RemoteNotFound

# Verbs that could mutate the remote. All refused 403, unconditionally.
_MUTATING = ("PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK")


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
        self.send_header("DAV", "1")
        self.send_header("Allow", "OPTIONS, GET, HEAD, PROPFIND")
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


def _make_mutating_handler(method: str) -> Any:
    def handler(self: _DavRequestHandler) -> None:
        self._forbidden()

    handler.__name__ = f"do_{method}"
    return handler


for _m in _MUTATING:
    setattr(_DavRequestHandler, f"do_{_m}", _make_mutating_handler(_m))


class _DavHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], fs: Any) -> None:
        super().__init__(address, _DavRequestHandler)
        self.fs = fs


class DavServer:
    """Read-only WebDAV server over a RemoteFS-like ``fs``, bound to loopback."""

    def __init__(self, fs: Any) -> None:
        self._fs = fs
        self._httpd: _DavHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> DavServer:
        if self._httpd is not None:
            return self
        self._httpd = _DavHTTPServer(("127.0.0.1", 0), self._fs)
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

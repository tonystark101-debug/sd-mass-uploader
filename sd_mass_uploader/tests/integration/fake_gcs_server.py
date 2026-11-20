"""A small in-process stand-in for the GCS JSON API, so the real Google client can be exercised.

It follows the documented protocol for multipart and resumable uploads, generation
preconditions and CRC32C. It proves this project drives the client correctly. It does not prove
anything about real Google behaviour, IAM or networks: that needs a real test bucket.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import google_crc32c


def _crc(data: bytes) -> str:
    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode()


class FakeGcsState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.objects: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.generation = 1000
        self.fail_next: list[int] = []  # HTTP statuses to return for the next upload calls.
        self.lose_next_response = 0  # Store the object, then answer 503.
        self.corrupt_next = 0  # Store altered bytes, as if corrupted in transit.
        self.denied_permissions: set[str] = set()
        self.upload_calls = 0
        self.chunk_requests = 0
        self.fail_on_chunk: set[int] = set()
        self.auth_headers: list[str | None] = []  # Authorization header of every request.
        self.upload_delay = 0.0  # Seconds to wait in every upload request (slow network).

    def store(self, name: str, data: bytes, meta: dict) -> dict:
        self.generation += 1
        if self.corrupt_next:
            self.corrupt_next -= 1
            data = b"X" + data[1:]
        record = {
            "kind": "storage#object",
            "name": name,
            "bucket": "test-bucket",
            "size": str(len(data)),
            "crc32c": _crc(data),
            "generation": str(self.generation),
            "metageneration": "1",
            "metadata": meta.get("metadata") or {},
            "contentType": meta.get("contentType", "application/octet-stream"),
        }
        self.objects[name] = {"record": record, "data": data}
        return record


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FakeGcsState

    def log_message(self, *_: object) -> None:
        pass

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _json(self, status: int, payload: dict, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str = "error") -> None:
        self._json(status, {"error": {"code": status, "message": message}})

    def do_GET(self) -> None:
        self.state.auth_headers.append(self.headers.get("Authorization"))
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path.endswith("/iam/testPermissions"):
            wanted = query.get("permissions", [])
            allowed = [p for p in wanted if p not in self.state.denied_permissions]
            self._json(200, {"kind": "storage#testIamPermissionsResponse", "permissions": allowed})
            return
        match = re.match(r"^/storage/v1/b/[^/]+/o/(.+)$", url.path)
        if not match:
            self._error(404)
            return
        item = self.state.objects.get(unquote(match.group(1)))
        if item is None:
            self._error(404, "No such object")
        else:
            self._json(200, item["record"])

    def _precondition_failed(self, name: str, query: dict) -> bool:
        wanted = query.get("ifGenerationMatch", [None])[0]
        if wanted is None:
            return False
        current = self.state.objects.get(name)
        return int(wanted) != (int(current["record"]["generation"]) if current else 0)

    def _pop_failure(self) -> int | None:
        return self.state.fail_next.pop(0) if self.state.fail_next else None

    def do_POST(self) -> None:
        self.state.auth_headers.append(self.headers.get("Authorization"))
        if self.state.upload_delay:
            time.sleep(self.state.upload_delay)
        url = urlparse(self.path)
        query = parse_qs(url.query)
        body = self._body()
        with self.state.lock:
            self.state.upload_calls += 1
            failure = self._pop_failure()
            if failure:
                self._error(failure, "injected")
                return
            if query.get("uploadType") == ["resumable"]:
                self._start_resumable(url, query, body)
            elif query.get("uploadType") == ["multipart"]:
                self._multipart(query, body)
            else:
                self._error(400)

    def _multipart(self, query: dict, body: bytes) -> None:
        boundary = re.search(r'boundary="?([^";]+)"?', self.headers["Content-Type"]).group(1)
        parts = body.split(f"--{boundary}".encode())
        meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].strip())
        data = parts[2].split(b"\r\n\r\n", 1)[1]
        data = data[:-2] if data.endswith(b"\r\n") else data
        self._finish(meta["name"], data, meta, query, meta.get("crc32c"))

    def _finish(self, name: str, data: bytes, meta: dict, query: dict,
                sent_crc: str | None) -> None:
        if self._precondition_failed(name, query):
            self._error(412, "Precondition Failed")
            return
        if sent_crc and sent_crc != _crc(data):
            self._error(400, "CRC mismatch")
            return
        record = self.state.store(name, data, meta)
        if self.state.lose_next_response:
            self.state.lose_next_response -= 1
            self._error(503, "response lost")
        else:
            self._json(200, record)

    def _start_resumable(self, url, query: dict, body: bytes) -> None:
        meta = json.loads(body or b"{}")
        name = meta.get("name") or query["name"][0]
        session = uuid.uuid4().hex
        self.state.sessions[session] = {"name": name, "meta": meta, "query": query,
                                        "data": bytearray()}
        host = self.headers["Host"]
        self._json(200, {}, {"Location": f"http://{host}{url.path}?upload_id={session}"})

    def do_PUT(self) -> None:
        self.state.auth_headers.append(self.headers.get("Authorization"))
        url = urlparse(self.path)
        session_id = parse_qs(url.query).get("upload_id", [""])[0]
        body = self._body()
        with self.state.lock:
            self.state.chunk_requests += 1
            session = self.state.sessions.get(session_id)
            if session is None:
                self._error(404, "Unknown upload session")
                return
            if self.state.chunk_requests in self.state.fail_on_chunk:
                self._error(503, "injected chunk failure")  # The chunk is not stored.
                return
            header = self.headers.get("Content-Range", "")
            match = re.match(r"bytes (?:(\d+)-(\d+)|\*)/(\d+|\*)", header)
            if not match:
                self._error(400, "Bad Content-Range")
                return
            start, _end, total = match.groups()
            if start is not None:
                del session["data"][int(start):]
                session["data"] += body
            if total != "*" and len(session["data"]) == int(total):
                self._finish(session["name"], bytes(session["data"]), session["meta"],
                             session["query"], None)
                return
            self.send_response(308)
            if session["data"]:
                self.send_header("Range", f"bytes=0-{len(session['data']) - 1}")
            self.send_header("Content-Length", "0")
            self.end_headers()


class FakeGcsServer:
    def __init__(self) -> None:
        self.state = FakeGcsState()
        handler = type("Handler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> FakeGcsServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()

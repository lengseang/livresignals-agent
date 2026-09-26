"""Minimal in-process ASGI HTTP client for tests (no httpx: Starlette's TestClient needs it and it is not
installed in .venv; adding a dependency for tests is not worth it). Supports plain requests and incremental
reading of streaming (SSE) responses, with a client disconnect on close.
"""
from __future__ import annotations

import atexit
import json
import queue
from contextlib import contextmanager
from urllib.parse import urlencode

import anyio
from anyio.from_thread import start_blocking_portal


# One event loop for the whole test session, created at import (collection) time: on Windows, creating an
# asyncio loop opens a loopback socketpair, which tests/loop/conftest.py's no-network guard refuses inside a test.
_PORTAL_CM = start_blocking_portal()
_PORTAL = _PORTAL_CM.__enter__()
atexit.register(_PORTAL_CM.__exit__, None, None, None)


class Response:
    def __init__(self, status_code: int, headers: dict, chunks):
        self.status_code = status_code
        self.headers = headers
        self._chunks = chunks          # iterator of bytes
        self._body: bytes | None = None

    def read(self) -> bytes:
        if self._body is None:
            self._body = b"".join(self._chunks)
        return self._body

    def json(self):
        return json.loads(self.read())

    def iter_lines(self):
        buf = b""
        for chunk in self._chunks:
            buf += chunk
            while True:
                i = buf.find(b"\n")
                if i < 0:
                    break
                line, buf = buf[:i], buf[i + 1:]
                yield line.rstrip(b"\r").decode("utf-8")
        if buf:
            yield buf.decode("utf-8")


class ASGIClient:
    __test__ = False

    def __init__(self, app):
        self.app = app
        self.portal = _PORTAL

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @contextmanager
    def stream(self, method: str, path: str, params: dict | None = None, headers: dict | None = None):
        q: queue.Queue = queue.Queue()
        disconnect = self.portal.call(anyio.Event)
        qs = urlencode(params or {})
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method.upper(),
            "scheme": "http", "path": path.split("?")[0], "raw_path": path.encode(), "root_path": "",
            "query_string": qs.encode(), "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 50000),
            "headers": [(b"host", b"127.0.0.1:8765")] + [(k.lower().encode(), str(v).encode())
                                                         for k, v in (headers or {}).items()],
        }
        sent = {"body": False}

        async def receive():
            if not sent["body"]:
                sent["body"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            q.put(msg)

        async def run():
            try:
                await self.app(scope, receive, send)
            except BaseException as e:          # surface app errors to the test thread
                q.put({"type": "error", "error": e})
                raise
            finally:
                q.put({"type": "done"})

        fut = self.portal.start_task_soon(run)
        start = q.get(timeout=10)
        if start["type"] == "error":
            raise start["error"]
        assert start["type"] == "http.response.start", start
        hdrs = {k.decode().lower(): v.decode() for k, v in start["headers"]}

        def chunks():
            while True:
                m = q.get(timeout=10)
                if m["type"] == "http.response.body":
                    if m.get("body"):
                        yield m["body"]
                    if not m.get("more_body"):
                        return
                elif m["type"] in ("done", "error"):
                    return

        try:
            yield Response(start["status"], hdrs, chunks())
        finally:
            self.portal.call(disconnect.set)
            try:
                fut.result(timeout=10)
            except BaseException:
                pass

    def request(self, method: str, path: str, params: dict | None = None, headers: dict | None = None) -> Response:
        with self.stream(method, path, params=params, headers=headers) as r:
            r.read()
        return r

    def get(self, path: str, params: dict | None = None, headers: dict | None = None) -> Response:
        return self.request("GET", path, params=params, headers=headers)

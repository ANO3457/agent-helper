"""Serve the MCP endpoint over stdio: `python -m agent_helper.stdio` (docs/decisions/0031).

For clients and registries that start an MCP server as a local process and talk to it over stdin and
stdout (one JSON-RPC message per line) instead of over HTTP. Each line goes through the same app as a
POST to `/mcp` would, middleware included, so tools, limits and answers are those of the HTTP endpoint.
The process is its own instance with its own `DATA_DIR`; it does not reach any other instance.

Stdout carries only JSON-RPC messages; logs go to stderr.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from collections.abc import Callable
from urllib.parse import urlsplit

from starlette.types import ASGIApp as App
from starlette.types import Message

from .app import create_app
from .config import Settings
from .mcp import META_VERSION

# The local process is the only client; it is counted and rate limited as one address.
PEER = ("127.0.0.1", 0)


def _header_value(value: str) -> str:
    """Non-ASCII header values travel in the Base64 sentinel form the endpoint decodes."""
    if value.isascii():
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def headers_for(line: bytes, host: str) -> list[tuple[bytes, bytes]]:
    """The HTTP headers an HTTP client would send with this message. Modern MCP requires headers that
    repeat the protocol version, method and tool name from the body; over stdio there are none, so they
    are derived from the message itself."""
    headers = [
        (b"host", host.encode("ascii")),
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
    ]
    try:
        msg = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return headers  # the endpoint answers with its own parse error
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
        return headers
    params = msg.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    version = meta.get(META_VERSION) if isinstance(meta, dict) else None
    if isinstance(version, str) and version.isascii():
        headers.append((b"mcp-protocol-version", version.encode("ascii")))
        headers.append((b"mcp-method", _header_value(msg["method"]).encode("ascii")))
        name = params.get("name") if isinstance(params, dict) else None
        if msg["method"] == "tools/call" and isinstance(name, str):
            headers.append((b"mcp-name", _header_value(name).encode("ascii")))
    return headers


async def post(app: App, line: bytes, host: str) -> tuple[int, bytes]:
    """One in-process POST to /mcp; returns the status and the body."""
    scope: dict = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [*headers_for(line, host), (b"content-length", str(len(line)).encode("ascii"))],
        "client": PEER,
        "server": (host, 80),
        "state": {},
    }
    sent = False
    status = 500
    body = bytearray()

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": line, "more_body": False}
        await asyncio.Event().wait()  # no disconnect while the call runs
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    await app(scope, receive, send)
    return status, bytes(body)


class Lifespan:
    """Runs the app's startup and shutdown, as a server would around serving requests."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.inbox: asyncio.Queue[Message] = asyncio.Queue()
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def _receive(self) -> Message:
        return await self.inbox.get()

    async def _send(self, message: Message) -> None:
        if message["type"].startswith("lifespan.startup"):
            self.started.set()
        elif message["type"].startswith("lifespan.shutdown"):
            self.stopped.set()

    async def __aenter__(self) -> Lifespan:
        scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}

        async def run() -> None:
            await self.app(scope, self._receive, self._send)

        self.task = asyncio.create_task(run())
        await self.inbox.put({"type": "lifespan.startup"})
        await self.started.wait()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.inbox.put({"type": "lifespan.shutdown"})
        await self.stopped.wait()
        if self.task is not None:
            await self.task


async def serve(app: App, host: str, reader: Callable[[], bytes], write: Callable[[bytes], None]) -> None:
    """Answer each line from `reader` until it returns b"" (end of input)."""
    loop = asyncio.get_running_loop()
    async with Lifespan(app):
        while line := await loop.run_in_executor(None, reader):
            line = line.strip()
            if not line:
                continue
            _, body = await post(app, line, host)
            if body:  # notifications are answered with 202 and no body; stdio sends nothing back
                write(body.replace(b"\n", b"") + b"\n")


def main() -> None:
    settings = Settings.from_env()
    host = urlsplit(settings.public_base_url).netloc or "localhost"

    def write(data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    asyncio.run(serve(create_app(settings), host, sys.stdin.buffer.readline, write))


if __name__ == "__main__":
    main()

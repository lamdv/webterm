"""HTTP static file server + WebSocket endpoint for webterm."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import signal
import ssl
import struct
from pathlib import Path

from .session import SessionManager
from .websocket import (
    OP_TEXT,
    ConnectionClosed,
    WebSocket,
    WebSocketError,
)

log = logging.getLogger("webterm")

STATIC_ROOT = Path(__file__).parent / "static"
WS_PATH = "/ws"
MAX_HTTP_HEADER = 64 * 1024

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
}


def _content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _nonneg_int(value: object) -> int:
    """Coerce a JSON value to a non-negative int, defaulting to 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(number, 0)


def check_auth(headers: dict[str, str], token: str | None) -> bool:
    """Return True when a request is allowed to proceed.

    With no configured token every request is allowed, preserving webterm's
    historical localhost-only behaviour.  With a token set, HTTP Basic
    credentials must match: the username is ignored and either the username or
    the password field may carry the secret.  Comparison is constant-time.
    """
    if token is None:
        return True
    scheme, _, encoded = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    username, _, password = decoded.partition(":")
    return hmac.compare_digest(username, token) or hmac.compare_digest(password, token)


async def read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str]]:
    """Read an HTTP request line + headers.  Returns (method, path, headers)."""
    raw = await asyncio.wait_for(
        reader.readuntil(b"\r\n\r\n"), timeout=30
    )
    if len(raw) > MAX_HTTP_HEADER:
        raise WebSocketError("request headers too large")
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        raise WebSocketError("malformed request line")
    method, target = parts[0], parts[1]
    path = target.split("?", 1)[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return method, path, headers


def _http_response(
    status: str,
    body: bytes = b"",
    *,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: str = "",
) -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{extra_headers}"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1") + body


def resolve_static(path: str) -> Path | None:
    """Map a URL path to a file inside STATIC_ROOT, or None if not found."""
    if path in ("/", ""):
        path = "/index.html"
    relative = path.lstrip("/")
    candidate = (STATIC_ROOT / relative).resolve()
    try:
        candidate.relative_to(STATIC_ROOT.resolve())
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None


async def serve_static(writer: asyncio.StreamWriter, method: str, path: str) -> None:
    if method not in ("GET", "HEAD"):
        writer.write(_http_response("405 Method Not Allowed"))
        await writer.drain()
        return

    file_path = resolve_static(path)
    if file_path is None:
        writer.write(_http_response("404 Not Found", b"Not found\n"))
        await writer.drain()
        return

    body = file_path.read_bytes()
    response = _http_response("200 OK", body, content_type=_content_type(file_path))
    writer.write(response)
    if method == "GET":
        await writer.drain()


async def serve_config(writer: asyncio.StreamWriter, config: dict) -> None:
    """Serve the small runtime config script consumed by app.js."""
    body = ("window.WEBTERM_CONFIG = " + json.dumps(config) + ";\n").encode("utf-8")
    writer.write(
        _http_response(
            "200 OK",
            body,
            content_type="text/javascript; charset=utf-8",
            extra_headers="Cache-Control: no-store\r\n",
        )
    )
    await writer.drain()


class ServerControl:
    """Process-lifecycle requests raised by clients or signals.

    A single instance is shared by every connection.  ``shutdown`` is set for
    both a plain stop and a restart; ``restart_requested`` distinguishes the
    two so the entry point can re-exec the process instead of exiting.
    """

    def __init__(self) -> None:
        self.shutdown = asyncio.Event()
        self.restart_requested = False

    def request_stop(self) -> None:
        self.shutdown.set()

    def request_restart(self) -> None:
        self.restart_requested = True
        self.shutdown.set()


class ClientConnection:
    """One browser connection: fans session output to it and handles commands."""

    def __init__(
        self, ws: WebSocket, manager: SessionManager, control: ServerControl | None = None
    ) -> None:
        self.ws = ws
        self.manager = manager
        self.control = control
        self.out_queue: asyncio.Queue[tuple[str, int, bytes | None]] = asyncio.Queue(maxsize=2048)
        self.subscriptions = set()
        self.closed = False
        self._backpressure = False
        self._closed_event = asyncio.Event()

    # ---------------------------------------------------- Subscriber protocol

    def offer_output(self, session, data: bytes, offset: int) -> None:
        self._offer((session.id, offset, data))

    def offer_exit(self, session) -> None:
        self._offer((session.id, 0, None))

    def request_close(self) -> None:
        """Ask the connection loop to shut down (used during server stop)."""
        self._closed_event.set()

    def _offer(self, item: tuple[str, int, bytes | None]) -> None:
        if self.closed:
            return
        try:
            self.out_queue.put_nowait(item)
        except asyncio.QueueFull:
            # The client is too slow.  Rather than corrupt its terminal by
            # dropping output, disconnect it; it can reconnect and replay.
            if not self._backpressure:
                self._backpressure = True
                log.warning("client too slow, disconnecting")
                self._closed_event.set()

    # ------------------------------------------------------------ main loop

    async def run(self) -> None:
        sender = asyncio.create_task(self._send_loop())
        reader = asyncio.create_task(self._read_loop())
        closer = asyncio.create_task(self._closed_event.wait())
        try:
            await self.send_sessions()
            await asyncio.wait(
                {sender, reader, closer}, return_when=asyncio.FIRST_COMPLETED
            )
        except (WebSocketError, ConnectionClosed):
            pass
        finally:
            self.closed = True
            for session in list(self.subscriptions):
                session.detach(self)
            self.subscriptions.clear()
            for task in (sender, reader, closer):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await self.ws.close()

    async def send_sessions(self) -> None:
        if self.closed:
            return
        try:
            await self.ws.send_json(
                {"type": "sessions", "sessions": self.manager.list_info()}
            )
        except (ConnectionClosed, WebSocketError):
            self._closed_event.set()

    async def send_restarting(self) -> None:
        """Tell the browser the server is about to restart and reconnect."""
        if self.closed:
            return
        try:
            await self.ws.send_json({"type": "restarting"})
        except (ConnectionClosed, WebSocketError):
            self._closed_event.set()

    async def _send_loop(self) -> None:
        while True:
            session_id, offset, payload = await self.out_queue.get()
            if payload is None:
                await self.ws.send_json({"type": "exit", "session_id": session_id})
                continue
            sid = session_id.encode("utf-8")
            # The 8-byte big-endian offset marks where this payload begins in
            # the session's absolute output stream, so the client can resume
            # exactly (skipping overlap or detecting a gap) after a reconnect.
            await self.ws.send_binary(
                bytes([len(sid)]) + sid + struct.pack(">Q", offset) + payload
            )

    async def _read_loop(self) -> None:
        while True:
            message = await self.ws.recv()
            if message is None:
                return
            opcode, payload = message
            if opcode != OP_TEXT:
                continue
            try:
                data = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                log.debug("ignoring malformed message")
                continue
            if isinstance(data, dict):
                try:
                    await self._handle(data)
                except (ConnectionClosed, WebSocketError):
                    return
                except Exception:  # noqa: BLE001 - keep the connection alive
                    log.exception("error handling message %r", data.get("type"))

    async def _handle(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "subscribe":
            await self._subscribe(
                str(message.get("session_id", "")), _nonneg_int(message.get("since"))
            )
        elif kind == "unsubscribe":
            self._unsubscribe(str(message.get("session_id", "")))
        elif kind == "create":
            self._create(message)
        elif kind == "kill":
            self.manager.kill(str(message.get("session_id", "")))
        elif kind == "rename":
            self._rename(message)
        elif kind == "input":
            self._input(message)
        elif kind == "resize":
            self._resize(message)
        elif kind == "list":
            await self.send_sessions()
        elif kind == "restart":
            self._restart()
        else:
            log.debug("unknown message type %r", kind)

    def _restart(self) -> None:
        if self.control is None:
            log.debug("restart requested but server control is unavailable")
            return
        log.info("restart requested by client")
        self.control.request_restart()

    async def _subscribe(self, session_id: str, since: int = 0) -> None:
        session = self.manager.get(session_id)
        if session is None:
            await self.send_sessions()
            return
        if session not in self.subscriptions:
            # Attach and enqueue the replay without awaiting in between so no
            # live output can slip ahead of the replayed scrollback.  When the
            # client's ``since`` offset has been evicted the replay starts at
            # the earliest retained byte; the client notices the gap from the
            # frame offset and resets its terminal.
            self.subscriptions.add(session)
            session.attach(self)
            start, replay = session.replay_since(since)
            if replay:
                self._offer((session.id, start, replay))
        await self.ws.send_json(
            {
                "type": "attached",
                "session": session.info(),
            }
        )

    def _unsubscribe(self, session_id: str) -> None:
        session = self.manager.get(session_id)
        if session is not None:
            session.detach(self)
            self.subscriptions.discard(session)

    def _create(self, message: dict) -> None:
        command = message.get("command")
        if command is not None:
            if isinstance(command, str):
                command = command if command.strip() else None
            elif (
                not isinstance(command, list)
                or not command
                or not all(isinstance(arg, str) for arg in command)
            ):
                log.debug("ignoring invalid command %r", command)
                command = None
        self.manager.create(
            cols=int(message.get("cols", 80)),
            rows=int(message.get("rows", 24)),
            title=message.get("title"),
            command=command,
        )

    def _rename(self, message: dict) -> None:
        session = self.manager.get(str(message.get("session_id", "")))
        if session is not None:
            session.title = str(message.get("title", session.title))[:200]
            self.manager.notify()

    def _input(self, message: dict) -> None:
        session = self.manager.get(str(message.get("session_id", "")))
        if session is None:
            return
        data = message.get("data", "")
        if isinstance(data, str) and data:
            session.write(data.encode("utf-8"))

    def _resize(self, message: dict) -> None:
        session = self.manager.get(str(message.get("session_id", "")))
        if session is not None:
            session.resize(
                int(message.get("cols", session.cols)),
                int(message.get("rows", session.rows)),
            )


def _consume_task_exception(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.debug("background task failed", exc_info=task.exception())


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    manager: SessionManager,
    connections: set[ClientConnection],
    config: dict | None = None,
    control: ServerControl | None = None,
    token: str | None = None,
) -> None:
    try:
        try:
            method, path, headers = await read_http_request(reader)
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            WebSocketError,
            asyncio.LimitOverrunError,
        ):
            return

        if not check_auth(headers, token):
            writer.write(
                _http_response(
                    "401 Unauthorized",
                    b"Authentication required\n",
                    extra_headers='WWW-Authenticate: Basic realm="webterm"\r\n',
                )
            )
            with contextlib.suppress(Exception):
                await writer.drain()
            return

        is_upgrade = headers.get("upgrade", "").lower() == "websocket"
        if is_upgrade and path == WS_PATH:
            try:
                ws = await WebSocket.handshake(reader, writer, headers)
            except WebSocketError:
                writer.write(_http_response("400 Bad Request", b"Bad WebSocket request\n"))
                with contextlib.suppress(Exception):
                    await writer.drain()
                return
            connection = ClientConnection(ws, manager, control)
            connections.add(connection)
            try:
                await connection.run()
            finally:
                connections.discard(connection)
            return

        if path == "/config.js":
            await serve_config(writer, config or {})
            return

        await serve_static(writer, method, path)
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def serve(
    host: str,
    port: int,
    manager: SessionManager,
    config: dict | None = None,
    *,
    token: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> bool:
    """Run the server until stopped.  Returns True if a restart was requested."""
    connections: set[ClientConnection] = set()
    client_tasks: set[asyncio.Task] = set()
    control = ServerControl()

    def on_sessions_changed() -> None:
        for connection in list(connections):
            task = asyncio.create_task(connection.send_sessions())
            task.add_done_callback(_consume_task_exception)

    manager.add_listener(on_sessions_changed)

    loop = asyncio.get_running_loop()

    def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = loop.create_task(
            handle_client(reader, writer, manager, connections, config, control, token)
        )
        client_tasks.add(task)
        task.add_done_callback(client_tasks.discard)

    server = await asyncio.start_server(on_client, host, port, ssl=ssl_context)
    addresses = ", ".join(
        f"{sock.getsockname()[0]}:{sock.getsockname()[1]}"
        for sock in server.sockets or ()
    )
    scheme = "https" if ssl_context is not None else "http"
    log.info("webterm listening on %s://%s", scheme, addresses)

    for sig, request in (
        (signal.SIGINT, control.request_stop),
        (signal.SIGTERM, control.request_stop),
        (signal.SIGHUP, control.request_restart),
    ):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request)

    try:
        await control.shutdown.wait()
    finally:
        log.info("restarting" if control.restart_requested else "shutting down")
        if control.restart_requested:
            # Let every browser know this is a deliberate restart, not a crash,
            # so it can show the right status while it reconnects.
            pending = [
                asyncio.create_task(connection.send_restarting())
                for connection in list(connections)
            ]
            await asyncio.gather(*pending, return_exceptions=True)
        # Stop accepting, then disconnect clients.  As of Python 3.12
        # Server.wait_closed() waits for every active connection, so an open
        # browser WebSocket would otherwise make Ctrl+C hang forever.
        server.close()
        for connection in list(connections):
            connection.request_close()
        manager.close_all()
        for task in list(client_tasks):
            task.cancel()
        if client_tasks:
            await asyncio.gather(*client_tasks, return_exceptions=True)
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=2)
    return control.restart_requested

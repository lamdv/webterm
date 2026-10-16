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
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

from .control import ControlServer, default_control_socket, looks_like_tcp
from .session import (
    DepthExceededError,
    Session,
    SessionError,
    SessionLimitError,
    SessionManager,
)
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


#: Cookie carrying the browser session established by the ``?bootstrap=`` flow.
COOKIE_NAME = "webterm_session"


def _constant_time_equals(a: str, b: str) -> bool:
    """Compare two strings without leaking their contents via timing.

    ``hmac.compare_digest`` only accepts ASCII text, so encode first; the
    surrogateescape handler keeps arbitrary header bytes from raising.
    """
    return hmac.compare_digest(
        a.encode("utf-8", "surrogateescape"), b.encode("utf-8", "surrogateescape")
    )


def session_cookie_value(token: str) -> str:
    """Derive the opaque ``COOKIE_NAME`` value for *token*.

    One-way and deterministic, so the browser cookie never exposes the shared
    secret even though it grants the same access.
    """
    return hmac.new(token.encode("utf-8"), b"webterm-session", "sha256").hexdigest()


def _cookie(headers: dict[str, str], name: str) -> str | None:
    for chunk in headers.get("cookie", "").split(";"):
        key, _, value = chunk.strip().partition("=")
        if key == name and value:
            return value
    return None


def _check_basic(header: str, token: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    username, _, password = decoded.partition(":")
    return _constant_time_equals(username, token) or _constant_time_equals(password, token)


def check_auth(headers: dict[str, str], token: str | None) -> bool:
    """Return True when a request is allowed to proceed.

    With no configured token every request is allowed, preserving webterm's
    historical localhost-only behaviour.  With a token set, either HTTP Basic
    credentials (the username is ignored; either field may carry the secret) or
    a ``COOKIE_NAME`` cookie from the bootstrap flow is accepted.  All
    comparisons are constant-time.
    """
    if token is None:
        return True
    if _check_basic(headers.get("authorization", ""), token):
        return True
    cookie = _cookie(headers, COOKIE_NAME)
    return cookie is not None and _constant_time_equals(cookie, session_cookie_value(token))


async def read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], dict[str, str]]:
    """Read an HTTP request line + headers.

    Returns ``(method, path, query, headers)``.  *path* excludes the query
    string; *query* holds the decoded single-valued parameters.
    """
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
    path, _, query_string = target.partition("?")
    query = {
        key: values[0]
        for key, values in parse_qs(query_string, keep_blank_values=True).items()
        if values
    }
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return method, path, query, headers


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


class RequestError(Exception):
    """A client request was rejected; carries a protocol error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ClientConnection:
    """One client (browser or local agent) attached to the session manager.

    The transport is either the browser :class:`~webterm.websocket.WebSocket`
    or a local :class:`~webterm.transport.FramedTransport`; one dispatcher
    serves both.  Requests may carry an ``id`` and then receive a correlated
    ``result`` / ``error``; browser requests omit it and stay fire-and-forget.
    """

    def __init__(
        self,
        transport,
        manager: SessionManager,
        control: ServerControl | None = None,
        *,
        peer: str = "browser",
        profiles: dict[str, dict] | None = None,
    ) -> None:
        self.transport = transport
        self.manager = manager
        self.control = control
        self.peer = peer
        self.profiles = profiles or {}
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
                await self.transport.close()

    async def send_sessions(self) -> None:
        if self.closed:
            return
        try:
            await self.transport.send_json(
                {
                    "type": "sessions",
                    "sessions": self.manager.list_info(include_private=self.peer == "agent"),
                }
            )
        except (ConnectionClosed, WebSocketError):
            self._closed_event.set()

    async def send_restarting(self) -> None:
        """Tell the browser the server is about to restart and reconnect."""
        if self.closed:
            return
        try:
            await self.transport.send_json({"type": "restarting"})
        except (ConnectionClosed, WebSocketError):
            self._closed_event.set()

    async def _send_loop(self) -> None:
        while True:
            session_id, offset, payload = await self.out_queue.get()
            if payload is None:
                await self.transport.send_json({"type": "exit", "session_id": session_id})
                continue
            sid = session_id.encode("utf-8")
            # The 8-byte big-endian offset marks where this payload begins in
            # the session's absolute output stream, so the client can resume
            # exactly (skipping overlap or detecting a gap) after a reconnect.
            await self.transport.send_binary(
                bytes([len(sid)]) + sid + struct.pack(">Q", offset) + payload
            )

    async def _read_loop(self) -> None:
        while True:
            message = await self.transport.recv()
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
            if not isinstance(data, dict):
                continue
            request_id = data.get("id")
            try:
                result = await self._dispatch(data)
            except RequestError as exc:
                await self._send_error(request_id, exc.code, str(exc))
                continue
            except (ConnectionClosed, WebSocketError):
                return
            except Exception:  # noqa: BLE001 - keep the connection alive
                log.exception("error handling message %r", data.get("type"))
                await self._send_error(request_id, "internal", "internal error")
                continue
            if request_id is not None:
                await self._send_result(request_id, result)

    async def _send_result(self, request_id: object, result: dict | None) -> None:
        try:
            await self.transport.send_json(
                {"type": "result", "id": request_id, "result": result or {}}
            )
        except (ConnectionClosed, WebSocketError):
            self._closed_event.set()

    async def _send_error(self, request_id: object, code: str, message: str) -> None:
        if request_id is None:
            return
        try:
            await self.transport.send_json(
                {"type": "error", "id": request_id, "error": {"code": code, "message": message}}
            )
        except (ConnectionClosed, WebSocketError):
            self._closed_event.set()

    async def _dispatch(self, message: dict) -> dict:
        kind = message.get("type")
        if kind == "subscribe":
            info = await self._subscribe(
                str(message.get("session_id", "")), _nonneg_int(message.get("since"))
            )
            return {"session": info}
        if kind == "unsubscribe":
            self._unsubscribe(str(message.get("session_id", "")))
            return {"ok": True}
        if kind == "create":
            return {"session": self._create(message).info()}
        if kind == "kill":
            killed = self.manager.kill(
                str(message.get("session_id", "")), tree=bool(message.get("tree", True))
            )
            return {"ok": bool(killed), "killed": killed}
        if kind == "rename":
            return self._rename(message)
        if kind == "input":
            self._input(message)
            return {"ok": True}
        if kind == "resize":
            self._resize(message)
            return {"ok": True}
        if kind == "list":
            result = {
                "sessions": self.manager.list_info(include_private=self.peer == "agent")
            }
            if message.get("id") is None:
                await self.send_sessions()
            return result
        if kind == "run":
            return await self._run(message, spawn=False)
        if kind == "spawn":
            return await self._run(message, spawn=True)
        if kind == "send":
            return self._send(message)
        if kind == "capture":
            return self._capture(message)
        if kind == "wait":
            return await self._wait(message)
        if kind == "close":
            return self._close(message)
        if kind == "restart":
            self._restart()
            return {"ok": True}
        log.debug("unknown message type %r", kind)
        raise RequestError("bad_request", f"unknown message type {kind!r}")

    def _restart(self) -> None:
        if self.control is None:
            log.debug("restart requested but server control is unavailable")
            return
        log.info("restart requested by client")
        self.control.request_restart()

    async def _subscribe(self, session_id: str, since: int = 0) -> dict | None:
        session = self.manager.get(session_id)
        if session is None:
            await self.send_sessions()
            return None
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
        await self.transport.send_json(
            {
                "type": "attached",
                "session": session.info(),
            }
        )
        return session.info()

    def _unsubscribe(self, session_id: str) -> None:
        session = self.manager.get(session_id)
        if session is not None:
            session.detach(self)
            self.subscriptions.discard(session)

    def _create(self, message: dict) -> Session:
        command = _valid_command(message.get("command"))
        try:
            return self.manager.create(
                cols=int(message.get("cols", 80)),
                rows=int(message.get("rows", 24)),
                title=message.get("title"),
                command=command,
                cwd=message.get("cwd"),
                env=message.get("env"),
                parent_id=message.get("parent_id"),
                origin=str(message.get("origin", "user")),
                role=str(message.get("role", "shell")),
                agent=message.get("agent"),
                task=message.get("task"),
                visibility=str(message.get("visibility", "public")),
            )
        except SessionError as exc:
            raise _request_error(exc) from exc

    def _rename(self, message: dict) -> dict:
        session = self._require(message)
        session.title = str(message.get("title", session.title))[:200]
        self.manager.notify()
        return {"session": session.info()}

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

    # -------------------------------------------------------- agent verbs

    def _require(self, message: dict) -> Session:
        session = self.manager.get(str(message.get("session_id", "")))
        if session is None:
            raise RequestError("not_found", f"unknown session {message.get('session_id')!r}")
        return session

    def _resolve_agent(
        self, agent: str | None, task: str | None, command, interactive: bool
    ) -> list[str] | str:
        """Turn an agent profile + task into the argv to run."""
        command = _valid_command(command)
        if command is not None:
            return command
        if not agent:
            raise RequestError("bad_request", "agent or command is required")
        profile = self.profiles.get(agent)
        if profile is None:
            raise RequestError("bad_request", f"unknown agent {agent!r}")
        key = "interactive" if interactive else "print"
        template = profile.get(key)
        if not template:
            raise RequestError("bad_request", f"agent {agent!r} has no {key} command")
        parts = [str(part) for part in template]
        if task and any("{task}" in part for part in parts):
            return [part.replace("{task}", task) for part in parts]
        if task:
            parts.append(task)
        return parts

    async def _run(self, message: dict, *, spawn: bool) -> dict:
        interactive = bool(message.get("interactive")) or message.get("mode") == "interactive"
        task = message.get("task")
        agent = message.get("agent")
        common = dict(
            cwd=message.get("cwd"),
            env=message.get("env"),
            title=message.get("title"),
            cols=int(message.get("cols", 80)),
            rows=int(message.get("rows", 24)),
            parent_id=message.get("parent_id") or None,
            visibility=str(message.get("visibility", "public")),
        )
        try:
            if spawn:
                argv = self._resolve_agent(agent, task, message.get("command"), interactive)
                session = self.manager.spawn(
                    argv, interactive=interactive, agent=agent, task=task, **common
                )
            else:
                command = _valid_command(message.get("command"))
                if command is None:
                    raise RequestError("bad_request", "command is required")
                if interactive:
                    session = self.manager.create(
                        command=command, origin="agent", **common
                    )
                else:
                    session = self.manager.create_single(
                        command, origin="agent", **common
                    )
        except RequestError:
            raise
        except SessionError as exc:
            raise _request_error(exc) from exc
        except ValueError as exc:
            raise RequestError("bad_request", str(exc)) from exc

        wait = bool(message.get("wait", not interactive))
        if not wait:
            return {"session_id": session.id, "session": session.info()}
        code = await session.wait(timeout=_optional_float(message.get("timeout")))
        result = self._output_result(session, message)
        result["exit_code"] = code
        result["timed_out"] = code is None
        return result

    def _output_result(self, session: Session, message: dict) -> dict:
        fmt = str(message.get("format", "base64"))
        start, end, data = session.capture(
            0, strip_ansi=bool(message.get("strip_ansi", False))
        )
        result: dict[str, object] = {
            "session_id": session.id,
            "start": start,
            "end": end,
            "total": session.total,
            "truncated": start > 0,
            "output_base64": base64.b64encode(data).decode("ascii"),
        }
        if fmt == "text":
            result["output_text"] = data.decode("utf-8", "replace")
        return result

    def _send(self, message: dict) -> dict:
        session = self._require(message)
        data = message.get("data", "")
        if not isinstance(data, str):
            raise RequestError("bad_request", "data must be a string")
        if message.get("submit"):
            data += "\n"
        if data:
            session.write(data.encode("utf-8"))
        return {"ok": True}

    def _capture(self, message: dict) -> dict:
        session = self._require(message)
        fmt = str(message.get("format", "base64"))
        start, end, data = session.capture(
            _nonneg_int(message.get("since")), strip_ansi=bool(message.get("strip_ansi", False))
        )
        result: dict[str, object] = {"start": start, "end": end, "total": session.total}
        if fmt == "text":
            result["text"] = data.decode("utf-8", "replace")
        else:
            result["data_base64"] = base64.b64encode(data).decode("ascii")
        return result

    async def _wait(self, message: dict) -> dict:
        session = self._require(message)
        until = str(message.get("until", "exit"))
        timeout = _optional_float(message.get("timeout"))
        if until == "text":
            pattern = message.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                raise RequestError("bad_request", "pattern is required for until=text")
            matched, offset, match = await session.wait_for_text(
                pattern,
                since=_nonneg_int(message.get("since")),
                timeout=timeout,
                strip_ansi=bool(message.get("strip_ansi", True)),
            )
            return {
                "matched": matched,
                "offset": offset,
                "match": match,
                "exit_code": session.exit_code,
            }
        if until == "idle":
            matched = await session.wait_for_idle(
                int(message.get("quiet_ms", 500)), timeout=timeout
            )
            return {"matched": matched, "offset": session.total, "exit_code": session.exit_code}
        if until == "exit":
            code = await session.wait(timeout=timeout)
            return {"matched": code is not None, "exit_code": code}
        raise RequestError("bad_request", f"unknown wait target {until!r}")

    def _close(self, message: dict) -> dict:
        session_id = str(message.get("session_id", ""))
        if self.manager.get(session_id) is None:
            raise RequestError("not_found", f"unknown session {session_id!r}")
        killed = self.manager.kill(session_id, tree=False)
        return {"ok": bool(killed), "killed": killed}


def _valid_command(command) -> list[str] | str | None:
    """Normalise a create/run command, rejecting malformed values."""
    if command is None:
        return None
    if isinstance(command, str):
        return command if command.strip() else None
    if isinstance(command, list) and command and all(isinstance(arg, str) for arg in command):
        return command
    log.debug("ignoring invalid command %r", command)
    return None


def _optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _request_error(exc: SessionError) -> RequestError:
    if isinstance(exc, DepthExceededError):
        return RequestError("depth_exceeded", str(exc))
    if isinstance(exc, SessionLimitError):
        return RequestError("too_many_sessions", str(exc))
    return RequestError("bad_request", str(exc))


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
    profiles: dict | None = None,
) -> None:
    try:
        try:
            method, path, query, headers = await read_http_request(reader)
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            WebSocketError,
            asyncio.LimitOverrunError,
        ):
            return

        # The desktop app navigates once to /?bootstrap=<token>; exchange it
        # for an HttpOnly cookie and strip the secret from the URL.  Only
        # document/asset requests are eligible, not the WebSocket upgrade.
        bootstrap = query.get("bootstrap")
        if (
            token is not None
            and path != WS_PATH
            and bootstrap is not None
            and _constant_time_equals(bootstrap, token)
        ):
            writer.write(
                _http_response(
                    "302 Found",
                    extra_headers=(
                        "Location: /\r\n"
                        f"Set-Cookie: {COOKIE_NAME}={session_cookie_value(token)}; "
                        "HttpOnly; SameSite=Strict; Path=/\r\n"
                        "Cache-Control: no-store\r\n"
                    ),
                )
            )
            with contextlib.suppress(Exception):
                await writer.drain()
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
            connection = ClientConnection(ws, manager, control, peer="browser", profiles=profiles)
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


class ServerHandle:
    """A bound webterm server that can be driven programmatically.

    :func:`start_server` returns one of these once the listener is up.  An
    embedded host (for example the Tauri desktop app) can read :attr:`port` /
    :attr:`url`, then ``await wait()`` or ``await stop()``.  ``manager`` and
    ``control`` are exposed so a host can attach further control surfaces (such
    as an agent/MCP control socket) without reaching into module state.
    """

    def __init__(
        self,
        server: asyncio.AbstractServer,
        control: ServerControl,
        manager: SessionManager,
        connections: set[ClientConnection],
        client_tasks: set[asyncio.Task],
        host: str,
        port: int,
        scheme: str,
        on_sessions_changed: Callable[[], None],
        control_server: ControlServer | None = None,
    ) -> None:
        self.server = server
        self.control = control
        self.manager = manager
        self.connections = connections
        self.client_tasks = client_tasks
        self.host = host
        self.port = port
        self.scheme = scheme
        self.control_server = control_server
        self._on_sessions_changed = on_sessions_changed
        self._stopped = False

    @property
    def url(self) -> str:
        """Base URL a client should load, suitable for a webview or browser."""
        host = self.host if self.host not in ("", "0.0.0.0", "::") else "127.0.0.1"
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def control_socket(self) -> str | None:
        """Path to the local control socket, if one is running."""
        return str(self.control_server.path) if self.control_server is not None else None

    def request_stop(self) -> None:
        """Ask the server to shut down (safe from a signal handler)."""
        self.control.request_stop()

    def request_restart(self) -> None:
        """Ask the server to restart, exiting with the restart flag set."""
        self.control.request_restart()

    async def wait(self) -> bool:
        """Run until stopped, tear down, and return whether a restart was asked."""
        try:
            await self.control.shutdown.wait()
        finally:
            await self.stop()
        return self.control.restart_requested

    async def stop(self) -> None:
        """Close the listener, disconnect clients, and kill every session."""
        if self._stopped:
            return
        self._stopped = True
        control = self.control
        log.info("restarting" if control.restart_requested else "shutting down")
        if control.restart_requested:
            # Let every browser know this is a deliberate restart, not a crash,
            # so it can show the right status while it reconnects.
            pending = [
                asyncio.create_task(connection.send_restarting())
                for connection in list(self.connections)
            ]
            await asyncio.gather(*pending, return_exceptions=True)
        # Stop accepting, then disconnect clients.  As of Python 3.12
        # Server.wait_closed() waits for every active connection, so an open
        # browser WebSocket would otherwise make Ctrl+C hang forever.
        self.server.close()
        if self.control_server is not None:
            await self.control_server.stop()
            self.control_server = None
        for connection in list(self.connections):
            connection.request_close()
        self.manager.close_all()
        self.manager.remove_listener(self._on_sessions_changed)
        for task in list(self.client_tasks):
            task.cancel()
        if self.client_tasks:
            await asyncio.gather(*self.client_tasks, return_exceptions=True)
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(self.server.wait_closed(), timeout=2)


async def start_server(
    host: str,
    port: int,
    manager: SessionManager,
    config: dict | None = None,
    *,
    token: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
    enable_control: bool = True,
    control_socket: str | None = None,
    profiles: dict | None = None,
) -> ServerHandle:
    """Bind the listener and start serving, returning a :class:`ServerHandle`.

    This does not block or install signal handlers, so it is safe to call from
    an embedded host running the event loop on a worker thread.  Use
    ``await handle.wait()`` to block until stopped.  :func:`serve` wraps this
    for the CLI together with main-thread signal handling.

    Unless *enable_control* is false, a local ``AF_UNIX`` control socket is
    also started (at *control_socket* or the default per-user path), and every
    child process is told where to find it via ``WEBTERM_SOCKET``.
    """
    connections: set[ClientConnection] = set()
    client_tasks: set[asyncio.Task] = set()
    control = ServerControl()

    # Validate the control path before binding the TCP listener so a
    # misconfiguration cannot leave a half-started server behind.
    control_path: Path | None = None
    if enable_control:
        control_path = Path(control_socket) if control_socket else default_control_socket()
        if looks_like_tcp(str(control_path)):
            raise ValueError(f"control socket must be a local path, got {control_path!r}")

    def on_sessions_changed() -> None:
        for connection in list(connections):
            task = asyncio.create_task(connection.send_sessions())
            task.add_done_callback(_consume_task_exception)

    manager.add_listener(on_sessions_changed)

    loop = asyncio.get_running_loop()

    def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = loop.create_task(
            handle_client(
                reader, writer, manager, connections, config, control, token, profiles
            )
        )
        client_tasks.add(task)
        task.add_done_callback(client_tasks.discard)

    server = await asyncio.start_server(on_client, host, port, ssl=ssl_context)
    sockets = server.sockets or []
    bound_host = sockets[0].getsockname()[0] if sockets else host
    bound_port = sockets[0].getsockname()[1] if sockets else port
    scheme = "https" if ssl_context is not None else "http"
    addresses = ", ".join(
        f"{sock.getsockname()[0]}:{sock.getsockname()[1]}" for sock in sockets
    )
    log.info("webterm listening on %s://%s", scheme, addresses)
    handle = ServerHandle(
        server,
        control,
        manager,
        connections,
        client_tasks,
        bound_host,
        bound_port,
        scheme,
        on_sessions_changed,
    )

    if enable_control and control_path is not None:
        def make_control_connection(transport):
            return ClientConnection(
                transport, manager, control, peer="agent", profiles=profiles
            )

        control_server = ControlServer(
            manager,
            make_control_connection,
            path=control_path,
            url=handle.url,
            connections=connections,
        )
        await control_server.start()
        handle.control_server = control_server
        manager.set_control_env(socket=str(control_path), url=handle.url)
    return handle


async def serve(
    host: str,
    port: int,
    manager: SessionManager,
    config: dict | None = None,
    *,
    token: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
    enable_control: bool = True,
    control_socket: str | None = None,
    profiles: dict | None = None,
) -> bool:
    """Run the server until stopped.  Returns True if a restart was requested."""
    handle = await start_server(
        host,
        port,
        manager,
        config,
        token=token,
        ssl_context=ssl_context,
        enable_control=enable_control,
        control_socket=control_socket,
        profiles=profiles,
    )
    loop = asyncio.get_running_loop()
    for sig, request in (
        (signal.SIGINT, handle.request_stop),
        (signal.SIGTERM, handle.request_stop),
        (signal.SIGHUP, handle.request_restart),
    ):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request)
    return await handle.wait()

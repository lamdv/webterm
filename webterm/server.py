"""HTTP static file server + WebSocket endpoint for webterm."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
import shlex
import signal
import ssl
import struct
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from . import __version__
from .agent import (
    AgentClosed,
    AgentError,
    AgentSession,
    AgentTimeout,
)
from .agent.codec import NATIVE_CODEC, AgentCodec, CodecError, UnknownDialog
from .agent.protocol import CanonicalCommandError, response_record
from .control import ControlServer, default_control_socket, looks_like_tcp
from .integration.pi import (
    HISTORY_ENTRY_LIMIT,
    MAX_HISTORY_ENTRY_LIMIT,
    PiCanonicalTranslator,
    PiError,
    PiSession,
    PiSessionClosed,
    pi_command_images,
    pi_dialog_to_canonical,
    pi_ui_request_to_events,
    pi_ui_resolved_to_canonical,
)
from .integration.pi.codec import PI_CODEC
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
#: Unauthenticated service-discovery document (see :func:`serve_discovery`).
DISCOVERY_PATH = "/.well-known/webterm.json"
#: Wire-protocol version advertised in the discovery document.  A client uses
#: it to refuse a server whose protocol it does not understand.
DISCOVERY_PROTOCOL = 1
MAX_HTTP_HEADER = 64 * 1024
#: Timeout for one canonical agent command round trip.
AGENT_COMMAND_TIMEOUT = 30.0
#: Entries returned by one `list_entries` call (directories and files together).
MAX_LISTED_ENTRIES = 500

#: Codecs by ``session.adapter``.  An adapter nobody registered falls through
#: to the canonical codec, which is what a plain canonical agent speaks.
AGENT_CODECS: dict[str, AgentCodec] = {"pi": PI_CODEC}


def _agent_codec(session: object) -> AgentCodec | None:
    """Return the codec for a structured agent session, else ``None``."""
    if getattr(session, "kind", "") != "agent":
        return None
    adapter = getattr(session, "adapter", None)
    return AGENT_CODECS.get(adapter, NATIVE_CODEC)


def _is_pi(session: object) -> bool:
    """True for a pi-backed agent session (it keeps the legacy ``pi_*`` wire)."""
    return getattr(session, "adapter", None) == "pi"


def _legacy_pi_error() -> RequestError:
    """The error for pi's legacy messages aimed at another adapter."""
    return RequestError(
        "bad_request", "session is not a pi session (use the agent envelope)"
    )

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


def _check_bearer(header: str, token: str) -> bool:
    """Accept ``Authorization: Bearer <token>``.

    Some reverse proxies and native clients prefer a bearer credential to
    Basic (which needs a base64 round-trip), so webterm accepts either.
    """
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return _constant_time_equals(value.strip(), token)


def check_auth(headers: dict[str, str], token: str | None) -> bool:
    """Return True when a request is allowed to proceed.

    With no configured token every request is allowed, preserving webterm's
    historical localhost-only behaviour.  With a token set, any one of the
    following is accepted:

    - HTTP Basic credentials (the username is ignored; either field may carry
      the secret),
    - ``Authorization: Bearer <token>``, for clients and reverse proxies that
      forward a bearer credential,
    - a ``COOKIE_NAME`` cookie from the bootstrap flow.

    All comparisons are constant-time.
    """
    if token is None:
        return True
    authorization = headers.get("authorization", "")
    if _check_basic(authorization, token) or _check_bearer(authorization, token):
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


async def serve_pi_download(
    writer: asyncio.StreamWriter,
    manager: SessionManager,
    method: str,
    query: dict[str, str],
) -> None:
    """Serve an exported pi file from the managed root as an attachment."""
    if method not in ("GET", "HEAD"):
        writer.write(_http_response("405 Method Not Allowed"))
        await writer.drain()
        return
    path = manager.resolve_pi_path(query.get("path"))
    if path is None or not os.path.isfile(path):
        writer.write(_http_response("404 Not Found", b"Not found\n"))
        await writer.drain()
        return
    try:
        body = Path(path).read_bytes()
    except OSError:
        writer.write(_http_response("404 Not Found", b"Not found\n"))
        await writer.drain()
        return
    content_type = (
        "text/html; charset=utf-8" if path.endswith(".html") else _content_type(Path(path))
    )
    filename = os.path.basename(path).replace('"', "")
    writer.write(
        _http_response(
            "200 OK",
            body,
            content_type=content_type,
            extra_headers=f'Content-Disposition: attachment; filename="{filename}"\r\n',
        )
    )
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


async def serve_discovery(
    writer: asyncio.StreamWriter, token: str | None, method: str = "GET"
) -> None:
    """Serve the unauthenticated ``/.well-known/webterm.json`` document.

    A client uses this to confirm that a URL really is a webterm server and to
    learn how to authenticate *before* it holds any credentials.  It exposes
    nothing beyond the service name, the version and the accepted credential
    types, and can be disabled entirely with ``--no-discovery``.
    """
    payload = {
        "service": "webterm",
        "version": __version__,
        "protocol": DISCOVERY_PROTOCOL,
        "auth_required": token is not None,
        "auth_methods": ["basic", "bearer", "cookie"] if token is not None else [],
        "websocket_path": WS_PATH,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response = _http_response(
        "200 OK",
        body,
        content_type="application/json; charset=utf-8",
        extra_headers="Cache-Control: no-store\r\n",
    )
    if method == "HEAD":
        # Same headers, no body, so a probe can read Content-Length cheaply.
        header_block = response.split(b"\r\n\r\n", 1)[0]
        writer.write(header_block + b"\r\n\r\n")
    else:
        writer.write(response)
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
        # Items are tagged: ("output", (session_id, offset, bytes)),
        # ("exit", session_id), or ("json", message).  One queue keeps
        # terminal bytes and pi events in the order they were produced.
        self.out_queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=2048)
        self.subscriptions = set()
        self.closed = False
        self._backpressure = False
        self._closed_event = asyncio.Event()
        #: When true, pi sessions also emit the canonical ``agent_*`` shadow
        #: stream.  The legacy ``pi_*`` messages stay unchanged.
        self.agent_api = False
        self._pi_translators: dict[str, PiCanonicalTranslator] = {}
        self._agent_command_counter = 0

    # ---------------------------------------------------- Subscriber protocol

    def offer_output(self, session, data: bytes, offset: int) -> None:
        self._offer(("output", (session.id, offset, data)))

    def offer_exit(self, session) -> None:
        self._pi_translators.pop(session.id, None)
        self._offer(("exit", session.id))

    def offer_pi_event(self, session, seq: int, record: dict) -> None:
        self._offer_json(
            {"type": "pi_event", "session_id": session.id, "seq": seq, "record": record}
        )
        translator = self._pi_translators.get(session.id)
        if translator is None:
            return
        for event in translator.feed(record):
            self._offer_agent_event(session, event, seq=seq)

    def _offer_agent_event(self, session, event: dict, *, seq: int | None = None) -> None:
        """Queue one canonical event; several may share a pi ``seq``."""
        if seq is None:
            seq = int(getattr(session, "seq", 0))
        self._offer_json(
            {
                "type": "agent_event",
                "session_id": session.id,
                "seq": seq,
                "event": event,
            }
        )

    def offer_pi_ui_request(self, session, request: dict) -> None:
        self._offer_json(
            {"type": "pi_ui_request", "session_id": session.id, "request": request}
        )
        if session.id not in self._pi_translators:
            return
        dialog = pi_dialog_to_canonical(request)
        if dialog is not None:
            self._offer_json(
                {
                    "type": "agent_dialog_request",
                    "session_id": session.id,
                    "request": dialog,
                }
            )
            return
        # Extension UI records never reach ``offer_pi_event``, so the
        # fire-and-forget methods (notify, setTitle, setStatus, setWidget) are
        # shadowed here as notices, session info, and raw events.
        for event in pi_ui_request_to_events(request):
            self._offer_agent_event(session, event)

    def offer_pi_ui_resolved(self, session, request_id: str, reason: str) -> None:
        self._offer_json(
            {
                "type": "pi_ui_resolved",
                "session_id": session.id,
                "request_id": request_id,
                "reason": reason,
            }
        )
        if session.id in self._pi_translators:
            resolved = pi_ui_resolved_to_canonical({"id": request_id, "reason": reason})
            if resolved is not None:
                self._offer_json(
                    {
                        "type": "agent_dialog_resolved",
                        "session_id": session.id,
                        "dialog_id": resolved["id"],
                        "reason": resolved["reason"],
                    }
                )

    def offer_agent_event(self, session, seq: int, event: dict) -> None:
        """Receive one canonical event from a native agent session."""
        self._offer_agent_event(session, event, seq=seq)

    def offer_agent_dialog(self, session, request: dict) -> None:
        self._offer_json(
            {
                "type": "agent_dialog_request",
                "session_id": session.id,
                "request": request,
            }
        )

    def offer_agent_dialog_resolved(
        self, session, dialog_id: str, reason: str
    ) -> None:
        self._offer_json(
            {
                "type": "agent_dialog_resolved",
                "session_id": session.id,
                "dialog_id": dialog_id,
                "reason": reason,
            }
        )

    def _offer_json(self, message: dict) -> None:
        self._offer(("json", message))

    def request_close(self) -> None:
        """Ask the connection loop to shut down (used during server stop)."""
        self._closed_event.set()

    def _offer(self, item: tuple[str, object]) -> None:
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
            kind, payload = await self.out_queue.get()
            if kind == "exit":
                await self.transport.send_json({"type": "exit", "session_id": payload})
                continue
            if kind == "json":
                await self.transport.send_json(payload)
                continue
            session_id, offset, data = payload
            sid = session_id.encode("utf-8")
            # The 8-byte big-endian offset marks where this payload begins in
            # the session's absolute output stream, so the client can resume
            # exactly (skipping overlap or detecting a gap) after a reconnect.
            await self.transport.send_binary(
                bytes([len(sid)]) + sid + struct.pack(">Q", offset) + data
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
            if bool(message.get("agent_api", False)):
                self.agent_api = True
            info = await self._subscribe(
                str(message.get("session_id", "")), _nonneg_int(message.get("since"))
            )
            return {"session": info}
        if kind == "agent_api":
            await self._set_agent_api(bool(message.get("enabled", True)))
            return {"enabled": self.agent_api}
        if kind == "unsubscribe":
            self._unsubscribe(str(message.get("session_id", "")))
            return {"ok": True}
        if kind == "create":
            return {"session": (await self._create(message)).info()}
        if kind == "kill":
            killed = self.manager.kill(
                str(message.get("session_id", "")), tree=bool(message.get("tree", True))
            )
            return {"ok": bool(killed), "killed": killed}
        if kind == "rename":
            return await self._rename(message)
        if kind == "input":
            self._input(message)
            return {"ok": True}
        if kind == "resize":
            self._resize(message)
            return {"ok": True}
        if kind == "pi":
            return await self._pi_command(message)
        if kind == "agent":
            return await self._agent_command(message)
        if kind == "pi_snapshot":
            return await self._pi_snapshot(message)
        if kind == "pi_history":
            return await self._pi_history(message)
        if kind == "pi_ui_response":
            return self._pi_ui_response(message)
        if kind == "list_dirs":
            return self._list_dirs(message)
        if kind == "list_entries":
            return self._list_entries(message)
        if kind == "list_pi_sessions":
            return self._list_pi_sessions(message)
        if kind == "pi_models":
            return await self._pi_models(message)
        if kind == "pi_export":
            return await self._pi_export(message)
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

    async def _set_agent_api(self, enabled: bool) -> None:
        """Enable or disable the canonical shadow stream for this client."""
        self.agent_api = enabled
        if not enabled:
            self._pi_translators.clear()
            return
        for session in list(self.subscriptions):
            if _is_pi(session):
                self._pi_translators.setdefault(session.id, PiCanonicalTranslator())
                await self._send_agent_snapshot(session)

    async def _subscribe(self, session_id: str, since: int = 0) -> dict | None:
        session = self.manager.get(session_id)
        if session is None:
            await self.send_sessions()
            return None
        codec = _agent_codec(session)
        if self.agent_api and _is_pi(session):
            self._pi_translators.setdefault(session.id, PiCanonicalTranslator())
        if session not in self.subscriptions:
            self.subscriptions.add(session)
            session.attach(self)
            if codec is not None:
                # Queue the attach and snapshot through the send loop so live
                # events cannot overtake them.  Events produced during the
                # snapshot carry a higher seq and are applied by the client.
                self._offer_json({"type": "attached", "session": session.info()})
                if _is_pi(session):
                    await self._send_pi_snapshot(session)
                else:
                    await self._send_agent_snapshot(session)
                    for event_seq, event in session.events_since(since):
                        self._offer_agent_event(session, event, seq=event_seq)
                return session.info()
            # Attach and enqueue the replay without awaiting in between so no
            # live output can slip ahead of the replayed scrollback.  When the
            # client's ``since`` offset has been evicted the replay starts at
            # the earliest retained byte; the client notices the gap from the
            # frame offset and resets its terminal.
            start, replay = session.replay_since(since)
            if replay:
                self._offer(("output", (session.id, start, replay)))
        await self.transport.send_json(
            {
                "type": "attached",
                "session": session.info(),
            }
        )
        return session.info()

    async def _send_pi_snapshot(self, session: PiSession, *, strict: bool = False) -> None:
        try:
            snapshot = await session.snapshot()
        except (PiError, PiSessionClosed, TimeoutError) as exc:
            log.warning("pi session %s: snapshot failed: %s", session.id, exc)
            if strict:
                raise RequestError("pi_error", str(exc)) from exc
            return
        self._offer_json(
            {
                "type": "pi_resync",
                "session_id": session.id,
                # The raw pi envelope carries no capability map; the pi view
                # gates image input (and anything else) on this declaration.
                "capabilities": PI_CODEC.capabilities(session),
                **snapshot,
            }
        )
        if self.agent_api:
            await self._send_agent_snapshot(session)

    async def _send_agent_snapshot(
        self,
        session: Session | PiSession | AgentSession,
        *,
        snapshot: dict | None = None,
        strict: bool = False,
    ) -> dict | None:
        """Push the canonical snapshot for one attached agent session.

        Returns the canonical payload so a command can answer with the same
        data it pushed, or ``None`` when the snapshot failed and *strict* is
        false.  Never sends canonical traffic for an agent whose client did
        not opt in (pi keeps its legacy ``pi_resync`` path for those).
        """
        codec = _agent_codec(session)
        if codec is None:
            return None
        try:
            if snapshot is None:
                snapshot = await codec.snapshot(session)
        except (PiError, PiSessionClosed, TimeoutError, AgentError) as exc:
            log.warning("agent session %s: snapshot failed: %s", session.id, exc)
            if strict:
                raise RequestError("agent_error", str(exc)) from exc
            return None
        canonical = dict(snapshot)
        canonical.setdefault("capabilities", codec.capabilities(session))
        self._offer_json(
            {
                "type": "agent_resync",
                "session_id": session.id,
                **canonical,
            }
        )
        # The snapshot is authoritative; later live events must not continue
        # the translator's pre-resync message ids.
        self._pi_translators[session.id] = PiCanonicalTranslator()
        return canonical

    def _unsubscribe(self, session_id: str) -> None:
        session = self.manager.get(session_id)
        self._pi_translators.pop(session_id, None)
        if session is not None:
            session.detach(self)
            self.subscriptions.discard(session)

    async def _create(self, message: dict) -> Session | PiSession | AgentSession:
        mode = message.get("mode") or message.get("kind")
        if mode == "pi":
            return self._create_pi(message)
        if mode == "agent":
            return await self._create_agent(message)
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

    async def _create_agent(self, message: dict) -> AgentSession:
        """Start a native canonical agent session (its child speaks JSONL)."""
        command = _valid_command(message.get("command"))
        argv = shlex.split(command) if isinstance(command, str) else list(command or [])
        if not argv:
            raise RequestError("bad_request", "a structured agent needs a command")
        try:
            session = await self.manager.create_agent(
                argv,
                cwd=message.get("cwd"),
                env=message.get("env"),
                title=message.get("title"),
                parent_id=message.get("parent_id") or None,
                origin=str(message.get("origin", "user")),
                role=str(message.get("role", "agent")),
                agent=message.get("agent"),
                task=message.get("task"),
                visibility=str(message.get("visibility", "public")),
                trusted=None,
                adapter=str(message.get("adapter") or "generic"),
            )
        except SessionError as exc:
            raise _request_error(exc) from exc
        except (OSError, ValueError, AgentError) as exc:
            raise RequestError("bad_request", str(exc)) from exc
        prompt = message.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            try:
                await session.prompt(prompt)
            except (AgentError, TimeoutError) as exc:
                self.manager.kill(session.id, tree=False)
                raise RequestError("agent_error", str(exc)) from exc
        return session

    def _create_pi(self, message: dict) -> PiSession:
        if not self.manager.pi_available:
            raise RequestError(
                "unavailable",
                "pi is not installed on the server; install pi or configure a pi command",
            )
        trust = message.get("trust")
        if trust not in (None, "allow", "deny"):
            raise RequestError("bad_request", "trust must be 'allow' or 'deny'")
        trusted = trust == "allow"
        resume = message.get("resume")
        resume_path = None
        if resume is not None:
            resume_path = self.manager.resolve_pi_path(resume)
            if resume_path is None or not os.path.isfile(resume_path):
                raise RequestError("bad_request", "resume path is not a pi session file")
            owner = self.manager.pi_session_file_owner(resume_path)
            if owner is not None:
                raise RequestError(
                    "conflict",
                    f"this session is already open as {owner.id} ({owner.title})",
                )
        argv = list(self.manager.pi_command)
        argv.extend(["--mode", "rpc"])
        root = self.manager.pi_session_dir
        if root:
            argv.extend(["--session-dir", root])
        else:
            argv.append("--no-session")
        if resume_path is not None:
            argv.extend(["--session", resume_path])
        argv.append("--approve" if trusted else "--no-approve")
        if self.manager.pi_extension:
            argv.extend(["-e", self.manager.pi_extension])
        provider = message.get("provider")
        model = message.get("model")
        for name, value in (("provider", provider), ("model", model)):
            if value is not None and not isinstance(value, str):
                raise RequestError("bad_request", f"{name} must be a string")
        provider = provider.strip() if isinstance(provider, str) else None
        model = model.strip() if isinstance(model, str) else None
        if provider and not model:
            raise RequestError("bad_request", "provider requires a model id")
        if not model and resume_path is None:
            # A resumed session restores its own model, so the default only
            # applies to new conversations and never overrides a resume.
            model = self.manager.pi_default_model
        if provider:
            argv.extend(["--provider", provider])
        if model:
            argv.extend(["--model", model])
        pi_env = dict(message.get("env") or {})
        if root:
            # Nested pi processes started by this one stay in the managed root.
            pi_env["PI_CODING_AGENT_SESSION_DIR"] = root
        try:
            session = self.manager.create_pi(
                argv,
                cwd=message.get("cwd"),
                env=pi_env,
                title=message.get("title") or _pi_resume_title(resume_path),
                parent_id=message.get("parent_id"),
                origin=str(message.get("origin", "user")),
                role=str(message.get("role", "agent")),
                agent=message.get("agent"),
                task=message.get("task"),
                visibility=str(message.get("visibility", "public")),
                trusted=trusted,
                session_file=resume_path,
            )
        except SessionError as exc:
            raise _request_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise RequestError("bad_request", str(exc)) from exc
        prompt = message.get("prompt")
        if isinstance(prompt, str) and prompt:
            try:
                session.send_command({"type": "prompt", "message": prompt})
            except PiSessionClosed:
                pass
        return session

    def _list_dirs(self, message: dict) -> dict:
        raw = message.get("path")
        base = self.manager.cwd if raw is None or not str(raw).strip() else str(raw)
        path = os.path.abspath(os.path.expanduser(base))
        if not os.path.isdir(path):
            raise RequestError("bad_request", f"not a directory: {path}")
        entries: list[dict[str, str]] = []
        truncated = False
        try:
            with os.scandir(path) as scan:
                for entry in scan:
                    try:
                        if not entry.is_dir():
                            continue
                    except OSError:
                        continue
                    if len(entries) >= 500:
                        truncated = True
                        break
                    entries.append({"name": entry.name, "path": entry.path})
        except OSError as exc:
            raise RequestError("bad_request", str(exc)) from exc
        entries.sort(key=lambda item: item["name"].casefold())
        parent = os.path.dirname(path)
        return {
            "path": path,
            "parent": None if parent == path else parent,
            "dirs": entries,
            "truncated": truncated,
        }

    def _list_entries(self, message: dict) -> dict:
        """List directories and files for literal `@path` completion.

        Unlike `list_dirs` this includes files; directories come first, then
        files, each sorted case-insensitively. The result is capped so one huge
        directory cannot flood the browser.
        """
        raw = message.get("path")
        base = self.manager.cwd if raw is None or not str(raw).strip() else str(raw)
        path = os.path.abspath(os.path.expanduser(base))
        if not os.path.isdir(path):
            raise RequestError("bad_request", f"not a directory: {path}")
        entries: list[dict[str, str]] = []
        truncated = False
        try:
            with os.scandir(path) as scan:
                for entry in scan:
                    try:
                        is_dir = entry.is_dir()
                        is_file = entry.is_file()
                    except OSError:
                        continue
                    if not is_dir and not is_file:
                        continue
                    if len(entries) >= MAX_LISTED_ENTRIES:
                        truncated = True
                        break
                    entries.append(
                        {
                            "name": entry.name,
                            "path": entry.path,
                            "type": "dir" if is_dir else "file",
                        }
                    )
        except OSError as exc:
            raise RequestError("bad_request", str(exc)) from exc
        entries.sort(key=lambda item: (item["type"] != "dir", item["name"].casefold()))
        parent = os.path.dirname(path)
        return {
            "path": path,
            "parent": None if parent == path else parent,
            "entries": entries,
            "truncated": truncated,
        }

    async def _pi_models(self, message: dict) -> dict:
        """Return pi's model list for the new-session picker.

        The probe runs in the directory the session would be created in, so
        project-level configuration and trust are reflected.
        """
        if not self.manager.pi_available:
            raise RequestError(
                "unavailable", "pi is not installed on the server; install pi first"
            )
        raw = message.get("cwd")
        cwd = self.manager.cwd
        if isinstance(raw, str) and raw.strip():
            cwd = os.path.abspath(os.path.expanduser(raw))
            if not os.path.isdir(cwd):
                raise RequestError("bad_request", f"not a directory: {cwd}")
        try:
            return await self.manager.pi_models(cwd, refresh=bool(message.get("refresh")))
        except (PiError, PiSessionClosed, TimeoutError) as exc:
            raise RequestError("unavailable", f"cannot list pi models: {exc}") from exc
        except OSError as exc:
            raise RequestError("internal", f"cannot start pi: {exc}") from exc

    def _list_pi_sessions(self, message: dict) -> dict:
        sessions = self.manager.list_pi_sessions()
        cwd = message.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            target = os.path.realpath(os.path.abspath(os.path.expanduser(cwd)))
            sessions = [item for item in sessions if item.get("cwd") == target]
        return {
            "root": self.manager.pi_session_dir,
            "sessions": sessions,
        }

    async def _pi_export(self, message: dict) -> dict:
        session = self._require(message)
        if not _is_pi(session):
            raise _legacy_pi_error()
        fmt = str(message.get("format", "html")).lower()
        if fmt not in ("html", "jsonl"):
            raise RequestError("bad_request", "format must be 'html' or 'jsonl'")
        root = self.manager.pi_session_dir
        if not root:
            raise RequestError("bad_request", "pi session storage is not configured")
        if fmt == "html":
            export_dir = os.path.join(root, "exports")
            os.makedirs(export_dir, mode=0o700, exist_ok=True)
            output_path = os.path.join(
                export_dir, f"{session.id}-{time.time_ns()}.html"
            )
            try:
                response = await session.rpc(
                    {"type": "export_html", "outputPath": output_path}
                )
            except (PiError, PiSessionClosed, TimeoutError) as exc:
                raise RequestError("pi_error", str(exc)) from exc
            if not response.get("success"):
                raise RequestError("pi_error", str(response.get("error", "export failed")))
            data = response.get("data") or {}
            path = self.manager.resolve_pi_path(data.get("path") or output_path)
        else:
            try:
                response = await session.rpc({"type": "get_state"})
            except (PiError, PiSessionClosed, TimeoutError) as exc:
                raise RequestError("pi_error", str(exc)) from exc
            if not response.get("success"):
                raise RequestError("pi_error", str(response.get("error", "get_state failed")))
            path = self.manager.resolve_pi_path((response.get("data") or {}).get("sessionFile"))
        if path is None or not os.path.isfile(path):
            raise RequestError("pi_error", "pi produced no downloadable file")
        return {
            "format": fmt,
            "path": path,
            "url": "/pi/download?path=" + quote(path, safe=""),
        }

    async def _pi_snapshot(self, message: dict) -> dict:
        session = self._require(message)
        if not _is_pi(session):
            raise _legacy_pi_error()
        await self._send_pi_snapshot(session, strict=True)
        return {"ok": True}

    async def _pi_history(self, message: dict) -> dict:
        """Return one backwards page of transcript entries for the pi view."""
        session = self._require(message)
        if not _is_pi(session):
            raise _legacy_pi_error()
        before = message.get("before")
        if not isinstance(before, str) or not before:
            raise RequestError("bad_request", "before is required")
        limit = _nonneg_int(message.get("limit"))
        if not limit:
            limit = HISTORY_ENTRY_LIMIT
        limit = min(limit, MAX_HISTORY_ENTRY_LIMIT)
        try:
            return await session.history(before, limit)
        except TimeoutError as exc:
            raise RequestError("timeout", "pi history request timed out") from exc
        except PiSessionClosed as exc:
            raise RequestError("not_found", str(exc)) from exc
        except PiError as exc:
            raise RequestError("bad_request", str(exc)) from exc

    def _pi_ui_response(self, message: dict) -> dict:
        session = self._require(message)
        if not _is_pi(session):
            raise _legacy_pi_error()
        request_id = message.get("request_id")
        response = message.get("response")
        if not isinstance(request_id, str) or not request_id:
            raise RequestError("bad_request", "request_id is required")
        if not isinstance(response, dict):
            raise RequestError("bad_request", "response must be an object")
        try:
            session.resolve_ui(request_id, response)
        except PiSessionClosed as exc:
            raise RequestError("not_found", str(exc)) from exc
        except PiError as exc:
            raise RequestError("bad_request", str(exc)) from exc
        return {"ok": True}

    def _agent_command_id(self, message: dict) -> str:
        """Return the command id to echo, generating one when the client omits it."""
        raw = message.get("id")
        if isinstance(raw, str) and raw:
            return raw
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return str(raw)
        self._agent_command_counter += 1
        return f"webterm-{os.getpid()}-{self._agent_command_counter}"

    async def _agent_command(self, message: dict) -> dict:
        """Answer one canonical command from the canonical browser view.

        The client sends a flat canonical record inside the envelope, e.g.
        ``{"type":"agent","session_id":...,"command":{"type":"prompt"}}``.
        The answer is a canonical ``response`` record, so no client needs to
        know which adapter (pi today, anything else tomorrow) is behind the
        session.
        """
        session = self._require(message)
        codec = _agent_codec(session)
        if codec is None:
            raise RequestError(
                "bad_request", "session is not a structured agent session"
            )
        command = message.get("command")
        if not isinstance(command, dict):
            raise RequestError("bad_request", "command must be an object")
        name = command.get("type")
        if not isinstance(name, str) or not name:
            raise RequestError("bad_request", "command.type is required")
        if name not in codec.commands:
            raise RequestError(
                "bad_request",
                f"adapter {session.adapter!r} does not support agent command {name!r}",
            )
        command_id = self._agent_command_id(message)
        timeout = _optional_float(message.get("timeout"))
        try:
            response = await self._run_agent_command(
                session, codec, name, command, command_id, timeout=timeout
            )
        except CanonicalCommandError as exc:
            raise RequestError("bad_request", str(exc)) from exc
        except UnknownDialog as exc:
            raise RequestError("not_found", str(exc)) from exc
        except CodecError as exc:
            raise RequestError("bad_request", str(exc)) from exc
        except (TimeoutError, AgentTimeout) as exc:
            raise RequestError("timeout", f"agent command {name} timed out") from exc
        except (PiSessionClosed, AgentClosed) as exc:
            raise RequestError("not_found", str(exc)) from exc
        except (PiError, AgentError) as exc:
            raise RequestError("bad_request", str(exc)) from exc
        return {"agent_response": response}

    async def _run_agent_command(
        self,
        session: Session | PiSession | AgentSession,
        codec: AgentCodec,
        name: str,
        command: dict,
        command_id: str,
        *,
        timeout: float | None = None,
    ) -> dict:
        """Execute one validated canonical command against an agent session."""
        if name == "get_snapshot":
            limit = _nonneg_int(command.get("limit")) or None
            data = await codec.snapshot(session, limit=limit)
            if self.agent_api or not _is_pi(session):
                # Opted-in pi clients get the snapshot on the push channel so
                # live message ids are reset; native agents have no legacy
                # channel to stay compatible with.
                data = await self._send_agent_snapshot(
                    session, snapshot=data, strict=True
                ) or data
            return response_record(name, command_id, data=data)
        if name == "get_history":
            before = command.get("before")
            if not isinstance(before, str) or not before:
                raise CanonicalCommandError("get_history needs a 'before' entry id")
            limit = _nonneg_int(command.get("limit")) or HISTORY_ENTRY_LIMIT
            limit = min(limit, MAX_HISTORY_ENTRY_LIMIT)
            page = await codec.history(session, before, limit)
            return response_record(name, command_id, data=page)
        if name == "list_models":
            models = await codec.models(
                session, self.manager, refresh=bool(command.get("refresh"))
            )
            return response_record(name, command_id, data=models)
        if name == "dialog_response":
            dialog_id = command.get("dialog_id")
            response = command.get("response")
            if not isinstance(dialog_id, str) or not dialog_id:
                raise CanonicalCommandError("dialog_response needs a dialog_id")
            if not isinstance(response, dict):
                raise CanonicalCommandError("dialog_response needs a response object")
            codec.resolve_dialog(session, dialog_id, response)
            return response_record(name, command_id)
        return await codec.command(
            session, name, command, command_id, timeout=timeout
        )

    async def _pi_command(self, message: dict) -> dict:
        session = self._require(message)
        if not _is_pi(session):
            raise _legacy_pi_error()
        command = message.get("command")
        if not isinstance(command, dict):
            raise RequestError("bad_request", "command must be an object")
        if command.get("type") == "prompt" and command.get("images") is not None:
            # The raw envelope skips the canonical translation, so validate its
            # images here: the server, not the browser, enforces the limits.
            try:
                pi_command_images(command)
            except CanonicalCommandError as exc:
                raise RequestError("bad_request", str(exc)) from exc
        timeout = _optional_float(message.get("timeout"))
        try:
            response = await session.rpc(
                command, timeout=timeout if timeout is not None else 30.0
            )
        except TimeoutError as exc:
            raise RequestError("timeout", "pi command timed out") from exc
        except PiSessionClosed as exc:
            raise RequestError("not_found", str(exc)) from exc
        except PiError as exc:
            raise RequestError("bad_request", str(exc)) from exc
        return {"pi_response": response}

    async def _rename(self, message: dict) -> dict:
        session = self._require(message)
        title = str(message.get("title", session.title))[:200]
        session.title = title
        codec = _agent_codec(session)
        if codec is not None and title and "set_session_name" in codec.commands:
            try:
                await codec.command(
                    session,
                    "set_session_name",
                    {"type": "set_session_name", "name": title},
                    "rename",
                    timeout=5.0,
                )
            except (
                CanonicalCommandError,
                CodecError,
                PiError,
                PiSessionClosed,
                AgentError,
                AgentClosed,
                AgentTimeout,
                TimeoutError,
            ) as exc:
                # A rename is cosmetic; an adapter that cannot store a name
                # must not fail the request.
                log.debug("agent session %s: rename failed: %s", session.id, exc)
        self.manager.notify()
        return {"session": session.info()}

    def _input(self, message: dict) -> None:
        session = self.manager.get(str(message.get("session_id", "")))
        if session is None:
            return
        if session.kind != "shell":
            raise RequestError("bad_request", "agent sessions take commands, not keystrokes")
        data = message.get("data", "")
        if isinstance(data, str) and data:
            session.write(data.encode("utf-8"))

    def _resize(self, message: dict) -> None:
        session = self.manager.get(str(message.get("session_id", "")))
        if session is not None:
            if session.kind != "shell":
                raise RequestError("bad_request", "agent sessions do not have a terminal size")
            session.resize(
                int(message.get("cols", session.cols)),
                int(message.get("rows", session.rows)),
            )

    # -------------------------------------------------------- agent verbs

    def _require(self, message: dict) -> Session | PiSession:
        session = self.manager.get(str(message.get("session_id", "")))
        if session is None:
            raise RequestError("not_found", f"unknown session {message.get('session_id')!r}")
        return session

    def _resolve_agent(
        self,
        agent: str | None,
        task: str | None,
        command,
        interactive: bool,
        *,
        rpc: bool = False,
    ) -> list[str] | str:
        """Turn an agent profile + task into the argv to run."""
        command = _valid_command(command)
        if command is not None:
            if task and not rpc:
                # An explicit command has no profile template to substitute, so
                # the task is appended exactly as it is for a template without
                # a ``{task}`` placeholder.  Structured agents receive the task
                # as an RPC prompt below instead.
                if isinstance(command, list):
                    return [*command, str(task)]
                return f"{command} {shlex.quote(str(task))}"
            return command
        if not agent:
            raise RequestError("bad_request", "agent or command is required")
        profile = self.profiles.get(agent)
        if profile is None:
            raise RequestError("bad_request", f"unknown agent {agent!r}")
        key = "rpc" if rpc else ("interactive" if interactive else "print")
        template = profile.get(key)
        if not template:
            raise RequestError("bad_request", f"agent {agent!r} has no {key} command")
        parts = [str(part) for part in template]
        if task:
            if any("{task}" in part for part in parts):
                return [part.replace("{task}", task) for part in parts]
            if rpc:
                # Structured agents receive the task as an RPC prompt instead
                # of an argv message.
                return parts
            parts.append(task)
            return parts
        # No task supplied: drop arguments that are only the empty placeholder
        # (e.g. an interactive profile's bare {task}), keeping the command.
        return [part for part in (p.replace("{task}", "").strip() for p in parts) if part]

    async def _run(self, message: dict, *, spawn: bool) -> dict:
        interactive = bool(message.get("interactive")) or message.get("mode") == "interactive"
        rpc = bool(message.get("rpc")) or message.get("mode") == "rpc"
        task = message.get("task")
        agent = message.get("agent")
        provider = message.get("provider")
        model = message.get("model")
        for name, value in (("provider", provider), ("model", model)):
            if value is not None and not isinstance(value, str):
                raise RequestError("bad_request", f"{name} must be a string")
            if isinstance(value, str) and not value.strip():
                raise RequestError("bad_request", f"{name} must not be empty")
        provider = provider.strip() if isinstance(provider, str) else None
        model = model.strip() if isinstance(model, str) else None
        if provider and not model:
            raise RequestError("bad_request", "provider requires a model id")
        if (provider or model) and not rpc:
            raise RequestError("bad_request", "model selection requires rpc: true")
        adapter = message.get("adapter")
        profile = self.profiles.get(str(agent)) if agent else None
        if not isinstance(adapter, str) or not adapter:
            profile_adapter = profile.get("adapter") if isinstance(profile, dict) else None
            if isinstance(profile_adapter, str) and profile_adapter:
                adapter = profile_adapter
            else:
                # Preserve the historical default for custom profiles; a native
                # adapter sets ``adapter`` in its profile or passes --adapter.
                adapter = "pi"
        adapter = str(adapter)
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
            if spawn and rpc and adapter != "pi":
                argv = self._resolve_agent(
                    agent, None, message.get("command"), False, rpc=True
                )
                if isinstance(argv, str):
                    argv = shlex.split(argv)
                if not argv:
                    raise RequestError("bad_request", "structured agent command is empty")
                session = await self.manager.create_agent(
                    argv,
                    cwd=message.get("cwd"),
                    env=message.get("env"),
                    title=message.get("title") or task,
                    parent_id=message.get("parent_id") or None,
                    origin="agent",
                    role="agent",
                    agent=agent,
                    task=task,
                    visibility=str(message.get("visibility", "public")),
                    adapter=adapter,
                )
                if model:
                    try:
                        timeout = _optional_float(message.get("timeout"))
                        fields = {"model": model}
                        if provider:
                            fields["provider"] = provider
                        await session.command(
                            "set_model",
                            timeout=(
                                timeout
                                if timeout is not None
                                else AGENT_COMMAND_TIMEOUT
                            ),
                            **fields,
                        )
                    except (AgentError, TimeoutError) as exc:
                        self.manager.kill(session.id, tree=False)
                        raise RequestError("agent_error", _agent_error(exc, "agent")) from exc
                if task:
                    try:
                        timeout = _optional_float(message.get("timeout"))
                        if timeout is None:
                            await session.prompt(task)
                        else:
                            await session.prompt(task, timeout=timeout)
                    except (AgentError, TimeoutError) as exc:
                        self.manager.kill(session.id, tree=False)
                        raise RequestError("agent_error", _agent_error(exc, "agent")) from exc
            elif spawn and rpc:
                argv = self._resolve_agent(agent, None, message.get("command"), False, rpc=True)
                if isinstance(argv, str):
                    argv = shlex.split(argv)
                if not argv:
                    raise RequestError("bad_request", "structured agent command is empty")
                if self.manager.pi_extension and not any(
                    part in ("-e", "--extension") for part in argv
                ):
                    argv.extend(["-e", self.manager.pi_extension])
                if provider:
                    argv.extend(["--provider", provider])
                if model:
                    argv.extend(["--model", model])
                session = self.manager.create_pi(
                    argv,
                    cwd=message.get("cwd"),
                    env=message.get("env"),
                    title=message.get("title") or task,
                    parent_id=message.get("parent_id") or None,
                    origin="agent",
                    role="agent",
                    agent=agent,
                    task=task,
                    visibility=str(message.get("visibility", "public")),
                    trusted=False,
                )
                if task:
                    try:
                        timeout = _optional_float(message.get("timeout"))
                        request = {"type": "prompt", "message": task}
                        if timeout is None:
                            response = await session.rpc(request)
                        else:
                            response = await session.rpc(request, timeout=timeout)
                    except (PiError, PiSessionClosed, TimeoutError) as exc:
                        self.manager.kill(session.id, tree=False)
                        raise RequestError("pi_error", _agent_error(exc, "pi")) from exc
                    if not response.get("success"):
                        self.manager.kill(session.id, tree=False)
                        raise RequestError(
                            "pi_error", str(response.get("error", "prompt rejected"))
                        )
            elif spawn:
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
        codec = _agent_codec(session)
        if codec is not None:
            timeout = _optional_float(message.get("timeout"))
            settled = await session.wait_for_settled(timeout)
            result = await codec.result(session, settled=settled)
            result["timed_out"] = not settled
            return result
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
        if session.kind != "shell":
            raise RequestError("bad_request", "agent sessions take commands, not keystrokes")
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
        if session.kind != "shell":
            raise RequestError("bad_request", "agent sessions have no byte stream to capture")
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
        if until == "settled":
            if _agent_codec(session) is None:
                raise RequestError(
                    "bad_request", "until=settled is only for agent sessions"
                )
            matched = await session.wait_for_settled(timeout)
            return {
                "matched": matched,
                "settled": matched,
                "exit_code": session.exit_code,
            }
        if until != "exit" and session.kind != "shell":
            raise RequestError(
                "bad_request",
                "only until=exit or until=settled is available for agent sessions",
            )
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


def _pi_resume_title(path: str | None) -> str | None:
    if path is None:
        return None
    stem = os.path.basename(path)
    return stem.rsplit(".", 1)[0][:200] or None


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


def _agent_error(exc: BaseException, agent: str) -> str:
    """Describe a failed adapter request, never returning an empty message.

    ``asyncio.TimeoutError`` has no text of its own; a bare empty error hid the
    difference between a slow agent and a command that does not speak RPC at
    all, which is the common mistake when ``spawn --rpc`` names a plain command.
    """
    text = str(exc).strip()
    if text:
        return text
    if isinstance(exc, TimeoutError):
        return (
            f"{agent} did not answer before the timeout; "
            "an --rpc command must speak the agent's RPC protocol"
        )
    return f"{agent} request failed ({type(exc).__name__})"


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
    discovery: bool = True,
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

        # Discovery is served without authentication so a client can classify
        # a server before it holds any credentials.
        if discovery and path == DISCOVERY_PATH and method in ("GET", "HEAD"):
            await serve_discovery(writer, token, method)
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
            # Redirect to / minus the secret, but keep any other query
            # parameters the client asked for (e.g. the desktop app passing a
            # sidebar width through the bootstrap).
            remaining = {key: value for key, value in query.items() if key != "bootstrap"}
            location = "/" + ("?" + urlencode(remaining) if remaining else "")
            writer.write(
                _http_response(
                    "302 Found",
                    extra_headers=(
                        f"Location: {location}\r\n"
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
        if path == "/pi/download":
            await serve_pi_download(writer, manager, method, query)
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
    discovery: bool = True,
) -> ServerHandle:
    """Bind the listener and start serving, returning a :class:`ServerHandle`.

    This does not block or install signal handlers, so it is safe to call from
    an embedded host running the event loop on a worker thread.  Use
    ``await handle.wait()`` to block until stopped.  :func:`serve` wraps this
    for the CLI together with main-thread signal handling.

    Unless *enable_control* is false, a local ``AF_UNIX`` control socket is
    also started (at *control_socket* or the default per-user path), and every
    child process is told where to find it via ``WEBTERM_SOCKET``.

    Unless *discovery* is false, ``GET /.well-known/webterm.json`` is served
    without authentication so a client can identify the server and the
    credential types it accepts.
    """
    connections: set[ClientConnection] = set()
    client_tasks: set[asyncio.Task] = set()
    control = ServerControl()
    # Advertise the opt-in canonical agent stream.  app.js falls back to the
    # legacy pi view when an older server does not set this flag.
    config = dict(config or {})
    config.setdefault("agentApi", True)

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
                reader,
                writer,
                manager,
                connections,
                config,
                control,
                token,
                profiles,
                discovery=discovery,
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
    discovery: bool = True,
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
        discovery=discovery,
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

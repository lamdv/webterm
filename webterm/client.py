"""A minimal stdlib client for webterm's local control socket.

Intended for agents and scripts running on the same machine: connect to the
Unix socket advertised in ``$WEBTERM_SOCKET`` (or the default per-user path),
then send `type`-keyed requests and read correlated results.  The browser
WebSocket is a separate, network-capable path; this one is local only.
"""

from __future__ import annotations

import json
import socket
import struct
from pathlib import Path
from typing import Any

_HEADER = struct.Struct(">IB")
_OP_TEXT = 1
_OP_BINARY = 2

#: Environment variable every child process inherits from webterm.
SOCKET_ENV = "WEBTERM_SOCKET"
SESSION_ENV = "WEBTERM_SESSION_ID"


class ControlError(Exception):
    """The server rejected a request; carries the protocol error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def discover_socket(explicit: str | None = None) -> str:
    """Resolve the control socket path from *explicit*, the env, or the default."""
    if explicit:
        return explicit
    from .control import default_control_socket

    return str(default_control_socket())


def current_session() -> str | None:
    """The id of the session this process is running in, if any."""
    import os

    return os.environ.get(SESSION_ENV) or None


class ControlClient:
    """Blocking request/response client for the local control socket."""

    def __init__(self, path: str | Path, *, timeout: float | None = None) -> None:
        self.path = str(path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if timeout is not None:
            self._sock.settimeout(timeout)
        self._sock.connect(self.path)
        self._counter = 0
        self._buffer = b""
        #: Binary output frames seen while waiting for a result.
        self.output: list[bytes] = []

    # ------------------------------------------------------------------ wire

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> ControlClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _send(self, kind: int, payload: bytes) -> None:
        self._sock.sendall(_HEADER.pack(len(payload), kind) + payload)

    def _fill(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("control connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def recv_frame(self) -> tuple[int, bytes]:
        length, kind = _HEADER.unpack(self._fill(_HEADER.size))
        return kind, self._fill(length)

    def notify(self, type_: str, **params: Any) -> None:
        self._send(_OP_TEXT, json.dumps({"type": type_, **params}).encode("utf-8"))

    def request(self, type_: str, **params: Any) -> dict:
        """Send a request and return its ``result`` (or raise :class:`ControlError`)."""
        self._counter += 1
        request_id = str(self._counter)
        body = {"type": type_, "id": request_id, **params}
        self._send(_OP_TEXT, json.dumps(body).encode("utf-8"))
        while True:
            kind, payload = self.recv_frame()
            if kind == _OP_BINARY:
                self.output.append(payload)
                continue
            message = json.loads(payload)
            if message.get("id") != request_id:
                continue
            if message.get("type") == "error":
                error = message.get("error", {})
                raise ControlError(error.get("code", "error"), error.get("message", "error"))
            if message.get("type") == "result":
                return message.get("result", {})

    # -------------------------------------------------------------- verbs

    def list_sessions(self) -> list[dict]:
        return self.request("list")["sessions"]

    def create(self, **params: Any) -> dict:
        return self.request("create", **params)["session"]

    def run(self, command, **params: Any) -> dict:
        return self.request("run", command=command, **params)

    def spawn(self, **params: Any) -> dict:
        return self.request("spawn", **params)

    def pi(self, session_id: str, command: dict, *, timeout: float | None = None) -> dict:
        result = self.request(
            "pi", session_id=session_id, command=command, timeout=timeout
        )
        return result.get("pi_response") or {}

    def send(self, session_id: str, data: str, *, submit: bool = False) -> dict:
        return self.request("send", session_id=session_id, data=data, submit=submit)

    def capture(self, session_id: str, *, since: int = 0, text: bool = False) -> dict:
        return self.request(
            "capture",
            session_id=session_id,
            since=since,
            format="text" if text else "base64",
        )

    def wait(self, session_id: str, **params: Any) -> dict:
        return self.request("wait", session_id=session_id, **params)

    def kill(self, session_id: str, *, tree: bool = True) -> dict:
        return self.request("kill", session_id=session_id, tree=tree)

    def rename(self, session_id: str, title: str) -> dict:
        return self.request("rename", session_id=session_id, title=title)

    def close_session(self, session_id: str) -> dict:
        return self.request("close", session_id=session_id)

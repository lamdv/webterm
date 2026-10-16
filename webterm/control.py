"""Local control plane: an ``AF_UNIX`` socket for agents and scripts.

The browser WebSocket is intentionally network-capable; this module is not.
It binds a Unix socket guarded by filesystem permissions (``0600`` inside a
``0700`` directory) and, where the platform exposes them, peer credentials, so
only local processes belonging to the same user can issue control commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import struct
import tempfile
from pathlib import Path

from .transport import FramedTransport

log = logging.getLogger("webterm")

DISCOVERY_NAME = "server.json"
DEFAULT_SOCKET_NAME = "control.sock"


def default_control_dir() -> Path:
    """Per-user runtime directory for the control socket and discovery file."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "webterm"
    return Path(tempfile.gettempdir()) / f"webterm-{os.geteuid()}"


def default_control_socket() -> Path:
    """The socket path used when ``--control-socket`` is not given."""
    return default_control_dir() / DEFAULT_SOCKET_NAME


def looks_like_tcp(value: str) -> bool:
    """True when *value* names a network address rather than a filesystem path.

    The control plane is local-only by construction; refusing address-looking
    values keeps a misconfigured ``--control-socket`` from ever implying TCP.
    """
    return (
        "://" in value
        or value.startswith(("tcp:", "unix://"))
        or (":" in value and not value.startswith("/"))
    )


def peer_uid(sock: socket.socket) -> int | None:
    """Return the connected peer's uid, or ``None`` if it cannot be checked."""
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


class ControlServer:
    """Accepts local connections and hands each to a connection factory."""

    def __init__(
        self,
        manager,
        connection_factory,
        *,
        path: str | Path,
        url: str | None = None,
        connections: set | None = None,
    ):
        self.manager = manager
        self._factory = connection_factory
        self.path = Path(path)
        self.url = url
        # Shared with the HTTP server so agent connections receive session
        # broadcasts and are closed on shutdown.
        self.connections: set = connections if connections is not None else set()
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> ControlServer:
        """Bind the socket, then publish the discovery file."""
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)
        self._unlink_stale()
        self._server = await asyncio.start_unix_server(
            self._on_client, path=str(self.path)
        )
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        self._write_discovery()
        log.info("control socket listening on %s", self.path)
        return self

    def _unlink_stale(self) -> None:
        if not self.path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(self.path))
        except OSError:
            with contextlib.suppress(OSError):
                self.path.unlink()
        else:
            raise RuntimeError(f"another webterm control server is listening on {self.path}")
        finally:
            probe.close()

    def _write_discovery(self) -> None:
        data = {"socket": str(self.path), "url": self.url, "pid": os.getpid()}
        target = self.path.parent / DISCOVERY_NAME
        with contextlib.suppress(OSError):
            target.write_text(json.dumps(data))
            os.chmod(target, 0o600)

    def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.get_running_loop().create_task(self._serve_client(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        raw = writer.get_extra_info("socket")
        uid = peer_uid(raw) if raw is not None else None
        if uid is not None and uid != os.geteuid():
            log.warning("refusing control connection from uid %s", uid)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        connection = self._factory(FramedTransport(reader, writer))
        self.connections.add(connection)
        try:
            await connection.run()
        finally:
            self.connections.discard(connection)

    async def stop(self) -> None:
        """Close the listener and remove the socket and discovery file."""
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(OSError):
            self.path.unlink()
        with contextlib.suppress(OSError):
            (self.path.parent / DISCOVERY_NAME).unlink()

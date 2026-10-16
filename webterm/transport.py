"""Transport abstraction for the shared session command dispatcher.

The browser path uses :class:`webterm.websocket.WebSocket`; the local agent
control path uses :class:`FramedTransport` over an ``AF_UNIX`` socket.  Both
expose the same four methods (``send_json``, ``send_binary``, ``recv``,
``close``), so one :class:`webterm.server.ClientConnection` serves both.
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Protocol, runtime_checkable

from .websocket import OP_BINARY, OP_TEXT, ConnectionClosed

#: ``u32 big-endian payload length | u8 kind``.
_HEADER = struct.Struct(">IB")
#: Guard against a bogus/hostile frame length.
MAX_FRAME = 16 * 1024 * 1024


@runtime_checkable
class Transport(Protocol):
    """A framed, message-oriented byte channel."""

    async def send_json(self, obj: object) -> None: ...

    async def send_binary(self, data: bytes) -> None: ...

    async def recv(self) -> tuple[int, bytes] | None: ...

    async def close(self) -> None: ...


class FramedTransport:
    """Length-prefixed frames over an asyncio stream (the control socket).

    ``kind`` is ``OP_TEXT`` (1) for JSON control frames and ``OP_BINARY`` (2)
    for terminal output, matching the WebSocket opcodes so the dispatcher does
    not care which transport it is on.  A binary frame payload is the same
    session-id/offset layout the browser receives.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()
        self._closed = False

    async def _send(self, kind: int, payload: bytes) -> None:
        if self._closed:
            raise ConnectionClosed()
        async with self._lock:
            try:
                self._writer.write(_HEADER.pack(len(payload), kind) + payload)
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                raise ConnectionClosed() from exc

    async def send_json(self, obj: object) -> None:
        await self._send(OP_TEXT, json.dumps(obj).encode("utf-8"))

    async def send_binary(self, data: bytes) -> None:
        await self._send(OP_BINARY, data)

    async def recv(self) -> tuple[int, bytes] | None:
        if self._closed:
            return None
        try:
            header = await self._reader.readexactly(_HEADER.size)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return None
        length, kind = _HEADER.unpack(header)
        if length > MAX_FRAME:
            raise ConnectionClosed()
        try:
            payload = await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return None
        return kind, payload

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass

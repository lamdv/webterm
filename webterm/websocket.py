"""A tiny asyncio WebSocket server implementation (RFC 6455).

Only what webterm needs: the opening handshake, masked client frames (including
fragmentation), text/binary messages, ping/pong and close.  Intentionally
dependency-free so the whole project runs on the standard library.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
from collections.abc import Mapping

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

DEFAULT_MAX_MESSAGE = 8 * 1024 * 1024


class WebSocketError(Exception):
    """Raised on a protocol violation."""


class ConnectionClosed(Exception):
    """Raised when the peer goes away uncleanly."""


def accept_key(key: str) -> str:
    """Compute the ``Sec-WebSocket-Accept`` value for a client key."""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(opcode: int, payload: bytes = b"", *, fin: bool = True) -> bytes:
    """Encode a single server->client frame (never masked)."""
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", b0, length)
    elif length < 1 << 16:
        header = struct.pack("!BBH", b0, 126, length)
    else:
        header = struct.pack("!BBQ", b0, 127, length)
    return header + payload


def _unmask(data: bytes, mask: bytes) -> bytes:
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        out[i] = byte ^ mask[i & 3]
    return bytes(out)


async def read_frame(
    reader: asyncio.StreamReader, *, max_message: int = DEFAULT_MAX_MESSAGE
) -> tuple[bool, int, bytes]:
    """Read one frame, returning ``(fin, opcode, payload)``.

    Client frames must be masked; anything larger than *max_message* is a
    protocol error.
    """
    try:
        header = await reader.readexactly(2)
    except asyncio.IncompleteReadError as exc:
        raise ConnectionClosed("connection closed while reading frame") from exc

    b0, b1 = header
    fin = bool(b0 & 0x80)
    if b0 & 0x70:
        raise WebSocketError("reserved bits set")
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F

    if length == 126:
        (length,) = struct.unpack("!H", await reader.readexactly(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", await reader.readexactly(8))

    if length > max_message:
        raise WebSocketError("message too large")
    if not masked:
        raise WebSocketError("client frames must be masked")

    mask = await reader.readexactly(4)
    payload = await reader.readexactly(length) if length else b""
    return fin, opcode, _unmask(payload, mask)


class WebSocket:
    """Server side of a single WebSocket connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_message: int = DEFAULT_MAX_MESSAGE,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._max_message = max_message
        self._write_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def handshake(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: Mapping[str, str],
        **kwargs: object,
    ) -> WebSocket:
        """Complete the opening handshake for an already-read request."""
        key = headers.get("sec-websocket-key")
        if not key:
            raise WebSocketError("missing Sec-WebSocket-Key")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
            "\r\n"
        )
        writer.write(response.encode("ascii"))
        await writer.drain()
        return cls(reader, writer, **kwargs)

    async def _send(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise ConnectionClosed("connection is closed")
        async with self._write_lock:
            self._writer.write(encode_frame(opcode, payload))
            try:
                await self._writer.drain()
            except (ConnectionError, RuntimeError) as exc:
                raise ConnectionClosed("connection lost") from exc

    async def send_text(self, text: str) -> None:
        await self._send(OP_TEXT, text.encode("utf-8"))

    async def send_binary(self, data: bytes) -> None:
        await self._send(OP_BINARY, data)

    async def send_json(self, obj: object) -> None:
        import json

        await self.send_text(json.dumps(obj, separators=(",", ":")))

    async def send_ping(self, payload: bytes = b"") -> None:
        await self._send(OP_PING, payload)

    async def recv(self) -> tuple[int, bytes] | None:
        """Return the next complete data message as ``(opcode, payload)``.

        Control frames are handled transparently.  ``None`` means the peer
        sent a close frame.
        """
        buffer = bytearray()
        message_opcode: int | None = None

        while True:
            try:
                fin, opcode, payload = await read_frame(
                    self._reader, max_message=self._max_message
                )
            except ConnectionClosed:
                return None

            if opcode == OP_CLOSE:
                await self._reply_close(payload)
                return None
            if opcode == OP_PING:
                await self._send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue

            if opcode in (OP_TEXT, OP_BINARY):
                if message_opcode is not None:
                    raise WebSocketError("new data frame before previous one finished")
                message_opcode = opcode
                buffer.extend(payload)
            elif opcode == OP_CONT:
                if message_opcode is None:
                    raise WebSocketError("continuation frame without start")
                buffer.extend(payload)
            else:
                raise WebSocketError(f"unknown opcode {opcode:#x}")

            if len(buffer) > self._max_message:
                raise WebSocketError("message too large")

            if fin:
                return message_opcode, bytes(buffer)

    async def _reply_close(self, payload: bytes) -> None:
        if self._closed:
            return
        try:
            await self._send(OP_CLOSE, payload[:125])
        except ConnectionClosed:
            pass
        finally:
            self._closed = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame (best effort) and close the transport."""
        if not self._closed:
            try:
                await self._send(OP_CLOSE, struct.pack("!H", code) + reason.encode())
            except ConnectionClosed:
                pass
            self._closed = True
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, RuntimeError):
            pass

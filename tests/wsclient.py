"""Minimal masked WebSocket client used by the tests (stdlib only)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import struct

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8


def decode_output_frame(frame: bytes) -> tuple[str, int, bytes]:
    """Decode a server output frame into ``(session_id, offset, payload)``.

    The frame layout is ``[id_length][session_id][offset:8][payload]`` where
    ``offset`` is the absolute position of ``payload`` in the session's byte
    stream (big-endian, unsigned).
    """
    if not frame:
        raise ValueError("empty output frame")
    id_length = frame[0]
    header = 1 + id_length + 8
    if len(frame) < header:
        raise ValueError("truncated output frame")
    session_id = frame[1 : 1 + id_length].decode("utf-8")
    (offset,) = struct.unpack(">Q", frame[1 + id_length : header])
    return session_id, offset, frame[header:]


def encode_client_frame(opcode: int, payload: bytes) -> bytes:
    """Encode a masked client frame."""
    b0 = 0x80 | opcode
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", b0, 0x80 | length)
    elif length < 1 << 16:
        header = struct.pack("!BBH", b0, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", b0, 0x80 | 127, length)
    masked = bytes(byte ^ mask[i & 3] for i, byte in enumerate(payload))
    return header + mask + masked


class WSClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        path: str = "/ws",
        headers: dict[str, str] | None = None,
        *,
        ssl: ssl.SSLContext | None = None,
        server_hostname: str | None = None,
    ) -> WSClient:
        reader, writer = await asyncio.open_connection(
            host, port, ssl=ssl, server_hostname=server_hostname
        )
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
        )
        for name, value in (headers or {}).items():
            request += f"{name}: {value}\r\n"
        request += "\r\n"
        writer.write(request.encode("ascii"))
        await writer.drain()
        header = await reader.readuntil(b"\r\n\r\n")
        if b" 101 " not in header.split(b"\r\n")[0]:
            raise AssertionError(f"handshake failed: {header!r}")
        return cls(reader, writer)

    async def send_json(self, obj: object) -> None:
        self.writer.write(encode_client_frame(OP_TEXT, json.dumps(obj).encode("utf-8")))
        await self.writer.drain()

    async def send_binary(self, data: bytes) -> None:
        self.writer.write(encode_client_frame(OP_BINARY, data))
        await self.writer.drain()

    async def recv_frame(self) -> tuple[int, bytes]:
        b0, b1 = await self.reader.readexactly(2)
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", await self.reader.readexactly(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", await self.reader.readexactly(8))
        payload = await self.reader.readexactly(length) if length else b""
        return opcode, payload

    async def recv_json(self) -> dict:
        while True:
            opcode, payload = await self.recv_frame()
            if opcode == OP_TEXT:
                return json.loads(payload)

    async def recv_json_until(self, predicate, timeout: float = 10.0) -> dict:
        async def _loop() -> dict:
            while True:
                message = await self.recv_json()
                if predicate(message):
                    return message

        return await asyncio.wait_for(_loop(), timeout)

    async def recv_until_contains(self, needle: bytes, timeout: float = 10.0) -> bytes:
        collected = bytearray()

        async def _loop() -> bytes:
            while True:
                opcode, payload = await self.recv_frame()
                if opcode == OP_BINARY:
                    collected.extend(payload)
                    if needle in collected:
                        return bytes(collected)

        return await asyncio.wait_for(_loop(), timeout)

    async def recv_output(self, timeout: float = 10.0) -> tuple[str, int, bytes]:
        """Receive the next binary output frame as (session_id, offset, payload)."""

        async def _loop() -> tuple[str, int, bytes]:
            while True:
                opcode, frame = await self.recv_frame()
                if opcode == OP_BINARY:
                    return decode_output_frame(frame)

        return await asyncio.wait_for(_loop(), timeout)

    async def recv_output_until_contains(
        self, needle: bytes, timeout: float = 10.0
    ) -> tuple[str, int, bytes]:
        """Collect binary output until ``needle`` appears.

        Returns ``(session_id, consumed, data)`` where ``consumed`` is the
        absolute offset just past the bytes collected (the resume point to
        pass back as ``since``).
        """
        collected = bytearray()
        session_id = ""
        consumed = 0

        async def _loop() -> tuple[str, int, bytes]:
            nonlocal session_id, consumed
            while True:
                sid, offset, payload = await self.recv_output()
                session_id = sid
                consumed = offset + len(payload)
                collected.extend(payload)
                if needle in collected:
                    return session_id, consumed, bytes(collected)

        return await asyncio.wait_for(_loop(), timeout)

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, RuntimeError):
            pass

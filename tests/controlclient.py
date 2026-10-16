"""A stdlib client for the local control socket, used by the tests.

It speaks the same framing as :class:`webterm.transport.FramedTransport` but is
written independently so the tests validate the wire format itself:
``u32 big-endian length | u8 kind | payload``.
"""

from __future__ import annotations

import asyncio
import json
import struct

_HEADER = struct.Struct(">IB")
OP_TEXT = 1
OP_BINARY = 2


class ControlClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._counter = 0
        #: Binary output frames received while waiting for a result.
        self.output: list[bytes] = []

    @classmethod
    async def connect(cls, path: str) -> ControlClient:
        reader, writer = await asyncio.open_unix_connection(path)
        return cls(reader, writer)

    async def _send(self, kind: int, payload: bytes) -> None:
        self._writer.write(_HEADER.pack(len(payload), kind) + payload)
        await self._writer.drain()

    async def notify(self, type_: str, **params) -> None:
        await self._send(OP_TEXT, json.dumps({"type": type_, **params}).encode())

    async def request(self, type_: str, **params) -> dict:
        self._counter += 1
        request_id = str(self._counter)
        body = {"type": type_, "id": request_id, **params}
        await self._send(OP_TEXT, json.dumps(body).encode())
        while True:
            kind, payload = await self.read_frame()
            if kind == OP_BINARY:
                self.output.append(payload)
                continue
            message = json.loads(payload)
            if message.get("id") == request_id and message.get("type") in ("result", "error"):
                return message

    async def read_frame(self, timeout: float = 5.0) -> tuple[int, bytes]:
        header = await asyncio.wait_for(self._reader.readexactly(_HEADER.size), timeout)
        length, kind = _HEADER.unpack(header)
        payload = await asyncio.wait_for(self._reader.readexactly(length), timeout)
        return kind, payload

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001 - best effort in teardown
            pass

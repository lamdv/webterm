"""Unit tests for the hand-rolled WebSocket implementation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
import unittest

from webterm.websocket import (
    OP_BINARY,
    OP_CLOSE,
    OP_PING,
    ConnectionClosed,
    WebSocket,
    WebSocketError,
    accept_key,
    encode_frame,
    read_frame,
)


def mask(payload: bytes, key: bytes = b"\x01\x02\x03\x04") -> bytes:
    return bytes(byte ^ key[i & 3] for i, byte in enumerate(payload))


class FakeWriter:
    """Stands in for asyncio.StreamWriter in WebSocket unit tests."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def client_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    b0 = (0x80 if fin else 0) | opcode
    header = struct.pack("!BB", b0, 0x80 | len(payload))
    return header + b"\x01\x02\x03\x04" + mask(payload)


class AcceptKeyTests(unittest.TestCase):
    def test_rfc6455_vector(self) -> None:
        # The example key/value from RFC 6455 section 1.3.
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_matches_manual_digest(self) -> None:
        key = "x3JJHMbDL1EzLkh9GBhXDw=="
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        self.assertEqual(accept_key(key), expected)


class EncodeFrameTests(unittest.TestCase):
    def test_small_payload(self) -> None:
        frame = encode_frame(OP_BINARY, b"hi")
        self.assertEqual(frame, b"\x82\x02hi")

    def test_medium_payload_uses_16bit_length(self) -> None:
        frame = encode_frame(OP_BINARY, b"a" * 200)
        self.assertEqual(frame[:4], b"\x82\x7e\x00\xc8")

    def test_large_payload_uses_64bit_length(self) -> None:
        frame = encode_frame(OP_BINARY, b"a" * 70000)
        self.assertEqual(frame[0], 0x82)
        self.assertEqual(frame[1], 127)
        self.assertEqual(struct.unpack("!Q", frame[2:10])[0], 70000)


class ReadFrameTests(unittest.IsolatedAsyncioTestCase):
    def make_reader(self, data: bytes) -> asyncio.StreamReader:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return reader

    async def test_reads_masked_frame(self) -> None:
        reader = self.make_reader(client_frame(OP_BINARY, b"hello"))
        fin, opcode, payload = await read_frame(reader)
        self.assertTrue(fin)
        self.assertEqual(opcode, OP_BINARY)
        self.assertEqual(payload, b"hello")

    async def test_rejects_unmasked_frame(self) -> None:
        reader = self.make_reader(b"\x82\x02hi")
        with self.assertRaises(WebSocketError):
            await read_frame(reader)

    async def test_eof_raises_connection_closed(self) -> None:
        reader = self.make_reader(b"")
        with self.assertRaises(ConnectionClosed):
            await read_frame(reader)


class RecvTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_message_is_reassembled(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(client_frame(0x1, b"hel", fin=False))
        reader.feed_data(client_frame(0x0, b"lo", fin=True))
        reader.feed_eof()
        writer = FakeWriter()
        ws = WebSocket(reader, writer)
        opcode, payload = await ws.recv()
        self.assertEqual(opcode, 0x1)
        self.assertEqual(payload, b"hello")

    async def test_ping_is_answered(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(client_frame(OP_PING, b"hi"))
        reader.feed_data(client_frame(0x1, b"data"))
        reader.feed_eof()
        writer = FakeWriter()
        ws = WebSocket(reader, writer)
        opcode, payload = await ws.recv()
        self.assertEqual((opcode, payload), (0x1, b"data"))
        # A pong frame was written in reply to the ping.
        written = bytes(writer.data)
        self.assertEqual(written[0], 0x8A)
        self.assertEqual(written[2:], b"hi")

    async def test_close_returns_none(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(client_frame(OP_CLOSE, struct.pack("!H", 1000)))
        reader.feed_eof()
        writer = FakeWriter()
        ws = WebSocket(reader, writer)
        self.assertIsNone(await ws.recv())

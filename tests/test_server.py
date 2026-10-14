"""End-to-end tests: HTTP static serving and the WebSocket session protocol."""

from __future__ import annotations

import asyncio
import contextlib
import unittest

from webterm.server import handle_client
from webterm.session import SessionManager

from .wsclient import WSClient


class ServerFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.connections: set = set()
        self.manager = SessionManager(shell="/bin/sh", cwd="/tmp", scrollback=100_000)

        def on_change() -> None:
            for connection in list(self.connections):
                asyncio.create_task(connection.send_sessions())

        self.manager.add_listener(on_change)

        self.server = await asyncio.start_server(
            lambda r, w: handle_client(r, w, self.manager, self.connections),
            "127.0.0.1",
            0,
        )
        self.host, self.port = self.server.sockets[0].getsockname()[:2]

    async def asyncTearDown(self) -> None:
        self.manager.close_all()
        self.server.close()
        with contextlib.suppress(Exception):
            await self.server.wait_closed()
        await asyncio.sleep(0.05)


class StaticFileTests(ServerFixture):
    async def _get(self, path: str) -> tuple[str, bytes]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        writer.write(
            f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        raw = await reader.read()
        writer.close()
        head, _, body = raw.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0].decode()
        return status, body

    async def test_index_is_served(self) -> None:
        status, body = await self._get("/")
        self.assertIn("200", status)
        self.assertIn(b"webterm", body)
        self.assertIn(b"xterm.js", body)

    async def test_xterm_is_loaded_from_cdn(self) -> None:
        status, body = await self._get("/")
        self.assertIn("200", status)
        self.assertIn(b"cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0", body)

    async def test_vendor_assets_are_not_bundled(self) -> None:
        status, _ = await self._get("/vendor/xterm.js")
        self.assertIn("404", status)

    async def test_missing_file_is_404(self) -> None:
        status, _ = await self._get("/nope.txt")
        self.assertIn("404", status)

    async def test_config_script_is_served(self) -> None:
        status, body = await self._get("/config.js")
        self.assertIn("200", status)
        self.assertIn(b"window.WEBTERM_CONFIG", body)

    async def test_path_traversal_is_blocked(self) -> None:
        status, _ = await self._get("/../pyproject.toml")
        self.assertIn("404", status)


class SessionProtocolTests(ServerFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.client = await WSClient.connect(self.host, self.port)
        hello = await self.client.recv_json()
        self.assertEqual(hello["type"], "sessions")
        self.assertEqual(hello["sessions"], [])

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await super().asyncTearDown()

    async def test_create_attach_input_and_output(self) -> None:
        await self.client.send_json({"type": "create", "cols": 80, "rows": 24})
        sessions = await self.client.recv_json_until(
            lambda m: m["type"] == "sessions" and m["sessions"]
        )
        session_id = sessions["sessions"][0]["id"]

        await self.client.send_json({"type": "subscribe", "session_id": session_id})
        attached = await self.client.recv_json_until(lambda m: m["type"] == "attached")
        self.assertEqual(attached["session"]["id"], session_id)

        await self.client.send_json(
            {"type": "input", "session_id": session_id, "data": "echo HELLO_WEBTERM\n"}
        )
        output = await self.client.recv_until_contains(b"HELLO_WEBTERM")
        self.assertIn(b"HELLO_WEBTERM", output)

    async def test_create_with_command_runs_it(self) -> None:
        await self.client.send_json(
            {"type": "create", "command": ["/bin/sh", "-c", "echo AGENT_MARKER; sleep 5"]}
        )
        sessions = await self.client.recv_json_until(
            lambda m: m["type"] == "sessions" and m["sessions"]
        )
        session = sessions["sessions"][0]
        self.assertEqual(session["title"], "sh")
        session_id = session["id"]
        await self.client.send_json({"type": "subscribe", "session_id": session_id})
        await self.client.recv_json_until(lambda m: m["type"] == "attached")
        output = await self.client.recv_until_contains(b"AGENT_MARKER")
        self.assertIn(b"AGENT_MARKER", output)

    async def test_resize_is_accepted(self) -> None:
        await self.client.send_json({"type": "create"})
        sessions = await self.client.recv_json_until(
            lambda m: m["type"] == "sessions" and m["sessions"]
        )
        session_id = sessions["sessions"][0]["id"]
        await self.client.send_json(
            {"type": "resize", "session_id": session_id, "cols": 100, "rows": 30}
        )
        await asyncio.sleep(0.1)
        self.assertEqual(self.manager.get(session_id).cols, 100)
        self.assertEqual(self.manager.get(session_id).rows, 30)

    async def test_kill_removes_session(self) -> None:
        await self.client.send_json({"type": "create"})
        sessions = await self.client.recv_json_until(
            lambda m: m["type"] == "sessions" and m["sessions"]
        )
        session_id = sessions["sessions"][0]["id"]

        await self.client.send_json({"type": "kill", "session_id": session_id})
        empty = await self.client.recv_json_until(
            lambda m: m["type"] == "sessions" and not m["sessions"]
        )
        self.assertEqual(empty["sessions"], [])
        self.assertIsNone(self.manager.get(session_id))

    async def test_second_client_gets_scrollback(self) -> None:
        await self.client.send_json({"type": "create"})
        sessions = await self.client.recv_json_until(
            lambda m: m["type"] == "sessions" and m["sessions"]
        )
        session_id = sessions["sessions"][0]["id"]
        await self.client.send_json({"type": "subscribe", "session_id": session_id})
        await self.client.recv_json_until(lambda m: m["type"] == "attached")
        await self.client.send_json(
            {"type": "input", "session_id": session_id, "data": "echo SHARED_MARKER\n"}
        )
        await self.client.recv_until_contains(b"SHARED_MARKER")

        second = await WSClient.connect(self.host, self.port)
        try:
            await second.recv_json_until(
                lambda m: m["type"] == "sessions" and m["sessions"]
            )
            await second.send_json({"type": "subscribe", "session_id": session_id})
            await second.recv_json_until(lambda m: m["type"] == "attached")
            replay = await second.recv_until_contains(b"SHARED_MARKER")
            self.assertIn(b"SHARED_MARKER", replay)
        finally:
            await second.close()

    async def test_invalid_path_rejected(self) -> None:
        # A normal HTTP GET to the websocket path must not upgrade.
        reader, writer = await asyncio.open_connection(self.host, self.port)
        writer.write(b"GET /ws HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        await writer.drain()
        raw = await reader.read()
        writer.close()
        self.assertNotIn(b"101", raw.split(b"\r\n")[0])


if __name__ == "__main__":
    unittest.main()

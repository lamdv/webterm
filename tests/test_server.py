"""End-to-end tests: HTTP static serving and the WebSocket session protocol."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import unittest

from webterm.__main__ import build_ssl_context
from webterm.server import handle_client, serve
from webterm.session import SessionManager

from .wsclient import WSClient


class ServerFixture(unittest.IsolatedAsyncioTestCase):
    token: str | None = None

    async def asyncSetUp(self) -> None:
        self.connections: set = set()
        self.manager = SessionManager(shell="/bin/sh", cwd="/tmp", scrollback=100_000)

        def on_change() -> None:
            for connection in list(self.connections):
                asyncio.create_task(connection.send_sessions())

        self.manager.add_listener(on_change)

        self.server = await asyncio.start_server(
            lambda r, w: handle_client(r, w, self.manager, self.connections, token=self.token),
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


class AuthTests(ServerFixture):
    token = "s3cret-token"

    @staticmethod
    def _basic(username: str, password: str) -> str:
        raw = base64.b64encode(f"{username}:{password}".encode()).decode()
        return f"Basic {raw}"

    async def _get(self, path: str, authorization: str | None = None) -> tuple[str, bytes]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        request = f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
        if authorization:
            request += f"Authorization: {authorization}\r\n"
        request += "\r\n"
        writer.write(request.encode())
        await writer.drain()
        raw = await reader.read()
        writer.close()
        head, _, body = raw.partition(b"\r\n\r\n")
        return head.split(b"\r\n")[0].decode(), body

    async def test_missing_credentials_are_rejected(self) -> None:
        status, _ = await self._get("/")
        self.assertIn("401", status)

    async def test_wrong_credentials_are_rejected(self) -> None:
        status, _ = await self._get("/", self._basic("user", "wrong"))
        self.assertIn("401", status)

    async def test_token_as_password_is_accepted(self) -> None:
        status, body = await self._get("/", self._basic("user", self.token))
        self.assertIn("200", status)
        self.assertIn(b"webterm", body)

    async def test_token_as_username_is_accepted(self) -> None:
        status, _ = await self._get("/config.js", self._basic(self.token, ""))
        self.assertIn("200", status)

    async def test_websocket_requires_credentials(self) -> None:
        with self.assertRaises(AssertionError):
            await WSClient.connect(self.host, self.port)

    async def test_websocket_accepts_credentials(self) -> None:
        client = await WSClient.connect(
            self.host,
            self.port,
            headers={"Authorization": self._basic("user", self.token)},
        )
        try:
            hello = await client.recv_json()
            self.assertEqual(hello["type"], "sessions")
        finally:
            await client.close()


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


class RestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_notifies_client_and_returns_true(self) -> None:
        manager = SessionManager(shell="/bin/sh", cwd="/tmp")

        # Pick a free port, then let serve() claim it.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        server_task = asyncio.create_task(serve("127.0.0.1", port, manager))
        client = None
        try:
            for _ in range(100):
                try:
                    client = await WSClient.connect("127.0.0.1", port)
                    break
                except OSError:
                    await asyncio.sleep(0.02)
            self.assertIsNotNone(client, "server did not start")

            await client.recv_json_until(lambda m: m["type"] == "sessions")
            await client.send_json({"type": "restart"})
            message = await client.recv_json_until(lambda m: m["type"] == "restarting")
            self.assertEqual(message, {"type": "restarting"})

            self.assertTrue(await asyncio.wait_for(server_task, timeout=5))
        finally:
            if client is not None:
                await client.close()
            if not server_task.done():
                server_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await server_task


@unittest.skipUnless(shutil.which("openssl"), "openssl not available")
class TlsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp(prefix="webterm-tls-")
        cls.cert = os.path.join(cls.tmpdir, "cert.pem")
        cls.key = os.path.join(cls.tmpdir, "key.pem")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                cls.key,
                "-out",
                cls.cert,
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    async def test_build_ssl_context_loads_cert(self) -> None:
        context = build_ssl_context(self.cert, self.key)
        self.assertIsInstance(context, ssl.SSLContext)

    async def test_https_static_and_wss_protocol(self) -> None:
        manager = SessionManager(shell="/bin/sh", cwd="/tmp")
        server_ctx = build_ssl_context(self.cert, self.key)
        client_ctx = ssl.create_default_context(cafile=self.cert)

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        server_task = asyncio.create_task(
            serve("127.0.0.1", port, manager, token="tls-token", ssl_context=server_ctx)
        )
        client = None
        try:
            auth = "Basic " + base64.b64encode(b"user:tls-token").decode()
            for _ in range(100):
                try:
                    client = await WSClient.connect(
                        "127.0.0.1",
                        port,
                        headers={"Authorization": auth},
                        ssl=client_ctx,
                        server_hostname="localhost",
                    )
                    break
                except (OSError, ssl.SSLError):
                    await asyncio.sleep(0.02)
            self.assertIsNotNone(client, "TLS server did not start")
            hello = await client.recv_json()
            self.assertEqual(hello["type"], "sessions")

            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=client_ctx, server_hostname="localhost"
            )
            writer.write(
                (
                    "GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
                    f"Authorization: {auth}\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            raw = await reader.read()
            writer.close()
            self.assertIn(b"200 OK", raw.split(b"\r\n", 1)[0])
            self.assertIn(b"webterm", raw)
        finally:
            if client is not None:
                await client.close()
            manager.close_all()
            if not server_task.done():
                server_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await server_task


class ReaperShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_all_uninstalls_sigchld_handler(self) -> None:
        """The reaper must be removed before the loop's wakeup fd closes."""
        with tempfile.TemporaryDirectory() as tmp:
            manager = SessionManager(shell="/bin/sh", cwd=tmp)
            manager.create(cols=80, rows=24)
            self.assertTrue(manager._reaper_installed)
            manager.close_all()
            self.assertFalse(manager._reaper_installed)
            self.assertIs(signal.getsignal(signal.SIGCHLD), signal.SIG_DFL)


if __name__ == "__main__":
    unittest.main()

"""pi absence behavior: the core must work without a pi executable."""

from __future__ import annotations

import shutil
import tempfile
import unittest

from webterm.server import start_server
from webterm.session import SessionManager

from .wsclient import WSClient


class PiAbsenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cwd = tempfile.mkdtemp(prefix="webterm-no-pi-")
        self.manager = SessionManager(
            shell="/bin/sh",
            cwd=self.cwd,
            retained_ttl=None,
            pi_command=["definitely-not-a-real-pi"],
            pi_available=False,
        )
        self.handle = await start_server(
            "127.0.0.1",
            0,
            self.manager,
            enable_control=False,
            discovery=False,
        )
        self.client = await WSClient.connect("127.0.0.1", self.handle.port)
        await self.client.recv_json()  # initial sessions broadcast

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.handle.stop()
        shutil.rmtree(self.cwd, ignore_errors=True)

    async def test_create_pi_is_rejected_without_starting_a_child(self) -> None:
        await self.client.send_json(
            {
                "type": "create",
                "id": "create-pi",
                "mode": "pi",
                "cwd": self.cwd,
            }
        )
        reply = await self.client.recv_json_until(
            lambda message: message.get("type") == "error"
            and message.get("id") == "create-pi"
        )
        self.assertEqual(reply["error"]["code"], "unavailable")
        self.assertEqual(self.manager.sessions, {})

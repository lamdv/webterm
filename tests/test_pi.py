"""Tests for structured pi sessions and their server transport."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from webterm.server import start_server
from webterm.session import SessionManager

from .wsclient import WSClient

FAKE_PI = Path(__file__).with_name("fake_pi.py")


class Collector:
    """PiSubscriber that records events and exit notifications."""

    def __init__(self) -> None:
        self.events: list[tuple[int, dict]] = []
        self.exited = False

    def offer_pi_event(self, session, seq: int, record: dict) -> None:
        self.events.append((seq, record))

    def offer_exit(self, session) -> None:
        self.exited = True


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true in time")


class PiSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cwd = tempfile.mkdtemp(prefix="webterm-pi-")
        self.manager = SessionManager(
            shell="/bin/sh",
            cwd=self.cwd,
            retained_ttl=None,
        )

    async def asyncTearDown(self) -> None:
        self.manager.close_all()
        shutil.rmtree(self.cwd, ignore_errors=True)

    def start(self, env: dict[str, str] | None = None):
        return self.manager.create_pi(
            [sys.executable, str(FAKE_PI)],
            cwd=self.cwd,
            env=env,
        )

    async def test_prompt_streams_events_and_snapshot(self) -> None:
        session = self.start()
        collector = Collector()
        session.attach(collector)

        response = await session.rpc({"type": "prompt", "message": "hello pi"})
        self.assertTrue(response["success"])
        await wait_until(
            lambda: any(record.get("type") == "agent_settled" for _, record in collector.events)
        )

        kinds = [record.get("type") for _, record in collector.events]
        self.assertIn("message_update", kinds)
        self.assertIn("agent_settled", kinds)
        seqs = [seq for seq, _ in collector.events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

        snapshot = await session.snapshot()
        self.assertEqual(snapshot["state"]["model"]["id"], "fake-model")
        self.assertEqual(snapshot["entries"]["leafId"], "entry-1")
        self.assertEqual(snapshot["commands"]["commands"][0]["name"], "review")
        self.assertGreaterEqual(snapshot["seq"], seqs[-1])

        session.close()
        self.assertIsNotNone(await session.wait(timeout=5.0))
        self.assertTrue(collector.exited)

    async def test_close_escalates_when_stdin_eof_is_ignored(self) -> None:
        session = self.start({"FAKE_PI_IGNORE_EOF": "1", "FAKE_PI_IGNORE_TERM": "1"})
        session.PI_STDIN_GRACE = 0.05
        session.PI_TERM_GRACE = 0.2
        session.PI_FORCE_GRACE = 0.2
        session.close()
        self.assertIsNotNone(await session.wait(timeout=5.0))
        self.assertTrue(session.dead)

    async def test_close_kills_the_process_group(self) -> None:
        child_file = Path(self.cwd) / "child.pid"
        marker = Path(self.cwd) / "child.term"
        session = self.start(
            {
                "FAKE_PI_CHILD_PID_FILE": str(child_file),
                "FAKE_PI_CHILD_MARKER": str(marker),
                "FAKE_PI_IGNORE_EOF": "1",
            }
        )
        await wait_until(child_file.exists)
        self.assertTrue(int(child_file.read_text(encoding="utf-8")) > 0)
        session.close()
        self.assertIsNotNone(await session.wait(timeout=5.0))
        # The sleeping grandchild only records the marker when SIGTERM reaches
        # the process group, proving close() did not just stop the pi parent.
        await wait_until(marker.exists, timeout=5.0)


class PiServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cwd = tempfile.mkdtemp(prefix="webterm-pi-server-")
        self.manager = SessionManager(
            shell="/bin/sh",
            cwd=self.cwd,
            retained_ttl=None,
            pi_command=[sys.executable, str(FAKE_PI)],
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

    async def _create_pi(self, title: str = "fake chat") -> dict:
        await self.client.send_json(
            {
                "type": "create",
                "id": "create-pi",
                "mode": "pi",
                "cwd": self.cwd,
                "title": title,
                "trust": "allow",
            }
        )
        result = await self.client.recv_json_until(
            lambda message: message.get("type") == "result"
            and message.get("id") == "create-pi"
        )
        return result["result"]["session"]

    async def _subscribe_pi(self, session_id: str) -> dict:
        await self.client.send_json({"type": "subscribe", "session_id": session_id})
        await self.client.recv_json_until(
            lambda message: message.get("type") == "attached"
            and message["session"]["id"] == session_id
        )
        return await self.client.recv_json_until(
            lambda message: message.get("type") == "pi_resync"
            and message.get("session_id") == session_id
        )

    async def test_list_dirs_lists_only_directories(self) -> None:
        root = Path(self.cwd)
        (root / "beta").mkdir()
        (root / "alpha").mkdir()
        (root / "note.txt").write_text("x", encoding="utf-8")

        await self.client.send_json(
            {"type": "list_dirs", "id": "list-dirs", "path": self.cwd}
        )
        result = await self.client.recv_json_until(
            lambda message: message.get("type") == "result"
            and message.get("id") == "list-dirs"
        )
        listing = result["result"]
        self.assertEqual(listing["path"], str(root.resolve()))
        self.assertEqual([entry["name"] for entry in listing["dirs"]], ["alpha", "beta"])
        self.assertEqual(listing["parent"], str(root.parent.resolve()))

    async def test_rename_updates_pi_session_name(self) -> None:
        session = await self._create_pi()
        session_id = session["id"]
        await self._subscribe_pi(session_id)

        await self.client.send_json(
            {
                "type": "rename",
                "id": "rename-pi",
                "session_id": session_id,
                "title": "renamed chat",
            }
        )
        result = None
        event = None
        while result is None or event is None:
            message = await self.client.recv_json()
            if message.get("type") == "result" and message.get("id") == "rename-pi":
                result = message
            elif (
                message.get("type") == "pi_event"
                and message.get("session_id") == session_id
                and message["record"].get("type") == "session_info_changed"
            ):
                event = message

        self.assertEqual(result["result"]["session"]["title"], "renamed chat")
        self.assertEqual(event["record"]["name"], "renamed chat")

    async def test_snapshot_request_sends_resync(self) -> None:
        session = await self._create_pi()
        session_id = session["id"]
        await self._subscribe_pi(session_id)

        await self.client.send_json(
            {"type": "pi_snapshot", "id": "snapshot-pi", "session_id": session_id}
        )
        result = None
        resync = None
        while result is None or resync is None:
            message = await self.client.recv_json()
            if message.get("type") == "result" and message.get("id") == "snapshot-pi":
                result = message
            elif (
                message.get("type") == "pi_resync"
                and message.get("session_id") == session_id
            ):
                resync = message

        self.assertTrue(result["result"]["ok"])
        self.assertEqual(resync["entries"]["leafId"], "entry-1")

    async def test_create_prompt_and_subscribe(self) -> None:
        await self.client.send_json(
            {
                "type": "create",
                "id": "c1",
                "mode": "pi",
                "cwd": self.cwd,
                "title": "fake chat",
                "trust": "allow",
            }
        )
        result = await self.client.recv_json_until(
            lambda message: message.get("type") == "result" and message.get("id") == "c1"
        )
        session = result["result"]["session"]
        self.assertEqual(session["kind"], "pi")
        self.assertTrue(session["trusted"])
        session_id = session["id"]

        await self.client.send_json({"type": "subscribe", "session_id": session_id})
        attached = await self.client.recv_json_until(
            lambda message: message.get("type") == "attached"
            and message["session"]["id"] == session_id
        )
        self.assertEqual(attached["session"]["kind"], "pi")
        resync = await self.client.recv_json_until(
            lambda message: message.get("type") == "pi_resync"
            and message.get("session_id") == session_id
        )
        self.assertEqual(resync["entries"]["leafId"], "entry-1")

        await self.client.send_json(
            {
                "type": "pi",
                "id": "p1",
                "session_id": session_id,
                "command": {"type": "prompt", "message": "hello"},
            }
        )
        result = await self.client.recv_json_until(
            lambda message: message.get("type") == "result" and message.get("id") == "p1"
        )
        self.assertTrue(result["result"]["pi_response"]["success"])

        events = []

        async def collect() -> None:
            while True:
                message = await self.client.recv_json()
                if message.get("type") == "pi_event" and message.get("session_id") == session_id:
                    events.append(message["record"])
                    if message["record"].get("type") == "agent_settled":
                        return

        await asyncio.wait_for(collect(), timeout=5.0)
        self.assertIn(
            "message_update",
            [record.get("type") for record in events],
        )

        await self.client.send_json({"type": "kill", "session_id": session_id})
        await self.client.recv_json_until(
            lambda message: message.get("type") == "sessions"
            and all(item["id"] != session_id for item in message["sessions"])
        )


if __name__ == "__main__":
    unittest.main()

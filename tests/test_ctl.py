"""End-to-end tests for the sync ``ControlClient`` and the ``webtermctl`` CLI."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from webterm.client import ControlClient
from webterm.ctl import main as ctl_main
from webterm.server import ServerHandle, start_server
from webterm.session import SessionManager

FAKE_PI = Path(__file__).with_name("fake_pi.py")


class CtlBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.tmp.name) / "control.sock")
        self.manager = SessionManager(shell="/bin/sh", cwd="/tmp")
        self.profiles = {
            "echoer": {
                "print": ["/bin/sh", "-c", "printf '{task}'"],
                "interactive": ["/bin/sh", "-c", "printf '{task}'"],
            },
            "pi": {
                "print": [sys.executable, str(FAKE_PI)],
                "interactive": [sys.executable, str(FAKE_PI)],
                "rpc": [sys.executable, str(FAKE_PI)],
            },
        }
        self.handle: ServerHandle = await start_server(
            "127.0.0.1", 0, self.manager, control_socket=self.socket_path, profiles=self.profiles
        )

    async def asyncTearDown(self) -> None:
        await self.handle.stop()
        self.tmp.cleanup()

    async def run_ctl(self, *args: str) -> tuple[int, bytes]:
        """Run the blocking CLI on a worker thread and capture its stdout."""
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="utf-8", write_through=True)
        argv = ["--socket", self.socket_path, *args]
        with contextlib.redirect_stdout(stream):
            code = await asyncio.to_thread(ctl_main, argv)
        stream.flush()
        return code, buffer.getvalue()


class CtlCommandTests(CtlBase):
    async def test_list_empty(self) -> None:
        code, out = await self.run_ctl("list")
        self.assertEqual(code, 0)
        self.assertIn(b"no sessions", out)

    async def test_run_streams_output_and_exit_code(self) -> None:
        code, out = await self.run_ctl("run", "--", "sh", "-c", "printf RUN_OK; exit 5")
        self.assertEqual(code, 5)
        self.assertEqual(out, b"RUN_OK")

    async def test_run_json(self) -> None:
        code, out = await self.run_ctl("--json", "run", "--", "sh", "-c", "echo jsonrun")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn(b"jsonrun", base64.b64decode(payload["output_base64"]))

    async def test_spawn_command_and_list_tree(self) -> None:
        code, out = await self.run_ctl("spawn", "--command", "printf SPAWNED", "--text")
        self.assertEqual(code, 0)
        self.assertEqual(out, b"SPAWNED")

        code, out = await self.run_ctl("list")
        self.assertEqual(code, 0)
        self.assertIn(b"agent", out)
        self.assertIn(b"exit 0", out)

    async def test_spawn_with_profile(self) -> None:
        code, out = await self.run_ctl(
            "spawn", "--agent", "echoer", "--task", "TASK_DONE", "--text"
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, b"TASK_DONE")

    async def test_spawn_rpc_and_pi_prompt(self) -> None:
        code, out = await self.run_ctl(
            "spawn", "--agent", "pi", "--rpc", "--task", "hello", "--text"
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, b"fake reply")

        code, out = await self.run_ctl(
            "--json", "spawn", "--agent", "pi", "--rpc", "--task", "hello", "--no-wait"
        )
        self.assertEqual(code, 0)
        session_id = json.loads(out)["session_id"]
        code, out = await self.run_ctl(
            "pi", session_id, "prompt", "second task", "--no-wait", "--json"
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["response"]["success"])
        code, out = await self.run_ctl("wait", session_id, "--settled", "--json")
        self.assertEqual(code, 0)
        code, out = await self.run_ctl("--json", "pi", session_id, "last-text")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["response"]["data"]["text"], "fake reply")
        await self.run_ctl("kill", session_id)

    async def test_spawn_without_agent_or_command_fails(self) -> None:
        code, _ = await self.run_ctl("spawn")
        self.assertEqual(code, 2)

    async def test_spawn_interactive_does_not_wait(self) -> None:
        code, out = await self.run_ctl(
            "spawn", "--agent", "echoer", "--task", "x", "--interactive"
        )
        self.assertEqual(code, 0)
        session_id = out.decode().strip()
        self.assertTrue(session_id)
        await self.run_ctl("kill", session_id)

    async def test_new_capture_and_kill(self) -> None:
        code, out = await self.run_ctl("new")
        self.assertEqual(code, 0)
        session_id = out.decode().strip()
        self.assertTrue(session_id)

        await self.run_ctl("send", session_id, "printf CAPTURED", "--enter")
        await self.run_ctl("wait", session_id, "--text", "CAPTURED")

        code, out = await self.run_ctl("capture", session_id, "--text")
        self.assertEqual(code, 0)
        self.assertIn(b"CAPTURED", out)

        code, out = await self.run_ctl("--json", "kill", session_id)
        self.assertEqual(code, 0)
        self.assertIn(session_id, json.loads(out)["killed"])

    async def test_wait_exit_on_no_wait_run(self) -> None:
        code, out = await self.run_ctl("run", "--no-wait", "--", "sh", "-c", "sleep 0.2; exit 9")
        self.assertEqual(code, 0)
        session_id = out.decode().strip()
        code, _ = await self.run_ctl("wait", session_id, "--exit")
        self.assertEqual(code, 9)

    async def test_no_socket_reports_error(self) -> None:
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="utf-8", write_through=True)
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = await asyncio.to_thread(
                ctl_main, ["--socket", f"{self.tmp.name}/missing.sock", "list"]
            )
        stream.flush()
        self.assertEqual(code, 2)
        self.assertIn(b"no control socket", buffer.getvalue())


class ControlClientLibraryTests(CtlBase):
    async def test_library_run(self) -> None:
        def _call() -> dict:
            with ControlClient(self.socket_path) as client:
                return client.run(["sh", "-c", "printf LIB"], wait=True, format="text")

        result = await asyncio.to_thread(_call)
        self.assertEqual(result["output_text"], "LIB")
        self.assertEqual(result["exit_code"], 0)

    async def test_library_spawn_and_lineage(self) -> None:
        def _call() -> tuple[dict, list[dict]]:
            with ControlClient(self.socket_path) as client:
                parent = client.create()
                child = client.spawn(
                    agent="echoer", task="KID", parent_id=parent["id"], wait=True, format="text"
                )
                return child, client.list_sessions()

        child, sessions = await asyncio.to_thread(_call)
        self.assertEqual(child["output_text"], "KID")
        record = next(s for s in sessions if s["id"] == child["session_id"])
        self.assertIsNotNone(record["parent_id"])
        self.assertEqual(record["role"], "agent")


if __name__ == "__main__":
    unittest.main()

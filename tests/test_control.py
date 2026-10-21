"""End-to-end tests for the local (Unix socket) agent control plane."""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from tests.controlclient import ControlClient
from webterm.control import looks_like_tcp
from webterm.server import ClientConnection, RequestError, ServerHandle, start_server
from webterm.session import SessionManager

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")


class ControlSocketBase(unittest.IsolatedAsyncioTestCase):
    """Starts a real server whose control socket lives in a temp directory."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.tmp.name) / "control.sock")
        self.manager = SessionManager(shell="/bin/sh", cwd="/tmp")
        self.handle: ServerHandle = await start_server(
            "127.0.0.1", 0, self.manager, control_socket=self.socket_path
        )
        self.client = await ControlClient.connect(self.socket_path)

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.handle.stop()
        self.tmp.cleanup()

    def result(self, message: dict) -> dict:
        self.assertEqual(message.get("type"), "result", message)
        return message["result"]


class ControlSocketTests(ControlSocketBase):
    async def test_socket_is_private_and_discoverable(self) -> None:
        mode = stat.S_IMODE(os.stat(self.socket_path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.handle.control_socket, self.socket_path)
        self.assertEqual(self.manager.control_socket, self.socket_path)

        discovery = json.loads((Path(self.tmp.name) / "server.json").read_text())
        self.assertEqual(discovery["socket"], self.socket_path)
        self.assertEqual(discovery["url"], self.handle.url)

    async def test_list_returns_sessions(self) -> None:
        result = self.result(await self.client.request("list"))
        self.assertEqual(result["sessions"], [])

    async def test_create_input_and_capture(self) -> None:
        created = self.result(await self.client.request("create", cols=100, rows=30))
        session_id = created["session"]["id"]
        self.assertEqual(created["session"]["cols"], 100)

        await self.client.request(
            "send", session_id=session_id, data="printf 'HELLO_AGENT\\n'", submit=True
        )
        await self.client.request(
            "wait", session_id=session_id, until="text", pattern="HELLO_AGENT"
        )

        captured = self.result(
            await self.client.request("capture", session_id=session_id, format="text")
        )
        self.assertIn("HELLO_AGENT", captured["text"])

    async def test_run_returns_output_and_exit_code(self) -> None:
        result = self.result(
            await self.client.request(
                "run", command="printf 'RAN_OK'; exit 4", wait=True, format="text"
            )
        )
        self.assertEqual(result["exit_code"], 4)
        self.assertEqual(result["output_text"], "RAN_OK")
        self.assertFalse(result["truncated"])

    async def test_run_without_wait_returns_session_id(self) -> None:
        result = self.result(
            await self.client.request("run", command="sleep 30", wait=False)
        )
        self.assertIn("session_id", result)
        self.assertTrue(result["session"]["alive"])
        await self.client.request("kill", session_id=result["session_id"])

    async def test_spawn_with_profile(self) -> None:
        # Rebuild the server with a test profile so spawn has a command to run.
        await self.client.close()
        await self.handle.stop()
        profiles = {"echoer": {"print": ["/bin/sh", "-c", "printf '{task}'"]}}
        self.handle = await start_server(
            "127.0.0.1", 0, self.manager, control_socket=self.socket_path, profiles=profiles
        )
        self.client = await ControlClient.connect(self.socket_path)

        parent = self.result(await self.client.request("create"))
        parent_id = parent["session"]["id"]
        result = self.result(
            await self.client.request(
                "spawn",
                agent="echoer",
                task="AGENT_DONE",
                parent_id=parent_id,
                wait=True,
                format="text",
            )
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output_text"], "AGENT_DONE")

        listed = self.result(await self.client.request("list"))["sessions"]
        child = next(s for s in listed if s["id"] == result["session_id"])
        self.assertEqual(child["parent_id"], parent_id)
        self.assertEqual(child["role"], "agent")
        self.assertEqual(child["agent"], "echoer")
        self.assertEqual(child["task"], "AGENT_DONE")

    async def test_spawn_command_delivers_the_task(self) -> None:
        # An explicit command has no {task} template; the task must still be
        # delivered instead of only becoming session metadata.
        for command in (
            "/bin/sh -c 'printf %s \"$0\"'",
            ["/bin/sh", "-c", 'printf %s "$0"'],
        ):
            with self.subTest(command=command):
                result = self.result(
                    await self.client.request(
                        "spawn",
                        command=command,
                        task="from the parent",
                        wait=True,
                        format="text",
                    )
                )
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(result["output_text"], "from the parent")

    async def test_rpc_prompt_timeout_has_a_message(self) -> None:
        await self.client.close()
        await self.handle.stop()
        profiles = {
            "mute": {"rpc": ["/bin/sh", "-c", "sleep 5"], "adapter": "pi"}
        }
        self.handle = await start_server(
            "127.0.0.1", 0, self.manager, control_socket=self.socket_path, profiles=profiles
        )
        self.client = await ControlClient.connect(self.socket_path)

        message = await self.client.request(
            "spawn", agent="mute", rpc=True, task="hello", timeout=0.2
        )
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["error"]["code"], "pi_error")
        self.assertIn("timeout", message["error"]["message"])

    async def test_spawn_profile_can_select_an_adapter_and_model(self) -> None:
        await self.client.close()
        await self.handle.stop()
        profiles = {
            "llama": {
                "rpc": [sys.executable, str(FAKE_AGENT)],
                "adapter": "llama",
            }
        }
        self.handle = await start_server(
            "127.0.0.1", 0, self.manager, control_socket=self.socket_path, profiles=profiles
        )
        self.client = await ControlClient.connect(self.socket_path)

        result = self.result(
            await self.client.request(
                "spawn",
                agent="llama",
                rpc=True,
                task="do it",
                model="other-model",
                wait=False,
            )
        )
        session_id = result["session_id"]
        self.assertEqual(result["session"]["adapter"], "llama")
        self.assertEqual(result["session"]["model"], "other-model")
        await self.client.request("kill", session_id=session_id)

    async def test_spawn_unknown_agent_is_bad_request(self) -> None:
        message = await self.client.request("spawn", agent="nope", task="x")
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["error"]["code"], "bad_request")

    async def test_unknown_verb_is_an_error(self) -> None:
        message = await self.client.request("frobnicate")
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["error"]["code"], "bad_request")

    async def test_unknown_session_is_not_found(self) -> None:
        message = await self.client.request("capture", session_id="ghost")
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["error"]["code"], "not_found")

    async def test_kill_is_cascading(self) -> None:
        root = self.result(await self.client.request("run", command="sleep 30", wait=False))
        root_id = root["session_id"]
        child = self.result(
            await self.client.request(
                "run", command="sleep 30", wait=False, parent_id=root_id
            )
        )
        child_id = child["session_id"]

        killed = self.result(await self.client.request("kill", session_id=root_id))
        self.assertEqual(set(killed["killed"]), {root_id, child_id})

        listed = self.result(await self.client.request("list"))["sessions"]
        self.assertEqual(listed, [])

    async def test_wait_for_idle(self) -> None:
        created = self.result(await self.client.request("create"))
        session_id = created["session"]["id"]
        await self.client.request(
            "send", session_id=session_id, data="printf 'tick'; sleep 0.5", submit=True
        )
        result = self.result(
            await self.client.request(
                "wait", session_id=session_id, until="idle", quiet_ms=150, timeout=5
            )
        )
        self.assertTrue(result["matched"])

    async def test_child_inherits_control_env(self) -> None:
        created = self.result(await self.client.request("create"))
        session_id = created["session"]["id"]
        await self.client.request(
            "send",
            session_id=session_id,
            data='printf "%s" "$WEBTERM_SOCKET"; printf "\\n"; '
            'printf "%s" "$WEBTERM_SESSION_ID"',
            submit=True,
        )
        await self.client.request("wait", session_id=session_id, until="text", pattern=session_id)
        captured = self.result(
            await self.client.request("capture", session_id=session_id, format="text")
        )
        self.assertIn(self.socket_path, captured["text"])
        self.assertIn(session_id, captured["text"])

    async def test_capture_base64_roundtrip(self) -> None:
        result = self.result(await self.client.request("run", command="printf 'abc'", wait=True))
        self.assertEqual(base64.b64decode(result["output_base64"]), b"abc")


class ResolveAgentTests(unittest.TestCase):
    def connection(self, profiles: dict) -> ClientConnection:
        return ClientConnection(None, None, profiles=profiles)

    def test_task_is_substituted(self) -> None:
        conn = self.connection({"p": {"print": ["/bin/echo", "{task}"]}})
        self.assertEqual(conn._resolve_agent("p", "hi", None, False), ["/bin/echo", "hi"])

    def test_missing_task_drops_placeholder(self) -> None:
        conn = self.connection({"p": {"interactive": ["/bin/echo", "{task}"]}})
        self.assertEqual(conn._resolve_agent("p", None, None, True), ["/bin/echo"])

    def test_task_is_appended_without_placeholder(self) -> None:
        conn = self.connection({"p": {"print": ["/bin/echo"]}})
        self.assertEqual(conn._resolve_agent("p", "hi", None, False), ["/bin/echo", "hi"])

    def test_task_is_appended_to_an_explicit_argv(self) -> None:
        conn = self.connection({})
        self.assertEqual(
            conn._resolve_agent(None, "hi", ["/bin/echo"], False), ["/bin/echo", "hi"]
        )

    def test_task_is_appended_to_an_explicit_shell_command(self) -> None:
        conn = self.connection({})
        self.assertEqual(
            conn._resolve_agent(None, "a b", "/bin/echo", False),
            "/bin/echo 'a b'",
        )

    def test_rpc_explicit_command_keeps_the_task_for_the_prompt(self) -> None:
        conn = self.connection({})
        self.assertEqual(
            conn._resolve_agent(None, "hi", ["/bin/echo"], False, rpc=True),
            ["/bin/echo"],
        )

    def test_missing_mode_is_rejected(self) -> None:
        conn = self.connection({"p": {"print": ["/bin/echo"]}})
        with self.assertRaises(RequestError):
            conn._resolve_agent("p", None, None, True)


class ControlConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_looks_like_tcp(self) -> None:
        self.assertTrue(looks_like_tcp("127.0.0.1:9999"))
        self.assertTrue(looks_like_tcp("tcp://1.2.3.4:1"))
        self.assertFalse(looks_like_tcp("/run/user/1000/webterm/control.sock"))

    async def test_tcp_looking_socket_is_rejected(self) -> None:
        manager = SessionManager(shell="/bin/sh", cwd="/tmp")
        with self.assertRaises(ValueError):
            await start_server(
                "127.0.0.1", 0, manager, control_socket="127.0.0.1:9999"
            )

    async def test_control_can_be_disabled(self) -> None:
        manager = SessionManager(shell="/bin/sh", cwd="/tmp")
        handle = await start_server("127.0.0.1", 0, manager, enable_control=False)
        try:
            self.assertIsNone(handle.control_socket)
        finally:
            await handle.stop()


if __name__ == "__main__":
    unittest.main()

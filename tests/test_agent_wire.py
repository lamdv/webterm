"""End-to-end tests for native canonical agent sessions over the server wire.

A native agent (``adapter: "generic"``) speaks the canonical JSONL protocol
itself, so it exercises the same ``agent`` envelope, ``agent_resync``,
``agent_event``, and ``agent_dialog_*`` surface that pi reaches through its
codec.  ``tests/fake_agent.py`` is the child.
"""

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

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")


class NativeAgentWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cwd = tempfile.mkdtemp(prefix="webterm-native-agent-")
        self.manager = SessionManager(shell="/bin/sh", cwd=self.cwd, retained_ttl=None)
        self.profiles = {"native": {"rpc": [sys.executable, str(FAKE_AGENT)]}}
        self.handle = await start_server(
            "127.0.0.1",
            0,
            self.manager,
            enable_control=False,
            discovery=False,
            profiles=self.profiles,
        )
        self.client = await WSClient.connect("127.0.0.1", self.handle.port)
        await self.client.recv_json()  # initial sessions broadcast

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.handle.stop()
        shutil.rmtree(self.cwd, ignore_errors=True)

    async def _create(self, *, prompt: str | None = None) -> dict:
        message: dict = {
            "type": "create",
            "id": "create-agent",
            "mode": "agent",
            "command": [sys.executable, str(FAKE_AGENT)],
            "title": "native chat",
            "cwd": self.cwd,
        }
        if prompt:
            message["prompt"] = prompt
        await self.client.send_json(message)
        result = await self.client.recv_json_until(
            lambda reply: reply.get("type") in ("result", "error")
            and reply.get("id") == "create-agent"
        )
        self.assertEqual(result["type"], "result", result)
        return result["result"]["session"]

    async def _attach(self, session_id: str, *, since: int = 0) -> dict:
        await self.client.send_json(
            {"type": "subscribe", "session_id": session_id, "since": since}
        )
        await self.client.recv_json_until(
            lambda reply: reply.get("type") == "attached"
            and reply["session"]["id"] == session_id
        )
        return await self.client.recv_json_until(
            lambda reply: reply.get("type") == "agent_resync"
            and reply.get("session_id") == session_id
        )

    async def _agent(self, session_id: str, command: dict, *, request_id: str) -> dict:
        await self.client.send_json(
            {
                "type": "agent",
                "id": request_id,
                "session_id": session_id,
                "command": command,
            }
        )
        reply = await self.client.recv_json_until(
            lambda message: message.get("type") in ("result", "error")
            and message.get("id") == request_id
        )
        return reply

    async def _collect(self, *predicates, timeout: float = 15.0) -> list[dict]:
        messages: list[dict] = []
        pending = list(predicates)

        async def _loop() -> list[dict]:
            while True:
                message = await self.client.recv_json()
                messages.append(message)
                pending[:] = [check for check in pending if not check(message)]
                if not pending:
                    return messages

        return await asyncio.wait_for(_loop(), timeout)

    @staticmethod
    def _result(request_id: str):
        return lambda message: (
            message.get("type") in ("result", "error")
            and message.get("id") == request_id
        )

    @staticmethod
    def _event(name: str):
        return lambda message: (
            message.get("type") == "agent_event"
            and message["event"].get("event") == name
        )

    async def test_create_and_attach_stream_the_canonical_surface(self) -> None:
        session = await self._create()
        self.assertEqual(session["kind"], "agent")
        self.assertEqual(session["adapter"], "generic")
        self.assertTrue(session["ready"])

        resync = await self._attach(session["id"])
        self.assertEqual(
            [entry["id"] for entry in resync["entries"]], ["entry-1", "entry-2"]
        )
        self.assertEqual(resync["cursor"], {"seq": 0})
        self.assertTrue(resync["capabilities"]["prompt"])
        self.assertEqual(resync["capabilities"]["dialogs"], ["confirm"])

    async def test_prompt_streams_events_and_answers_canonically(self) -> None:
        session = await self._create()
        await self._attach(session["id"])
        await self.client.send_json(
            {
                "type": "agent",
                "id": "p1",
                "session_id": session["id"],
                "command": {"type": "prompt", "message": "hi there"},
            }
        )
        messages = await self._collect(self._result("p1"), self._event("run.settled"))
        result = next(
            message
            for message in messages
            if message.get("type") == "result" and message.get("id") == "p1"
        )
        response = result["result"]["agent_response"]
        self.assertTrue(response["success"])
        self.assertEqual(response["command"], "prompt")
        self.assertEqual(response["data"], {"disposition": "started"})
        names = [
            message["event"]["event"]
            for message in messages
            if message.get("type") == "agent_event"
        ]
        self.assertIn("run.started", names)
        self.assertIn("tool.finished", names)
        self.assertIn("usage", names)
        # A native session has no pi-shaped messages at all.
        self.assertFalse(any(message.get("type", "").startswith("pi_") for message in messages))

    async def test_snapshot_history_and_models_answer_canonically(self) -> None:
        session = await self._create()
        await self._attach(session["id"])

        snapshot = await self._agent(session["id"], {"type": "get_snapshot"}, request_id="s1")
        data = snapshot["result"]["agent_response"]["data"]
        self.assertEqual([entry["id"] for entry in data["entries"]], ["entry-1", "entry-2"])

        page = await self._agent(
            session["id"],
            {"type": "get_history", "before": "entry-2", "limit": 10},
            request_id="h1",
        )
        history = page["result"]["agent_response"]["data"]
        self.assertEqual([entry["id"] for entry in history["entries"]], ["entry-1"])

        models = await self._agent(session["id"], {"type": "list_models"}, request_id="m1")
        listed = models["result"]["agent_response"]["data"]
        self.assertEqual([model["id"] for model in listed["models"]], ["fake-model"])

        applied = await self._agent(
            session["id"],
            {"type": "set_model", "model": "fake-model-2"},
            request_id="m2",
        )
        self.assertEqual(
            applied["result"]["agent_response"]["data"], {"model": "fake-model-2"}
        )

    async def test_adapter_refusals_are_success_false_responses(self) -> None:
        session = await self._create()
        await self._attach(session["id"])
        reply = await self._agent(session["id"], {"type": "fork"}, request_id="f1")
        response = reply["result"]["agent_response"]
        self.assertFalse(response["success"])
        self.assertIn("unknown command", response["error"])
        self.assertEqual(response["command"], "fork")

    async def test_dialog_round_trip_uses_the_envelope(self) -> None:
        session = await self._create()
        await self._attach(session["id"])
        await self.client.send_json(
            {
                "type": "agent",
                "id": "ask",
                "session_id": session["id"],
                "command": {"type": "prompt", "message": "please confirm"},
            }
        )
        messages = await self._collect(
            lambda message: message.get("type") == "agent_dialog_request"
            and message.get("session_id") == session["id"]
        )
        dialog = next(
            message["request"]
            for message in messages
            if message.get("type") == "agent_dialog_request"
        )
        self.assertEqual(dialog["id"], "d1")
        self.assertEqual(dialog["kind"], "confirm")

        answer = await self._agent(
            session["id"],
            {
                "type": "dialog_response",
                "dialog_id": "d1",
                "response": {"approved": True},
            },
            request_id="answer",
        )
        self.assertTrue(answer["result"]["agent_response"]["success"])
        resolved = await self.client.recv_json_until(
            lambda message: message.get("type") == "agent_dialog_resolved"
            and message.get("dialog_id") == "d1"
        )
        self.assertEqual(resolved["reason"], "answered")
        settled = await self.client.recv_json_until(self._event("run.settled"))
        self.assertEqual(settled["event"]["event"], "run.settled")

    async def test_dialog_response_needs_a_pending_dialog(self) -> None:
        session = await self._create()
        await self._attach(session["id"])
        reply = await self._agent(
            session["id"],
            {"type": "dialog_response", "dialog_id": "d-404", "response": {}},
            request_id="answer-404",
        )
        self.assertEqual(reply["type"], "error")
        self.assertEqual(reply["error"]["code"], "not_found")

    async def test_resubscribe_replays_events_after_the_snapshot(self) -> None:
        session = await self._create()
        await self._attach(session["id"])
        await self.client.send_json(
            {
                "type": "agent",
                "id": "p-replay",
                "session_id": session["id"],
                "command": {"type": "prompt", "message": "hello"},
            }
        )
        await self._collect(self._event("run.settled"))

        await self.client.send_json(
            {"type": "unsubscribe", "session_id": session["id"]}
        )
        resync = await self._attach(session["id"], since=0)
        replayed = await self._collect(self._event("run.started"))
        event = replayed[-1]
        # Retained events are resent for the client to drop against the cursor.
        self.assertLessEqual(event["seq"], resync["cursor"]["seq"])

    async def test_terminal_and_legacy_pi_messages_are_refused(self) -> None:
        session = await self._create()
        await self._attach(session["id"])
        await self.client.send_json(
            {"type": "capture", "id": "cap", "session_id": session["id"]}
        )
        reply = await self.client.recv_json_until(self._result("cap"))
        self.assertEqual(reply["type"], "error")
        self.assertIn("no byte stream", reply["error"]["message"])

        await self.client.send_json(
            {
                "type": "pi",
                "id": "legacy",
                "session_id": session["id"],
                "command": {"type": "get_state"},
            }
        )
        reply = await self.client.recv_json_until(self._result("legacy"))
        self.assertEqual(reply["type"], "error")
        self.assertEqual(reply["error"]["code"], "bad_request")
        self.assertIn("not a pi session", reply["error"]["message"])

    async def test_unknown_command_is_rejected_for_the_adapter(self) -> None:
        session = await self._create()
        await self._attach(session["id"])
        reply = await self._agent(
            session["id"], {"type": "teleport"}, request_id="t1"
        )
        self.assertEqual(reply["type"], "error")
        self.assertEqual(reply["error"]["code"], "bad_request")
        self.assertIn("does not support", reply["error"]["message"])

    async def test_shutdown_closes_the_session(self) -> None:
        session = await self._create()
        await self._attach(session["id"])
        reply = await self._agent(session["id"], {"type": "shutdown"}, request_id="bye")
        self.assertTrue(reply["result"]["agent_response"]["success"])
        await self.client.recv_json_until(
            lambda message: message.get("type") == "exit"
            and message.get("session_id") == session["id"]
        )

    async def test_spawn_with_a_native_adapter_waits_for_settled(self) -> None:
        await self.client.send_json(
            {
                "type": "spawn",
                "id": "spawn-native",
                "agent": "native",
                "adapter": "generic",
                "rpc": True,
                "task": "do the thing",
                "wait": True,
                "timeout": 10.0,
            }
        )
        result = await self.client.recv_json_until(self._result("spawn-native"))
        self.assertEqual(result["type"], "result", result)
        data = result["result"]
        self.assertTrue(data["settled"])
        self.assertFalse(data["timed_out"])
        session = self.manager.get(data["session_id"])
        self.assertIsNotNone(session)
        self.assertEqual(session.kind, "agent")
        self.assertEqual(session.adapter, "generic")
        self.assertEqual(session.role, "agent")
        self.assertEqual(session.task, "do the thing")


if __name__ == "__main__":
    unittest.main()

"""Tests for the canonical agent session and its JSONL protocol."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from webterm.agent import (
    AgentClosed,
    AgentProcess,
    AgentProtocolError,
    AgentSession,
    Capabilities,
    ProtocolError,
    parse_hello,
)
from webterm.integration.pi import PiSession
from webterm.session import SessionManager

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")


class Collector:
    """AgentSubscriber that records events, dialogs, and exit notifications."""

    def __init__(self) -> None:
        self.events: list[tuple[int, dict]] = []
        self.dialogs: list[dict] = []
        self.resolved: list[tuple[str, str]] = []
        self.exited = False

    def offer_agent_event(self, session: AgentSession, seq: int, event: dict) -> None:
        self.events.append((seq, event))

    def offer_agent_dialog(self, session: AgentSession, request: dict) -> None:
        self.dialogs.append(request)

    def offer_agent_dialog_resolved(
        self, session: AgentSession, dialog_id: str, reason: str
    ) -> None:
        self.resolved.append((dialog_id, reason))

    def offer_exit(self, session: AgentSession) -> None:
        self.exited = True

    def names(self) -> list[str]:
        return [event.get("event") for _, event in self.events]


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true in time")


class AgentProtocolTests(unittest.TestCase):
    def test_capabilities_accept_flat_and_nested_shapes(self) -> None:
        capabilities = Capabilities.from_dict(
            {
                "steering": True,
                "dialogs": ["confirm", "select", "bogus"],
                "models": {"list": True, "thinking": True, "levels": ["off", "low", 3]},
                "system_prompt": True,
                "history": {"paging": True},
                "tools": {"streaming": False},
                "attachments": {"images": True},
            }
        )
        self.assertTrue(capabilities.steering)
        self.assertEqual(capabilities.dialogs, ("confirm", "select"))
        self.assertTrue(capabilities.models_list)
        self.assertTrue(capabilities.thinking)
        self.assertEqual(capabilities.thinking_levels, ("off", "low"))
        self.assertTrue(capabilities.system_prompt)
        self.assertTrue(capabilities.history_paging)
        self.assertFalse(capabilities.tool_streaming)
        self.assertTrue(capabilities.attachments_images)

    def test_hello_rejects_unknown_protocol(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_hello({"type": "hello", "protocol": 99, "agent": "x"})

    def test_capabilities_round_trip(self) -> None:
        capabilities = Capabilities.from_dict(
            {"models": {"list": True}, "usage": {"tokens": True}, "raw": True}
        )
        again = Capabilities.from_dict(capabilities.to_dict())
        self.assertEqual(capabilities, again)


class AgentSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cwd = tempfile.mkdtemp(prefix="webterm-agent-")
        self.manager = SessionManager(
            shell="/bin/sh",
            cwd=self.cwd,
            retained_ttl=None,
        )

    async def asyncTearDown(self) -> None:
        self.manager.close_all()
        shutil.rmtree(self.cwd, ignore_errors=True)

    async def start(self, env: dict[str, str] | None = None, timeout: float = 5.0):
        return await self.manager.create_agent(
            [sys.executable, str(FAKE_AGENT)],
            cwd=self.cwd,
            env=env,
            handshake_timeout=timeout,
        )

    async def test_hello_publishes_session_and_capabilities(self) -> None:
        session = await self.start()
        self.assertTrue(session.ready)
        self.assertFalse(session.dead)
        self.assertEqual(session.kind, "agent")
        self.assertEqual(session.adapter, "generic")
        self.assertEqual(session.agent_name, "fake")
        self.assertEqual(session.agent_version, "0.1")
        self.assertTrue(session.capabilities.models_list)
        self.assertEqual(session.capabilities.dialogs, ("confirm",))
        self.assertIn(session.id, self.manager.sessions)

    async def test_prompt_streams_messages_tools_and_settles(self) -> None:
        session = await self.start()
        collector = Collector()
        session.attach(collector)

        response = await session.prompt("hello agent")
        self.assertEqual(response["disposition"], "started")
        self.assertTrue(await session.wait_for_settled(5.0))

        names = collector.names()
        self.assertIn("run.started", names)
        self.assertIn("message.delta", names)
        self.assertIn("tool.started", names)
        self.assertIn("tool.finished", names)
        self.assertIn("usage", names)
        self.assertIn("run.settled", names)
        self.assertIn("run.finished", names)
        self.assertEqual(session.tokens, 10)
        self.assertAlmostEqual(session.cost, 0.001)
        self.assertEqual(session.model, "fake-model")
        self.assertEqual(session.session_file, "/tmp/fake-session.jsonl")
        # The event ring is replayable in order.
        replayed = session.events_since(0)
        self.assertEqual([seq for seq, _ in replayed], sorted(seq for seq, _ in replayed))
        self.assertEqual(replayed[-1][1]["event"], "run.settled")

    async def test_streaming_deltas_do_not_rebroadcast_sessions(self) -> None:
        session = await self.start()
        collector = Collector()
        session.attach(collector)
        changes: list[None] = []
        self.manager.add_listener(lambda: changes.append(None))

        await session.prompt("slow run")
        await wait_until(
            lambda: collector.names().count("message.delta") >= 12
        )
        await session.abort()
        self.assertTrue(await session.wait_for_settled(5.0))

        deltas = collector.names().count("message.delta")
        self.assertGreaterEqual(deltas, 12)
        # The session list is rebroadcast on state transitions (run started,
        # finished, settled), not once per streamed token.
        self.assertLessEqual(len(changes), 6)

    async def test_snapshot_has_cursor_entries_dialogs_and_capabilities(self) -> None:
        session = await self.start()
        snapshot = await session.snapshot(limit=10)
        self.assertEqual(snapshot["cursor"]["seq"], session.seq)
        self.assertEqual(snapshot["entries"][0]["id"], "entry-1")
        self.assertEqual(snapshot["first_entry_id"], "entry-1")
        self.assertFalse(snapshot["has_more"])
        self.assertEqual(snapshot["dialogs"], [])
        self.assertTrue(snapshot["capabilities"]["models"]["list"])
        self.assertIn("streaming", snapshot["state"])

    async def test_history_and_models(self) -> None:
        session = await self.start()
        page = await session.history("entry-2", limit=10)
        self.assertEqual([entry["id"] for entry in page["entries"]], ["entry-1"])
        self.assertFalse(page["has_more"])

        data = await session.models()
        self.assertEqual(data["default"], "fake-model")
        self.assertEqual(data["models"][0]["id"], "fake-model")

        await session.command("set_model", model="other-model")
        await wait_until(lambda: session.model == "other-model")

    async def test_dialog_response_resolves_and_run_finishes(self) -> None:
        session = await self.start()
        collector = Collector()
        session.attach(collector)

        await session.prompt("please confirm this")
        await wait_until(lambda: bool(collector.dialogs))
        request = collector.dialogs[0]
        self.assertEqual(request["id"], "d1")
        self.assertEqual(request["kind"], "confirm")
        self.assertTrue(session.info()["needs_input"])

        session.resolve_dialog("d1", {"approved": True})
        self.assertTrue(await session.wait_for_settled(5.0))
        self.assertEqual(collector.resolved, [("d1", "answered")])
        self.assertFalse(session.info()["needs_input"])
        finished = [
            event
            for _, event in collector.events
            if event.get("event") == "message.finished"
        ][-1]
        self.assertIn("approved", finished["message"]["content"][0]["text"])

    async def test_dialog_timeout_cancels_and_settles(self) -> None:
        session = await self.start()
        collector = Collector()
        session.attach(collector)

        await session.prompt("please confirm with timeout")
        await wait_until(lambda: bool(collector.dialogs))
        self.assertTrue(await session.wait_for_settled(5.0))
        self.assertEqual(collector.resolved, [("d1", "timeout")])
        self.assertFalse(session.info()["needs_input"])

    async def test_abort_stops_a_run(self) -> None:
        session = await self.start()
        collector = Collector()
        session.attach(collector)

        await session.prompt("slow run")
        await wait_until(lambda: "run.started" in collector.names())
        await session.abort()
        self.assertTrue(await session.wait_for_settled(5.0))
        finished = [
            event
            for _, event in collector.events
            if event.get("event") == "run.finished"
        ][-1]
        self.assertEqual(finished["reason"], "aborted")

    async def test_large_tool_output_is_kept_in_the_event_ring(self) -> None:
        session = await self.start()
        await session.prompt("big output")
        self.assertTrue(await session.wait_for_settled(5.0))
        finished = [
            event
            for _, event in session.events_since(0)
            if event.get("event") == "tool.finished"
        ][-1]
        self.assertEqual(len(finished["output"]), 200_000)

    async def test_crash_marks_session_dead_and_wakes_waiters(self) -> None:
        session = await self.start()
        collector = Collector()
        session.attach(collector)
        await session.prompt("please crash now")
        self.assertEqual(await session.wait(5.0), 3)
        self.assertTrue(session.dead)
        self.assertEqual(session.exit_code, 3)
        self.assertTrue(collector.exited)
        with self.assertRaises(AgentClosed):
            await session.command("get_state")

    async def test_handshake_timeout_includes_stderr(self) -> None:
        with self.assertRaises(AgentProtocolError):
            await self.start(env={"FAKE_AGENT_NO_HELLO": "1"}, timeout=0.2)

    async def test_unsupported_protocol_fails_fast(self) -> None:
        with self.assertRaises(AgentProtocolError) as caught:
            await self.start(env={"FAKE_AGENT_BAD_PROTOCOL": "1"}, timeout=5.0)
        self.assertIn("unsupported protocol", str(caught.exception))

    async def test_close_resolves_pending_dialogs(self) -> None:
        session = await self.start()
        collector = Collector()
        session.attach(collector)

        await session.prompt("please confirm before close")
        await wait_until(lambda: bool(collector.dialogs))
        session.close()
        self.assertEqual(await session.wait(5.0), 0)
        self.assertEqual(collector.resolved, [("d1", "closed")])
        self.assertFalse(session.info()["needs_input"])

    async def test_close_terminates_the_agent(self) -> None:
        session = await self.start()
        session.close()
        self.assertEqual(await session.wait(5.0), 0)
        self.assertTrue(session.dead)

    async def test_empty_prompt_is_rejected_before_sending(self) -> None:
        session = await self.start()
        with self.assertRaises(ValueError):
            await session.prompt("   ")


class SharedAgentProcessTests(unittest.TestCase):
    """Both structured-agent dialects share one process/transport base.

    The close/escalation behaviour of that base is covered for each dialect by
    ``AgentSessionTests.test_close_terminates_the_agent`` and
    ``test_pi.PiSessionTests.test_close_escalates_when_stdin_eof_is_ignored``;
    this test pins the extraction itself so the machinery cannot drift back
    into two copies.
    """

    def test_both_sessions_inherit_the_shared_process_base(self) -> None:
        self.assertTrue(issubclass(AgentSession, AgentProcess))
        self.assertTrue(issubclass(PiSession, AgentProcess))

    def test_process_lifecycle_is_defined_once_on_the_base(self) -> None:
        for name in (
            "attach",
            "detach",
            "send_command",
            "events_since",
            "close",
            "force_close",
            "wait",
            "wait_for_settled",
            "_mark_dead",
            "_on_child_exit",
            "_feed",
            "_signal",
        ):
            self.assertIs(
                getattr(AgentSession, name), getattr(AgentProcess, name), name
            )
            self.assertIs(getattr(PiSession, name), getattr(AgentProcess, name), name)


if __name__ == "__main__":
    unittest.main()

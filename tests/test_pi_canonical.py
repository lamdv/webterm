"""Tests for translating pi RPC records into the canonical agent protocol."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from webterm.integration.pi.canonical import (
    PiCanonicalTranslator,
    pi_dialog_response_to_pi,
    pi_dialog_to_canonical,
    pi_history_to_canonical,
    pi_models_to_canonical,
    pi_snapshot_to_canonical,
    pi_ui_request_to_events,
    pi_ui_resolved_to_canonical,
)
from webterm.server import start_server
from webterm.session import SessionManager

from .test_agent_session import wait_until
from .wsclient import WSClient

FAKE_PI = Path(__file__).with_name("fake_pi.py")


def stream_records() -> list[dict]:
    """A realistic pi stream with thinking, text, a tool call, and usage."""
    return [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0},
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "thinking_delta",
                "contentIndex": 0,
                "delta": "plan",
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "thinking_end",
                "contentIndex": 0,
                "content": "plan",
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_start", "contentIndex": 1},
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 1,
                "delta": "hello",
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_end",
                "contentIndex": 1,
                "content": "hello",
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "toolcall_start",
                "contentIndex": 2,
                "id": "call-1",
                "toolName": "bash",
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "toolcall_delta",
                "contentIndex": 2,
                "delta": '{"command":"ls"}',
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "toolcall_end",
                "contentIndex": 2,
                "toolCall": {
                    "id": "call-1",
                    "name": "bash",
                    "arguments": {"command": "ls"},
                },
            },
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": "bash",
            "args": {"command": "ls"},
        },
        {
            "type": "tool_execution_update",
            "toolCallId": "call-1",
            "toolName": "bash",
            "args": {"command": "ls"},
            "partialResult": {"content": [{"type": "text", "text": "a\n"}]},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "a\nb\n"}]},
            "isError": False,
            "durationMs": 12,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet",
                "content": [
                    {"type": "text", "text": "hello"},
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "bash",
                        "arguments": {"command": "ls"},
                    },
                ],
                "usage": {
                    "input": 10,
                    "output": 5,
                    "totalTokens": 15,
                    "cost": {"total": 0.002},
                },
                "stopReason": "toolUse",
            },
        },
        {"type": "turn_end"},
        {"type": "agent_end", "willRetry": False},
        {"type": "agent_settled", "aborted": False},
    ]


class PiTranslatorTests(unittest.TestCase):
    def test_stream_maps_to_canonical_run_message_and_tool_events(self) -> None:
        translator = PiCanonicalTranslator()
        events = [
            event
            for record in stream_records()
            for event in translator.feed(record)
        ]
        names = [event["event"] for event in events]

        self.assertEqual(names[0], "run.started")
        self.assertIn("message.started", names)
        self.assertIn("message.block", names)
        self.assertIn("message.delta", names)
        self.assertIn("message.finished", names)
        self.assertIn("tool.started", names)
        self.assertIn("tool.updated", names)
        self.assertIn("tool.finished", names)
        self.assertIn("usage", names)
        self.assertEqual(names[-2:], ["run.finished", "run.settled"])

        run_ids = {
            event["run_id"]
            for event in events
            if event["event"].startswith("run.")
        }
        self.assertEqual(run_ids, {"run-1"})

        message_ids = {
            event["message_id"]
            for event in events
            if event["event"].startswith("message.") and "message_id" in event
        }
        self.assertEqual(message_ids, {"live-1"})

        blocks = [
            event for event in events if event["event"] == "message.block"
        ]
        self.assertEqual(
            [event["block_index"] for event in blocks],
            [0, 0, 1, 1, 2, 2],
        )
        self.assertEqual(
            [event["block"]["type"] for event in blocks],
            ["thinking", "thinking", "text", "text", "tool_call", "tool_call"],
        )
        tool_end = blocks[-1]["block"]
        self.assertEqual(tool_end["call_id"], "call-1")
        self.assertEqual(tool_end["name"], "bash")
        self.assertEqual(tool_end["args"], {"command": "ls"})

        tool_update = next(event for event in events if event["event"] == "tool.updated")
        self.assertEqual(tool_update["delta"], "a\n")
        tool_finish = next(event for event in events if event["event"] == "tool.finished")
        self.assertEqual(tool_finish["output"], "a\nb\n")
        self.assertFalse(tool_finish["is_error"])

        usage = next(event for event in events if event["event"] == "usage")
        self.assertEqual(usage["prompt_tokens"], 10)
        self.assertEqual(usage["completion_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 15)
        self.assertEqual(usage["cost"], 0.002)

        finished = next(
            event for event in events if event["event"] == "message.finished"
        )["message"]
        self.assertEqual(finished["role"], "assistant")
        self.assertEqual(finished["model"], "claude-sonnet")
        self.assertEqual(finished["stop_reason"], "toolUse")
        self.assertEqual(finished["content"][0], {"type": "text", "text": "hello"})
        self.assertEqual(finished["content"][1]["type"], "tool_call")

    def test_missing_content_index_still_separates_block_types(self) -> None:
        translator = PiCanonicalTranslator()
        records = [
            {"type": "agent_start"},
            {"type": "message_start", "message": {"role": "assistant", "content": []}},
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_start"},
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta", "delta": "hm"},
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_end", "content": "hm"},
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "hi"},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                },
            },
            {"type": "agent_settled"},
        ]
        events = [
            event for record in records for event in translator.feed(record)
        ]
        blocks = [
            event for event in events if event["event"] == "message.block"
        ]
        self.assertEqual([block["block_index"] for block in blocks], [0, 0])
        deltas = [
            event for event in events if event["event"] == "message.delta"
        ]
        self.assertEqual([delta["block_index"] for delta in deltas], [0, 1])

    def test_user_and_tool_result_messages_have_stable_ids(self) -> None:
        translator = PiCanonicalTranslator()
        records = [
            {"type": "message_start", "message": {"role": "user", "content": "hi"}},
            {"type": "message_end", "message": {"role": "user", "content": "hi"}},
            {
                "type": "message_start",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "out"}],
                    "isError": False,
                },
            },
            {
                "type": "message_end",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "out"}],
                    "isError": False,
                },
            },
        ]
        events = [event for record in records for event in translator.feed(record)]
        started = [
            event for event in events if event["event"] == "message.started"
        ]
        finished = [
            event for event in events if event["event"] == "message.finished"
        ]
        self.assertEqual(
            [event["message"]["id"] for event in started],
            [event["message"]["id"] for event in finished],
        )
        self.assertEqual(finished[-1]["message"]["role"], "tool")
        self.assertEqual(finished[-1]["message"]["tool_call_id"], "call-1")
        self.assertEqual(finished[-1]["message"]["tool_name"], "bash")

    def test_retry_runs_get_distinct_ids_and_settle_the_last_one(self) -> None:
        translator = PiCanonicalTranslator()
        records = [
            {"type": "agent_start"},
            {"type": "agent_end", "willRetry": True},
            {"type": "agent_start"},
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled", "aborted": False},
        ]
        events = [event for record in records for event in translator.feed(record)]
        started = [event["run_id"] for event in events if event["event"] == "run.started"]
        self.assertEqual(started, ["run-1", "run-2"])
        settled = next(event for event in events if event["event"] == "run.settled")
        self.assertEqual(settled["run_id"], "run-2")
        finished = [event["run_id"] for event in events if event["event"] == "run.finished"]
        self.assertEqual(finished, ["run-1", "run-2"])

    def test_unmapped_records_become_raw_events(self) -> None:
        translator = PiCanonicalTranslator()
        events = translator.feed({"type": "something_new", "value": 1})
        self.assertEqual(events[0]["event"], "raw")
        self.assertEqual(events[0]["protocol"], "pi")
        self.assertEqual(events[0]["record"]["type"], "something_new")

    def test_retry_and_compaction_become_notices(self) -> None:
        translator = PiCanonicalTranslator()
        events = translator.feed(
            {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3}
        )
        self.assertEqual(events[0]["event"], "notice")
        self.assertEqual(events[0]["code"], "pi.retry")
        events = translator.feed(
            {"type": "compaction_end", "result": {"tokensBefore": 1000}}
        )
        self.assertEqual(events[0]["event"], "notice")
        self.assertIn("1000", events[0]["text"])


class PiSnapshotTests(unittest.TestCase):
    def snapshot(self) -> dict:
        return {
            "state": {
                "model": {"id": "claude-sonnet", "provider": "anthropic"},
                "thinkingLevel": "medium",
                "sessionFile": "/tmp/session.jsonl",
                "sessionName": "feature",
                "pendingMessageCount": 2,
                "isStreaming": False,
            },
            "entries": {
                "entries": [
                    {
                        "type": "message",
                        "id": "entry-0",
                        "timestamp": "2024-01-01T00:00:00Z",
                        "message": {"role": "system", "content": "prompt"},
                    },
                    {
                        "type": "message",
                        "id": "entry-1",
                        "timestamp": "2024-01-01T00:00:01Z",
                        "message": {"role": "user", "content": "hi"},
                    },
                    {
                        "type": "model_change",
                        "id": "entry-x",
                        "provider": "anthropic",
                        "modelId": "claude-sonnet",
                    },
                    {
                        "type": "message",
                        "id": "entry-2",
                        "timestamp": "2024-01-01T00:00:02Z",
                        "message": {
                            "role": "assistant",
                            "model": "claude-sonnet",
                            "content": [
                                {"type": "text", "text": "hello"},
                                {
                                    "type": "toolCall",
                                    "id": "call-1",
                                    "name": "bash",
                                    "arguments": {"command": "ls"},
                                },
                            ],
                            "usage": {
                                "input": 3,
                                "output": 4,
                                "totalTokens": 7,
                                "cost": {"total": 0.01},
                            },
                            "stopReason": "toolUse",
                        },
                    },
                    {
                        "type": "message",
                        "id": "entry-3",
                        "timestamp": "2024-01-01T00:00:03Z",
                        "message": {
                            "role": "toolResult",
                            "toolCallId": "call-1",
                            "toolName": "bash",
                            "content": [{"type": "text", "text": "out"}],
                            "isError": False,
                        },
                    },
                    {
                        "type": "message",
                        "id": "entry-4",
                        "timestamp": "2024-01-01T00:00:04Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "data": "abc",
                                    "mimeType": "image/png",
                                }
                            ],
                        },
                    },
                ],
                "hasMore": True,
                "firstEntryId": "entry-1",
            },
            "ui": {
                "requests": [
                    {
                        "type": "extension_ui_request",
                        "id": "ui-1",
                        "method": "confirm",
                        "title": "Proceed?",
                        "message": "sure?",
                        "timeout": 5000,
                    },
                    {
                        "type": "extension_ui_request",
                        "id": "ui-2",
                        "method": "notify",
                        "message": "hi",
                    },
                ],
                "status": {"fake": "working"},
                "title": "pi",
            },
            "commands": {"commands": [{"name": "review"}]},
            "streaming": False,
            "settled": True,
            "seq": 42,
        }

    def test_snapshot_maps_entries_state_dialogs_and_cursor(self) -> None:
        canonical = pi_snapshot_to_canonical(self.snapshot(), tokens=7, cost=0.01)
        self.assertEqual(
            [entry["id"] for entry in canonical["entries"]],
            ["entry-1", "entry-2", "entry-3", "entry-4"],
        )
        self.assertEqual(canonical["entries"][0]["content"], [{"type": "text", "text": "hi"}])
        self.assertEqual(
            canonical["entries"][1]["content"][1],
            {
                "type": "tool_call",
                "call_id": "call-1",
                "name": "bash",
                "args": {"command": "ls"},
            },
        )
        self.assertEqual(canonical["entries"][2]["role"], "tool")
        self.assertEqual(canonical["entries"][2]["tool_call_id"], "call-1")
        self.assertEqual(canonical["entries"][3]["content"][0]["type"], "image")
        self.assertEqual(canonical["entries"][3]["content"][0]["mime_type"], "image/png")
        self.assertTrue(canonical["has_more"])
        self.assertEqual(canonical["first_entry_id"], "entry-1")
        self.assertEqual(canonical["cursor"], {"seq": 42})
        self.assertEqual(canonical["state"]["model"], "claude-sonnet")
        self.assertEqual(canonical["state"]["thinking_level"], "medium")
        self.assertEqual(canonical["state"]["tokens"], 7)
        self.assertEqual(canonical["state"]["cost"], 0.01)
        self.assertEqual(canonical["dialogs"][0]["kind"], "confirm")
        self.assertEqual(canonical["dialogs"][0]["timeout_ms"], 5000)
        self.assertEqual(len(canonical["dialogs"]), 1)
        self.assertEqual(
            canonical["capabilities"]["dialogs"],
            ["confirm", "select", "input", "editor"],
        )
        self.assertEqual(canonical["ui"]["status"], {"fake": "working"})

    def test_history_page_maps_entries(self) -> None:
        page = {
            "entries": self.snapshot()["entries"]["entries"],
            "hasMore": False,
            "firstEntryId": "entry-1",
        }
        canonical = pi_history_to_canonical(page)
        self.assertEqual(
            [entry["id"] for entry in canonical["entries"]],
            ["entry-1", "entry-2", "entry-3", "entry-4"],
        )
        self.assertFalse(canonical["has_more"])
        self.assertEqual(canonical["first_entry_id"], "entry-1")

    def test_models_map_to_canonical_fields(self) -> None:
        canonical = pi_models_to_canonical(
            {
                "models": [
                    {
                        "id": "m1",
                        "provider": "p",
                        "name": "Model One",
                        "contextWindow": 1000,
                        "maxTokens": 100,
                        "reasoning": True,
                        "input": ["text"],
                    },
                    {"id": "bad"},
                ],
                "default": {"id": "m1", "provider": "p"},
                "fetchedAt": 123.0,
                "cwd": "/tmp",
                "cached": False,
            }
        )
        self.assertEqual(len(canonical["models"]), 1)
        self.assertEqual(canonical["models"][0]["context_window"], 1000)
        self.assertEqual(canonical["models"][0]["max_tokens"], 100)
        self.assertEqual(canonical["default"], {"id": "m1", "provider": "p"})
        self.assertEqual(canonical["fetched_at"], 123.0)


class PiDialogTests(unittest.TestCase):
    def test_blocking_dialog_maps_to_canonical_request(self) -> None:
        dialog = pi_dialog_to_canonical(
            {
                "type": "extension_ui_request",
                "id": "ui-1",
                "method": "confirm",
                "title": "Proceed?",
                "message": "sure?",
                "timeout": 5000,
            }
        )
        self.assertEqual(
            dialog,
            {
                "type": "dialog_request",
                "id": "ui-1",
                "kind": "confirm",
                "title": "Proceed?",
                "message": "sure?",
                "timeout_ms": 5000,
            },
        )
        self.assertIsNone(
            pi_dialog_to_canonical(
                {"type": "extension_ui_request", "id": "n", "method": "notify"}
            )
        )

    def test_notify_and_title_map_to_events(self) -> None:
        events = pi_ui_request_to_events(
            {
                "type": "extension_ui_request",
                "id": "n",
                "method": "notify",
                "message": "careful",
                "notifyType": "warning",
            }
        )
        self.assertEqual(events[0]["event"], "notice")
        self.assertEqual(events[0]["level"], "warning")
        events = pi_ui_request_to_events(
            {
                "type": "extension_ui_request",
                "id": "t",
                "method": "setTitle",
                "title": "new title",
            }
        )
        self.assertEqual(events[0], {"event": "session.info", "title": "new title"})

    def test_responses_map_to_pi_shape(self) -> None:
        self.assertEqual(
            pi_dialog_response_to_pi("confirm", {"approved": True}),
            {"confirmed": True},
        )
        self.assertEqual(
            pi_dialog_response_to_pi("confirm", {"cancelled": True}),
            {"cancelled": True},
        )
        self.assertEqual(
            pi_dialog_response_to_pi("select", {"value": "Allow"}),
            {"value": "Allow"},
        )
        self.assertEqual(
            pi_ui_resolved_to_canonical({"id": "ui-1", "reason": "timeout"}),
            {"type": "dialog_resolved", "id": "ui-1", "reason": "timeout"},
        )


class CanonicalCollector:
    """PiSubscriber that projects pi records through the translator."""

    def __init__(self) -> None:
        self.translator = PiCanonicalTranslator()
        self.events: list[dict] = []
        self.dialogs: list[dict] = []
        self.resolved: list[dict] = []
        self.exited = False

    def offer_pi_event(self, session, seq: int, record: dict) -> None:
        self.events.extend(self.translator.feed(record))

    def offer_pi_ui_request(self, session, request: dict) -> None:
        dialog = pi_dialog_to_canonical(request)
        if dialog is not None:
            self.dialogs.append(dialog)
        self.events.extend(pi_ui_request_to_events(request))

    def offer_pi_ui_resolved(self, session, request_id: str, reason: str) -> None:
        resolved = pi_ui_resolved_to_canonical({"id": request_id, "reason": reason})
        if resolved is not None:
            self.resolved.append(resolved)

    def offer_exit(self, session) -> None:
        self.exited = True


class PiCanonicalIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cwd = tempfile.mkdtemp(prefix="webterm-pi-canonical-")
        self.manager = SessionManager(shell="/bin/sh", cwd=self.cwd, retained_ttl=None)

    async def asyncTearDown(self) -> None:
        self.manager.close_all()
        shutil.rmtree(self.cwd, ignore_errors=True)

    async def start(self):
        return self.manager.create_pi([sys.executable, str(FAKE_PI)], cwd=self.cwd)

    async def test_fake_pi_run_projects_to_canonical_events(self) -> None:
        session = await self.start()
        collector = CanonicalCollector()
        session.attach(collector)

        await session.rpc({"type": "prompt", "message": "hello pi"})
        self.assertTrue(await session.wait_for_settled(5.0))

        names = [event["event"] for event in collector.events]
        self.assertIn("run.started", names)
        self.assertIn("message.finished", names)
        self.assertIn("tool.started", names)
        self.assertIn("tool.finished", names)
        self.assertIn("usage", names)
        self.assertIn("run.settled", names)

        snapshot = await session.snapshot()
        canonical = pi_snapshot_to_canonical(
            snapshot, tokens=session.tokens, cost=session.cost
        )
        self.assertEqual(canonical["cursor"], {"seq": snapshot["seq"]})
        self.assertEqual([entry["id"] for entry in canonical["entries"]], ["entry-1", "entry-2"])
        self.assertEqual(canonical["state"]["model"], "fake-model")
        self.assertEqual(canonical["state"]["tokens"], 8)
        self.assertAlmostEqual(canonical["state"]["cost"], 0.001)
        self.assertTrue(canonical["capabilities"]["resume"])

    async def test_fake_pi_dialog_and_notify_projects(self) -> None:
        session = await self.start()
        collector = CanonicalCollector()
        session.attach(collector)

        await session.rpc({"type": "prompt", "message": "please ask"})
        await wait_until(lambda: bool(collector.dialogs))
        dialog = collector.dialogs[0]
        self.assertEqual(dialog["kind"], "confirm")
        self.assertEqual(dialog["id"], "ui-1")
        session.resolve_ui("ui-1", {"confirmed": True})
        self.assertTrue(await session.wait_for_settled(5.0))
        self.assertEqual(collector.resolved[0]["reason"], "answered")

        session2 = await self.start()
        collector2 = CanonicalCollector()
        session2.attach(collector2)
        await session2.rpc({"type": "prompt", "message": "notify me"})
        self.assertTrue(await session2.wait_for_settled(5.0))
        names = [event["event"] for event in collector2.events]
        self.assertIn("notice", names)
        notices = [event for event in collector2.events if event["event"] == "notice"]
        self.assertEqual(notices[0]["level"], "warning")
        self.assertEqual(notices[0]["text"], "fake notification")
        titles = [
            event
            for event in collector2.events
            if event["event"] == "session.info" and event.get("title")
        ]
        self.assertEqual(titles[0]["title"], "fake title")
        self.assertTrue(any(event["event"] == "raw" for event in collector2.events))


class PiShadowServerTests(unittest.IsolatedAsyncioTestCase):
    """The canonical stream is additive: pi_* messages remain unchanged."""

    async def asyncSetUp(self) -> None:
        self.cwd = tempfile.mkdtemp(prefix="webterm-pi-shadow-")
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

    async def _create_pi(self) -> dict:
        await self.client.send_json(
            {
                "type": "create",
                "id": "create-pi",
                "mode": "pi",
                "cwd": self.cwd,
                "title": "shadow chat",
                "trust": "allow",
            }
        )
        result = await self.client.recv_json_until(
            lambda message: message.get("type") == "result"
            and message.get("id") == "create-pi"
        )
        return result["result"]["session"]

    async def _subscribe(self, session_id: str, *, agent_api: bool = False) -> dict:
        message = {"type": "subscribe", "session_id": session_id}
        if agent_api:
            message["agent_api"] = True
        await self.client.send_json(message)
        await self.client.recv_json_until(
            lambda reply: reply.get("type") == "attached"
            and reply["session"]["id"] == session_id
        )
        resync = await self.client.recv_json_until(
            lambda reply: reply.get("type") == "pi_resync"
            and reply.get("session_id") == session_id
        )
        if agent_api:
            canonical = await self.client.recv_json_until(
                lambda reply: reply.get("type") == "agent_resync"
                and reply.get("session_id") == session_id
            )
            return {"pi": resync, "agent": canonical}
        return resync

    async def _collect_until(self, predicate, timeout: float = 10.0) -> list[dict]:
        messages: list[dict] = []

        async def _loop() -> list[dict]:
            while True:
                message = await self.client.recv_json()
                messages.append(message)
                if predicate(message):
                    return messages

        return await asyncio.wait_for(_loop(), timeout)

    async def test_config_advertises_the_canonical_stream(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.handle.port)
        writer.write(
            b"GET /config.js HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()
        self.assertIn(b'"agentApi": true', raw)

    async def test_agent_resync_and_events_keep_pi_stream(self) -> None:
        session = await self._create_pi()
        sync = await self._subscribe(session["id"], agent_api=True)
        canonical = sync["agent"]
        self.assertEqual(canonical["session_id"], session["id"])
        self.assertIn("cursor", canonical)
        self.assertIn("capabilities", canonical)
        self.assertEqual(
            canonical["capabilities"]["dialogs"],
            ["confirm", "select", "input", "editor"],
        )
        self.assertEqual([entry["id"] for entry in canonical["entries"]], ["entry-1", "entry-2"])

        await self.client.send_json(
            {
                "type": "pi",
                "id": "prompt-1",
                "session_id": session["id"],
                "command": {"type": "prompt", "message": "hello shadow"},
            }
        )
        messages = await self._collect_until(
            lambda message: message.get("type") == "agent_event"
            and message["event"].get("event") == "run.settled"
        )
        self.assertTrue(any(message.get("type") == "pi_event" for message in messages))
        canonical_events = [
            message["event"]
            for message in messages
            if message.get("type") == "agent_event"
        ]
        names = [event["event"] for event in canonical_events]
        self.assertIn("run.started", names)
        self.assertIn("message.finished", names)
        self.assertIn("tool.finished", names)
        self.assertIn("usage", names)
        self.assertIn("run.settled", names)

    async def test_agent_dialog_shadow_is_additive(self) -> None:
        session = await self._create_pi()
        await self._subscribe(session["id"], agent_api=True)
        await self.client.send_json(
            {
                "type": "pi",
                "id": "prompt-ask",
                "session_id": session["id"],
                "command": {"type": "prompt", "message": "please ask"},
            }
        )
        messages = await self._collect_until(
            lambda message: message.get("type") == "agent_dialog_request"
            and message.get("session_id") == session["id"]
        )
        self.assertTrue(any(message.get("type") == "pi_ui_request" for message in messages))
        dialog = next(
            message["request"]
            for message in messages
            if message.get("type") == "agent_dialog_request"
        )
        self.assertEqual(dialog["kind"], "confirm")
        self.assertEqual(dialog["id"], "ui-1")

        await self.client.send_json(
            {
                "type": "pi_ui_response",
                "id": "dialog-1",
                "session_id": session["id"],
                "request_id": "ui-1",
                "response": {"confirmed": True},
            }
        )
        resolved = await self._collect_until(
            lambda message: message.get("type") == "agent_dialog_resolved"
            and message.get("session_id") == session["id"]
        )
        self.assertTrue(any(message.get("type") == "pi_ui_resolved" for message in resolved))
        canonical = next(
            message
            for message in resolved
            if message.get("type") == "agent_dialog_resolved"
        )
        self.assertEqual(canonical["dialog_id"], "ui-1")
        self.assertEqual(canonical["reason"], "answered")

    async def test_agent_api_enabled_after_subscribe_sends_resync(self) -> None:
        session = await self._create_pi()
        await self._subscribe(session["id"], agent_api=False)
        await self.client.send_json(
            {"type": "agent_api", "id": "enable-agent-api", "enabled": True}
        )
        messages: list[dict] = []

        async def _collect_both() -> list[dict]:
            while True:
                message = await self.client.recv_json()
                messages.append(message)
                has_sync = any(
                    item.get("type") == "agent_resync"
                    and item.get("session_id") == session["id"]
                    for item in messages
                )
                has_result = any(
                    item.get("type") == "result"
                    and item.get("id") == "enable-agent-api"
                    for item in messages
                )
                if has_sync and has_result:
                    return messages

        await asyncio.wait_for(_collect_both(), 10.0)
        result = next(
            message
            for message in messages
            if message.get("type") == "result"
            and message.get("id") == "enable-agent-api"
        )
        self.assertTrue(result["result"]["enabled"])

        await self.client.send_json(
            {
                "type": "pi",
                "id": "prompt-3",
                "session_id": session["id"],
                "command": {"type": "prompt", "message": "after enable"},
            }
        )
        events = await self._collect_until(
            lambda message: message.get("type") == "agent_event"
            and message["event"].get("event") == "run.settled"
        )
        self.assertTrue(any(message.get("type") == "agent_event" for message in events))

    async def test_default_subscribe_stays_pi_only(self) -> None:
        session = await self._create_pi()
        await self._subscribe(session["id"], agent_api=False)
        await self.client.send_json(
            {
                "type": "pi",
                "id": "prompt-2",
                "session_id": session["id"],
                "command": {"type": "prompt", "message": "hello legacy"},
            }
        )
        messages = await self._collect_until(
            lambda message: message.get("type") == "pi_event"
            and message.get("record", {}).get("type") == "agent_settled"
        )
        self.assertFalse(any(message.get("type") == "agent_event" for message in messages))


if __name__ == "__main__":
    unittest.main()

"""Tests for structured pi sessions and their server transport."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from webterm.integration.pi import PiError
from webterm.server import start_server
from webterm.session import SessionManager

from .wsclient import WSClient

FAKE_PI = Path(__file__).with_name("fake_pi.py")


class Collector:
    """PiSubscriber that records events and exit notifications."""

    def __init__(self) -> None:
        self.events: list[tuple[int, dict]] = []
        self.ui_requests: list[dict] = []
        self.ui_resolved: list[tuple[str, str]] = []
        #: The session's reported activity after each event was applied.
        self.activity: list[str | None] = []
        self.exited = False

    def offer_pi_event(self, session, seq: int, record: dict) -> None:
        self.events.append((seq, record))
        self.activity.append(session.info()["activity"])

    def offer_pi_ui_request(self, session, request: dict) -> None:
        self.ui_requests.append(request)

    def offer_pi_ui_resolved(self, session, request_id: str, reason: str) -> None:
        self.ui_resolved.append((request_id, reason))

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

        # The tab dot follows the activity phase: faint while pi thinks or runs
        # a tool, bright while it streams an answer, nothing once settled.
        transitions = [
            (kind, phase)
            for index, (kind, phase) in enumerate(zip(kinds, collector.activity, strict=True))
            if phase != (collector.activity[index - 1] if index else None)
        ]
        self.assertEqual(
            transitions,
            [
                ("agent_start", "thinking"),
                ("tool_execution_start", "tool"),
                ("tool_execution_end", "thinking"),
                ("message_update", "answering"),
                ("agent_settled", None),
            ],
        )

        seqs = [seq for seq, _ in collector.events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

        info = session.info()
        self.assertFalse(info["streaming"])
        self.assertIsNone(info["activity"])
        self.assertEqual(info["answers"], 1)
        self.assertEqual(info["model"], "fake-model")
        self.assertEqual(info["tokens"], 8)
        self.assertAlmostEqual(info["cost"], 0.001)

        snapshot = await session.snapshot()
        self.assertFalse(snapshot["streaming"])
        self.assertTrue(snapshot["settled"])
        self.assertEqual(snapshot["state"]["model"]["id"], "fake-model")
        self.assertEqual(snapshot["entries"]["leafId"], "entry-2")
        self.assertEqual(snapshot["commands"]["commands"][0]["name"], "review")
        self.assertGreaterEqual(snapshot["seq"], seqs[-1])

        models = await session.rpc({"type": "get_available_models"})
        self.assertTrue(models["success"])
        self.assertEqual(models["data"]["models"][0]["provider"], "fake")
        switched = await session.rpc(
            {"type": "set_model", "provider": "fake", "modelId": "fake-model-2"}
        )
        self.assertTrue(switched["success"])
        self.assertEqual(switched["data"]["id"], "fake-model-2")

        levels = await session.rpc({"type": "get_available_thinking_levels"})
        self.assertTrue(levels["success"])
        self.assertEqual(levels["data"]["levels"][0], "off")
        thought = await session.rpc({"type": "set_thinking_level", "level": "low"})
        self.assertTrue(thought["success"])
        state = await session.rpc({"type": "get_state"})
        self.assertEqual(state["data"]["thinkingLevel"], "low")
        rejected = await session.rpc({"type": "set_thinking_level", "level": "nope"})
        self.assertFalse(rejected["success"])

        session.close()
        self.assertIsNotNone(await session.wait(timeout=5.0))
        self.assertTrue(collector.exited)

    async def test_snapshot_returns_newest_entries_and_history_pages_backwards(self) -> None:
        session = self.start()

        snapshot = await session.snapshot(entry_limit=1)
        self.assertEqual(
            [entry["id"] for entry in snapshot["entries"]["entries"]], ["entry-2"]
        )
        self.assertTrue(snapshot["entries"]["hasMore"])
        self.assertEqual(snapshot["entries"]["firstEntryId"], "entry-2")

        page = await session.history("entry-2", 1)
        self.assertEqual([entry["id"] for entry in page["entries"]], ["entry-1"])
        self.assertFalse(page["hasMore"])
        self.assertEqual(page["firstEntryId"], "entry-1")

        empty = await session.history("entry-1", 1)
        self.assertEqual(empty["entries"], [])
        self.assertFalse(empty["hasMore"])
        self.assertIsNone(empty["firstEntryId"])

        full = await session.snapshot(entry_limit=10)
        self.assertFalse(full["entries"]["hasMore"])
        self.assertEqual(full["entries"]["firstEntryId"], "entry-1")

        with self.assertRaises(PiError):
            await session.history("missing", 1)

        session.close()
        self.assertIsNotNone(await session.wait(timeout=5.0))

    async def test_wait_for_settled_tracks_dialog_blocked_run(self) -> None:
        session = self.start()
        collector = Collector()
        session.attach(collector)
        self.assertTrue(await session.wait_for_settled(timeout=0.1))
        await session.rpc({"type": "prompt", "message": "please ask"})
        await wait_until(lambda: bool(collector.ui_requests))
        self.assertFalse(await session.wait_for_settled(timeout=0.1))
        session.resolve_ui("ui-1", {"confirmed": True})
        self.assertTrue(await session.wait_for_settled(timeout=2.0))

    async def test_extension_dialog_is_queued_and_resolved(self) -> None:
        session = self.start()
        collector = Collector()
        session.attach(collector)

        response = await session.rpc({"type": "prompt", "message": "please ask"})
        self.assertTrue(response["success"])
        await wait_until(lambda: bool(collector.ui_requests))
        request = collector.ui_requests[0]
        self.assertEqual(request["id"], "ui-1")
        self.assertEqual(request["method"], "confirm")
        self.assertTrue(session.info()["needs_input"])

        snapshot = await session.snapshot()
        self.assertEqual(snapshot["ui"]["requests"][0]["id"], "ui-1")
        self.assertTrue(snapshot["ui"]["needsInput"])

        session.resolve_ui("ui-1", {"confirmed": True})
        await wait_until(lambda: bool(collector.ui_resolved))
        self.assertEqual(collector.ui_resolved[0], ("ui-1", "response"))
        self.assertFalse(session.info()["needs_input"])
        await wait_until(
            lambda: any(
                record.get("type") == "extension_ui_response"
                and (record.get("response") or {}).get("confirmed") is True
                for _, record in collector.events
            )
        )

    async def test_agent_dialogs_are_cancelled_without_a_viewer(self) -> None:
        ack_file = Path(self.cwd) / "ui-ack.json"
        session = self.manager.create_pi(
            [sys.executable, str(FAKE_PI)],
            cwd=self.cwd,
            env={"FAKE_PI_UI_ACK_FILE": str(ack_file)},
            origin="agent",
        )
        response = await session.rpc({"type": "prompt", "message": "please ask"})
        self.assertTrue(response["success"])
        await wait_until(ack_file.exists)
        ack = json.loads(ack_file.read_text(encoding="utf-8"))
        self.assertTrue(ack["cancelled"])
        self.assertFalse(session.info()["needs_input"])

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
        self.session_root = Path(tempfile.mkdtemp(prefix="webterm-pi-sessions-"))
        self.manager = SessionManager(
            shell="/bin/sh",
            cwd=self.cwd,
            retained_ttl=None,
            pi_command=[sys.executable, str(FAKE_PI)],
            pi_session_dir=str(self.session_root.resolve()),
        )
        # Production always points this at the bundled extension, so browser
        # sessions (and the pi processes they spawn) inherit the webterm context.
        self.manager.pi_extension = str(Path(self.cwd) / "extension.ts")
        self.handle = await start_server(
            "127.0.0.1",
            0,
            self.manager,
            enable_control=False,
            discovery=False,
            profiles={
                "pi": {
                    "print": [sys.executable, str(FAKE_PI)],
                    "interactive": [sys.executable, str(FAKE_PI)],
                    "rpc": [sys.executable, str(FAKE_PI)],
                }
            },
        )
        self.client = await WSClient.connect("127.0.0.1", self.handle.port)
        await self.client.recv_json()  # initial sessions broadcast

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.handle.stop()
        shutil.rmtree(self.cwd, ignore_errors=True)
        shutil.rmtree(self.session_root, ignore_errors=True)

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

    def _write_session(self, name: str, session_id: str, cwd: str | None = None) -> Path:
        path = self.session_root / "project" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": "2024-01-01T00:00:00.000Z",
            "cwd": cwd or self.cwd,
        }
        path.write_text(json.dumps(header) + "\n", encoding="utf-8")
        return path

    async def _request(self, message: dict) -> dict:
        await self.client.send_json(message)
        result = await self.client.recv_json_until(
            lambda reply: reply.get("type") == "result" and reply.get("id") == message["id"]
        )
        return result["result"]

    async def test_list_pi_sessions_reads_headers_and_ignores_unsafe_files(self) -> None:
        first = self._write_session("first.jsonl", "session-a")
        second = self._write_session("second.jsonl", "session-b")
        (self.session_root / "notes.txt").write_text("not jsonl", encoding="utf-8")
        outside = Path(self.cwd) / "outside.jsonl"
        outside.write_text(
            json.dumps(
                {
                    "type": "session",
                    "id": "outside",
                    "cwd": self.cwd,
                    "timestamp": "2024-01-01T00:00:00.000Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (self.session_root / "project" / "link.jsonl").symlink_to(outside)

        listing = await self._request({"type": "list_pi_sessions", "id": "list-pi"})
        self.assertEqual(listing["root"], str(self.session_root.resolve()))
        ids = {entry["id"] for entry in listing["sessions"]}
        self.assertIn("session-a", ids)
        self.assertIn("session-b", ids)
        self.assertNotIn("outside", ids)
        paths = {entry["path"] for entry in listing["sessions"]}
        self.assertIn(str(first), paths)
        self.assertIn(str(second), paths)

    async def test_create_resume_passes_the_session_file(self) -> None:
        session_path = self._write_session("resume.jsonl", "session-resume")
        argv_file = Path(self.cwd) / "argv.json"
        result = await self._request(
            {
                "type": "create",
                "id": "create-resume",
                "mode": "pi",
                "cwd": self.cwd,
                "trust": "deny",
                "resume": str(session_path),
                "env": {"FAKE_PI_ARGV_FILE": str(argv_file)},
            }
        )
        session = result["session"]
        self.assertEqual(session["title"], "resume")
        await wait_until(argv_file.exists)
        argv = json.loads(argv_file.read_text(encoding="utf-8"))
        self.assertIn("--session", argv)
        self.assertEqual(argv[argv.index("--session") + 1], str(session_path))
        self.assertIn("--session-dir", argv)
        self.assertEqual(
            argv[argv.index("--session-dir") + 1], str(self.session_root.resolve())
        )
        self.assertIn("--no-approve", argv)
        self.assertIn("-e", argv)
        self.assertEqual(argv[argv.index("-e") + 1], self.manager.pi_extension)

    async def test_resume_refuses_a_file_that_is_already_open(self) -> None:
        """One server, one tab per session file: pi itself does not lock files,
        so two live sessions over one JSONL would fork the transcript in two."""
        session_path = self._write_session("open.jsonl", "session-open")
        request = {
            "type": "create",
            "mode": "pi",
            "cwd": self.cwd,
            "trust": "deny",
            "resume": str(session_path),
        }
        first = (await self._request({**request, "id": "resume-1"}))["session"]

        await self.client.send_json({**request, "id": "resume-2"})
        error = await self.client.recv_json_until(
            lambda message: message.get("type") == "error"
            and message.get("id") == "resume-2"
        )
        self.assertEqual(error["error"]["code"], "conflict")
        self.assertIn(first["id"], error["error"]["message"])
        self.assertEqual(len(self.manager.sessions), 1)

        await self._request(
            {"type": "kill", "id": "kill-open", "session_id": first["id"]}
        )
        second = (await self._request({**request, "id": "resume-3"}))["session"]
        self.assertNotEqual(second["id"], first["id"])

    async def test_resume_refuses_the_file_of_a_live_session(self) -> None:
        """A fresh session learns its file from ``get_state``; resuming that
        file while the session is live is refused the same way."""
        session_path = self._write_session("live.jsonl", "session-live")
        created = await self._request(
            {
                "type": "create",
                "id": "live-1",
                "mode": "pi",
                "cwd": self.cwd,
                "trust": "deny",
                "env": {"FAKE_PI_SESSION_FILE": str(session_path)},
            }
        )
        session_id = created["session"]["id"]
        await self._request(
            {"type": "pi_snapshot", "id": "live-snap", "session_id": session_id}
        )
        self.assertEqual(
            self.manager.sessions[session_id].session_file, str(session_path)
        )

        await self.client.send_json(
            {
                "type": "create",
                "id": "live-2",
                "mode": "pi",
                "cwd": self.cwd,
                "trust": "deny",
                "resume": str(session_path),
            }
        )
        error = await self.client.recv_json_until(
            lambda message: message.get("type") == "error"
            and message.get("id") == "live-2"
        )
        self.assertEqual(error["error"]["code"], "conflict")
        self.assertIn(session_id, error["error"]["message"])

    async def test_create_rejects_resume_outside_the_managed_root(self) -> None:
        outside = Path(self.cwd) / "outside.jsonl"
        outside.write_text('{"type":"session"}\n', encoding="utf-8")
        await self.client.send_json(
            {
                "type": "create",
                "id": "bad-resume",
                "mode": "pi",
                "cwd": self.cwd,
                "trust": "deny",
                "resume": str(outside),
            }
        )
        result = await self.client.recv_json_until(
            lambda message: message.get("type") == "error"
            and message.get("id") == "bad-resume"
        )
        self.assertEqual(result["error"]["code"], "bad_request")

    async def test_export_html_returns_a_download_url(self) -> None:
        session = await self._create_pi()
        result = await self._request(
            {
                "type": "pi_export",
                "id": "export-html",
                "session_id": session["id"],
                "format": "html",
            }
        )
        self.assertEqual(result["format"], "html")
        self.assertTrue(result["url"].startswith("/pi/download?path="))
        self.assertTrue(result["path"].startswith(str(self.session_root.resolve())))
        self.assertTrue(Path(result["path"]).is_file())

        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.handle.port
        )
        writer.write(
            f"GET {result['url']} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        raw = await reader.read()
        writer.close()
        status, _, body = raw.partition(b"\r\n\r\n")
        self.assertIn(b"200", status.split(b"\r\n")[0])
        self.assertIn(b"fake pi export", body)
        self.assertIn(b"Content-Disposition: attachment", raw)

    async def test_export_jsonl_returns_the_session_file(self) -> None:
        session_path = self._write_session("live.jsonl", "session-live")
        result = await self._request(
            {
                "type": "create",
                "id": "create-live",
                "mode": "pi",
                "cwd": self.cwd,
                "trust": "deny",
                "env": {"FAKE_PI_SESSION_FILE": str(session_path)},
            }
        )
        exported = await self._request(
            {
                "type": "pi_export",
                "id": "export-jsonl",
                "session_id": result["session"]["id"],
                "format": "jsonl",
            }
        )
        self.assertEqual(exported["format"], "jsonl")
        self.assertEqual(exported["path"], str(session_path))
        self.assertIn("/pi/download?path=", exported["url"])

    async def test_download_rejects_paths_outside_the_root(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.handle.port)
        outside = quote(str(Path(self.cwd) / "outside.jsonl"), safe="")
        writer.write(
            f"GET /pi/download?path={outside} HTTP/1.1\r\n"
            "Host: x\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        raw = await reader.read()
        writer.close()
        self.assertIn(b"404", raw.split(b"\r\n", 1)[0])

    async def test_spawn_rpc_agent_waits_for_settled_result(self) -> None:
        await self.client.send_json(
            {
                "type": "spawn",
                "id": "spawn-rpc",
                "agent": "pi",
                "rpc": True,
                "task": "do the thing",
                "wait": True,
                "timeout": 5,
            }
        )
        result = await self.client.recv_json_until(
            lambda message: message.get("type") == "result"
            and message.get("id") == "spawn-rpc"
        )
        data = result["result"]
        self.assertTrue(data["settled"])
        self.assertFalse(data["timed_out"])
        self.assertEqual(data["text"], "fake reply")
        self.assertEqual(data["state"]["model"]["id"], "fake-model")
        session = self.manager.get(data["session_id"])
        self.assertEqual(session.kind, "agent")
        self.assertEqual(session.adapter, "pi")
        self.assertEqual(session.role, "agent")
        self.assertEqual(session.task, "do the thing")

    async def test_extension_dialog_round_trip(self) -> None:
        session = await self._create_pi()
        session_id = session["id"]
        await self._subscribe_pi(session_id)

        await self.client.send_json(
            {
                "type": "pi",
                "id": "prompt-ask",
                "session_id": session_id,
                "command": {"type": "prompt", "message": "please ask"},
            }
        )
        await self.client.recv_json_until(
            lambda message: message.get("type") == "result"
            and message.get("id") == "prompt-ask"
        )
        ui_request = await self.client.recv_json_until(
            lambda message: message.get("type") == "pi_ui_request"
            and message.get("session_id") == session_id
        )
        self.assertEqual(ui_request["request"]["id"], "ui-1")
        self.assertEqual(ui_request["request"]["method"], "confirm")

        leaked = await self.client.recv_json_until(
            lambda message: message.get("type") == "sessions"
            and any(
                item["id"] == session_id and item["needs_input"]
                for item in message["sessions"]
            ),
            timeout=5.0,
        )
        self.assertTrue(leaked)

        await self.client.send_json(
            {
                "type": "pi_ui_response",
                "id": "ui-answer",
                "session_id": session_id,
                "request_id": "ui-1",
                "response": {"confirmed": True},
            }
        )
        result = await self.client.recv_json_until(
            lambda message: message.get("type") == "result"
            and message.get("id") == "ui-answer"
        )
        self.assertTrue(result["result"]["ok"])
        resolved = await self.client.recv_json_until(
            lambda message: message.get("type") == "pi_ui_resolved"
            and message.get("session_id") == session_id
        )
        self.assertEqual(resolved["request_id"], "ui-1")
        self.assertEqual(resolved["reason"], "response")
        ack = await self.client.recv_json_until(
            lambda message: message.get("type") == "pi_event"
            and message.get("session_id") == session_id
            and message["record"].get("type") == "extension_ui_response"
        )
        self.assertTrue(ack["record"]["response"]["confirmed"])

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
        self.assertEqual(resync["entries"]["leafId"], "entry-2")
        self.assertFalse(resync["entries"]["hasMore"])
        self.assertEqual(resync["entries"]["firstEntryId"], "entry-1")

    async def test_pi_history_returns_older_entries(self) -> None:
        session = await self._create_pi()
        session_id = session["id"]
        await self._subscribe_pi(session_id)

        await self.client.send_json(
            {
                "type": "pi_history",
                "id": "history-pi",
                "session_id": session_id,
                "before": "entry-2",
                "limit": 1,
            }
        )
        result = await self.client.recv_json_until(
            lambda message: message.get("type") == "result"
            and message.get("id") == "history-pi"
        )
        page = result["result"]
        self.assertEqual([entry["id"] for entry in page["entries"]], ["entry-1"])
        self.assertFalse(page["hasMore"])
        self.assertEqual(page["firstEntryId"], "entry-1")

        await self.client.send_json(
            {
                "type": "pi_history",
                "id": "history-bad",
                "session_id": session_id,
                "before": "missing",
            }
        )
        error = await self.client.recv_json_until(
            lambda message: message.get("type") == "error"
            and message.get("id") == "history-bad"
        )
        self.assertEqual(error["error"]["code"], "bad_request")

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
        # pi is one adapter behind the canonical agent surface.
        self.assertEqual(session["kind"], "agent")
        self.assertEqual(session["adapter"], "pi")
        self.assertTrue(session["trusted"])
        session_id = session["id"]

        await self.client.send_json({"type": "subscribe", "session_id": session_id})
        attached = await self.client.recv_json_until(
            lambda message: message.get("type") == "attached"
            and message["session"]["id"] == session_id
        )
        self.assertEqual(attached["session"]["kind"], "agent")
        self.assertEqual(attached["session"]["adapter"], "pi")
        resync = await self.client.recv_json_until(
            lambda message: message.get("type") == "pi_resync"
            and message.get("session_id") == session_id
        )
        self.assertEqual(resync["entries"]["leafId"], "entry-2")

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

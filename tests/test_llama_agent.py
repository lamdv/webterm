"""The llama-server / llama-swap canonical agent adapter."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from webterm.agent.codec import NATIVE_CODEC
from webterm.agent.session import AgentCommandError
from webterm.integration.llama import config as llama_config
from webterm.session import SessionManager


class FakeLlamaHandler(BaseHTTPRequestHandler):
    """A tiny OpenAI-compatible streaming server; no model is involved."""

    requests: list[dict[str, object]] = []

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps(
            {
                "data": [
                    {"id": "model-a", "name": "Model A"},
                    {"id": "model-b", "name": "Model B"},
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        FakeLlamaHandler.requests.append(request)
        model = request.get("model", "")
        slow = "slow" in json.dumps(request.get("messages", []))
        reasoning = "reason please" in json.dumps(request.get("messages", []))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [
            {
                "choices": [
                    {"delta": {"role": "assistant", "content": None}, "index": 0}
                ],
                "model": model,
            }
        ]
        if reasoning:
            chunks.append(
                {
                    "choices": [
                        {"delta": {"reasoning_content": "pondering "}, "index": 0}
                    ],
                    "model": model,
                }
            )
        if not slow:
            chunks.extend(
                {
                    "choices": [
                        {
                            "delta": {
                                "content": piece,
                            },
                            "index": 0,
                        }
                    ],
                    "model": model,
                }
                for piece in ("hello ", "from ", "llama")
            )
            chunks.append(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                    },
                }
            )
        for chunk in chunks:
            try:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if slow:
                time.sleep(0.05)
        if slow:
            for index in range(40):
                try:
                    self.wfile.write(
                        (
                            "data: "
                            + json.dumps(
                                {
                                    "choices": [
                                        {"delta": {"content": str(index)}, "index": 0}
                                    ],
                                    "model": model,
                                }
                            )
                            + "\n\n"
                        ).encode()
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(0.05)
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class LlamaProfileTests(unittest.TestCase):
    def test_profile_forwards_chat_defaults(self) -> None:
        profile = llama_config.agent_profile(
            "http://127.0.0.1:8080", "model-a", "Be terse.", "low"
        )
        argv = profile["rpc"]
        assert isinstance(argv, list)
        self.assertEqual(profile["adapter"], "llama")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "Be terse.")
        self.assertEqual(argv[argv.index("--thinking") + 1], "low")

    def test_profile_omits_unset_defaults(self) -> None:
        profile = llama_config.agent_profile("http://127.0.0.1:8080")
        argv = profile["rpc"]
        assert isinstance(argv, list)
        self.assertNotIn("--system-prompt", argv)
        self.assertNotIn("--thinking", argv)

    def test_prompt_dir_precedence(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg"}, clear=True):
            self.assertEqual(
                llama_config.default_prompt_dir(), Path("/tmp/xdg/webterm/prompts")
            )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                llama_config.default_prompt_dir(),
                Path.home() / ".config" / "webterm" / "prompts",
            )
        with mock.patch.dict(
            os.environ,
            {"WEBTERM_PROMPT_DIR": "/tmp/env", "XDG_CONFIG_HOME": "/tmp/xdg"},
            clear=True,
        ):
            self.assertEqual(llama_config.default_prompt_dir(), Path("/tmp/env"))
            self.assertEqual(
                llama_config.default_prompt_dir("/tmp/flag"), Path("/tmp/flag")
            )


class LlamaAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaHandler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.manager = SessionManager(shell="/bin/sh", cwd="/tmp")
        FakeLlamaHandler.requests.clear()
        self.prompt_root = tempfile.mkdtemp(prefix="webterm-prompts-")
        self.addCleanup(shutil.rmtree, self.prompt_root, True)
        # The library directory itself must not exist yet, so "first use" can
        # create it and seed the starters.
        self.prompt_dir = str(Path(self.prompt_root) / "prompts")
        # Never let a prompt-library test reach the real XDG config directory.
        env = mock.patch.dict(os.environ, {"WEBTERM_PROMPT_DIR": self.prompt_dir})
        env.start()
        self.addCleanup(env.stop)

    async def asyncTearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    async def start(
        self, url: str | None = None, prompt_dir: str | None = None
    ) -> object:
        profile = llama_config.agent_profile(url or self.url)
        argv = list(profile["rpc"])  # type: ignore[arg-type]
        if prompt_dir is not None:
            argv.extend(["--prompt-dir", prompt_dir])
        session = await self.manager.create_agent(
            argv,
            adapter="llama",
            task="chat",
        )
        return session

    async def test_streams_a_reply_and_reports_state(self) -> None:
        session = await self.start()
        try:
            await session.prompt("Say hi")
            self.assertTrue(await session.wait_for_settled(5))
            events = [event for _, event in session.events_since(0)]
            text = "".join(
                event["delta"]
                for event in events
                if event.get("event") == "message.delta"
                and event.get("block_type") == "text"
            )
            self.assertEqual(text, "hello from llama")
            self.assertIn("run.settled", {event.get("event") for event in events})
            self.assertEqual(session.model, "model-a")
            self.assertEqual(session.tokens, 7)

            snapshot = await session.snapshot()
            self.assertEqual(snapshot["entries"][0]["role"], "user")
            self.assertEqual(
                snapshot["entries"][-1]["content"],
                [{"type": "text", "text": "hello from llama"}],
            )

            models = await session.models()
            self.assertEqual(
                [model["id"] for model in models["models"]], ["model-a", "model-b"]
            )
            await session.command("set_model", model="model-b")
            self.assertEqual(session.model, "model-b")

            text = await session.command(
                "raw", command={"type": "get_last_assistant_text"}
            )
            self.assertEqual(text["text"], "hello from llama")
            result = await NATIVE_CODEC.result(session, settled=True)
            self.assertEqual(result["text"], "hello from llama")
        finally:
            session.force_close()

    async def test_system_prompt_and_thinking_shape_the_request(self) -> None:
        session = await self.start()
        try:
            await session.command("set_system_prompt", text="  Be terse.  ")
            await session.command("set_thinking", level="low")
            await session.prompt("Say hi")
            self.assertTrue(await session.wait_for_settled(5))

            request = FakeLlamaHandler.requests[-1]
            self.assertEqual(
                request["messages"][0],
                {"role": "system", "content": "Be terse."},
            )
            self.assertEqual(request["reasoning_effort"], "low")
            self.assertEqual(
                request["chat_template_kwargs"],
                {"enable_thinking": True, "reasoning_effort": "low"},
            )

            state = await session.get_state()
            self.assertEqual(state["system_prompt"], "Be terse.")
            self.assertEqual(state["thinking_level"], "low")
            snapshot = await session.snapshot()
            self.assertEqual(snapshot["state"]["system_prompt"], "Be terse.")
            self.assertEqual(snapshot["state"]["thinking_level"], "low")
        finally:
            session.force_close()

    async def test_default_thinking_leaves_server_settings_alone(self) -> None:
        session = await self.start()
        try:
            await session.prompt("Say hi")
            self.assertTrue(await session.wait_for_settled(5))
            request = FakeLlamaHandler.requests[-1]
            self.assertNotIn("reasoning_effort", request)
            self.assertNotIn("chat_template_kwargs", request)
        finally:
            session.force_close()

    async def test_thinking_off_disables_reasoning(self) -> None:
        session = await self.start()
        try:
            await session.command("set_thinking", level="off")
            await session.prompt("Say hi")
            self.assertTrue(await session.wait_for_settled(5))
            request = FakeLlamaHandler.requests[-1]
            self.assertEqual(request["reasoning_effort"], "none")
            self.assertEqual(
                request["chat_template_kwargs"], {"enable_thinking": False}
            )
        finally:
            session.force_close()

    async def test_thinking_level_is_validated(self) -> None:
        session = await self.start()
        try:
            with self.assertRaises(AgentCommandError) as caught:
                await session.command("set_thinking", level="bogus")
            self.assertIn("thinking level must be one of", str(caught.exception))
        finally:
            session.force_close()

    async def test_clearing_the_system_prompt_removes_the_system_message(self) -> None:
        session = await self.start()
        try:
            await session.command("set_system_prompt", text="Be terse.")
            await session.command("set_system_prompt", text="")
            await session.prompt("Say hi")
            self.assertTrue(await session.wait_for_settled(5))
            messages = FakeLlamaHandler.requests[-1]["messages"]
            self.assertEqual(messages[0], {"role": "user", "content": "Say hi"})
        finally:
            session.force_close()

    async def test_advertises_thinking_and_system_prompt(self) -> None:
        session = await self.start()
        try:
            capabilities = session.capabilities.to_dict()
            self.assertTrue(capabilities["models"]["thinking"])
            self.assertIn("off", capabilities["models"]["levels"])
            self.assertIn("default", capabilities["models"]["levels"])
            self.assertTrue(capabilities["system_prompt"])
        finally:
            session.force_close()

    async def test_abort_finishes_the_run(self) -> None:
        session = await self.start()
        try:
            await session.prompt("slow please")
            await session.command("abort")
            self.assertTrue(await session.wait_for_settled(5))
            events = [event for _, event in session.events_since(0)]
            finished = [
                event for event in events if event.get("event") == "run.finished"
            ]
            self.assertTrue(finished)
            self.assertEqual(finished[-1]["reason"], "aborted")
        finally:
            session.force_close()

    async def test_https_against_a_plain_http_server_hints_at_the_scheme(self) -> None:
        session = await self.start(url=self.url.replace("http://", "https://"))
        try:
            # Model discovery fails first, so the hint arrives on the command.
            with self.assertRaises(AgentCommandError) as caught:
                await session.prompt("Say hi")
            message = str(caught.exception)
            self.assertIn("SSL", message)
            self.assertIn("use http://", message)
            self.assertTrue(await session.wait_for_settled(5))
        finally:
            session.force_close()

    # ------------------------------------------------------- message editing

    async def wait_for_entries(self, session: object, count: int) -> list[dict]:
        """Wait until the transcript has *count* entries ending in a reply."""
        deadline = time.monotonic() + 5.0
        entries: list[dict] = []
        while time.monotonic() < deadline:
            entries = (await session.snapshot())["entries"]  # type: ignore[attr-defined]
            if len(entries) == count and entries[-1].get("role") == "assistant":
                return entries
            await asyncio.sleep(0.02)
        self.fail(f"transcript did not reach {count} entries: {entries!r}")
        return entries

    async def test_edit_message_rewrites_the_transcript_and_runs_again(self) -> None:
        session = await self.start()
        try:
            self.assertTrue(session.capabilities.to_dict()["edit_message"])
            self.assertIn("edit_message", NATIVE_CODEC.commands)
            await session.prompt("first question")
            await self.wait_for_entries(session, 2)
            await session.prompt("second question")
            entries = await self.wait_for_entries(session, 4)
            first_user = entries[0]
            self.assertEqual(first_user["role"], "user")

            edited = await NATIVE_CODEC.command(
                session,
                "edit_message",
                {
                    "type": "edit_message",
                    "entry_id": first_user["id"],
                    "message": "rewritten question",
                },
                "test-edit",
            )
            self.assertTrue(edited["success"])
            data = edited["data"]
            self.assertEqual(data["disposition"], "started")
            self.assertEqual(data["removed"], 4)
            self.assertNotEqual(data["entry_id"], first_user["id"])

            entries = await self.wait_for_entries(session, 2)
            self.assertEqual(
                entries[0]["content"], [{"type": "text", "text": "rewritten question"}]
            )
            self.assertEqual(entries[0]["id"], data["entry_id"])
            # The edited prompt is the whole conversation: the earlier helper
            # messages and their answers were dropped before the request.
            self.assertEqual(
                FakeLlamaHandler.requests[-1]["messages"],
                [{"role": "user", "content": "rewritten question"}],
            )

            events = [event for _, event in session.events_since(0)]
            truncated = [
                event for event in events if event.get("event") == "transcript.truncated"
            ]
            self.assertEqual(len(truncated), 1)
            self.assertEqual(truncated[0]["entry_id"], first_user["id"])
            self.assertEqual(truncated[0]["removed"], 4)
            user_events = [
                event["message"]
                for event in events
                if event.get("event") == "message.finished"
                and event.get("message", {}).get("role") == "user"
            ]
            self.assertEqual(
                [message["content"][0]["text"] for message in user_events],
                ["first question", "second question", "rewritten question"],
            )
        finally:
            session.force_close()

    async def test_edit_message_validates_the_entry_and_text(self) -> None:
        session = await self.start()
        try:
            await session.prompt("keep me")
            entries = await self.wait_for_entries(session, 2)
            user_id = entries[0]["id"]
            cases = (
                ({"entry_id": "entry-missing", "message": "x"}, "no entry"),
                ({"entry_id": user_id, "message": "   "}, "edited message is empty"),
                ({"entry_id": 7, "message": "x"}, "needs the entry_id"),
                ({"message": "x"}, "needs the entry_id"),
            )
            for fields, expected in cases:
                with self.assertRaises(AgentCommandError) as caught:
                    await session.command("edit_message", **fields)
                self.assertIn(expected, str(caught.exception))
            # A rejected edit changes nothing and does not wedge the run slot.
            self.assertEqual((await session.snapshot())["entries"], entries)
            await session.prompt("still alive")
            await self.wait_for_entries(session, 4)
        finally:
            session.force_close()

    async def test_edit_message_is_refused_while_a_run_is_active(self) -> None:
        session = await self.start()
        try:
            await session.prompt("slow please")
            entries = (await session.snapshot())["entries"]
            self.assertEqual(entries[0]["content"][0]["text"], "slow please")
            with self.assertRaises(AgentCommandError) as caught:
                await session.command(
                    "edit_message", entry_id=entries[0]["id"], message="faster"
                )
            self.assertIn("a run is already active", str(caught.exception))
            await session.command("abort")
            self.assertTrue(await session.wait_for_settled(5))
            entries = (await session.snapshot())["entries"]
            self.assertEqual(
                entries[0]["content"], [{"type": "text", "text": "slow please"}]
            )
        finally:
            session.force_close()

    async def test_edit_answer_rewrites_the_answer_without_a_run(self) -> None:
        session = await self.start()
        try:
            capabilities = session.capabilities.to_dict()
            self.assertTrue(capabilities["edit_message"])
            self.assertTrue(capabilities["edit_answer"])

            await session.prompt("reason please")
            entries = await self.wait_for_entries(session, 2)
            answer = entries[1]
            self.assertEqual(
                [block["type"] for block in answer["content"]], ["thinking", "text"]
            )
            await session.prompt("second question")
            await self.wait_for_entries(session, 4)
            requests_before = len(FakeLlamaHandler.requests)

            edited = await session.command(
                "edit_message", entry_id=answer["id"], message="corrected answer"
            )
            self.assertEqual(edited["disposition"], "updated")
            self.assertEqual(edited["removed"], 3)
            self.assertNotEqual(edited["entry_id"], answer["id"])

            entries = (await session.snapshot())["entries"]
            self.assertEqual([entry["role"] for entry in entries], ["user", "assistant"])
            self.assertEqual(entries[1]["id"], edited["entry_id"])
            # The answer's thinking survives; its text is the replacement.
            self.assertEqual(
                entries[1]["content"],
                [
                    {"type": "thinking", "text": "pondering "},
                    {"type": "text", "text": "corrected answer"},
                ],
            )
            # An answer edit is a transcript rewrite, not a run.
            self.assertEqual(len(FakeLlamaHandler.requests), requests_before)
            self.assertFalse((await session.get_state())["streaming"])

            events = [event for _, event in session.events_since(0)]
            truncated = [
                event for event in events if event.get("event") == "transcript.truncated"
            ]
            self.assertEqual(len(truncated), 1)
            self.assertEqual(truncated[0]["entry_id"], answer["id"])
            self.assertEqual(truncated[0]["removed"], 3)

            # The next prompt sends the corrected answer as history, which is
            # the point of editing one.
            await session.prompt("carry on")
            self.assertTrue(await session.wait_for_settled(5))
            self.assertEqual(
                FakeLlamaHandler.requests[-1]["messages"],
                [
                    {"role": "user", "content": "reason please"},
                    {"role": "assistant", "content": "corrected answer"},
                    {"role": "user", "content": "carry on"},
                ],
            )
        finally:
            session.force_close()

    async def test_streamed_answer_carries_its_transcript_entry_id(self) -> None:
        """The id a client sees while streaming is the one it can edit by.

        The assistant entry is appended only when the run ends, so its id has
        to be reserved up front; a per-run alias here would make an edit fail
        with "no entry 'message-1'" until the client re-read the snapshot.
        """
        session = await self.start()
        try:
            await session.prompt("reason please")
            self.assertTrue(await session.wait_for_settled(5))
            entries = (await session.snapshot())["entries"]
            self.assertEqual([entry["role"] for entry in entries], ["user", "assistant"])
            answer = entries[1]
            events = [event for _, event in session.events_since(0)]
            finished = [
                event["message"]
                for event in events
                if event.get("event") == "message.finished"
                and event.get("message", {}).get("role") == "assistant"
            ]
            self.assertEqual([message["id"] for message in finished], [answer["id"]])
            started = [
                event["message"]
                for event in events
                if event.get("event") == "message.started"
            ]
            self.assertEqual([message["id"] for message in started], [answer["id"]])
            deltas = {
                event["message_id"]
                for event in events
                if event.get("event") == "message.delta"
            }
            self.assertEqual(deltas, {answer["id"]})

            # The id from the stream alone is enough to edit the answer.
            edited = await session.command(
                "edit_message", entry_id=finished[0]["id"], message="rewritten answer"
            )
            self.assertEqual(edited["disposition"], "updated")
            self.assertEqual(
                (await session.snapshot())["entries"][1]["content"],
                [
                    {"type": "thinking", "text": "pondering "},
                    {"type": "text", "text": "rewritten answer"},
                ],
            )
        finally:
            session.force_close()

    async def test_edit_answer_is_refused_while_a_run_is_active(self) -> None:
        session = await self.start()
        try:
            await session.prompt("Say hi")
            entries = await self.wait_for_entries(session, 2)
            answer = entries[1]
            await session.prompt("slow please")
            with self.assertRaises(AgentCommandError) as caught:
                await session.command(
                    "edit_message", entry_id=answer["id"], message="softer"
                )
            self.assertIn("a run is already active", str(caught.exception))
            await session.command("abort")
            self.assertTrue(await session.wait_for_settled(5))
            entries = (await session.snapshot())["entries"]
            self.assertEqual(entries[1]["id"], answer["id"])
            self.assertEqual(
                entries[1]["content"], [{"type": "text", "text": "hello from llama"}]
            )
        finally:
            session.force_close()

    # ------------------------------------------------------- prompt library

    async def test_prompt_library_seeds_and_lists_starters(self) -> None:
        session = await self.start()
        try:
            self.assertTrue(session.capabilities.to_dict()["prompt_library"])
            data = await session.command("list_prompts")
            prompts = data["prompts"]
            self.assertEqual(
                [prompt["name"] for prompt in prompts],
                ["code-review", "concise", "explain", "socratic"],
            )
            for prompt in prompts:
                self.assertTrue(prompt["text"])
                self.assertEqual(prompt["text"], prompt["text"].strip())
            self.assertTrue((Path(self.prompt_dir) / "concise.md").is_file())
        finally:
            session.force_close()

    async def test_existing_empty_prompt_directory_is_not_seeded(self) -> None:
        Path(self.prompt_dir).mkdir()
        session = await self.start()
        try:
            data = await session.command("list_prompts")
            self.assertEqual(data, {"prompts": []})
            self.assertEqual(list(Path(self.prompt_dir).iterdir()), [])
        finally:
            session.force_close()

    async def test_save_prompt_then_list_skips_bad_files(self) -> None:
        root = Path(self.prompt_dir)
        root.mkdir()
        (root / "not a prompt.md").write_text("x", encoding="utf-8")
        (root / ".hidden.md").write_text("x", encoding="utf-8")
        (root / "notes.txt").write_text("x", encoding="utf-8")
        (root / "blank.md").write_text("   \n\n", encoding="utf-8")
        session = await self.start()
        try:
            saved = await session.command(
                "save_prompt", name="respectful", text="  Be kind.\n\n"
            )
            self.assertEqual(
                saved, {"prompt": {"name": "respectful", "text": "Be kind."}}
            )
            self.assertEqual(
                (root / "respectful.md").read_text(encoding="utf-8"), "Be kind.\n"
            )
            data = await session.command("list_prompts")
            prompts = {prompt["name"]: prompt["text"] for prompt in data["prompts"]}
            self.assertEqual(prompts["respectful"], "Be kind.")
            self.assertNotIn(".hidden", prompts)
            self.assertNotIn("not a prompt", prompts)
            self.assertNotIn("notes", prompts)
            self.assertNotIn("blank", prompts)
            self.assertEqual(
                [prompt["name"] for prompt in data["prompts"]],
                sorted(prompts),
            )
        finally:
            session.force_close()

    async def test_save_prompt_validates_name_and_text(self) -> None:
        session = await self.start()
        try:
            cases = (
                ("../escape", "hi", "prompt name must be"),
                (".hidden", "hi", "prompt name must be"),
                ("has space", "hi", "prompt name must be"),
                ("x" * 65, "hi", "prompt name must be"),
                ("ok", "   \n", "prompt text is empty"),
                ("ok", "x" * 20000, "limit is 16384"),
            )
            for name, text, expected in cases:
                with self.assertRaises(AgentCommandError) as caught:
                    await session.command("save_prompt", name=name, text=text)
                self.assertIn(expected, str(caught.exception))
            # Nothing was created, and the session still answers.
            self.assertFalse(Path(self.prompt_dir).exists())
            self.assertFalse(session.dead)
            self.assertEqual((await session.get_state())["system_prompt"], "")
        finally:
            session.force_close()

    async def test_delete_prompt_reports_whether_it_existed(self) -> None:
        session = await self.start()
        try:
            await session.command("save_prompt", name="temp", text="Use once.")
            deleted = await session.command("delete_prompt", name="temp")
            self.assertEqual(deleted, {"name": "temp", "deleted": True})
            missing = await session.command("delete_prompt", name="temp")
            self.assertEqual(missing, {"name": "temp", "deleted": False})
            data = await session.command("list_prompts")
            self.assertNotIn("temp", [prompt["name"] for prompt in data["prompts"]])
            with self.assertRaises(AgentCommandError) as caught:
                await session.command("delete_prompt", name="../escape")
            self.assertIn("prompt name must be", str(caught.exception))
        finally:
            session.force_close()

    async def test_prompt_commands_work_through_the_codec(self) -> None:
        # The browser sends every command through the codec, whose fields
        # include a ``name`` key; this is the exact path the view uses.
        for name in ("list_prompts", "save_prompt", "delete_prompt"):
            self.assertIn(name, NATIVE_CODEC.commands)
        session = await self.start()
        try:
            saved = await NATIVE_CODEC.command(
                session,
                "save_prompt",
                {"type": "save_prompt", "name": "via-envelope", "text": "Hi."},
                "test-1",
            )
            self.assertTrue(saved["success"])
            self.assertEqual(
                saved["data"],
                {"prompt": {"name": "via-envelope", "text": "Hi."}},
            )
            listed = await NATIVE_CODEC.command(
                session, "list_prompts", {"type": "list_prompts"}, "test-2"
            )
            self.assertIn(
                "via-envelope",
                [prompt["name"] for prompt in listed["data"]["prompts"]],
            )
            deleted = await NATIVE_CODEC.command(
                session,
                "delete_prompt",
                {"type": "delete_prompt", "name": "via-envelope"},
                "test-3",
            )
            self.assertEqual(deleted["data"], {"name": "via-envelope", "deleted": True})
            refused = await NATIVE_CODEC.command(
                session,
                "delete_prompt",
                {"type": "delete_prompt", "name": "../escape"},
                "test-4",
            )
            self.assertFalse(refused["success"])
            self.assertIn("prompt name must be", refused["error"])
        finally:
            session.force_close()

    async def test_prompt_dir_flag_overrides_the_environment(self) -> None:
        flag_root = tempfile.mkdtemp(prefix="webterm-prompts-flag-")
        self.addCleanup(shutil.rmtree, flag_root, True)
        flag_dir = str(Path(flag_root) / "prompts")
        session = await self.start(prompt_dir=flag_dir)
        try:
            data = await session.command("list_prompts")
            self.assertIn("concise", [prompt["name"] for prompt in data["prompts"]])
            self.assertTrue((Path(flag_dir) / "concise.md").is_file())
            self.assertFalse(Path(self.prompt_dir).exists())
        finally:
            session.force_close()

    async def test_saved_prompt_leaves_the_active_system_prompt_alone(self) -> None:
        session = await self.start()
        try:
            await session.command("save_prompt", name="terse", text="Be terse.")
            self.assertEqual((await session.snapshot())["entries"], [])
            self.assertEqual((await session.get_state())["system_prompt"], "")

            await session.prompt("Say hi")
            self.assertTrue(await session.wait_for_settled(5))
            self.assertEqual(
                FakeLlamaHandler.requests[-1]["messages"][0],
                {"role": "user", "content": "Say hi"},
            )

            await session.command("set_system_prompt", text="Be terse.")
            await session.prompt("Say hi again")
            self.assertTrue(await session.wait_for_settled(5))
            self.assertEqual(
                FakeLlamaHandler.requests[-1]["messages"][0],
                {"role": "system", "content": "Be terse."},
            )
        finally:
            session.force_close()

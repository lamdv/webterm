"""The llama-server / llama-swap canonical agent adapter."""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


class LlamaAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaHandler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.manager = SessionManager(shell="/bin/sh", cwd="/tmp")
        FakeLlamaHandler.requests.clear()

    async def asyncTearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    async def start(self, url: str | None = None, **kwargs: str) -> object:
        profile = llama_config.agent_profile(url or self.url, kwargs.get("model"))
        session = await self.manager.create_agent(
            profile["rpc"],  # type: ignore[arg-type]
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

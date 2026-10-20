"""Tests for pi model discovery and the create-time model choice."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from webterm.integration.pi.models import MODEL_FIELDS
from webterm.server import start_server
from webterm.session import SessionManager

from .wsclient import WSClient

FAKE_PI = Path(__file__).with_name("fake_pi.py")


async def wait_until(predicate, timeout: float = 5.0) -> None:
    """Wait for *predicate*, so a test never races a freshly forked child."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true in time")


class PiModelCacheTests(unittest.IsolatedAsyncioTestCase):
    """The probe that backs the new-session picker."""

    async def asyncSetUp(self) -> None:
        self.cwd = tempfile.mkdtemp(prefix="webterm-pi-models-")
        self.manager = SessionManager(
            shell="/bin/sh",
            cwd=self.cwd,
            retained_ttl=None,
            pi_command=[sys.executable, str(FAKE_PI)],
            pi_available=True,
        )

    async def asyncTearDown(self) -> None:
        self.manager.close_all()
        shutil.rmtree(self.cwd, ignore_errors=True)

    async def test_probe_lists_sorted_models_and_the_starting_model(self) -> None:
        payload = await self.manager.pi_models(self.cwd)
        self.assertFalse(payload["cached"])
        self.assertEqual(
            [(model["provider"], model["id"]) for model in payload["models"]],
            [("fake", "fake-model"), ("fake", "fake-model-2"), ("other", "other-model")],
        )
        self.assertEqual(payload["default"]["id"], "fake-model")
        self.assertEqual(payload["cwd"], self.cwd)

    async def test_probe_is_never_published_as_a_session(self) -> None:
        await self.manager.pi_models(self.cwd)
        self.assertEqual(self.manager.sessions, {})

    async def test_second_call_reuses_the_cache(self) -> None:
        await self.manager.pi_models(self.cwd)
        counter = self.manager._counter
        payload = await self.manager.pi_models(self.cwd)
        self.assertTrue(payload["cached"])
        self.assertEqual(self.manager._counter, counter)

    async def test_refresh_forces_a_new_probe(self) -> None:
        await self.manager.pi_models(self.cwd)
        counter = self.manager._counter
        payload = await self.manager.pi_models(self.cwd, refresh=True)
        self.assertFalse(payload["cached"])
        self.assertEqual(self.manager._counter, counter + 1)

    async def test_the_working_directory_is_part_of_the_cache_key(self) -> None:
        other = tempfile.mkdtemp(prefix="webterm-pi-models-other-")
        self.addCleanup(shutil.rmtree, other, True)
        await self.manager.pi_models(self.cwd)
        payload = await self.manager.pi_models(other)
        self.assertFalse(payload["cached"])
        self.assertEqual(payload["cwd"], other)

    async def test_a_failed_refresh_keeps_the_previous_result(self) -> None:
        await self.manager.pi_models(self.cwd)
        self.manager.pi_command = ["/nonexistent/pi-for-webterm-tests"]
        payload = await self.manager.pi_models(self.cwd, refresh=True)
        self.assertTrue(payload["cached"])
        self.assertTrue(payload.get("error"))
        self.assertEqual(len(payload["models"]), 3)


class PiModelPickerTests(unittest.IsolatedAsyncioTestCase):
    """``pi_models`` and the model a created session actually runs."""

    async def asyncSetUp(self) -> None:
        self.cwd = tempfile.mkdtemp(prefix="webterm-pi-picker-")
        self.session_root = Path(tempfile.mkdtemp(prefix="webterm-pi-picker-sessions-"))
        self.argv_file = Path(self.cwd) / "argv.json"
        self.manager = SessionManager(
            shell="/bin/sh",
            cwd=self.cwd,
            retained_ttl=None,
            pi_command=[sys.executable, str(FAKE_PI)],
            pi_session_dir=str(self.session_root.resolve()),
            pi_available=True,
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
        self._requests = 0

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.handle.stop()
        shutil.rmtree(self.cwd, ignore_errors=True)
        shutil.rmtree(self.session_root, ignore_errors=True)

    async def request(self, message: dict) -> dict:
        self._requests += 1
        request_id = f"picker-{self._requests}"
        await self.client.send_json({**message, "id": request_id})
        return await self.client.recv_json_until(
            lambda candidate: candidate.get("type") in ("result", "error")
            and candidate.get("id") == request_id
        )

    async def create_pi(self, **extra: object) -> dict:
        message = {
            "type": "create",
            "mode": "pi",
            "cwd": self.cwd,
            "title": "fake chat",
            "trust": "allow",
            "env": {"FAKE_PI_ARGV_FILE": str(self.argv_file)},
        }
        for key, value in extra.items():
            if value is None:
                message.pop(key, None)
            else:
                message[key] = value
        reply = await self.request(message)
        self.assertEqual(reply["type"], "result", reply)
        return reply["result"]["session"]

    async def created_argv(self) -> list[str]:
        await wait_until(self.argv_file.exists)
        return json.loads(self.argv_file.read_text(encoding="utf-8"))

    async def session_model(self, session_id: str) -> dict:
        reply = await self.request(
            {
                "type": "pi",
                "session_id": session_id,
                "command": {"type": "get_state"},
            }
        )
        self.assertEqual(reply["type"], "result", reply)
        return reply["result"]["pi_response"]["data"]["model"]

    async def test_pi_models_lists_models_over_the_socket(self) -> None:
        reply = await self.request({"type": "pi_models", "cwd": self.cwd})
        self.assertEqual(reply["type"], "result", reply)
        models = reply["result"]["models"]
        self.assertEqual(len(models), 3)
        self.assertEqual(models[0]["provider"], "fake")
        self.assertEqual(reply["result"]["default"]["id"], "fake-model")
        # Credentials and endpoints must never be forwarded to the browser.
        for model in models:
            self.assertLessEqual(set(model), set(MODEL_FIELDS))
            self.assertNotIn("apiKey", model)
            self.assertNotIn("baseUrl", model)

    async def test_pi_models_defaults_to_the_manager_working_directory(self) -> None:
        reply = await self.request({"type": "pi_models"})
        self.assertEqual(reply["type"], "result", reply)
        self.assertEqual(reply["result"]["cwd"], self.cwd)

    async def test_pi_models_rejects_a_missing_directory(self) -> None:
        reply = await self.request(
            {"type": "pi_models", "cwd": str(Path(self.cwd) / "nope")}
        )
        self.assertEqual(reply["type"], "error", reply)
        self.assertEqual(reply["error"]["code"], "bad_request")

    async def test_pi_models_reports_pi_as_unavailable(self) -> None:
        self.manager.pi_available = False
        reply = await self.request({"type": "pi_models"})
        self.assertEqual(reply["type"], "error", reply)
        self.assertEqual(reply["error"]["code"], "unavailable")

    async def test_create_uses_the_chosen_model(self) -> None:
        session = await self.create_pi(provider="fake", model="fake-model-2")
        argv = await self.created_argv()
        self.assertIn("--provider", argv)
        self.assertEqual(argv[argv.index("--provider") + 1], "fake")
        self.assertEqual(argv[argv.index("--model") + 1], "fake-model-2")
        self.assertEqual((await self.session_model(session["id"]))["id"], "fake-model-2")

    async def test_create_without_a_choice_uses_the_configured_default(self) -> None:
        self.manager.pi_default_model = "fake/fake-model-2"
        session = await self.create_pi()
        argv = await self.created_argv()
        self.assertNotIn("--provider", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "fake/fake-model-2")
        self.assertEqual((await self.session_model(session["id"]))["id"], "fake-model-2")

    async def test_an_explicit_model_wins_over_the_default(self) -> None:
        self.manager.pi_default_model = "fake/fake-model-2"
        session = await self.create_pi(provider="other", model="other-model")
        self.assertEqual((await self.session_model(session["id"]))["id"], "other-model")

    async def test_no_default_and_no_choice_leaves_the_model_to_pi(self) -> None:
        session = await self.create_pi()
        self.assertNotIn("--model", await self.created_argv())
        self.assertEqual((await self.session_model(session["id"]))["id"], "fake-model")

    async def test_provider_without_a_model_is_rejected(self) -> None:
        reply = await self.request(
            {"type": "create", "mode": "pi", "cwd": self.cwd, "trust": "allow", "provider": "fake"}
        )
        self.assertEqual(reply["type"], "error", reply)
        self.assertEqual(reply["error"]["code"], "bad_request")

    async def test_a_resumed_session_keeps_its_own_model(self) -> None:
        path = self.session_root / "resumed.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        self.manager.pi_default_model = "fake/fake-model-2"
        session = await self.create_pi(resume=str(path), title=None)
        self.assertEqual(session["title"], "resumed")
        self.assertNotIn("--model", await self.created_argv())

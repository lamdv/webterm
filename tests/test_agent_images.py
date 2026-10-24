"""Image input: the canonical ``images`` field, its limits, and the codec gate.

Validation is transport-level: a bad image answers an error instead of being
dropped, so every adapter inherits the same shape, type, count, and size checks.
These tests cover those checks, the capability gate native adapters get from
:class:`~webterm.agent.codec.NativeCodec`, the llama adapter's refusal, the raw
pi envelope's server-side check, and the browser controller's DOM behaviour.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from webterm.agent.codec import NATIVE_CODEC
from webterm.agent.protocol import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_COUNT,
    MAX_IMAGES_BYTES,
    CanonicalCommandError,
    Capabilities,
    validate_images,
)
from webterm.integration.llama.adapter import CAPABILITIES as LLAMA_CAPABILITIES
from webterm.integration.llama.adapter import LlamaAgent
from webterm.integration.pi.canonical import (
    PI_CAPABILITIES,
    pi_command_from_canonical,
    pi_command_images,
)
from webterm.server import resolve_static, start_server
from webterm.session import SessionManager

from .test_agent_session import wait_until
from .wsclient import WSClient

HERE = Path(__file__).resolve().parent
FAKE_PI = HERE / "fake_pi.py"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_bytes(size: int = 64) -> bytes:
    """Return *size* bytes of PNG-shaped data."""
    return PNG_MAGIC + b"\x00" * max(0, size - len(PNG_MAGIC))


def png_data(size: int = 64) -> str:
    """Return raw base64 for a PNG-shaped blob."""
    return base64.b64encode(png_bytes(size)).decode("ascii")


def png_image(size: int = 64, mime: str = "image/png") -> dict:
    """Return one canonical image entry."""
    return {"mime_type": mime, "data": png_data(size)}


class ValidateImagesTests(unittest.TestCase):
    """The shared ``images`` field checks in ``webterm.agent.protocol``."""

    def test_absent_or_empty_means_no_images(self) -> None:
        self.assertEqual(validate_images({}), [])
        self.assertEqual(validate_images({"images": None}), [])
        self.assertEqual(validate_images({"images": []}), [])

    def test_canonical_entries_are_accepted_and_normalized(self) -> None:
        image = png_image()
        self.assertEqual(validate_images({"images": [image]}), [image])

    def test_wrong_shapes_are_refused(self) -> None:
        cases = {
            "must be a list": {"images": {"mime_type": "image/png"}},
            "must be an object": {"images": ["not an image"]},
            "needs a mime_type": {"images": [{"data": png_data()}]},
            "needs base64 data": {"images": [{"mime_type": "image/png"}]},
        }
        for needle, command in cases.items():
            with self.subTest(needle=needle):
                with self.assertRaises(CanonicalCommandError) as caught:
                    validate_images(command)
                self.assertIn(needle, str(caught.exception))

    def test_unsupported_type_is_named(self) -> None:
        with self.assertRaises(CanonicalCommandError) as caught:
            validate_images({"images": [png_image(mime="image/tiff")]})
        self.assertIn("unsupported image type", str(caught.exception))
        self.assertIn("image/tiff", str(caught.exception))

    def test_data_urls_and_bad_base64_are_refused(self) -> None:
        with self.assertRaises(CanonicalCommandError) as caught:
            validate_images(
                {"images": [{"mime_type": "image/png", "data": "data:image/png;base64,xx"}]}
            )
        self.assertIn("not a data URL", str(caught.exception))
        with self.assertRaises(CanonicalCommandError) as caught:
            validate_images({"images": [{"mime_type": "image/png", "data": "not base64!"}]})
        self.assertIn("not valid base64", str(caught.exception))

    def test_declared_type_must_match_the_magic_bytes(self) -> None:
        with self.assertRaises(CanonicalCommandError) as caught:
            validate_images(
                {
                    "images": [
                        {
                            "mime_type": "image/png",
                            "data": base64.b64encode(b"GIF89a payload").decode("ascii"),
                        }
                    ]
                }
            )
        self.assertIn("is not a image/png image", str(caught.exception))

    def test_oversize_image_is_refused(self) -> None:
        with self.assertRaises(CanonicalCommandError) as caught:
            validate_images({"images": [png_image(size=MAX_IMAGE_BYTES + 1)]})
        self.assertIn("too large", str(caught.exception))
        # A payload that is over the limit even before decoding is refused too.
        with self.assertRaises(CanonicalCommandError) as caught:
            validate_images({"images": [png_image(size=MAX_IMAGE_BYTES * 2)]})
        self.assertIn("too large", str(caught.exception))

    def test_too_many_images_are_refused(self) -> None:
        with self.assertRaises(CanonicalCommandError) as caught:
            validate_images({"images": [png_image() for _ in range(MAX_IMAGE_COUNT + 1)]})
        self.assertIn("too many images", str(caught.exception))
        self.assertIn(str(MAX_IMAGE_COUNT), str(caught.exception))

    def test_total_payload_is_bounded(self) -> None:
        per_image = MAX_IMAGES_BYTES // 2 + 1024
        self.assertLess(per_image, MAX_IMAGE_BYTES)
        with self.assertRaises(CanonicalCommandError) as caught:
            validate_images({"images": [png_image(per_image), png_image(per_image)]})
        self.assertIn("too large", str(caught.exception))
        self.assertIn("in total", str(caught.exception))


class _FakeNativeAgent:
    """The slice of ``AgentSession`` that ``NativeCodec`` touches."""

    adapter = "fake"

    def __init__(self, *, images: bool) -> None:
        self.capabilities = Capabilities(attachments_images=images)
        self.prompts: list[dict] = []
        self.commands: list[tuple[str, dict]] = []

    async def prompt(self, message, *, images=None, timeout=30.0) -> dict:
        self.prompts.append({"message": message, "images": images})
        return {"disposition": "started"}

    async def command(self, name, *, timeout=30.0, **fields) -> dict:
        self.commands.append((name, fields))
        return {}


class NativeCodecImageTests(unittest.IsolatedAsyncioTestCase):
    """Every native adapter goes through the same validation and gate."""

    async def test_prompt_carries_images_to_a_capable_adapter(self) -> None:
        session = _FakeNativeAgent(images=True)
        response = await NATIVE_CODEC.command(
            session,
            "prompt",
            {"type": "prompt", "message": "look", "images": [png_image()]},
            "c1",
        )
        self.assertTrue(response["success"])
        self.assertEqual(
            session.prompts, [{"message": "look", "images": [png_image()]}]
        )

    async def test_mid_run_commands_carry_images(self) -> None:
        session = _FakeNativeAgent(images=True)
        for name in ("steer", "follow_up"):
            with self.subTest(name=name):
                await NATIVE_CODEC.command(
                    session,
                    name,
                    {"type": name, "message": "look", "images": [png_image()]},
                    "c2",
                )
        self.assertEqual(
            session.commands,
            [
                ("steer", {"message": "look", "images": [png_image()]}),
                ("follow_up", {"message": "look", "images": [png_image()]}),
            ],
        )

    async def test_an_adapter_without_the_capability_refuses_images(self) -> None:
        session = _FakeNativeAgent(images=False)
        with self.assertRaises(CanonicalCommandError) as caught:
            await NATIVE_CODEC.command(
                session,
                "prompt",
                {"type": "prompt", "message": "look", "images": [png_image()]},
                "c3",
            )
        self.assertIn("does not accept images", str(caught.exception))
        self.assertEqual(session.prompts, [])

    async def test_other_commands_refuse_images_instead_of_dropping_them(self) -> None:
        session = _FakeNativeAgent(images=True)
        with self.assertRaises(CanonicalCommandError) as caught:
            await NATIVE_CODEC.command(
                session,
                "abort",
                {"type": "abort", "images": [png_image()]},
                "c4",
            )
        self.assertIn("abort does not accept images", str(caught.exception))
        self.assertEqual(session.commands, [])

    async def test_invalid_payload_is_a_transport_error(self) -> None:
        session = _FakeNativeAgent(images=True)
        with self.assertRaises(CanonicalCommandError):
            await NATIVE_CODEC.command(
                session,
                "prompt",
                {
                    "type": "prompt",
                    "message": "look",
                    "images": [png_image(mime="image/tiff")],
                },
                "c5",
            )


class LlamaImageRefusalTests(unittest.TestCase):
    """The llama chat advertises no image input and refuses one loudly."""

    def test_capability_stays_false(self) -> None:
        self.assertFalse(LLAMA_CAPABILITIES["attachments"]["images"])

    def test_prompt_with_images_is_refused(self) -> None:
        agent = LlamaAgent("http://127.0.0.1:1")
        records: list[dict] = []
        agent.send = records.append  # type: ignore[method-assign]
        agent.prompt(
            {"type": "prompt", "id": "p1", "message": "look", "images": [png_image()]}
        )
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["success"])
        self.assertIn("does not accept images", records[0]["error"])
        self.assertEqual(agent.entries, [], "nothing reached the transcript")

    def test_edit_message_with_images_is_refused(self) -> None:
        agent = LlamaAgent("http://127.0.0.1:1")
        records: list[dict] = []
        agent.send = records.append  # type: ignore[method-assign]
        agent.edit_message(
            {
                "type": "edit_message",
                "id": "e1",
                "entry_id": "entry-1",
                "message": "look",
                "images": [png_image()],
            }
        )
        self.assertFalse(records[0]["success"])
        self.assertIn("does not accept images", records[0]["error"])


class PiImageTranslationTests(unittest.TestCase):
    """pi's wire shape, its capability, and the raw envelope's validation."""

    def test_capability_is_truthful(self) -> None:
        self.assertTrue(PI_CAPABILITIES.attachments_images)

    def test_prompt_translates_to_pi_image_content(self) -> None:
        rpc = pi_command_from_canonical(
            {"type": "prompt", "message": "look", "images": [png_image()]}
        )
        self.assertEqual(
            rpc,
            {
                "type": "prompt",
                "message": "look",
                "images": [
                    {"type": "image", "data": png_data(), "mimeType": "image/png"}
                ],
            },
        )

    def test_mid_run_commands_keep_images_and_streaming_behavior(self) -> None:
        for name, behavior in (("steer", "steer"), ("follow_up", "followUp")):
            with self.subTest(name=name):
                rpc = pi_command_from_canonical(
                    {"type": name, "message": "look", "images": [png_image()]}
                )
                self.assertEqual(rpc["streamingBehavior"], behavior)
                self.assertEqual(rpc["images"][0]["mimeType"], "image/png")

    def test_pi_shape_validation_shares_the_limits(self) -> None:
        self.assertEqual(
            pi_command_images(
                {"images": [{"type": "image", "data": png_data(), "mimeType": "image/png"}]}
            ),
            [png_image()],
        )
        with self.assertRaises(CanonicalCommandError):
            pi_command_images({"images": [{"type": "image", "mimeType": "image/tiff"}]})
        with self.assertRaises(CanonicalCommandError):
            pi_command_images({"images": "nope"})


class PiEnvelopeImageTests(unittest.IsolatedAsyncioTestCase):
    """The raw ``pi`` envelope is validated by the server before pi's stdin."""

    async def asyncSetUp(self) -> None:
        self.cwd = tempfile.mkdtemp(prefix="webterm-images-")
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

    async def _create_pi(self, prompt_log: Path) -> dict:
        with mock.patch.dict(os.environ, {"FAKE_PI_PROMPT_FILE": str(prompt_log)}):
            await self.client.send_json(
                {
                    "type": "create",
                    "id": "create",
                    "mode": "pi",
                    "cwd": self.cwd,
                    "title": "image chat",
                    "trust": "allow",
                }
            )
            result = await self.client.recv_json_until(
                lambda message: message.get("type") == "result"
                and message.get("id") == "create"
            )
        return result["result"]["session"]

    async def _send(self, session_id: str, command: dict, request_id: str) -> dict:
        await self.client.send_json(
            {
                "type": "pi",
                "id": request_id,
                "session_id": session_id,
                "command": command,
            }
        )
        return await self.client.recv_json_until(
            lambda message: message.get("type") in ("result", "error")
            and message.get("id") == request_id
        )

    async def test_prompt_images_reach_pi_stdin(self) -> None:
        prompt_log = Path(self.cwd) / "prompts.jsonl"
        session = await self._create_pi(prompt_log)
        image = {"type": "image", "data": png_data(), "mimeType": "image/png"}
        answer = await self._send(
            session["id"],
            {"type": "prompt", "message": "what is this", "images": [image]},
            "p1",
        )
        self.assertEqual(answer["type"], "result", answer)
        self.assertTrue(answer["result"]["pi_response"]["success"])
        await wait_until(lambda: prompt_log.exists() and prompt_log.stat().st_size)
        record = json.loads(prompt_log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["images"], [image])

    async def test_the_raw_envelope_rejects_bad_images(self) -> None:
        session = await self._create_pi(Path(self.cwd) / "unused.jsonl")
        cases = {
            "too large": [
                {
                    "type": "image",
                    "data": png_data(MAX_IMAGE_BYTES + 8),
                    "mimeType": "image/png",
                }
            ],
            "too many images": [
                {"type": "image", "data": png_data(), "mimeType": "image/png"}
                for _ in range(MAX_IMAGE_COUNT + 1)
            ],
            "unsupported image type": [
                {"type": "image", "data": png_data(), "mimeType": "image/tiff"}
            ],
        }
        for needle, images in cases.items():
            with self.subTest(needle=needle):
                answer = await self._send(
                    session["id"],
                    {"type": "prompt", "message": "look", "images": images},
                    "bad-" + needle.replace(" ", "-"),
                )
                self.assertEqual(answer["type"], "error", answer)
                self.assertEqual(answer["error"]["code"], "bad_request")
                self.assertIn(needle, answer["error"]["message"])


class AttachJSTests(unittest.TestCase):
    """The composer controller is served, loaded, and exercised under node."""

    def test_index_loads_the_attach_controller(self) -> None:
        index = resolve_static("/index.html")
        self.assertIsNotNone(index)
        html = index.read_text(encoding="utf-8")
        self.assertIn('/attach.js', html)
        self.assertLess(
            html.index("/attach.js"),
            html.index("/agent.js"),
            "the controller must exist before the views use it",
        )
        self.assertIsNotNone(resolve_static("/attach.js"))

    def test_attach_controller_smoke(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(HERE / "attach_test.js")],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"attach test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

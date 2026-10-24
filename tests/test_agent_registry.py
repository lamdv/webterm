"""Tests for the discovered agent adapter registry and its ``/config.js`` block."""

from __future__ import annotations

import asyncio
import json
import unittest

from webterm.__main__ import parse_agents
from webterm.agent.registry import KNOWN_ADAPTERS, agent_adapters
from webterm.integration.llama import config as llama_config
from webterm.server import start_server
from webterm.session import SessionManager


def _error(message: str) -> None:
    raise AssertionError(message)


def _pi_profiles(*, interactive: bool = True, rpc: bool = True) -> dict:
    """Profiles shaped like the default pi profile, without touching PATH."""
    print_mode = ["pi", "-p", "{task}"]
    interactive_mode = ["pi", "{task}"] if interactive else None
    rpc_mode = ["pi", "--mode", "rpc"] if rpc else None
    return parse_agents(
        ["pi=" + " ".join(print_mode)],
        ["pi=" + " ".join(interactive_mode)] if interactive_mode else None,
        _error,
        ["pi=" + " ".join(rpc_mode)] if rpc_mode else None,
    )


def _llama_profiles() -> dict:
    return {"llama": llama_config.agent_profile("http://127.0.0.1:8080", "gemma")}


def _manager(pi_available: bool) -> SessionManager:
    return SessionManager(shell="/bin/sh", cwd="/tmp", pi_available=pi_available)


def _by_name(entries: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(entry["name"]): entry for entry in entries}


class RegistryTests(unittest.TestCase):
    def test_pi_is_available_when_installed(self) -> None:
        entries = _by_name(agent_adapters(_manager(True), _pi_profiles()))
        self.assertEqual(
            entries["pi"],
            {
                "name": "pi",
                "label": "pi",
                "available": True,
                "view": "/pi-view.js",
                "launcher": True,
            },
        )

    def test_pi_is_unavailable_without_the_binary(self) -> None:
        entries = _by_name(agent_adapters(_manager(False), _pi_profiles()))
        self.assertFalse(entries["pi"]["available"])
        # The view is shipped regardless; availability says whether it is used.
        self.assertEqual(entries["pi"]["view"], "/pi-view.js")

    def test_pi_needs_an_rpc_profile(self) -> None:
        profiles = _pi_profiles(rpc=False)
        entries = _by_name(agent_adapters(_manager(True), profiles))
        self.assertFalse(entries["pi"]["available"])
        self.assertTrue(entries["pi"]["launcher"])

    def test_llama_is_available_when_configured(self) -> None:
        entries = _by_name(agent_adapters(_manager(False), _llama_profiles()))
        self.assertEqual(
            entries["llama"],
            {
                "name": "llama",
                "label": "llama-server",
                "available": True,
                "view": None,
                "launcher": False,
            },
        )

    def test_llama_is_listed_but_unavailable_by_default(self) -> None:
        entries = _by_name(agent_adapters(_manager(True), _pi_profiles()))
        self.assertIn("llama", entries)
        self.assertFalse(entries["llama"]["available"])

    def test_profile_only_adapter_appears_from_configuration(self) -> None:
        profiles = {"remote": {"rpc": ["remote-agent", "--stdio"]}}
        entries = agent_adapters(_manager(False), profiles)
        self.assertEqual(
            entries[-1],
            {
                "name": "remote",
                "label": "remote",
                "available": True,
                "view": None,
                "launcher": False,
            },
        )

    def test_profile_adapter_key_names_the_entry(self) -> None:
        profiles = {"chat": llama_config.agent_profile("http://127.0.0.1:8080")}
        entries = _by_name(agent_adapters(_manager(False), profiles))
        self.assertTrue(entries["llama"]["available"])
        self.assertNotIn("chat", entries)

    def test_unknown_adapter_needs_an_rpc_command(self) -> None:
        entries = _by_name(agent_adapters(_manager(False), {"remote": {"print": ["x"]}}))
        self.assertFalse(entries["remote"]["available"])

    def test_generic_fallback_is_not_an_entry(self) -> None:
        profiles = {"custom": {"rpc": ["x"], "adapter": "generic"}}
        names = [entry["name"] for entry in agent_adapters(_manager(False), profiles)]
        self.assertNotIn("generic", names)

    def test_order_is_stable(self) -> None:
        profiles = {
            **_llama_profiles(),
            "remote": {"rpc": ["remote-agent"]},
            "zeta": {"rpc": ["zeta"]},
        }
        first = agent_adapters(_manager(True), profiles)
        second = agent_adapters(_manager(True), profiles)
        self.assertEqual(first, second)
        self.assertEqual(
            [entry["name"] for entry in first],
            [*KNOWN_ADAPTERS, "remote", "zeta"],
        )

    def test_no_manager_means_nothing_is_available(self) -> None:
        entries = agent_adapters(None, _pi_profiles())
        self.assertFalse(_by_name(entries)["pi"]["available"])


class ConfigScriptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = _manager(True)
        profiles = {**_pi_profiles(), **_llama_profiles()}
        self.adapters = agent_adapters(self.manager, profiles)
        self.config = {
            "agents": [{"name": "pi", "rpc": ["pi", "--mode", "rpc"]}],
            "pi": {"available": True},
            "agentAdapters": self.adapters,
        }
        self.handle = await start_server(
            "127.0.0.1",
            0,
            self.manager,
            self.config,
            enable_control=False,
            discovery=False,
        )

    async def asyncTearDown(self) -> None:
        await self.handle.stop()

    async def _config_script(self) -> dict:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.handle.port)
        writer.write(
            b"GET /config.js HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        raw = await reader.read()
        writer.close()
        status, _, body = raw.partition(b"\r\n\r\n")
        self.assertIn(b"200", status)
        prefix = b"window.WEBTERM_CONFIG = "
        text = body.decode("utf-8")
        self.assertIn(prefix.decode(), text)
        return json.loads(text[len(prefix) :].rstrip(";\n"))

    async def test_config_script_advertises_adapters(self) -> None:
        served = await self._config_script()
        self.assertEqual(
            served["agentAdapters"],
            [
                {
                    "name": "pi",
                    "label": "pi",
                    "available": True,
                    "view": "/pi-view.js",
                    "launcher": True,
                },
                {
                    "name": "llama",
                    "label": "llama-server",
                    "available": True,
                    "view": None,
                    "launcher": False,
                },
                {
                    "name": "mva",
                    "label": "mva",
                    "available": False,
                    "view": None,
                    "launcher": False,
                },
            ],
        )

    async def test_existing_config_keys_are_untouched(self) -> None:
        served = await self._config_script()
        self.assertEqual(served["pi"], {"available": True})
        self.assertEqual(
            served["agents"], [{"name": "pi", "rpc": ["pi", "--mode", "rpc"]}]
        )
        self.assertTrue(served["agentApi"])

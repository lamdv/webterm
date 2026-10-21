"""Smoke tests for the canonical browser view."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from webterm.server import resolve_static

HERE = Path(__file__).resolve().parent


class AgentViewJSTests(unittest.TestCase):
    def test_index_loads_the_agent_view(self) -> None:
        index = resolve_static("/index.html")
        self.assertIsNotNone(index)
        self.assertIn('/agent.js', index.read_text(encoding="utf-8"))
        self.assertIsNotNone(resolve_static("/agent.js"))

    def test_agent_view_reducer_smoke(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(HERE / "agent_view_test.js")],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"agent view test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )


class AppViewJSTests(unittest.TestCase):
    def test_metadata_updates_do_not_steal_focus(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(HERE / "app_view_test.js")],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"app view test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()

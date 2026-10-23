"""Smoke tests for the adapter view registry and self-discovery."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from webterm.server import resolve_static

HERE = Path(__file__).resolve().parent


class AgentViewsJSTests(unittest.TestCase):
    def test_index_loads_the_registry_before_the_views(self) -> None:
        index = resolve_static("/index.html")
        self.assertIsNotNone(index)
        html = index.read_text(encoding="utf-8")
        self.assertIsNotNone(resolve_static("/views.js"))
        self.assertIn("/views.js", html)
        self.assertLess(html.index("/views.js"), html.index("/pi.js"))
        self.assertLess(html.index("/views.js"), html.index("/agent.js"))
        self.assertLess(html.index("/views.js"), html.index("/app.js"))

    def test_views_smoke(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(HERE / "views_test.js")],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"views test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

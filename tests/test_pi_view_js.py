"""Smoke tests for the opt-in canonical pi view."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from webterm.server import resolve_static

HERE = Path(__file__).resolve().parent


class PiViewJSTests(unittest.TestCase):
    def test_index_loads_the_pi_view_after_the_base_class(self) -> None:
        index = resolve_static("/index.html")
        self.assertIsNotNone(index)
        html = index.read_text(encoding="utf-8")
        self.assertIsNotNone(resolve_static("/pi-view.js"))
        self.assertIn("/pi-view.js", html)
        self.assertLess(html.index("/agent.js"), html.index("/pi-view.js"))
        self.assertLess(html.index("/pi-view.js"), html.index("/app.js"))

    def test_pi_view_smoke(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(HERE / "pi_view_test.js")],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"pi view test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

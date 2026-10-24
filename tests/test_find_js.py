"""Smoke tests for the browser's transcript-search module."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from webterm.server import resolve_static

HERE = Path(__file__).resolve().parent


class FindJSTests(unittest.TestCase):
    def test_index_loads_the_finder_before_the_chat_views(self) -> None:
        index = resolve_static("/index.html")
        self.assertIsNotNone(index)
        html = index.read_text(encoding="utf-8")
        self.assertIsNotNone(resolve_static("/find.js"))
        self.assertIn('/find.js', html)
        self.assertLess(html.index("/find.js"), html.index("/agent.js"))
        self.assertLess(html.index("/find.js"), html.index("/agent.js"))

    def test_find_smoke(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(HERE / "find_test.js")],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"transcript search test failed\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )

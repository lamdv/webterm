"""Smoke tests for the browser's literal `@path` completion module."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from webterm.server import resolve_static

HERE = Path(__file__).resolve().parent


class PathCompleteJSTests(unittest.TestCase):
    def test_index_loads_the_path_completer_before_the_composers(self) -> None:
        index = resolve_static("/index.html")
        self.assertIsNotNone(index)
        html = index.read_text(encoding="utf-8")
        self.assertIsNotNone(resolve_static("/pathcomplete.js"))
        self.assertIn('/pathcomplete.js', html)
        self.assertLess(html.index("/pathcomplete.js"), html.index("/pi.js"))
        self.assertLess(html.index("/pathcomplete.js"), html.index("/agent.js"))

    def test_path_complete_smoke(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(HERE / "path_complete_test.js")],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"path completion test failed\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )

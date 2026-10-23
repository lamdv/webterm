"""Smoke tests for the pi view's disclosure of TUI-only commands."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class LimitsJSTests(unittest.TestCase):
    def test_limits_smoke(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(HERE / "limits_test.js")],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"/limits test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

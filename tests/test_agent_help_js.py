"""Smoke tests for the canonical view's declared help/gap disclosure."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class AgentHelpJSTests(unittest.TestCase):
    def test_agent_help_smoke(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(HERE / "agent_help_test.js")],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"agent help test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

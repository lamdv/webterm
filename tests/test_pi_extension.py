"""The bundled pi extension: config wiring and real-CLI registration.

The prompt context itself lives in ``extension.ts`` and is injected through
``before_agent_start``, so it can only be observed by a real model run.  These
tests cover what is cheap to check: every pi process webterm starts loads the
extension, and the installed ``pi`` accepts the file and registers its command.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from webterm.integration.pi import config as pi_config

EXTENSION = Path(pi_config.__file__).with_name("extension.ts")
PI = "/opt/bin/pi"


class ExtensionArgumentTests(unittest.TestCase):
    """Every pi process webterm starts must load the bundled extension."""

    def setUp(self) -> None:
        patcher = mock.patch.object(pi_config.shutil, "which", return_value=PI)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_profiles_load_the_extension_in_every_mode(self) -> None:
        profiles = pi_config.default_agent_profiles(str(EXTENSION))
        for mode, argv in profiles["pi"].items():
            self.assertEqual(argv[0], PI, mode)
            self.assertIn("-e", argv, mode)
            self.assertEqual(argv[argv.index("-e") + 1], str(EXTENSION), mode)
        self.assertEqual(profiles["pi"]["print"][-2:], ["-p", "{task}"])

    def test_launcher_loads_the_extension(self) -> None:
        [launcher] = pi_config.default_launcher(str(EXTENSION))
        self.assertEqual(launcher["command"], [PI, "-e", str(EXTENSION)])

    def test_without_an_extension_nothing_is_added(self) -> None:
        profiles = pi_config.default_agent_profiles(None)
        for mode, argv in profiles["pi"].items():
            self.assertNotIn("-e", argv, mode)
        self.assertEqual(pi_config.default_launcher(None)[0]["command"], [PI])


@unittest.skipUnless(shutil.which("pi"), "pi is not installed")
class ExtensionRegistrationTests(unittest.TestCase):
    """The installed pi loads the file and registers its command."""

    def test_webterm_command_is_registered(self) -> None:
        process = subprocess.Popen(
            ["pi", "--mode", "rpc", "--no-session", "-e", str(EXTENSION)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(json.dumps({"type": "get_commands", "id": "c1"}) + "\n")
            process.stdin.flush()
            record = {}
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                record = json.loads(line)
                if (
                    record.get("type") == "response"
                    and record.get("command") == "get_commands"
                ):
                    break
        finally:
            process.stdin.close()
            process.wait(timeout=60)

        self.assertTrue(record.get("success"), record.get("error"))
        commands = {item["name"]: item for item in record["data"]["commands"]}
        self.assertIn("webterm", commands)
        self.assertEqual(commands["webterm"]["source"], "extension")
        self.assertEqual(commands["webterm"]["sourceInfo"]["path"], str(EXTENSION))

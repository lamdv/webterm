"""CLI parsing tests."""

from __future__ import annotations

import unittest

from webterm.__main__ import build_parser, main, parse_launchers


class LauncherParsingTests(unittest.TestCase):
    def test_label_and_command(self) -> None:
        launchers = parse_launchers(
            ["btop=btop", "pi agent=pi --model foo"], lambda message: None
        )
        self.assertEqual(
            launchers,
            [
                {"label": "btop", "command": ["btop"]},
                {"label": "pi agent", "command": ["pi", "--model", "foo"]},
            ],
        )

    def test_label_defaults_to_command(self) -> None:
        launchers = parse_launchers(["=htop"], lambda message: None)
        self.assertEqual(launchers, [{"label": "htop", "command": ["htop"]}])

    def test_empty_command_reports_error(self) -> None:
        errors = []
        parse_launchers(["just-a-label"], errors.append)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected LABEL=COMMAND", errors[0])

    def test_parser_accepts_repeated_launcher(self) -> None:
        args = build_parser().parse_args(["--launcher", "a=a", "--launcher", "b=b c"])
        self.assertEqual(args.launcher, ["a=a", "b=b c"])

    def test_main_rejects_invalid_launcher(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            main(["--launcher", "bad"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

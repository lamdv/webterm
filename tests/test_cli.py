"""CLI parsing tests."""

from __future__ import annotations

import unittest

from webterm.__main__ import build_parser, main, parse_agents, parse_launchers


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

    def test_parser_accepts_token(self) -> None:
        args = build_parser().parse_args(["--token", "s3cret"])
        self.assertEqual(args.token, "s3cret")

    def test_parser_accepts_tls_options(self) -> None:
        args = build_parser().parse_args(["--tls-cert", "c.pem", "--tls-key", "k.pem"])
        self.assertEqual(args.tls_cert, "c.pem")
        self.assertEqual(args.tls_key, "k.pem")

    def test_main_rejects_tls_key_without_cert(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            main(["--tls-key", "k.pem"])
        self.assertEqual(caught.exception.code, 2)

    def test_main_reports_missing_certificate(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            main(["--tls-cert", "/nonexistent/webterm-cert.pem"])
        self.assertEqual(caught.exception.code, 2)

    def test_parser_accepts_repeated_launcher(self) -> None:
        args = build_parser().parse_args(["--launcher", "a=a", "--launcher", "b=b c"])
        self.assertEqual(args.launcher, ["a=a", "b=b c"])

    def test_main_rejects_invalid_launcher(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            main(["--launcher", "bad"])
        self.assertEqual(caught.exception.code, 2)


class AgentProfileTests(unittest.TestCase):
    def test_print_and_interactive_templates(self) -> None:
        profiles = parse_agents(
            ["pi=pi -p {task}"], ["pi=pi {task}"], lambda message: None
        )
        self.assertEqual(profiles["pi"]["print"], ["pi", "-p", "{task}"])
        self.assertEqual(profiles["pi"]["interactive"], ["pi", "{task}"])

    def test_name_defaults_to_argv0(self) -> None:
        profiles = parse_agents(["=btop"], None, lambda message: None)
        self.assertEqual(profiles["btop"]["print"], ["btop"])

    def test_empty_command_reports_error(self) -> None:
        errors: list[str] = []
        parse_agents(["nope"], None, errors.append)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected NAME=COMMAND", errors[0])

    def test_rpc_profile_template(self) -> None:
        profiles = parse_agents(
            ["pi=pi -p {task}"],
            ["pi=pi {task}"],
            lambda message: None,
            ["pi=pi --mode rpc --no-session"],
        )
        self.assertEqual(profiles["pi"]["rpc"], ["pi", "--mode", "rpc", "--no-session"])

    def test_parser_accepts_agent_and_limits(self) -> None:
        args = build_parser().parse_args(
            [
                "--agent",
                "pi=pi -p {task}",
                "--max-agent-depth",
                "3",
                "--max-agent-children",
                "5",
                "--no-control",
            ]
        )
        self.assertEqual(args.agent, ["pi=pi -p {task}"])
        self.assertEqual(args.max_agent_depth, 3)
        self.assertEqual(args.max_agent_children, 5)
        self.assertTrue(args.no_control)


if __name__ == "__main__":
    unittest.main()

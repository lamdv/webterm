"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shlex
import shutil
import sys

from . import __version__
from .server import serve
from .session import DEFAULT_SCROLLBACK, SessionManager


def _default_shell() -> str:
    for candidate in (os.environ.get("SHELL"), "/bin/bash", "/bin/zsh", "/bin/sh"):
        if candidate and shutil.which(candidate):
            return candidate
    return "/bin/sh"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webterm",
        description="A web-based terminal multiplexer with persistent PTY sessions.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765)")
    parser.add_argument("--shell", default=None, help="shell to launch (default: $SHELL)")
    parser.add_argument(
        "--font",
        default=None,
        help="CSS font-family for the browser terminal (default: a Nerd Font aware stack)",
    )
    parser.add_argument(
        "--launcher",
        action="append",
        default=None,
        metavar="LABEL=COMMAND",
        help=(
            "add a launcher button that starts a session running COMMAND "
            "(repeatable). Defaults to a 'pi agent' button when pi is installed."
        ),
    )
    parser.add_argument("--cwd", default=None, help="working directory for new sessions (default: $HOME)")
    parser.add_argument(
        "--scrollback",
        type=int,
        default=DEFAULT_SCROLLBACK,
        help=f"bytes of scrollback kept per session (default: {DEFAULT_SCROLLBACK})",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="enable debug logging")
    parser.add_argument("--version", action="version", version=f"webterm {__version__}")
    return parser


def parse_launchers(items: list[str], error) -> list[dict[str, object]]:
    """Turn ``LABEL=COMMAND`` strings into launcher config entries."""
    launchers: list[dict[str, object]] = []
    for item in items:
        label, _, command = item.partition("=")
        argv = shlex.split(command)
        if not argv:
            error(f"invalid --launcher {item!r}: expected LABEL=COMMAND")
        launchers.append({"label": label.strip() or argv[0], "command": argv})
    return launchers


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    shell = args.shell or _default_shell()
    cwd = args.cwd or os.path.expanduser("~")

    manager = SessionManager(shell=shell, cwd=cwd, scrollback=args.scrollback)

    config: dict[str, object] = {}
    if args.font:
        config["fontFamily"] = args.font

    launchers: list[dict[str, object]] = []
    if args.launcher:
        launchers = parse_launchers(args.launcher, parser.error)
    else:
        pi = shutil.which("pi")
        if pi:
            launchers.append({"label": "pi agent", "command": [pi]})
    if launchers:
        config["launchers"] = launchers

    try:
        asyncio.run(serve(args.host, args.port, manager, config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

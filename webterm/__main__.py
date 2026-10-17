"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import os
import shlex
import shutil
import ssl
import sys
from pathlib import Path

from . import __version__
from .server import serve
from .session import (
    DEFAULT_MAX_AGENT_CHILDREN,
    DEFAULT_MAX_AGENT_DEPTH,
    DEFAULT_MAX_RETAINED,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_RETAINED_TTL,
    DEFAULT_SCROLLBACK,
    SessionManager,
    prepare_pi_session_dir,
)

log = logging.getLogger("webterm")


def _is_loopback(host: str) -> bool:
    """Return True when *host* only accepts connections from this machine."""
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_ssl_context(certfile: str, keyfile: str | None = None) -> ssl.SSLContext:
    """Create a server-side TLS context from a PEM certificate and key.

    *keyfile* may be ``None`` when the certificate file also contains the
    private key.  TLS 1.2 is the floor; older protocol versions are refused.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile, keyfile)
    return context


def _default_pi_session_dir() -> str:
    """Default webterm-managed root for persisted pi session files."""
    explicit = os.environ.get("WEBTERM_PI_SESSION_DIR")
    if explicit:
        return explicit
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "webterm", "pi-sessions")


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
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default: 127.0.0.1)",
    )
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
    parser.add_argument(
        "--cwd",
        default=None,
        help="working directory for new sessions (default: $HOME)",
    )
    parser.add_argument(
        "--tls-cert",
        default=None,
        metavar="PATH",
        help="PEM certificate (or chain) enabling HTTPS/WSS",
    )
    parser.add_argument(
        "--tls-key",
        default=None,
        metavar="PATH",
        help="PEM private key for --tls-cert (omit if it is in the cert file)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "require HTTP Basic auth with this shared secret for every request "
            "(default: $WEBTERM_TOKEN, or no authentication)"
        ),
    )
    parser.add_argument(
        "--scrollback",
        type=int,
        default=DEFAULT_SCROLLBACK,
        help=f"bytes of scrollback kept per session (default: {DEFAULT_SCROLLBACK})",
    )
    parser.add_argument(
        "--pi-session-dir",
        default=None,
        metavar="PATH",
        help=(
            "webterm-managed directory for persisted pi sessions "
            "(default: $WEBTERM_PI_SESSION_DIR or "
            "$XDG_STATE_HOME/webterm/pi-sessions)"
        ),
    )
    parser.add_argument(
        "--control-socket",
        default=None,
        metavar="PATH",
        help=(
            "Unix socket for the local agent control API "
            "(default: $XDG_RUNTIME_DIR/webterm/control.sock)"
        ),
    )
    parser.add_argument(
        "--no-control",
        action="store_true",
        help="disable the local agent control socket entirely",
    )
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        help="disable the unauthenticated /.well-known/webterm.json document",
    )
    parser.add_argument(
        "--agent",
        action="append",
        default=None,
        metavar="NAME=COMMAND",
        help=(
            "agent profile usable with `webtermctl spawn`, in print mode "
            "(repeatable; `{task}` is substituted). Defaults to pi when installed."
        ),
    )
    parser.add_argument(
        "--agent-interactive",
        action="append",
        default=None,
        metavar="NAME=COMMAND",
        help="override the interactive command for an agent profile (repeatable)",
    )
    parser.add_argument(
        "--agent-rpc",
        action="append",
        default=None,
        metavar="NAME=COMMAND",
        help="override the structured RPC command for an agent profile (repeatable)",
    )
    parser.add_argument(
        "--max-agent-depth",
        type=int,
        default=DEFAULT_MAX_AGENT_DEPTH,
        help=f"maximum sub-agent nesting depth (default: {DEFAULT_MAX_AGENT_DEPTH})",
    )
    parser.add_argument(
        "--max-agent-children",
        type=int,
        default=DEFAULT_MAX_AGENT_CHILDREN,
        help=(
            "maximum live child sessions per parent "
            f"(default: {DEFAULT_MAX_AGENT_CHILDREN})"
        ),
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=DEFAULT_MAX_SESSIONS,
        help=f"maximum concurrent sessions (default: {DEFAULT_MAX_SESSIONS})",
    )
    parser.add_argument(
        "--max-retained",
        type=int,
        default=DEFAULT_MAX_RETAINED,
        help=f"maximum finished one-shot sessions kept (default: {DEFAULT_MAX_RETAINED})",
    )
    parser.add_argument(
        "--retained-ttl",
        type=float,
        default=DEFAULT_RETAINED_TTL,
        help=(
            "seconds a finished one-shot session is kept before cleanup "
            f"(default: {DEFAULT_RETAINED_TTL:g}; negative disables)"
        ),
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


def parse_agents(
    items: list[str] | None,
    interactive: list[str] | None,
    error,
    rpc: list[str] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Turn ``--agent`` / ``--agent-interactive`` / ``--agent-rpc`` into profiles.

    Each profile can have a ``print`` template (one-shot), an ``interactive``
    template (a TUI), and an ``rpc`` template (a structured pi session).
    """
    profiles: dict[str, dict[str, list[str]]] = {}

    def entries(values: list[str] | None):
        for item in values or []:
            name, _, command = item.partition("=")
            argv = shlex.split(command)
            if not argv:
                error(f"invalid agent profile {item!r}: expected NAME=COMMAND")
            yield name.strip() or argv[0], argv

    for name, argv in entries(items):
        profiles[name] = {"print": argv, "interactive": list(argv)}
    for name, argv in entries(interactive):
        profile = profiles.setdefault(name, {"print": [], "interactive": []})
        profile["interactive"] = argv
    for name, argv in entries(rpc):
        profile = profiles.setdefault(name, {"print": [], "interactive": []})
        profile["rpc"] = argv
    return profiles


def default_agents(extension_path: str | None = None) -> dict[str, dict[str, list[str]]]:
    """Built-in agent profiles detected on ``PATH``."""
    profiles: dict[str, dict[str, list[str]]] = {}
    pi = shutil.which("pi")
    if pi:
        rpc = [pi, "--mode", "rpc", "--no-session", "--no-approve"]
        if extension_path:
            rpc.extend(["-e", extension_path])
        profiles["pi"] = {
            "print": [pi, "-p", "{task}"],
            "interactive": [pi, "{task}"],
            "rpc": rpc,
        }
    return profiles


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

    manager = SessionManager(
        shell=shell,
        cwd=cwd,
        scrollback=args.scrollback,
        max_agent_depth=args.max_agent_depth,
        max_agent_children=args.max_agent_children,
        max_sessions=args.max_sessions,
        max_retained=args.max_retained,
        retained_ttl=None if args.retained_ttl < 0 else args.retained_ttl,
        pi_command=[shutil.which("pi") or "pi"],
    )

    config: dict[str, object] = {}
    if args.font:
        config["fontFamily"] = args.font

    ssl_context: ssl.SSLContext | None = None
    if args.tls_key and not args.tls_cert:
        parser.error("--tls-key requires --tls-cert")
    if args.tls_cert:
        try:
            ssl_context = build_ssl_context(args.tls_cert, args.tls_key)
        except (OSError, ssl.SSLError) as exc:
            parser.error(f"cannot load TLS certificate: {exc}")

    pi_session_dir = args.pi_session_dir or _default_pi_session_dir()
    try:
        pi_session_dir = prepare_pi_session_dir(pi_session_dir)
    except OSError as exc:
        parser.error(f"cannot create pi session directory: {exc}")
    manager.pi_session_dir = pi_session_dir
    extension_path = Path(__file__).with_name("extensions") / "webterm-agents.ts"
    if extension_path.is_file():
        manager.pi_extension = str(extension_path)

    token = args.token or os.environ.get("WEBTERM_TOKEN") or None
    if not _is_loopback(args.host):
        if ssl_context is None:
            log.warning(
                "binding %s without TLS: traffic (including the secret) is unencrypted",
                args.host,
            )
        if token is None:
            log.warning(
                "binding %s without --token: anyone who can reach this port gets a shell",
                args.host,
            )

    launchers: list[dict[str, object]] = []
    if args.launcher:
        launchers = parse_launchers(args.launcher, parser.error)
    else:
        pi = shutil.which("pi")
        if pi:
            launchers.append({"label": "pi agent", "command": [pi]})
    if launchers:
        config["launchers"] = launchers
    config["pi"] = {
        "available": shutil.which("pi") is not None,
        "cwd": os.path.abspath(os.path.expanduser(cwd)),
        "sessionDir": pi_session_dir,
    }

    profiles = parse_agents(
        args.agent, args.agent_interactive, parser.error, args.agent_rpc
    )
    if not profiles:
        profiles = default_agents(manager.pi_extension)
    if profiles:
        config["agents"] = [
            {"name": name, **{key: value for key, value in profile.items() if value}}
            for name, profile in profiles.items()
        ]

    # Re-exec the same interpreter and package so a restart picks up edited
    # source without needing a supervisor.  `python -m webterm` is used rather
    # than sys.argv[0], which may be a console-script path or a module file.
    reexec_args = list(argv) if argv is not None else sys.argv[1:]
    restart = False
    try:
        restart = asyncio.run(
            serve(
                args.host,
                args.port,
                manager,
                config,
                token=token,
                ssl_context=ssl_context,
                enable_control=not args.no_control,
                control_socket=args.control_socket,
                profiles=profiles,
                discovery=not args.no_discovery,
            )
        )
    except KeyboardInterrupt:
        pass
    if restart:
        log.info("restarting webterm")
        os.execv(sys.executable, [sys.executable, "-m", "webterm", *reexec_args])
    return 0


if __name__ == "__main__":
    sys.exit(main())

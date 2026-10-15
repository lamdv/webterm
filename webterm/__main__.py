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

from . import __version__
from .server import serve
from .session import DEFAULT_SCROLLBACK, SessionManager

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

    ssl_context: ssl.SSLContext | None = None
    if args.tls_key and not args.tls_cert:
        parser.error("--tls-key requires --tls-cert")
    if args.tls_cert:
        try:
            ssl_context = build_ssl_context(args.tls_cert, args.tls_key)
        except (OSError, ssl.SSLError) as exc:
            parser.error(f"cannot load TLS certificate: {exc}")

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

    # Re-exec the same interpreter and package so a restart picks up edited
    # source without needing a supervisor.  `python -m webterm` is used rather
    # than sys.argv[0], which may be a console-script path or a module file.
    reexec_args = list(argv) if argv is not None else sys.argv[1:]
    restart = False
    try:
        restart = asyncio.run(
            serve(args.host, args.port, manager, config, token=token, ssl_context=ssl_context)
        )
    except KeyboardInterrupt:
        pass
    if restart:
        log.info("restarting webterm")
        os.execv(sys.executable, [sys.executable, "-m", "webterm", *reexec_args])
    return 0


if __name__ == "__main__":
    sys.exit(main())

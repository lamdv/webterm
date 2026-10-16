"""``webtermctl`` — control the local webterm server from a script or agent.

Talks to the local control socket (``$WEBTERM_SOCKET`` or the default per-user
path), so ``parent_id`` is filled in automatically for a process running inside
a webterm session.  See :mod:`webterm.client` for the library form.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import Any

from . import __version__
from .client import ControlClient, ControlError, current_session, discover_socket


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webtermctl",
        description="Control a local webterm server: sessions, commands, sub-agents.",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="control socket path (default: $WEBTERM_SOCKET or the per-user path)",
    )
    parser.add_argument("--json", action="store_true", help="print raw JSON results")
    parser.add_argument("--version", action="version", version=f"webtermctl {__version__}")
    sub = parser.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("list", help="list sessions")
    p.add_argument("--tree", action="store_true", help="indent by parent/child lineage")

    p = sub.add_parser("new", help="start a new interactive shell session")
    p.add_argument("--title")
    p.add_argument("--cwd")
    p.add_argument("--command", help="command line to type into the shell")
    p.add_argument("--private", action="store_true", help="hide from the browser sidebar")

    p = sub.add_parser("run", help="run a command to completion in its own PTY")
    p.add_argument("command", nargs=argparse.REMAINDER, help="command and arguments")
    p.add_argument("--cwd")
    p.add_argument("--title")
    p.add_argument("--parent", default=None, help="parent session id")
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--text", action="store_true", help="emit plain text (ANSI stripped)")
    p.add_argument("--private", action="store_true")

    p = sub.add_parser("spawn", help="start a sub-agent session")
    p.add_argument("--agent", help="agent profile name (e.g. pi)")
    p.add_argument("--task", help="task text passed to the agent")
    p.add_argument("--command", help="explicit command instead of an agent profile")
    p.add_argument("--print", dest="interactive", action="store_false", default=None)
    p.add_argument("--interactive", dest="interactive", action="store_true")
    p.set_defaults(interactive=None)
    p.add_argument("--cwd")
    p.add_argument("--title")
    p.add_argument("--parent", default=None, help="parent session id")
    p.add_argument("--timeout", type=float, default=None)
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait", dest="wait", action="store_true", default=None)
    wait.add_argument("--no-wait", dest="wait", action="store_false")
    p.add_argument("--text", action="store_true", help="emit plain text (ANSI stripped)")
    p.add_argument("--private", action="store_true")

    p = sub.add_parser("send", help="type text into a session")
    p.add_argument("session_id")
    p.add_argument("data")
    p.add_argument("--enter", action="store_true", help="append a newline (press Enter)")

    p = sub.add_parser("capture", help="read a session's output")
    p.add_argument("session_id")
    p.add_argument("--since", type=int, default=0, help="absolute byte offset to read from")
    p.add_argument("--text", action="store_true", help="emit plain text (ANSI stripped)")
    p.add_argument("--raw", action="store_true", help="do not strip ANSI escapes")

    p = sub.add_parser("wait", help="wait for a session to exit or produce output")
    p.add_argument("session_id")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--exit", action="store_true")
    target.add_argument("--text", dest="pattern", metavar="REGEX")
    target.add_argument("--idle", type=int, metavar="MS")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--timeout", type=float, default=None)

    p = sub.add_parser("kill", help="terminate a session and its descendants")
    p.add_argument("session_id")
    p.add_argument("--no-tree", action="store_true", help="leave children running")

    p = sub.add_parser("rename", help="rename a session")
    p.add_argument("session_id")
    p.add_argument("title")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    socket_path = discover_socket(args.socket)
    try:
        with ControlClient(socket_path) as client:
            return _dispatch(client, args)
    except FileNotFoundError:
        print(f"webtermctl: no control socket at {socket_path}", file=sys.stderr)
        return 2
    except (OSError, ControlError, ConnectionError) as exc:
        print(f"webtermctl: {exc}", file=sys.stderr)
        return 1


def _dispatch(client: ControlClient, args: argparse.Namespace) -> int:
    handler = {
        "list": _cmd_list,
        "new": _cmd_new,
        "run": _cmd_run,
        "spawn": _cmd_spawn,
        "send": _cmd_send,
        "capture": _cmd_capture,
        "wait": _cmd_wait,
        "kill": _cmd_kill,
        "rename": _cmd_rename,
    }[args.verb]
    return handler(client, args)


def _parent(explicit: str | None) -> str | None:
    """Explicit parent wins; otherwise inherit this process's session."""
    return explicit or current_session()


def _print_json(value: Any) -> None:
    print(json.dumps(value))


def _cmd_list(client: ControlClient, args: argparse.Namespace) -> int:
    sessions = client.list_sessions()
    if args.json:
        _print_json({"sessions": sessions})
        return 0
    if not sessions:
        print("no sessions")
        return 0
    known = {s["id"] for s in sessions}
    children: dict[str | None, list[dict]] = {}
    for session in sessions:
        parent = session.get("parent_id")
        if parent not in known:
            parent = None
        children.setdefault(parent, []).append(session)

    def render(parent: str | None, depth: int) -> None:
        for session in sorted(children.get(parent, []), key=lambda s: s.get("created_at", 0)):
            if session["alive"]:
                status = "running"
            else:
                status = f"exit {session['exit_code']}"
            label = session.get("task") or session.get("title") or ""
            role = " agent" if session.get("role") == "agent" else ""
            indent = "  " * depth
            print(f"{indent}{session['id']}  {label}  [{status}]{role}")
            render(session["id"], depth + 1)

    render(None, 0)
    return 0


def _cmd_new(client: ControlClient, args: argparse.Namespace) -> int:
    session = client.create(
        title=args.title,
        cwd=args.cwd,
        command=args.command,
        parent_id=_parent(None),
        origin="agent",
        visibility="private" if args.private else "public",
    )
    if args.json:
        _print_json(session)
    else:
        print(session["id"])
    return 0


def _cmd_run(client: ControlClient, args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("webtermctl: run needs a command", file=sys.stderr)
        return 2
    params: dict[str, Any] = {
        "cwd": args.cwd,
        "title": args.title,
        "parent_id": _parent(args.parent),
        "timeout": args.timeout,
        "visibility": "private" if args.private else "public",
    }
    if args.no_wait:
        result = client.run(command, wait=False, **params)
        if args.json:
            _print_json(result)
        else:
            print(result["session_id"])
        return 0
    result = client.run(
        command,
        wait=True,
        format="text" if args.text else "base64",
        strip_ansi=args.text,
        **params,
    )
    return _emit_run(result, args)


def _cmd_spawn(client: ControlClient, args: argparse.Namespace) -> int:
    if not args.agent and not args.command:
        print("webtermctl: spawn needs --agent or --command", file=sys.stderr)
        return 2
    params: dict[str, Any] = {
        "agent": args.agent,
        "task": args.task,
        "command": args.command,
        "cwd": args.cwd,
        "title": args.title,
        "parent_id": _parent(args.parent),
        "timeout": args.timeout,
        "visibility": "private" if args.private else "public",
    }
    if args.interactive is not None:
        params["interactive"] = args.interactive
    # Print-mode agents are awaited by default; an interactive TUI is not, so
    # the CLI returns the session id and the human attaches in the browser.
    wait = args.wait
    if wait is None:
        wait = not bool(args.interactive)
    if not wait:
        result = client.spawn(wait=False, **params)
        if args.json:
            _print_json(result)
        else:
            print(result["session_id"])
        return 0
    result = client.spawn(
        wait=True, format="text" if args.text else "base64", strip_ansi=args.text, **params
    )
    return _emit_run(result, args)


def _emit_run(result: dict, args: argparse.Namespace) -> int:
    if args.json:
        _print_json(result)
        return 0
    data = base64.b64decode(result.get("output_base64", ""))
    if data:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    if result.get("timed_out"):
        print(
            f"webtermctl: timed out (session {result.get('session_id')})", file=sys.stderr
        )
        return 124
    code = result.get("exit_code")
    return int(code) if code is not None else 0


def _cmd_send(client: ControlClient, args: argparse.Namespace) -> int:
    client.send(args.session_id, args.data, submit=args.enter)
    if args.json:
        _print_json({"ok": True})
    return 0


def _cmd_capture(client: ControlClient, args: argparse.Namespace) -> int:
    result = client.capture(args.session_id, since=args.since, text=args.text)
    if args.json:
        _print_json(result)
        return 0
    if args.text:
        sys.stdout.write(result.get("text", ""))
        return 0
    data = base64.b64decode(result.get("data_base64", ""))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


def _cmd_wait(client: ControlClient, args: argparse.Namespace) -> int:
    if args.pattern is not None:
        params: dict[str, Any] = {
            "until": "text",
            "pattern": args.pattern,
            "since": args.since,
        }
    elif args.idle is not None:
        params = {"until": "idle", "quiet_ms": args.idle}
    else:
        params = {"until": "exit"}
    params["timeout"] = args.timeout
    result = client.wait(args.session_id, **params)
    if args.json:
        _print_json(result)
        return 0
    if not result.get("matched"):
        return 124
    if args.pattern is not None:
        print(result.get("match"))
        return 0
    code = result.get("exit_code")
    return int(code) if code is not None else 0


def _cmd_kill(client: ControlClient, args: argparse.Namespace) -> int:
    result = client.kill(args.session_id, tree=not args.no_tree)
    if args.json:
        _print_json(result)
    else:
        print(" ".join(result.get("killed", [])))
    return 0


def _cmd_rename(client: ControlClient, args: argparse.Namespace) -> int:
    result = client.rename(args.session_id, args.title)
    if args.json:
        _print_json(result)
    else:
        print(result["session"]["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""A tiny scripted ``pi --mode rpc`` stand-in used by the test suite.

The real pi executable is not required to test webterm's transport, replay,
and lifecycle handling.  Behaviour can be adjusted with environment variables:

- ``FAKE_PI_IGNORE_EOF``: keep running after stdin closes.
- ``FAKE_PI_IGNORE_TERM``: ignore SIGTERM.
- ``FAKE_PI_CHILD_PID_FILE``: spawn a sleeping child and write its pid there.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


def send(record: dict) -> None:
    sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def respond(
    request_id: object,
    command: str,
    data: dict | None = None,
    *,
    success: bool = True,
    error: str | None = None,
) -> None:
    record: dict[str, object] = {
        "type": "response",
        "command": command,
        "success": success,
    }
    if request_id is not None:
        record["id"] = request_id
    if data is not None:
        record["data"] = data
    if error is not None:
        record["error"] = error
    send(record)


def emit_prompt_events(text: str) -> None:
    send({"type": "agent_start"})
    send({"type": "message_start", "message": {"role": "assistant", "content": []}})
    for chunk in (text[:3], text[3:6], text[6:]):
        if not chunk:
            continue
        send(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": chunk},
            }
        )
    send(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }
    )
    send({"type": "agent_end"})
    send({"type": "agent_settled"})


def main() -> int:
    if os.environ.get("FAKE_PI_IGNORE_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    child_file = os.environ.get("FAKE_PI_CHILD_PID_FILE")
    if child_file:
        marker = os.environ.get("FAKE_PI_CHILD_MARKER", "")
        code = (
            "import signal,sys,time;"
            "signal.signal(signal.SIGTERM,"
            "lambda *a: sys.exit(open(sys.argv[1],'w').write('term') and 0));"
            "time.sleep(60)"
        )
        child = subprocess.Popen([sys.executable, "-c", code, marker])
        with open(child_file, "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))

    for line in sys.stdin:
        try:
            command = json.loads(line)
        except ValueError:
            continue
        if not isinstance(command, dict):
            continue
        kind = command.get("type")
        request_id = command.get("id")
        if kind == "get_state":
            respond(
                request_id,
                "get_state",
                {
                    "model": {"provider": "fake", "id": "fake-model"},
                    "thinkingLevel": "off",
                    "isStreaming": False,
                    "isCompacting": False,
                    "messageCount": 0,
                    "pendingMessageCount": 0,
                },
            )
        elif kind == "get_entries":
            respond(
                request_id,
                "get_entries",
                {
                    "entries": [
                        {
                            "type": "message",
                            "id": "entry-1",
                            "parentId": None,
                            "timestamp": "2024-01-01T00:00:00.000Z",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "hello"}],
                            },
                        }
                    ],
                    "leafId": "entry-1",
                },
            )
        elif kind == "prompt":
            respond(request_id, "prompt", {"disposition": "started"})
            emit_prompt_events(str(command.get("message", "hello")))
        elif kind == "set_session_name":
            name = str(command.get("name", "")).strip()
            respond(request_id, "set_session_name", {})
            send({"type": "session_info_changed", "name": name})
        elif kind in ("abort", "clear_queue", "set_model", "new_session", "compact"):
            respond(request_id, str(kind), {})
        elif kind == "get_commands":
            respond(
                request_id,
                "get_commands",
                {
                    "commands": [
                        {
                            "name": "review",
                            "description": "Review the current changes",
                            "source": "prompt",
                        }
                    ]
                },
            )
        elif kind == "get_session_stats":
            respond(
                request_id,
                "get_session_stats",
                {"tokens": {"total": 0}, "cost": 0, "totalMessages": 0},
            )
        else:
            respond(request_id, str(kind), success=False, error="unsupported command")

    if os.environ.get("FAKE_PI_IGNORE_EOF"):
        while True:
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

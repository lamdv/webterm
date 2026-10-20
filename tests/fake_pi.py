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
    send({"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}})
    send(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "thinking_delta", "delta": "pondering"},
        }
    )
    send(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "thinking_end", "content": "pondering"},
        }
    )
    send(
        {
            "type": "tool_execution_start",
            "toolCallId": "tool-1",
            "toolName": "bash",
            "args": {"command": "echo hi"},
        }
    )
    send(
        {
            "type": "tool_execution_end",
            "toolCallId": "tool-1",
            "toolName": "bash",
            "result": "hi",
            "isError": False,
        }
    )
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
                "model": "fake-model",
                "content": [{"type": "text", "text": text}],
                "usage": {
                    "input": 3,
                    "output": 5,
                    "totalTokens": 8,
                    "cost": {"total": 0.001},
                },
            },
        }
    )
    send({"type": "agent_end"})
    send({"type": "agent_settled"})


ENTRIES = [
    {
        "type": "message",
        "id": "entry-1",
        "parentId": None,
        "timestamp": "2024-01-01T00:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
        },
    },
    {
        "type": "message",
        "id": "entry-2",
        "parentId": "entry-1",
        "timestamp": "2024-01-01T00:00:01.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "follow up"}],
        },
    },
]


def selected_model() -> dict:
    """Return the model pi would resolve from ``--provider`` / ``--model``.

    ``--model`` accepts ``id``, ``provider/id`` and a ``:thinking`` suffix, the
    same forms the real CLI accepts; without it pi reports its own default.
    """
    provider: str | None = None
    spec: str | None = None
    args = sys.argv[1:]
    for index, arg in enumerate(args[:-1]):
        if arg == "--provider":
            provider = args[index + 1]
        elif arg == "--model":
            spec = args[index + 1]
    if not spec:
        return {"provider": "fake", "id": "fake-model", "name": "Fake Model"}
    spec = spec.split(":", 1)[0]
    explicit_provider, separator, identifier = spec.partition("/")
    if separator:
        provider = explicit_provider
        spec = identifier
    return {"provider": provider or "fake", "id": spec, "name": spec}


def main() -> int:
    argv_file = os.environ.get("FAKE_PI_ARGV_FILE")
    if argv_file:
        with open(argv_file, "w", encoding="utf-8") as handle:
            json.dump(sys.argv, handle)

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

    thinking = {"level": "off"}

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
                    "model": selected_model(),
                    "thinkingLevel": thinking["level"],
                    "isStreaming": False,
                    "isCompacting": False,
                    "messageCount": 0,
                    "pendingMessageCount": 0,
                    "sessionFile": os.environ.get("FAKE_PI_SESSION_FILE"),
                },
            )
        elif kind == "get_entries":
            respond(
                request_id,
                "get_entries",
                {"entries": ENTRIES, "leafId": "entry-2"},
            )
        elif kind == "prompt":
            respond(request_id, "prompt", {"disposition": "started"})
            message = str(command.get("message", "hello"))
            if "ask" in message.lower():
                send(
                    {
                        "type": "extension_ui_request",
                        "id": "ui-1",
                        "method": "confirm",
                        "title": "Continue?",
                        "message": "The fake extension wants a decision.",
                    }
                )
            elif "notify" in message.lower():
                send(
                    {
                        "type": "extension_ui_request",
                        "id": "ui-notify",
                        "method": "notify",
                        "message": "fake notification",
                        "notifyType": "warning",
                    }
                )
                send(
                    {
                        "type": "extension_ui_request",
                        "id": "ui-status",
                        "method": "setStatus",
                        "statusKey": "fake",
                        "statusText": "working",
                    }
                )
                send(
                    {
                        "type": "extension_ui_request",
                        "id": "ui-widget",
                        "method": "setWidget",
                        "widgetKey": "fake",
                        "widgetLines": ["widget line"],
                        "widgetPlacement": "aboveEditor",
                    }
                )
                send(
                    {
                        "type": "extension_ui_request",
                        "id": "ui-title",
                        "method": "setTitle",
                        "title": "fake title",
                    }
                )
                send({"type": "agent_settled"})
            else:
                emit_prompt_events(message)
        elif kind == "extension_ui_response":
            ack_file = os.environ.get("FAKE_PI_UI_ACK_FILE")
            if ack_file:
                with open(ack_file, "w", encoding="utf-8") as handle:
                    json.dump(command, handle)
            send(
                {
                    "type": "extension_ui_response",
                    "id": command.get("id"),
                    "response": command,
                }
            )
            send({"type": "agent_settled"})
        elif kind == "set_session_name":
            name = str(command.get("name", "")).strip()
            respond(request_id, "set_session_name", {})
            send({"type": "session_info_changed", "name": name})
        elif kind == "get_tree":
            respond(
                request_id,
                "get_tree",
                {
                    "tree": [
                        {
                            "entry": ENTRIES[0],
                            "children": [{"entry": ENTRIES[1], "children": []}],
                        }
                    ],
                    "leafId": "entry-2",
                },
            )
        elif kind == "get_fork_messages":
            respond(
                request_id,
                "get_fork_messages",
                {"messages": [{"entryId": "entry-2", "text": "follow up"}]},
            )
        elif kind in ("fork", "clone"):
            respond(request_id, str(kind), {"cancelled": False, "text": "follow up"})
        elif kind == "export_html":
            output_path = command.get("outputPath")
            if not isinstance(output_path, str) or not output_path:
                respond(request_id, "export_html", success=False, error="no output path")
            else:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as handle:
                    handle.write("<!doctype html><title>fake pi export</title>")
                respond(request_id, "export_html", {"path": output_path})
        elif kind in ("abort", "clear_queue", "new_session", "compact"):
            respond(request_id, str(kind), {})
        elif kind == "get_available_models":
            respond(
                request_id,
                "get_available_models",
                {
                    "models": [
                        {"provider": "fake", "id": "fake-model", "name": "Fake Model"},
                        {"provider": "fake", "id": "fake-model-2", "name": "Fake Model 2"},
                        {"provider": "other", "id": "other-model", "name": "Other Model"},
                    ]
                },
            )
        elif kind == "set_model":
            respond(
                request_id,
                "set_model",
                {
                    "provider": command.get("provider"),
                    "id": command.get("modelId"),
                    "name": command.get("modelId"),
                },
            )
        elif kind == "get_available_thinking_levels":
            respond(
                request_id,
                "get_available_thinking_levels",
                {"levels": ["off", "minimal", "low", "medium", "high"]},
            )
        elif kind == "set_thinking_level":
            level = str(command.get("level", ""))
            if level not in ("off", "minimal", "low", "medium", "high"):
                respond(
                    request_id,
                    "set_thinking_level",
                    success=False,
                    error="invalid thinking level",
                )
            else:
                thinking["level"] = level
                respond(request_id, "set_thinking_level", {})
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
        elif kind == "get_last_assistant_text":
            respond(request_id, "get_last_assistant_text", {"text": "fake reply"})
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

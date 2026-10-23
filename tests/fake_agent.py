#!/usr/bin/env python3
"""A scripted agent that speaks webterm's canonical JSONL protocol.

Used by the test suite as the reference implementation of the protocol; the
real MVA adapter will be the production reference.  Behaviour is selected by
the prompt text and environment variables:

- ``FAKE_AGENT_NO_HELLO``: never send the hello record (handshake timeout).
- ``FAKE_AGENT_BAD_PROTOCOL``: announce an unsupported protocol version.
- ``FAKE_AGENT_IGNORE_EOF``: keep running after stdin closes.
- ``FAKE_AGENT_IGNORE_TERM``: ignore SIGTERM.
- ``FAKE_AGENT_CHILD_PID_FILE``: spawn a sleeping child and write its pid.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

WRITE_LOCK = threading.Lock()
DIALOG_EVENT = threading.Event()
DIALOG_RESPONSE: dict | None = None
ABORT_EVENT = threading.Event()
BUSY = False
MODEL = "fake-model"


def send(record: dict) -> None:
    payload = json.dumps(record, separators=(",", ":")) + "\n"
    with WRITE_LOCK:
        sys.stdout.write(payload)
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


def event(event_name: str, **fields: object) -> None:
    send({"type": "event", "event": event_name, **fields})


def text_delta(message_id: str, text: str, index: int = 0) -> None:
    event(
        "message.delta",
        message_id=message_id,
        block_index=index,
        block_type="text",
        delta=text,
    )


def message_finished(message_id: str, text: str) -> None:
    event(
        "message.finished",
        message={
            "id": message_id,
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    )


def stream_text(prompt: str) -> str:
    """Emit a small message, a tool call, and a settled run."""
    message_id = "m1"
    text = f"fake reply to {prompt}"
    event("run.started", run_id="r1")
    event("message.started", message={"id": message_id, "role": "assistant"})
    for chunk in ("fake ", "reply ", f"to {prompt}"):
        if ABORT_EVENT.is_set():
            break
        text_delta(message_id, chunk)
        time.sleep(0.02)
    if ABORT_EVENT.is_set():
        event("run.finished", run_id="r1", reason="aborted")
        event("run.settled", run_id="r1")
        return text
    event(
        "tool.started",
        call_id="t1",
        name="bash",
        args={"command": "printf tool"},
    )
    event("tool.updated", call_id="t1", delta="tool output")
    event("tool.finished", call_id="t1", output="tool output", is_error=False)
    message_finished(message_id, text)
    event(
        "usage",
        prompt_tokens=4,
        completion_tokens=6,
        total_tokens=10,
        cost=0.001,
    )
    event("session.info", model=MODEL, session_file="/tmp/fake-session.jsonl")
    event("run.finished", run_id="r1", reason="stop")
    event("run.settled", run_id="r1")
    return text


def run_prompt(prompt: str) -> None:
    global BUSY, DIALOG_RESPONSE
    BUSY = True
    ABORT_EVENT.clear()
    try:
        if "crash" in prompt:
            time.sleep(0.02)
            os._exit(3)
        if "confirm" in prompt:
            event("run.started", run_id="r1")
            event("message.started", message={"id": "m1", "role": "assistant"})
            text_delta("m1", "waiting for confirmation")
            DIALOG_EVENT.clear()
            DIALOG_RESPONSE = None
            send(
                {
                    "type": "dialog_request",
                    "id": "d1",
                    "kind": "confirm",
                    "title": "Proceed?",
                    "message": "run the dangerous thing",
                    "timeout_ms": 500,
                }
            )
            DIALOG_EVENT.wait(5.0)
            answer = DIALOG_RESPONSE or {}
            if answer.get("cancelled"):
                text_delta("m1", " cancelled")
                message_finished("m1", "waiting for confirmation cancelled")
                event("run.finished", run_id="r1", reason="cancelled")
            elif answer.get("approved"):
                text_delta("m1", " approved")
                message_finished("m1", "waiting for confirmation approved")
                event("run.finished", run_id="r1", reason="stop")
            else:
                text_delta("m1", " denied")
                message_finished("m1", "waiting for confirmation denied")
                event("run.finished", run_id="r1", reason="stop")
            event("run.settled", run_id="r1")
            return
        if "slow" in prompt:
            event("run.started", run_id="r1")
            event("message.started", message={"id": "m1", "role": "assistant"})
            for index in range(100):
                if ABORT_EVENT.is_set():
                    event("run.finished", run_id="r1", reason="aborted")
                    event("run.settled", run_id="r1")
                    return
                text_delta("m1", str(index), index=index)
                time.sleep(0.02)
            message_finished("m1", "slow")
            event("run.finished", run_id="r1", reason="stop")
            event("run.settled", run_id="r1")
            return
        if "big" in prompt:
            event("run.started", run_id="r1")
            event("message.started", message={"id": "m1", "role": "assistant"})
            event(
                "tool.started",
                call_id="t-big",
                name="bash",
                args={"command": "yes"},
            )
            event("tool.updated", call_id="t-big", delta="x" * 200_000)
            event("tool.finished", call_id="t-big", output="x" * 200_000, is_error=False)
            message_finished("m1", "big")
            event("run.finished", run_id="r1", reason="stop")
            event("run.settled", run_id="r1")
            return
        stream_text(prompt)
    finally:
        BUSY = False


ENTRIES = [
    {
        "id": "entry-1",
        "role": "user",
        "content": [{"type": "text", "text": "hello"}],
    },
    {
        "id": "entry-2",
        "role": "assistant",
        "content": [{"type": "text", "text": "hello from snapshot"}],
    },
]


def handle(command: dict) -> None:
    global DIALOG_RESPONSE, MODEL
    name = command.get("type")
    request_id = command.get("id")
    if name == "prompt":
        prompt = command.get("message")
        if not isinstance(prompt, str) or not prompt.strip():
            respond(request_id, "prompt", success=False, error="empty prompt")
            return
        respond(request_id, "prompt", {"disposition": "started"})
        threading.Thread(target=run_prompt, args=(prompt,), daemon=True).start()
    elif name == "abort":
        ABORT_EVENT.set()
        respond(request_id, "abort", {"ok": True})
    elif name == "command":
        declared = command.get("name")
        args = command.get("args") if isinstance(command.get("args"), dict) else {}
        if declared == "echo":
            respond(request_id, "command", {"text": str(args.get("text", ""))})
        elif declared == "boom":
            respond(request_id, "command", success=False, error="boom refused")
        else:
            respond(
                request_id,
                "command",
                success=False,
                error=f"unknown declared command {declared!r}",
            )
    elif name == "get_state":
        respond(
            request_id,
            "get_state",
            {"model": MODEL, "session_id": "fake-session", "streaming": BUSY},
        )
    elif name == "get_snapshot":
        respond(
            request_id,
            "get_snapshot",
            {
                "entries": ENTRIES,
                "has_more": False,
                "first_entry_id": ENTRIES[0]["id"],
                "state": {"model": MODEL},
            },
        )
    elif name == "get_history":
        before = command.get("before")
        entries = list(ENTRIES)
        if isinstance(before, str):
            entries = [entry for entry in entries if entry["id"] != before]
        respond(
            request_id,
            "get_history",
            {"entries": entries, "has_more": False, "first_entry_id": entries[0]["id"]},
        )
    elif name == "list_models":
        respond(
            request_id,
            "list_models",
            {
                "models": [
                    {"id": "fake-model", "provider": "fake", "name": "Fake Model"}
                ],
                "default": "fake-model",
            },
        )
    elif name == "set_model":
        model = command.get("model")
        if not isinstance(model, str) or not model:
            respond(request_id, "set_model", success=False, error="model is required")
            return
        MODEL = model
        event("session.info", model=MODEL)
        respond(request_id, "set_model", {"model": MODEL})
    elif name == "dialog_response":
        response = command.get("response")
        DIALOG_RESPONSE = response if isinstance(response, dict) else {}
        DIALOG_EVENT.set()
        respond(request_id, "dialog_response", {"ok": True})
    elif name == "shutdown":
        respond(request_id, "shutdown", {"ok": True})
        raise SystemExit(0)
    else:
        respond(request_id, str(name), success=False, error=f"unknown command {name!r}")


def main() -> None:
    if os.environ.get("FAKE_AGENT_IGNORE_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child_pid_file = os.environ.get("FAKE_AGENT_CHILD_PID_FILE")
    if child_pid_file:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(child_pid_file, "w", encoding="utf-8") as pid_file:
            pid_file.write(str(child.pid))
    if os.environ.get("FAKE_AGENT_NO_HELLO"):
        pass
    elif os.environ.get("FAKE_AGENT_BAD_PROTOCOL"):
        send({"type": "hello", "protocol": 99, "agent": "fake", "capabilities": {}})
    else:
        send(
            {
                "type": "hello",
                "protocol": 1,
                "agent": "fake",
                "version": "0.1",
                "session_id": "fake-session",
                "capabilities": {
                    "prompt": True,
                    "abort": True,
                    "dialogs": ["confirm"],
                    "models": {"list": True, "set": True},
                    "snapshot": True,
                    "history": {"paging": True},
                    "usage": {"tokens": True, "cost": True},
                    "tools": {"streaming": True, "updates": True},
                },
                # Declared commands: backend features the shared view renders
                # without knowing what they mean.
                "commands": [
                    {
                        "name": "echo",
                        "title": "Echo",
                        "description": "Echo text back",
                        "args": [{"name": "text", "type": "string", "required": True}],
                    },
                    {"name": "boom", "title": "Boom", "danger": True},
                ],
                "help": [{"name": "/login", "reason": "no authentication here"}],
            }
        )
    while True:
        line = sys.stdin.readline()
        if not line:
            if os.environ.get("FAKE_AGENT_IGNORE_EOF"):
                time.sleep(60)
                continue
            break
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(command, dict):
            continue
        try:
            handle(command)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - test helper must stay alive
            print(f"fake agent error: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()

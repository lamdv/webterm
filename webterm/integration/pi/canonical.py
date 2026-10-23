"""Translate pi RPC records into webterm's canonical agent protocol.

This module is deliberately side-effect free.  It does not move the pi session,
change the browser protocol, or replace :class:`PiSession`; it is the adapter
layer that lets the canonical ``AgentSession`` model describe a real pi run.

Two kinds of translation live here:

* :class:`PiCanonicalTranslator` consumes pi session events and emits canonical
  events.  It is stateful because pi's ``message_update`` records are deltas
  without a message id.
* ``*_to_canonical`` helpers translate pi snapshots, history pages, model lists,
  and extension UI records.  They are pure functions.

The canonical names and capability shape are defined in
:mod:`webterm.agent.protocol` and documented in ``docs/agent-protocol.md``.
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

from ...agent.protocol import (
    IMAGE_COMMANDS,
    CanonicalCommandError,
    Capabilities,
    CommandArg,
    DeclaredCommand,
    HelpEntry,
    response_record,
    validate_images,
)

#: Capabilities of the pi adapter.  pi exposes more than the canonical core
#: requires; the extra features stay available to pi-specific UI.
PI_CAPABILITIES = Capabilities(
    prompt=True,
    abort=True,
    steering=True,
    follow_up=True,
    queue=True,
    dialogs=("confirm", "select", "input", "editor"),
    models_list=True,
    models_set=True,
    thinking=True,
    snapshot=True,
    history_paging=True,
    resume=True,
    export_native=True,
    usage_tokens=True,
    usage_cost=True,
    tool_streaming=True,
    tool_updates=True,
    compaction=True,
    attachments_images=True,
    raw=True,
)


def pi_capabilities() -> Capabilities:
    """Return the capability declaration for a pi adapter session."""
    return PI_CAPABILITIES


# ---------------------------------------------------------------------------
# Shared conversion helpers
# ---------------------------------------------------------------------------


def _text_of(content: object) -> str:
    """Extract text from a pi content array, tool result, or plain value."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if "content" in content:
            return _text_of(content["content"])
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("output"), str):
            return content["output"]
    return "" if content is None else str(content)


def _canonical_block(block: object) -> dict:
    """Convert one pi content block to a canonical block.

    Known blocks are normalized; unknown blocks are preserved with their shape
    so an adapter-specific renderer can still inspect them.
    """
    if not isinstance(block, dict):
        return {"type": "text", "text": _text_of(block)}
    kind = block.get("type")
    if kind == "text":
        return {"type": "text", "text": str(block.get("text", ""))}
    if kind == "thinking":
        return {
            "type": "thinking",
            "text": str(block.get("thinking", block.get("text", ""))),
            "redacted": bool(block.get("redacted", False)),
        }
    if kind == "toolCall":
        arguments = block.get("arguments")
        return {
            "type": "tool_call",
            "call_id": str(block.get("id", "")),
            "name": str(block.get("name", "")),
            "args": arguments if isinstance(arguments, dict) else {},
        }
    if kind == "image":
        return {
            "type": "image",
            "data": block.get("data", ""),
            "mime_type": block.get("mimeType", ""),
        }
    return dict(block)


def _canonical_content(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [_canonical_block(block) for block in content]
    if isinstance(content, dict):
        return [_canonical_block(content)]
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def _model_name(model: object) -> str | None:
    """Return a display id from pi's full model object."""
    if isinstance(model, str):
        return model or None
    if isinstance(model, dict):
        for key in ("id", "name"):
            value = model.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _canonical_usage(usage: object) -> dict | None:
    """Convert pi usage to a canonical ``usage`` event payload."""
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("input")
    completion = usage.get("output")
    total = usage.get("totalTokens")
    if not isinstance(prompt, int) or isinstance(prompt, bool):
        prompt = 0
    if not isinstance(completion, int) or isinstance(completion, bool):
        completion = 0
    if not isinstance(total, int) or isinstance(total, bool):
        total = prompt + completion
    cost: float | None = None
    cost_data = usage.get("cost")
    if isinstance(cost_data, dict):
        value = cost_data.get("total")
        if isinstance(value, int | float) and not isinstance(value, bool):
            cost = float(value)
    elif isinstance(cost_data, int | float) and not isinstance(cost_data, bool):
        cost = float(cost_data)
    payload: dict[str, Any] = {
        "event": "usage",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    if cost is not None:
        payload["cost"] = cost
    return payload


def canonical_model(model: object) -> dict | None:
    """Return a browser-safe canonical model object."""
    if not isinstance(model, dict):
        return None
    identifier = model.get("id")
    provider = model.get("provider")
    if not isinstance(identifier, str) or not identifier:
        return None
    if not isinstance(provider, str) or not provider:
        return None
    result: dict[str, Any] = {"id": identifier, "provider": provider}
    for source, target in (
        ("name", "name"),
        ("contextWindow", "context_window"),
        ("maxTokens", "max_tokens"),
        ("reasoning", "reasoning"),
        ("input", "input"),
    ):
        if source in model:
            result[target] = model[source]
    return result


def _canonical_entries(entries: object) -> list[dict]:
    """Convert pi session entries into canonical transcript entries."""
    if not isinstance(entries, list):
        return []
    converted: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        converted.append(
            _canonical_message(
                message,
                entry_id=identifier,
                timestamp=entry.get("timestamp"),
            )
        )
    return converted


def _canonical_message(
    message: dict,
    *,
    entry_id: str,
    timestamp: object = None,
) -> dict:
    """Convert one pi AgentMessage into a canonical transcript entry."""
    role = message.get("role")
    if role == "toolResult":
        canonical_role = "tool"
    elif role == "bashExecution":
        canonical_role = "tool"
    elif isinstance(role, str) and role:
        canonical_role = role
    else:
        canonical_role = "unknown"

    content = _canonical_content(message.get("content"))
    if role == "bashExecution":
        content = [
            {
                "type": "text",
                "text": str(message.get("output", message.get("command", ""))),
            }
        ]
    result: dict[str, Any] = {
        "id": entry_id,
        "role": canonical_role,
        "content": content,
    }
    if timestamp is not None:
        result["timestamp"] = timestamp
    elif message.get("timestamp") is not None:
        result["timestamp"] = message["timestamp"]

    model = _model_name(message.get("model"))
    if model:
        result["model"] = model
    stop_reason = message.get("stopReason")
    if isinstance(stop_reason, str):
        result["stop_reason"] = stop_reason
    if role == "toolResult" or role == "bashExecution":
        result["tool_call_id"] = message.get("toolCallId", "")
        result["tool_name"] = message.get("toolName", "bash" if role == "bashExecution" else "")
        result["is_error"] = bool(message.get("isError", False))
        if "details" in message:
            result["details"] = message["details"]
    if role == "custom" and isinstance(message.get("customType"), str):
        result["custom_type"] = message["customType"]
    if isinstance(message.get("usage"), dict):
        usage = _canonical_usage(message["usage"])
        if usage is not None:
            result["usage"] = {key: value for key, value in usage.items() if key != "event"}
    return result


# ---------------------------------------------------------------------------
# Live event translation
# ---------------------------------------------------------------------------


class PiCanonicalTranslator:
    """Translate a stream of pi session events into canonical events.

    The translator is not thread-safe; feed it from one event loop.  It assigns
    live message ids because pi's ``message_update`` records do not carry one.
    A snapshot later replaces those ids with durable pi entry ids.
    """

    def __init__(self) -> None:
        self._run_counter = 0
        self._run_id: str | None = None
        self._last_run_id: str | None = None
        self._message_counter = 0
        self._assistant_id: str | None = None
        self._current_id: str | None = None
        self._current_role: str | None = None
        self._last_block: tuple[int, str] | None = None
        self._last_usage: dict | None = None
        self._usage_emitted = False

    # -- run / message state ------------------------------------------------

    def _new_run(self, events: list[dict]) -> str:
        """Start a fresh low-level run; retries produce a new run id."""
        self._run_counter += 1
        self._run_id = f"run-{self._run_counter}"
        self._last_run_id = self._run_id
        self._last_usage = None
        self._usage_emitted = False
        events.append({"event": "run.started", "run_id": self._run_id})
        return self._run_id

    def _begin_run(self, events: list[dict]) -> str:
        """Ensure there is a run for event kinds that may be missing agent_start."""
        return self._run_id or self._new_run(events)

    def _new_message_id(self) -> str:
        self._message_counter += 1
        return f"live-{self._message_counter}"

    def _ensure_assistant(self, events: list[dict]) -> str:
        if self._assistant_id is None:
            self._assistant_id = self._new_message_id()
            self._current_id = self._assistant_id
            self._current_role = "assistant"
            self._last_block = None
            events.append(
                {
                    "event": "message.started",
                    "message": {
                        "id": self._assistant_id,
                        "role": "assistant",
                        "content": [],
                    },
                }
            )
        return self._assistant_id

    def _block_index(self, index: object, block_type: str) -> int:
        if isinstance(index, int) and not isinstance(index, bool):
            self._last_block = (index, block_type)
            return index
        if self._last_block is None:
            self._last_block = (0, block_type)
            return 0
        last_index, last_type = self._last_block
        if last_type != block_type:
            self._last_block = (last_index + 1, block_type)
            return last_index + 1
        return last_index

    def _message_event(
        self,
        events: list[dict],
        name: str,
        **fields: object,
    ) -> None:
        message_id = self._ensure_assistant(events)
        events.append({"event": name, "message_id": message_id, **fields})

    # -- public entry point -------------------------------------------------

    def feed(self, record: object) -> list[dict]:
        """Consume one pi record and return zero or more canonical events."""
        if not isinstance(record, dict):
            return []
        kind = record.get("type")
        events: list[dict] = []

        if kind == "agent_start":
            self._new_run(events)

        elif kind == "agent_end":
            run_id = self._begin_run(events)
            if not self._usage_emitted:
                usage = self._last_usage
                if usage is not None:
                    events.append(usage)
                    self._usage_emitted = True
            events.append(
                {
                    "event": "run.finished",
                    "run_id": run_id,
                    "reason": "stop",
                    "will_retry": bool(record.get("willRetry", False)),
                }
            )
            self._run_id = None

        elif kind == "agent_settled":
            run_id = self._run_id or self._last_run_id
            if run_id is None:
                run_id = self._new_run(events)
            events.append(
                {
                    "event": "run.settled",
                    "run_id": run_id,
                    "aborted": bool(record.get("aborted", False)),
                }
            )
            self._run_id = None

        elif kind == "message_start":
            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") == "system":
                return events
            self._begin_run(events)
            self._message_counter += 1
            message_id = f"live-{self._message_counter}"
            role = message.get("role")
            self._current_id = message_id
            self._current_role = role if isinstance(role, str) else None
            if role == "assistant":
                self._assistant_id = message_id
                self._last_block = None
            events.append(
                {
                    "event": "message.started",
                    "message": _canonical_message(message, entry_id=message_id),
                }
            )

        elif kind == "message_update":
            self._begin_run(events)
            usage = _canonical_usage(record.get("usage"))
            if usage is not None:
                self._last_usage = usage
            self._update_stream(record.get("assistantMessageEvent"), events)

        elif kind == "message_end":
            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") == "system":
                return events
            self._begin_run(events)
            role = message.get("role")
            message_id = self._current_id
            if role == "assistant" and self._assistant_id:
                message_id = self._assistant_id
            if message_id is None or (
                isinstance(role, str) and self._current_role not in (None, role)
            ):
                message_id = self._new_message_id()
            events.append(
                {
                    "event": "message.finished",
                    "message": _canonical_message(message, entry_id=message_id),
                }
            )
            usage = _canonical_usage(message.get("usage")) or self._last_usage
            if usage is not None and not self._usage_emitted:
                events.append(usage)
                self._usage_emitted = True
            if role == "assistant":
                self._assistant_id = None
            self._current_id = None
            self._current_role = None
            self._last_block = None

        elif kind == "tool_execution_start":
            self._begin_run(events)
            events.append(
                {
                    "event": "tool.started",
                    "call_id": str(record.get("toolCallId", "")),
                    "name": str(record.get("toolName", "")),
                    "args": record.get("args")
                    if isinstance(record.get("args"), dict)
                    else {},
                }
            )

        elif kind == "tool_execution_update":
            self._begin_run(events)
            events.append(
                {
                    "event": "tool.updated",
                    "call_id": str(record.get("toolCallId", "")),
                    "delta": _text_of(record.get("partialResult")),
                }
            )

        elif kind == "tool_execution_end":
            self._begin_run(events)
            events.append(
                {
                    "event": "tool.finished",
                    "call_id": str(record.get("toolCallId", "")),
                    "name": str(record.get("toolName", "")),
                    "output": _text_of(record.get("result")),
                    "is_error": bool(record.get("isError", False)),
                    "duration_ms": record.get("durationMs"),
                }
            )

        elif kind == "queue_update":
            events.append(
                {
                    "event": "queue.updated",
                    "steering": record.get("steering")
                    if isinstance(record.get("steering"), list)
                    else [],
                    "follow_up": record.get("followUp")
                    if isinstance(record.get("followUp"), list)
                    else [],
                }
            )

        elif kind == "session_info_changed":
            name = record.get("name")
            events.append(
                {
                    "event": "session.info",
                    "name": name if isinstance(name, str) and name else None,
                }
            )

        elif kind == "thinking_level_changed":
            level = record.get("level")
            events.append(
                {
                    "event": "session.info",
                    "thinking_level": level if isinstance(level, str) else None,
                }
            )

        elif kind == "compaction_start":
            reason = record.get("reason")
            events.append(
                {
                    "event": "notice",
                    "level": "info",
                    "code": "pi.compaction",
                    "text": f"compacting conversation ({reason})"
                    if isinstance(reason, str)
                    else "compacting conversation",
                }
            )

        elif kind == "compaction_end":
            result = record.get("result")
            error = record.get("errorMessage")
            if isinstance(error, str) and error:
                events.append(
                    {
                        "event": "notice",
                        "level": "error",
                        "code": "pi.compaction",
                        "text": f"compaction failed: {error}",
                    }
                )
            else:
                tokens = result.get("tokensBefore") if isinstance(result, dict) else None
                events.append(
                    {
                        "event": "notice",
                        "level": "info",
                        "code": "pi.compaction",
                        "text": (
                            f"compacted {tokens} tokens"
                            if isinstance(tokens, int) and not isinstance(tokens, bool)
                            else "compaction finished"
                        ),
                    }
                )

        elif kind == "auto_retry_start":
            events.append(
                {
                    "event": "notice",
                    "level": "warning",
                    "code": "pi.retry",
                    "text": "retrying after an error"
                    f" (attempt {record.get('attempt', '?')})",
                }
            )

        elif kind == "auto_retry_end":
            success = bool(record.get("success", False))
            events.append(
                {
                    "event": "notice",
                    "level": "info" if success else "error",
                    "code": "pi.retry",
                    "text": "retry succeeded" if success else "retry failed",
                }
            )

        elif kind in (
            "summarization_retry_scheduled",
            "summarization_retry_attempt_start",
            "summarization_retry_finished",
        ):
            events.append(
                {
                    "event": "notice",
                    "level": "warning",
                    "code": "pi.summarization_retry",
                    "text": "summarization retry",
                }
            )

        elif kind == "extension_error":
            events.append(
                {
                    "event": "notice",
                    "level": "error",
                    "code": "pi.extension_error",
                    "text": str(record.get("error", "extension error")),
                }
            )

        elif kind in ("turn_start", "turn_end"):
            # Turns are an implementation detail in canonical terms; the
            # run/message events already describe the same boundary.
            pass

        elif kind in ("entry_appended", "bash_execution_update"):
            events.append({"event": "raw", "protocol": "pi", "record": record})

        elif kind == "extension_ui_request":
            # Blocking dialogs are handled by ``pi_dialog_to_canonical`` via
            # the PiSubscriber path; fire-and-forget methods map to notices.
            events.extend(pi_ui_request_to_events(record))

        elif kind == "extension_ui_response":
            # pi's own acknowledgement of an extension response is not part of
            # the canonical event stream.
            pass

        else:
            events.append({"event": "raw", "protocol": "pi", "record": record})

        return events

    def _update_stream(self, event: object, events: list[dict]) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        content_index = event.get("contentIndex")
        if kind in ("text_start", "text_delta", "text_end"):
            block_type = "text"
        elif kind in ("thinking_start", "thinking_delta", "thinking_end"):
            block_type = "thinking"
        elif kind in ("toolcall_start", "toolcall_delta", "toolcall_end"):
            block_type = "tool_call"
        else:
            # ``start``, ``done`` and ``error`` are provider lifecycle records
            # that pi's normal loop converts to message_start/message_end.
            return
        index = self._block_index(content_index, block_type)

        if kind == "text_start":
            self._message_event(
                events,
                "message.block",
                block_index=index,
                block={"type": "text", "text": ""},
            )
        elif kind == "text_delta":
            self._message_event(
                events,
                "message.delta",
                block_index=index,
                block_type=block_type,
                delta=str(event.get("delta", "")),
            )
        elif kind == "text_end":
            self._message_event(
                events,
                "message.block",
                block_index=index,
                block={"type": "text", "text": str(event.get("content", ""))},
            )
        elif kind == "thinking_start":
            self._message_event(
                events,
                "message.block",
                block_index=index,
                block={"type": "thinking", "text": ""},
            )
        elif kind == "thinking_delta":
            self._message_event(
                events,
                "message.delta",
                block_index=index,
                block_type=block_type,
                delta=str(event.get("delta", "")),
            )
        elif kind == "thinking_end":
            self._message_event(
                events,
                "message.block",
                block_index=index,
                block={
                    "type": "thinking",
                    "text": str(event.get("content", "")),
                },
            )
        elif kind == "toolcall_start":
            self._message_event(
                events,
                "message.block",
                block_index=index,
                block={
                    "type": "tool_call",
                    "call_id": str(event.get("id", "")),
                    "name": str(event.get("toolName", "")),
                    "args": {},
                },
            )
        elif kind == "toolcall_delta":
            self._message_event(
                events,
                "message.delta",
                block_index=index,
                block_type=block_type,
                delta=str(event.get("delta", "")),
            )
        elif kind == "toolcall_end":
            call = event.get("toolCall")
            call = call if isinstance(call, dict) else {}
            arguments = call.get("arguments")
            self._message_event(
                events,
                "message.block",
                block_index=index,
                block={
                    "type": "tool_call",
                    "call_id": str(call.get("id", "")),
                    "name": str(call.get("name", "")),
                    "args": arguments if isinstance(arguments, dict) else {},
                },
            )


def translate_pi_event(
    record: object, translator: PiCanonicalTranslator | None = None
) -> list[dict]:
    """Convenience wrapper for one-off translation.

    Streaming consumers should keep a :class:`PiCanonicalTranslator` instead of
    creating a new one per record.
    """
    return (translator or PiCanonicalTranslator()).feed(record)


# ---------------------------------------------------------------------------
# Snapshots, history, models
# ---------------------------------------------------------------------------


def pi_snapshot_to_canonical(
    snapshot: object,
    *,
    tokens: int = 0,
    cost: float = 0.0,
) -> dict:
    """Convert ``PiSession.snapshot()`` into the canonical snapshot shape."""
    data = snapshot if isinstance(snapshot, dict) else {}
    page = data.get("entries")
    page = page if isinstance(page, dict) else {}
    entries = _canonical_entries(page.get("entries"))
    state = data.get("state")
    state = state if isinstance(state, dict) else {}
    ui = data.get("ui")
    ui = ui if isinstance(ui, dict) else {}
    requests = ui.get("requests")
    dialogs = [
        dialog
        for request in (requests if isinstance(requests, list) else [])
        if (dialog := pi_dialog_to_canonical(request)) is not None
    ]
    first_entry_id = page.get("firstEntryId")
    if not isinstance(first_entry_id, str):
        first_entry_id = entries[0]["id"] if entries else None
    streaming = data.get("streaming")
    return {
        "entries": entries,
        "has_more": bool(page.get("hasMore", False)),
        "first_entry_id": first_entry_id,
        "state": pi_state_to_canonical(
            state,
            tokens=tokens,
            cost=cost,
            streaming=streaming if isinstance(streaming, bool) else None,
            settled=bool(data.get("settled", True)),
        ),
        "dialogs": dialogs,
        "ui": ui,
        "capabilities": PI_CAPABILITIES.to_dict(),
        "cursor": {"seq": data.get("seq", 0)},
    }


def pi_state_to_canonical(
    state: object,
    *,
    tokens: int = 0,
    cost: float = 0.0,
    streaming: bool | None = None,
    settled: bool | None = None,
) -> dict:
    """Convert pi's ``get_state`` payload into the canonical session state."""
    data = state if isinstance(state, dict) else {}
    if streaming is None:
        streaming = bool(data.get("isStreaming", False))
    return {
        "model": _model_name(data.get("model")),
        "thinking_level": data.get("thinkingLevel"),
        "session_file": data.get("sessionFile"),
        "name": data.get("sessionName"),
        "streaming": streaming,
        "settled": (not streaming) if settled is None else settled,
        "tokens": tokens,
        "cost": cost,
        "pending_message_count": data.get("pendingMessageCount", 0),
    }


def pi_history_to_canonical(page: object) -> dict:
    """Convert a ``PiSession.history()`` page into a canonical history page."""
    data = page if isinstance(page, dict) else {}
    entries = _canonical_entries(data.get("entries"))
    first_entry_id = data.get("firstEntryId")
    if not isinstance(first_entry_id, str):
        first_entry_id = entries[0]["id"] if entries else None
    return {
        "entries": entries,
        "has_more": bool(data.get("hasMore", False)),
        "first_entry_id": first_entry_id,
    }


def pi_models_to_canonical(payload: object) -> dict:
    """Convert a pi model probe payload into the canonical model-list shape."""
    data = payload if isinstance(payload, dict) else {}
    raw_models = data.get("models")
    models = [
        model
        for raw in (raw_models if isinstance(raw_models, list) else [])
        if (model := canonical_model(raw)) is not None
    ]
    default = data.get("default")
    if isinstance(default, dict):
        default = canonical_model(default)
    return {
        "models": models,
        "default": default,
        "fetched_at": data.get("fetchedAt"),
        "cwd": data.get("cwd"),
        "cached": data.get("cached"),
    }


# ---------------------------------------------------------------------------
# Extension UI
# ---------------------------------------------------------------------------


def pi_dialog_to_canonical(record: object) -> dict | None:
    """Convert a blocking pi extension UI request to a canonical dialog.

    Returns ``None`` for fire-and-forget methods (``notify``, ``setStatus``,
    ``setWidget``, ``setTitle``, ``set_editor_text``) and unknown methods.
    """
    if not isinstance(record, dict):
        return None
    method = record.get("method")
    if method not in ("confirm", "select", "input", "editor"):
        return None
    dialog_id = record.get("id")
    if not isinstance(dialog_id, str) or not dialog_id:
        return None
    dialog: dict[str, Any] = {
        "type": "dialog_request",
        "id": dialog_id,
        "kind": method,
    }
    title = record.get("title")
    if isinstance(title, str):
        dialog["title"] = title
    message = record.get("message")
    if isinstance(message, str):
        dialog["message"] = message
    options = record.get("options")
    if isinstance(options, list):
        dialog["options"] = options
    timeout = record.get("timeout")
    if isinstance(timeout, int) and not isinstance(timeout, bool):
        dialog["timeout_ms"] = timeout
    for source in ("placeholder", "prefill"):
        value = record.get(source)
        if isinstance(value, str):
            dialog[source] = value
    return dialog


def pi_ui_request_to_events(record: object) -> list[dict]:
    """Map fire-and-forget pi extension UI records to canonical events."""
    if not isinstance(record, dict):
        return []
    method = record.get("method")
    if method == "notify":
        level = record.get("notifyType")
        if level not in ("info", "warning", "error"):
            level = "info"
        return [
            {
                "event": "notice",
                "level": level,
                "code": "pi.notify",
                "text": str(record.get("message", "")),
            }
        ]
    if method == "setTitle":
        title = record.get("title")
        return [
            {
                "event": "session.info",
                "title": title if isinstance(title, str) else None,
            }
        ]
    if method in ("setStatus", "setWidget", "set_editor_text"):
        return [{"event": "raw", "protocol": "pi", "record": record}]
    return []


def pi_ui_resolved_to_canonical(record: object) -> dict | None:
    """Convert a pi extension UI resolution to a canonical ``dialog_resolved``."""
    if not isinstance(record, dict):
        return None
    dialog_id = record.get("id")
    if not isinstance(dialog_id, str) or not dialog_id:
        return None
    reason = record.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = "agent"
    # pi uses "response" for the normal answered path; canonical uses
    # "answered" so clients do not need pi-specific vocabulary.
    if reason == "response":
        reason = "answered"
    return {"type": "dialog_resolved", "id": dialog_id, "reason": reason}


def pi_dialog_response_to_pi(kind: str, response: dict) -> dict:
    """Translate a canonical dialog response into pi's response shape."""
    if response.get("cancelled") is True:
        return {"cancelled": True}
    if kind == "confirm":
        if isinstance(response.get("approved"), bool):
            return {"confirmed": response["approved"]}
        if isinstance(response.get("confirmed"), bool):
            return {"confirmed": response["confirmed"]}
        return {"confirmed": False}
    if "value" in response:
        return {"value": response["value"]}
    return {"cancelled": True}


# ---------------------------------------------------------------------------
# Canonical commands
# ---------------------------------------------------------------------------

#: Canonical commands the pi adapter answers with exactly one RPC call.
PI_DIRECT_COMMANDS = frozenset(
    {
        "prompt",
        "steer",
        "follow_up",
        "abort",
        "get_state",
        "set_model",
        "set_thinking",
        "set_session_name",
        "compact",
        "clear_queue",
        "raw",
    }
)

#: Canonical commands that need :class:`PiSession` state, not one RPC call.
#: ``export`` is here because the server owns the download route and the
#: managed export directory, even though pi does the HTML rendering.
PI_SESSION_COMMANDS = frozenset(
    {"get_snapshot", "get_history", "list_models", "dialog_response", "shutdown"}
)

#: Every canonical command this adapter understands.  ``command`` is resolved
#: by the server against :data:`PI_DECLARED_COMMANDS`, so it belongs to neither
#: translation set.
PI_COMMANDS = PI_DIRECT_COMMANDS | PI_SESSION_COMMANDS | frozenset({"command"})

#: pi operations the browser can invoke through the core ``command`` record.
#: They are declared in the resync manifest, so the shared view renders them
#: without knowing what a "fork" or a session tree is.
PI_DECLARED_COMMANDS = (
    DeclaredCommand(
        name="fork",
        title="Fork from a message",
        description="Branch the session from an earlier user message",
        args=(
            CommandArg(
                name="entry_id",
                title="Entry",
                description="Entry id of the user message to branch from",
                required=True,
            ),
        ),
    ),
    DeclaredCommand(
        name="clone",
        title="Clone branch",
        description="Duplicate the active branch into a new session",
    ),
    DeclaredCommand(
        name="export",
        title="Export session",
        description="Download the conversation",
        args=(
            CommandArg(
                name="format",
                type="choice",
                title="Format",
                required=True,
                choices=(("html", "HTML"), ("jsonl", "JSONL")),
            ),
        ),
    ),
    DeclaredCommand(
        name="tree",
        title="Session tree",
        description="Show the branch structure",
    ),
)

#: pi features with no RPC equivalent; the view lists them in its help dialog.
PI_HELP = (
    HelpEntry("/login, /logout", "provider sign-in is a TUI-only screen"),
    HelpEntry("/settings", "settings are edited through pi's own UI"),
    HelpEntry("/trust", "project trust is chosen before the process starts"),
    HelpEntry("/scoped-models", "per-scope model allowlists live in settings"),
    HelpEntry("/share", "publishing a session has no RPC command"),
    HelpEntry("/bug", "bug-report collection is interactive"),
    HelpEntry("/changelog", "release notes are a TUI viewer"),
    HelpEntry("extension screens", "ctx.ui.custom() has no RPC representation"),
)

#: pi RPC records a browser may never smuggle through ``raw``.  Dialog
#: responses are correlated and validated by the server instead.
PI_RAW_DENYLIST = frozenset({"extension_ui_response"})

#: pi's ``streamingBehavior`` for the canonical mid-run commands.
_STREAMING_BEHAVIOR = {"steer": "steer", "follow_up": "followUp"}


def _require_message(command: dict) -> str:
    message = command.get("message")
    if not isinstance(message, str) or not message.strip():
        raise CanonicalCommandError("prompt message must be a non-empty string")
    return message


def _pi_images(command: dict) -> list[dict[str, str]]:
    """Translate a command's canonical ``images`` into pi ``ImageContent``."""
    return [
        {"type": "image", "data": image["data"], "mimeType": image["mime_type"]}
        for image in validate_images(command)
    ]


def pi_command_images(command: dict) -> list[dict[str, str]]:
    """Validate a *pi-shaped* ``prompt`` command's ``images`` array.

    The legacy ``pi`` envelope bypasses the canonical translation, so the
    server validates it here with the same limits before pi's stdin sees it.
    Returns the images as canonical entries.
    """
    raw = command.get("images")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CanonicalCommandError("images must be a list of image objects")
    canonical: list[dict] = []
    for entry in raw:
        if isinstance(entry, dict):
            canonical.append(
                {"mime_type": entry.get("mimeType"), "data": entry.get("data")}
            )
        else:
            canonical.append({})
    return validate_images({"images": canonical})


def pi_command_from_canonical(command: dict) -> dict:
    """Translate one canonical command record into a pi RPC command.

    ``command`` is a flat canonical record such as
    ``{"type": "set_model", "model": "gpt", "provider": "openai"}``.  Only the
    commands in :data:`PI_DIRECT_COMMANDS` translate to a single RPC call;
    snapshot, history, models, dialogs, and shutdown need session state and are
    handled by the server.

    Raises :class:`CanonicalCommandError` for an unknown command, a missing
    field, or a ``raw`` record that must stay server-owned.
    """
    if not isinstance(command, dict):
        raise CanonicalCommandError("command must be an object")
    name = command.get("type")
    if not isinstance(name, str) or not name:
        raise CanonicalCommandError("command.type is required")
    if name not in PI_DIRECT_COMMANDS:
        if name in PI_SESSION_COMMANDS:
            raise CanonicalCommandError(f"{name} is resolved from session state")
        raise CanonicalCommandError(f"unsupported agent command {name!r}")
    if name != "raw" and name not in IMAGE_COMMANDS and command.get("images") is not None:
        raise CanonicalCommandError(f"{name} does not accept images")

    if name in ("prompt", "steer", "follow_up"):
        rpc: dict[str, Any] = {"type": "prompt", "message": _require_message(command)}
        images = _pi_images(command)
        if images:
            rpc["images"] = images
        behavior = _STREAMING_BEHAVIOR.get(name)
        if behavior is not None:
            rpc["streamingBehavior"] = behavior
        return rpc
    if name == "abort":
        return {"type": "abort"}
    if name == "get_state":
        return {"type": "get_state"}
    if name == "set_model":
        model = command.get("model")
        if not isinstance(model, str) or not model.strip():
            raise CanonicalCommandError("set_model needs a model id")
        rpc = {"type": "set_model", "modelId": model}
        provider = command.get("provider")
        if isinstance(provider, str) and provider.strip():
            rpc["provider"] = provider
        return rpc
    if name == "set_thinking":
        level = command.get("level")
        if not isinstance(level, str) or not level.strip():
            raise CanonicalCommandError("set_thinking needs a level")
        return {"type": "set_thinking_level", "level": level}
    if name == "set_session_name":
        session_name = command.get("name")
        if not isinstance(session_name, str):
            raise CanonicalCommandError("set_session_name needs a name")
        return {"type": "set_session_name", "name": session_name}
    if name == "compact":
        return {"type": "compact"}
    if name == "clear_queue":
        return {"type": "clear_queue"}
    # raw
    native = command.get("command")
    if not isinstance(native, dict):
        raise CanonicalCommandError("raw needs a native command object")
    native_type = native.get("type")
    if not isinstance(native_type, str) or not native_type:
        raise CanonicalCommandError("raw command.type is required")
    if native_type in PI_RAW_DENYLIST:
        raise CanonicalCommandError(f"raw {native_type} is server-owned")
    return dict(native)


def canonical_command_data(name: str, data: object) -> dict:
    """Normalize a pi RPC ``data`` payload for a canonical command response."""
    payload = data if isinstance(data, dict) else {}
    if name in ("prompt", "steer", "follow_up"):
        if name in _STREAMING_BEHAVIOR:
            # A mid-run command is queued by definition, whatever pi echoes.
            return {"disposition": "queued"}
        disposition = payload.get("disposition")
        if disposition not in ("started", "queued", "handled"):
            disposition = "started"
        return {"disposition": disposition}
    if name == "set_model":
        model = _model_name(payload)
        result: dict[str, Any] = {} if model is None else {"model": model}
        provider = payload.get("provider")
        if isinstance(provider, str) and provider:
            result["provider"] = provider
        return result
    return dict(payload)


def _preview(text: str, limit: int = 120) -> str:
    """Collapse whitespace and clip text for a one-line label."""
    collapsed = " ".join(str(text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def pi_declared_rpc(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Translate a declared pi command into the one pi RPC record it needs."""
    if name == "fork":
        entry_id = args.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise CanonicalCommandError("fork needs the entry_id of a user message")
        return {"type": "fork", "entryId": entry_id}
    if name == "clone":
        return {"type": "clone"}
    if name == "tree":
        return {"type": "get_tree"}
    raise CanonicalCommandError(f"no pi rpc for declared command {name!r}")


def _tree_label(entry: object) -> str:
    data = entry if isinstance(entry, dict) else {}
    message = data.get("message")
    if isinstance(message, dict):
        role = message.get("role") or "message"
        text = _preview(_text_of(message.get("content")))
        return f"{role}: {text}" if text else str(role)
    kind = data.get("type")
    name = data.get("name") or data.get("id") or "entry"
    return f"{kind} {name}" if kind else str(name)


def pi_tree_to_canonical(data: object) -> dict[str, Any]:
    """Convert pi's ``get_tree`` payload into the generic tree render hint."""
    payload = data if isinstance(data, dict) else {}

    def node(item: object) -> dict[str, Any] | None:
        branch = item if isinstance(item, dict) else {}
        entry = branch.get("entry")
        record: dict[str, Any] = {}
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(entry_id, str) and entry_id:
            record["id"] = entry_id
            record["actions"] = [
                {
                    "name": "fork",
                    "label": "Fork here",
                    "command": {"name": "fork", "args": {"entry_id": entry_id}},
                }
            ]
        record["label"] = _tree_label(entry)
        children = [
            child
            for raw in (branch.get("children") or [])
            if (child := node(raw)) is not None
        ]
        if children:
            record["children"] = children
        return record if record.get("label") else None

    nodes = [
        item
        for raw in (payload.get("tree") or [])
        if (item := node(raw)) is not None
    ]
    leaf_id = payload.get("leafId")
    tree: dict[str, Any] = {"nodes": nodes}
    if isinstance(leaf_id, str) and leaf_id:
        tree["leaf_id"] = leaf_id
    return tree


def pi_declared_result(name: str, response: object) -> dict[str, Any]:
    """Build the render-hint ``data`` for a declared pi command's answer."""
    payload = response if isinstance(response, dict) else {}
    if payload.get("success") is False:
        raise CanonicalCommandError(
            str(payload.get("error") or f"{name} failed")
        )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if name == "fork":
        if data.get("cancelled"):
            return {
                "notice": {
                    "level": "warning",
                    "text": "the fork was cancelled by an extension",
                }
            }
        text = _text_of(data.get("text"))
        result: dict[str, Any] = {"refresh": True}
        if text:
            result["notice"] = {
                "level": "info",
                "text": f"forked from: {_preview(text)}",
            }
        return result
    if name == "clone":
        if data.get("cancelled"):
            return {
                "notice": {
                    "level": "warning",
                    "text": "the clone was cancelled by an extension",
                }
            }
        return {
            "refresh": True,
            "notice": {"level": "info", "text": "cloned into a new session"},
        }
    if name == "tree":
        return {"tree": pi_tree_to_canonical(data)}
    raise CanonicalCommandError(f"no result rendering for declared command {name!r}")


async def pi_export(
    session: Any, manager: Any, fmt: str
) -> dict[str, Any]:
    """Write or resolve a downloadable file and describe it.

    Shared by the legacy ``pi_export`` request and the canonical ``export``
    declared command, so both answer the same path and download URL.
    """
    if fmt not in ("html", "jsonl"):
        raise CanonicalCommandError("export format must be 'html' or 'jsonl'")
    root = getattr(manager, "pi_session_dir", None)
    if not root:
        raise CanonicalCommandError("pi session storage is not configured")
    if fmt == "html":
        export_dir = os.path.join(root, "exports")
        os.makedirs(export_dir, mode=0o700, exist_ok=True)
        output_path = os.path.join(export_dir, f"{session.id}-{time.time_ns()}.html")
        try:
            response = await session.rpc(
                {"type": "export_html", "outputPath": output_path}
            )
        except Exception as exc:  # noqa: BLE001 - reported as a refusal
            raise CanonicalCommandError(str(exc)) from exc
        if not response.get("success"):
            raise CanonicalCommandError(str(response.get("error", "export failed")))
        data = response.get("data") or {}
        path = manager.resolve_pi_path(data.get("path") or output_path)
    else:
        try:
            response = await session.rpc({"type": "get_state"})
        except Exception as exc:  # noqa: BLE001 - reported as a refusal
            raise CanonicalCommandError(str(exc)) from exc
        if not response.get("success"):
            raise CanonicalCommandError(str(response.get("error", "get_state failed")))
        path = manager.resolve_pi_path((response.get("data") or {}).get("sessionFile"))
    if path is None or not os.path.isfile(path):
        raise CanonicalCommandError("pi produced no downloadable file")
    return {
        "format": fmt,
        "path": path,
        "url": "/pi/download?path=" + quote(path, safe=""),
    }


def canonical_response(
    name: str,
    command_id: str,
    response: object,
    *,
    tokens: int = 0,
    cost: float = 0.0,
) -> dict:
    """Build a canonical ``response`` record from a pi RPC response."""
    payload = response if isinstance(response, dict) else {}
    if payload.get("success") is False:
        return response_record(
            name, command_id, error=str(payload.get("error") or f"{name} failed")
        )
    data = payload.get("data")
    if name == "get_state":
        return response_record(
            name,
            command_id,
            data=pi_state_to_canonical(data, tokens=tokens, cost=cost),
        )
    return response_record(name, command_id, data=canonical_command_data(name, data))

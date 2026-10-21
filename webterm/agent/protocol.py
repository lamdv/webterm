"""Canonical JSONL protocol shared by webterm and structured agents.

The protocol is deliberately small.  It describes *orchestration*: a run, its
messages, tool activity, dialogs, usage, and a replayable transcript.  It does
not describe how an agent stores sessions, compacts context, or renders its own
special records; those stay in the agent and reach clients through ``raw``.

Records are newline-delimited JSON.  The four record families are:

``hello``
    Sent by the agent once at startup, carrying the protocol version and
    capability declaration.
``response``
    Answers a command, correlated by ``id``.
``event``
    Streams agent activity.  ``event`` selects the canonical name.
``dialog_request`` / ``dialog_resolved``
    Ask the human a question, or report that a pending question is gone.

Commands are flat records whose ``type`` is the command name, e.g.
``{"type": "prompt", "id": "...", "message": "..."}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Current protocol version.  Agents must send this in ``hello``.
PROTOCOL_VERSION = 1
#: Versions a client accepts.  Keep old versions working while agents catch up.
SUPPORTED_PROTOCOLS = frozenset({1})

# ---------------------------------------------------------------------------
# Canonical event names
# ---------------------------------------------------------------------------

RUN_STARTED = "run.started"
RUN_FINISHED = "run.finished"
RUN_SETTLED = "run.settled"
MESSAGE_STARTED = "message.started"
MESSAGE_DELTA = "message.delta"
MESSAGE_BLOCK = "message.block"
MESSAGE_FINISHED = "message.finished"
TOOL_STARTED = "tool.started"
TOOL_UPDATED = "tool.updated"
TOOL_FINISHED = "tool.finished"
QUEUE_UPDATED = "queue.updated"
USAGE = "usage"
SESSION_INFO = "session.info"
NOTICE = "notice"
RAW = "raw"

EVENT_NAMES = frozenset(
    {
        RUN_STARTED,
        RUN_FINISHED,
        RUN_SETTLED,
        MESSAGE_STARTED,
        MESSAGE_DELTA,
        MESSAGE_BLOCK,
        MESSAGE_FINISHED,
        TOOL_STARTED,
        TOOL_UPDATED,
        TOOL_FINISHED,
        QUEUE_UPDATED,
        USAGE,
        SESSION_INFO,
        NOTICE,
        RAW,
    }
)

#: Content block types used by canonical messages.
BLOCK_TYPES = frozenset({"text", "thinking", "tool_call", "image", "file"})

#: Dialog kinds a core adapter may need to validate.
DIALOG_KINDS = frozenset({"confirm", "select", "input", "editor", "notify"})

#: Commands every canonical agent answers.
CORE_COMMANDS = frozenset(
    {
        "prompt",
        "abort",
        "get_state",
        "get_snapshot",
        "get_history",
        "list_models",
        "set_model",
        "shutdown",
    }
)

#: Capability-gated commands: an adapter may refuse them with ``success:false``.
OPTIONAL_COMMANDS = frozenset(
    {
        "steer",
        "follow_up",
        "clear_queue",
        "set_thinking",
        "set_system_prompt",
        "set_session_name",
        "compact",
        "fork",
        "clone",
        "switch_session",
        "export",
        "dialog_response",
        "raw",
    }
)

#: Adapter-specific commands for a plain-text system-prompt library.  An
#: adapter advertises them with the ``prompt_library`` capability; one that
#: does not simply answers ``success:false``.
PROMPT_LIBRARY_COMMANDS = frozenset(
    {
        "list_prompts",
        "save_prompt",
        "delete_prompt",
    }
)

#: Every command a canonical agent may receive.
CANONICAL_COMMANDS = CORE_COMMANDS | OPTIONAL_COMMANDS | PROMPT_LIBRARY_COMMANDS


class CanonicalCommandError(ValueError):
    """A canonical command an adapter cannot express.

    This is a validation error, not an agent refusal: the wire answer is a
    transport error asking the caller to fix the command, whereas an agent
    that chooses not to run a valid command answers ``success:false``.
    """


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def _flag(source: object, key: str, default: bool = False) -> bool:
    """Read a boolean from a mapping, ignoring non-boolean values."""
    if isinstance(source, dict):
        value = source.get(key)
        if isinstance(value, bool):
            return value
    return default


def _section(source: object, key: str) -> dict:
    if isinstance(source, dict):
        value = source.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _flag_from(
    source: object, key: str, section: object, nested: str, default: bool = False
) -> bool:
    """Read a flag from a flat key, then a nested section, then *default*."""
    if isinstance(source, dict) and isinstance(source.get(key), bool):
        return bool(source[key])
    if isinstance(section, dict) and isinstance(section.get(nested), bool):
        return bool(section[nested])
    return default


@dataclass(frozen=True)
class Capabilities:
    """What an agent can do, declared in ``hello``.

    The browser uses this to hide controls rather than discovering missing
    features by failure.  A field may be ``False`` even when the agent is
    capable; that only means the adapter does not expose it.
    """

    prompt: bool = True
    abort: bool = True
    steering: bool = False
    follow_up: bool = False
    queue: bool = False
    dialogs: tuple[str, ...] = ()
    models_list: bool = False
    models_set: bool = False
    thinking: bool = False
    #: Levels accepted by ``set_thinking``.  Empty means the adapter has no
    #: fixed vocabulary; the client falls back to the canonical pi-style set.
    thinking_levels: tuple[str, ...] = ()
    system_prompt: bool = False
    snapshot: bool = True
    history_paging: bool = False
    resume: bool = False
    export_native: bool = False
    usage_tokens: bool = False
    usage_cost: bool = False
    tool_streaming: bool = True
    tool_updates: bool = True
    fork: bool = False
    clone: bool = False
    compaction: bool = False
    attachments_images: bool = False
    raw: bool = False
    #: The adapter serves a plain-text system-prompt library
    #: (``list_prompts`` / ``save_prompt`` / ``delete_prompt``).
    prompt_library: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: object) -> Capabilities:
        """Build capabilities from an untrusted agent declaration."""
        source = raw if isinstance(raw, dict) else {}
        models = _section(source, "models")
        history = _section(source, "history")
        export = _section(source, "export")
        usage = _section(source, "usage")
        tools = _section(source, "tools")
        attachments = _section(source, "attachments")

        levels: tuple[str, ...] = ()
        raw_levels = models.get("levels") if isinstance(models, dict) else None
        if not isinstance(raw_levels, list):
            raw_levels = source.get("thinking_levels")
        if isinstance(raw_levels, list):
            levels = tuple(
                value for value in raw_levels if isinstance(value, str) and value
            )

        dialogs: tuple[str, ...] = ()
        raw_dialogs = source.get("dialogs")
        if isinstance(raw_dialogs, list):
            dialogs = tuple(
                value for value in raw_dialogs if isinstance(value, str) and value in DIALOG_KINDS
            )
        elif raw_dialogs is True:
            dialogs = tuple(sorted(DIALOG_KINDS))

        return cls(
            prompt=_flag(source, "prompt", True),
            abort=_flag(source, "abort", True),
            steering=_flag(source, "steering"),
            follow_up=_flag(source, "follow_up"),
            queue=_flag(source, "queue"),
            dialogs=dialogs,
            models_list=_flag_from(source, "models_list", models, "list"),
            models_set=_flag_from(source, "models_set", models, "set"),
            thinking=_flag_from(source, "thinking", models, "thinking"),
            thinking_levels=levels,
            system_prompt=_flag(source, "system_prompt"),
            snapshot=_flag(source, "snapshot", True),
            history_paging=_flag_from(source, "history_paging", history, "paging"),
            resume=_flag(source, "resume"),
            export_native=_flag_from(source, "export_native", export, "native"),
            usage_tokens=_flag_from(source, "usage_tokens", usage, "tokens"),
            usage_cost=_flag_from(source, "usage_cost", usage, "cost"),
            tool_streaming=_flag_from(source, "tool_streaming", tools, "streaming", True),
            tool_updates=_flag_from(source, "tool_updates", tools, "updates", True),
            fork=_flag(source, "fork"),
            clone=_flag(source, "clone"),
            compaction=_flag(source, "compaction"),
            attachments_images=_flag_from(source, "attachments_images", attachments, "images"),
            raw=_flag(source, "raw"),
            prompt_library=_flag(source, "prompt_library"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the nested shape used on the wire."""
        return {
            "prompt": self.prompt,
            "abort": self.abort,
            "steering": self.steering,
            "follow_up": self.follow_up,
            "queue": self.queue,
            "dialogs": list(self.dialogs),
            "models": {
                "list": self.models_list,
                "set": self.models_set,
                "thinking": self.thinking,
                "levels": list(self.thinking_levels),
            },
            "system_prompt": self.system_prompt,
            "snapshot": self.snapshot,
            "history": {"paging": self.history_paging},
            "resume": self.resume,
            "export": {"native": self.export_native},
            "usage": {"tokens": self.usage_tokens, "cost": self.usage_cost},
            "tools": {"streaming": self.tool_streaming, "updates": self.tool_updates},
            "fork": self.fork,
            "clone": self.clone,
            "compaction": self.compaction,
            "attachments": {"images": self.attachments_images},
            "raw": self.raw,
            "prompt_library": self.prompt_library,
        }

    def supports_dialog(self, kind: str) -> bool:
        """Return True when *kind* is an advertised dialog method."""
        return kind in self.dialogs


# ---------------------------------------------------------------------------
# Hello
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    """The agent sent a record that violates the canonical protocol."""


@dataclass(frozen=True)
class Hello:
    """The agent's startup declaration."""

    protocol: int
    agent: str
    capabilities: Capabilities
    session_id: str | None = None
    version: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def parse_hello(record: object) -> Hello:
    """Validate and return an agent's ``hello`` record."""
    if not isinstance(record, dict):
        raise ProtocolError("hello must be an object")
    protocol = record.get("protocol")
    if not isinstance(protocol, int) or isinstance(protocol, bool):
        raise ProtocolError("hello.protocol must be an integer")
    if protocol not in SUPPORTED_PROTOCOLS:
        supported = ", ".join(str(value) for value in sorted(SUPPORTED_PROTOCOLS))
        raise ProtocolError(f"unsupported protocol version {protocol} (supported: {supported})")
    agent = record.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        raise ProtocolError("hello.agent must be a non-empty string")
    session_id = record.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise ProtocolError("hello.session_id must be a string")
    version = record.get("version")
    if version is not None and not isinstance(version, str):
        raise ProtocolError("hello.version must be a string")
    return Hello(
        protocol=protocol,
        agent=agent.strip(),
        capabilities=Capabilities.from_dict(record.get("capabilities")),
        session_id=session_id,
        version=version,
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def command_name(command: dict[str, Any]) -> str:
    """Return a command's name, raising if it is malformed."""
    name = command.get("type")
    if not isinstance(name, str) or not name:
        raise ProtocolError("command.type must be a non-empty string")
    return name


def response_record(
    name: str, command_id: str, *, data: dict | None = None, error: str | None = None
) -> dict[str, Any]:
    """Build one canonical ``response`` record.

    A success carries ``data`` (possibly empty); a failure carries ``error`` and
    no data, so clients cannot mistake a refusal for a result.
    """
    record: dict[str, Any] = {
        "type": "response",
        "id": command_id,
        "command": name,
        "success": error is None,
    }
    if error is None:
        record["data"] = data or {}
    else:
        record["error"] = error
    return record


def validate_event(event: object) -> dict[str, Any]:
    """Validate a canonical ``event`` payload and return it.

    Unknown event names are allowed: a newer agent may emit events this
    webterm does not model yet.  The core forwards them and keeps them in the
    replay ring, so clients can ignore or render them.
    """
    if not isinstance(event, dict):
        raise ProtocolError("event must be an object")
    name = event.get("event")
    if not isinstance(name, str) or not name:
        raise ProtocolError("event.event must be a non-empty string")
    if "seq" in event:
        raise ProtocolError("agents must not set event.seq; the core assigns it")
    return event


def validate_snapshot(data: object) -> dict[str, Any]:
    """Validate the data returned by a ``get_snapshot`` command."""
    if not isinstance(data, dict):
        raise ProtocolError("snapshot data must be an object")
    entries = data.get("entries")
    if entries is not None and not isinstance(entries, list):
        raise ProtocolError("snapshot.entries must be a list")
    return data


def validate_page(data: object) -> dict[str, Any]:
    """Validate the data returned by a ``get_history`` command."""
    if not isinstance(data, dict):
        raise ProtocolError("history data must be an object")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ProtocolError("history.entries must be a list")
    return data


def validate_models(data: object) -> dict[str, Any]:
    """Validate the data returned by a ``list_models`` command."""
    if not isinstance(data, dict):
        raise ProtocolError("models data must be an object")
    models = data.get("models")
    if not isinstance(models, list):
        raise ProtocolError("models.models must be a list")
    return data


def validate_dialog_request(record: object) -> dict[str, Any]:
    """Validate an agent's ``dialog_request`` record."""
    if not isinstance(record, dict):
        raise ProtocolError("dialog_request must be an object")
    dialog_id = record.get("id")
    if not isinstance(dialog_id, str) or not dialog_id:
        raise ProtocolError("dialog_request.id must be a non-empty string")
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in DIALOG_KINDS:
        raise ProtocolError(f"dialog_request.kind must be one of {sorted(DIALOG_KINDS)}")
    message = record.get("message")
    if message is not None and not isinstance(message, str):
        raise ProtocolError("dialog_request.message must be a string")
    options = record.get("options")
    if options is not None and not isinstance(options, list):
        raise ProtocolError("dialog_request.options must be a list")
    timeout = record.get("timeout_ms")
    if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool)):
        raise ProtocolError("dialog_request.timeout_ms must be an integer")
    return record

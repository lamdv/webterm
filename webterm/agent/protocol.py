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

import base64
import binascii
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
TRANSCRIPT_TRUNCATED = "transcript.truncated"
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
        TRANSCRIPT_TRUNCATED,
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
        "command",
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
        "edit_message",
        "set_session_name",
        "compact",
        "switch_session",
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

#: Commands that may carry an ``images`` field.
IMAGE_COMMANDS = frozenset({"prompt", "steer", "follow_up"})

#: Image media types a canonical command may carry, matching what pi accepts.
IMAGE_MIME_TYPES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})

#: Most images one command may carry.
MAX_IMAGE_COUNT = 4

#: Largest decoded image accepted, in bytes.  Base64 inflates by ~4/3, so this
#: is about 4 MiB once encoded.
MAX_IMAGE_BYTES = 3 * 1024 * 1024

#: Largest decoded image payload accepted per command.  Encoded, that is about
#: 5.6 MiB, which leaves room under the 8 MiB WebSocket frame cap
#: (``websocket.DEFAULT_MAX_MESSAGE``) for the JSON envelope.
MAX_IMAGES_BYTES = 4 * 1024 * 1024

#: Base64 characters one image of :data:`MAX_IMAGE_BYTES` occupies.
_MAX_IMAGE_BASE64_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)


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
    compaction: bool = False
    attachments_images: bool = False
    raw: bool = False
    #: The adapter serves a plain-text system-prompt library
    #: (``list_prompts`` / ``save_prompt`` / ``delete_prompt``).
    prompt_library: bool = False
    #: The adapter accepts ``edit_message``: rewrite a past user entry,
    #: dropping the entries after it, and run from the edited prompt.
    edit_message: bool = False
    #: ``edit_message`` also accepts an ``assistant`` entry: rewrite the answer
    #: in place, dropping the entries after it, without starting a run.
    edit_answer: bool = False
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
            compaction=_flag(source, "compaction"),
            attachments_images=_flag_from(source, "attachments_images", attachments, "images"),
            raw=_flag(source, "raw"),
            prompt_library=_flag(source, "prompt_library"),
            edit_message=_flag(source, "edit_message"),
            edit_answer=_flag(source, "edit_answer"),
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
            "compaction": self.compaction,
            "attachments": {"images": self.attachments_images},
            "raw": self.raw,
            "prompt_library": self.prompt_library,
            "edit_message": self.edit_message,
            "edit_answer": self.edit_answer,
        }

    def supports_dialog(self, kind: str) -> bool:
        """Return True when *kind* is an advertised dialog method."""
        return kind in self.dialogs


# ---------------------------------------------------------------------------
# Declared commands and help
# ---------------------------------------------------------------------------

#: Argument types a declared command may use.  The browser renders a control per
#: type and treats an unknown type as a plain string.
ARG_TYPES = frozenset({"string", "choice", "boolean"})


@dataclass(frozen=True)
class CommandArg:
    """One argument of an adapter-declared command."""

    name: str
    type: str = "string"
    title: str = ""
    description: str = ""
    required: bool = False
    #: ``(value, label)`` pairs for ``type="choice"``.
    choices: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DeclaredCommand:
    """A backend feature the browser can invoke without knowing its meaning.

    The canonical view lists these in its palette and sends
    ``{"type":"command","name":...,"args":{...}}``; only the adapter interprets
    the name.  This is what keeps one backend's vocabulary out of the shared UI.
    """

    name: str
    title: str = ""
    description: str = ""
    args: tuple[CommandArg, ...] = ()
    danger: bool = False


@dataclass(frozen=True)
class HelpEntry:
    """A backend feature that cannot work through the protocol.

    ``/help`` lists them, so a missing feature reads as a documented gap
    instead of a broken button.
    """

    name: str
    reason: str = ""


def _parse_command_args(raw: object, command: str) -> tuple[CommandArg, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProtocolError(f"hello.commands[{command!r}].args must be a list")
    args: list[CommandArg] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ProtocolError(f"hello.commands[{command!r}] args must be objects")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError(f"hello.commands[{command!r}] arg needs a name")
        kind = entry.get("type", "string")
        if not isinstance(kind, str):
            raise ProtocolError(f"hello.commands[{command!r}].{name}.type must be a string")
        if kind not in ARG_TYPES:
            raise ProtocolError(
                f"unsupported arg type {kind!r} (allowed: {', '.join(sorted(ARG_TYPES))})"
            )
        choices: list[tuple[str, str]] = []
        raw_choices = entry.get("choices")
        if isinstance(raw_choices, list):
            for choice in raw_choices:
                if isinstance(choice, dict):
                    value = choice.get("value")
                    label = choice.get("label", value)
                    if isinstance(value, str) and isinstance(label, str):
                        choices.append((value, label))
                elif isinstance(choice, str):
                    choices.append((choice, choice))
        args.append(
            CommandArg(
                name=name.strip(),
                type=kind,
                title=str(entry.get("title") or ""),
                description=str(entry.get("description") or ""),
                required=bool(entry.get("required", False)),
                choices=tuple(choices),
            )
        )
    return tuple(args)


def parse_declared_commands(raw: object) -> tuple[DeclaredCommand, ...]:
    """Validate an adapter's declared-command manifest."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProtocolError("hello.commands must be a list")
    commands: list[DeclaredCommand] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ProtocolError("hello.commands entries must be objects")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError("hello.commands entries need a name")
        name = name.strip()
        if name in seen:
            raise ProtocolError(f"duplicate declared command {name!r}")
        seen.add(name)
        title = entry.get("title")
        description = entry.get("description")
        commands.append(
            DeclaredCommand(
                name=name,
                title=title if isinstance(title, str) else name,
                description=description if isinstance(description, str) else "",
                args=_parse_command_args(entry.get("args"), name),
                danger=bool(entry.get("danger", False)),
            )
        )
    return tuple(commands)


def parse_help_entries(raw: object) -> tuple[HelpEntry, ...]:
    """Validate an adapter's declared help/gap list."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProtocolError("hello.help must be a list")
    entries: list[HelpEntry] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ProtocolError("hello.help entries must be objects")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError("hello.help entries need a name")
        reason = entry.get("reason")
        entries.append(
            HelpEntry(
                name=name.strip(),
                reason=reason if isinstance(reason, str) else "",
            )
        )
    return tuple(entries)


def find_declared_command(
    commands: tuple[DeclaredCommand, ...] | list[DeclaredCommand] | object, name: str
) -> DeclaredCommand | None:
    """Return the declared command called *name*, or None."""
    for command in commands if isinstance(commands, (tuple, list)) else ():
        if command.name == name:
            return command
    return None


def declared_command_record(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the canonical ``command`` record that invokes a declared command."""
    return {"type": "command", "name": name, "args": dict(args or {})}


def parse_command_invocation(command: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return ``(name, args)`` for a ``command`` record, validated."""
    name = command.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProtocolError("command needs the name of a declared command")
    raw_args = command.get("args")
    if raw_args is None:
        return name.strip(), {}
    if not isinstance(raw_args, dict):
        raise ProtocolError("command args must be an object")
    return name.strip(), dict(raw_args)


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
    #: Adapter features the browser may invoke without knowing their meaning.
    commands: tuple[DeclaredCommand, ...] = ()
    #: Backend features that cannot work through the protocol.
    help: tuple[HelpEntry, ...] = ()
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
        commands=parse_declared_commands(record.get("commands")),
        help=parse_help_entries(record.get("help")),
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


#: Magic bytes a declared image type must start with.
_IMAGE_MAGIC = {
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
}


def _looks_like_image(mime_type: str, blob: bytes) -> bool:
    """Return True when *blob* carries *mime_type*'s signature."""
    if mime_type == "image/webp":
        return len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"
    return blob.startswith(_IMAGE_MAGIC[mime_type])


def _reject_image_size(index: int) -> None:
    limit = MAX_IMAGE_BYTES // (1024 * 1024)
    raise CanonicalCommandError(f"image {index} is too large: at most {limit} MiB")


def validate_images(command: dict[str, Any]) -> list[dict[str, str]]:
    """Validate a command's optional ``images`` field and return it normalized.

    The canonical shape is ``[{"mime_type": "image/png", "data": "<base64>"}]``
    with raw base64, no data-URL prefix.  A missing or empty field means no
    images.  Every problem raises :class:`CanonicalCommandError`, because an
    image is either delivered whole or the command is refused: dropping one
    would let a prompt claim to show the agent something it never saw.
    """
    raw = command.get("images")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CanonicalCommandError(
            "images must be a list of {mime_type, data} objects"
        )
    if len(raw) > MAX_IMAGE_COUNT:
        raise CanonicalCommandError(
            f"too many images: at most {MAX_IMAGE_COUNT} per message"
        )
    images: list[dict[str, str]] = []
    total = 0
    for position, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise CanonicalCommandError(f"image {position} must be an object")
        mime_type = entry.get("mime_type")
        if not isinstance(mime_type, str) or not mime_type:
            raise CanonicalCommandError(f"image {position} needs a mime_type")
        if mime_type not in IMAGE_MIME_TYPES:
            allowed = ", ".join(sorted(IMAGE_MIME_TYPES))
            raise CanonicalCommandError(
                f"unsupported image type {mime_type!r} (allowed: {allowed})"
            )
        data = entry.get("data")
        if not isinstance(data, str) or not data:
            raise CanonicalCommandError(f"image {position} needs base64 data")
        if data.startswith("data:"):
            raise CanonicalCommandError(
                f"image {position} must carry raw base64, not a data URL"
            )
        if len(data) > _MAX_IMAGE_BASE64_CHARS:
            _reject_image_size(position)
        try:
            blob = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CanonicalCommandError(
                f"image {position} is not valid base64"
            ) from exc
        if len(blob) > MAX_IMAGE_BYTES:
            _reject_image_size(position)
        total += len(blob)
        if total > MAX_IMAGES_BYTES:
            limit = MAX_IMAGES_BYTES // (1024 * 1024)
            raise CanonicalCommandError(
                f"images are too large: at most {limit} MiB in total"
            )
        if not _looks_like_image(mime_type, blob):
            raise CanonicalCommandError(
                f"image {position} is not a {mime_type} image"
            )
        images.append({"mime_type": mime_type, "data": data})
    return images


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

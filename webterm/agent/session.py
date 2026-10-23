"""Generic structured-agent sessions.

An :class:`AgentSession` owns a child process that speaks the canonical JSONL
protocol in :mod:`webterm.agent.protocol`.  It is deliberately independent of
any particular agent: command correlation, event replay, dialogs, and state are
core; an adapter can later translate a native protocol (such as pi's RPC mode)
into the same canonical records.

The process and transport lifecycle (pipes, readers, event ring, timers,
two-phase close) lives in :class:`webterm.agent.process.AgentProcess`; this
module keeps only the canonical dialect: record parsing and validation, the
hello handshake, run/usage state, and dialogs.

The class is a sibling of :class:`webterm.session.Session`, not a subclass: it
shares the lifecycle surface (``id``, ``pid``, ``attach``, ``detach``,
``close``, ``wait``, lineage metadata) but has no PTY, scrollback bytes, or
terminal resize.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol

from .process import AgentProcess
from .protocol import (
    Capabilities,
    Hello,
    ProtocolError,
    parse_hello,
    serialize_commands,
    serialize_help,
    validate_dialog_request,
    validate_event,
    validate_models,
    validate_page,
    validate_snapshot,
)

log = logging.getLogger("webterm")

#: Transport tuning lives on :class:`~webterm.agent.process.AgentProcess`.
READ_SIZE = AgentProcess.READ_SIZE
MAX_LINE = AgentProcess.MAX_LINE
EVENT_RING_BYTES = AgentProcess.EVENT_RING_BYTES
EVENT_RING_MIN = AgentProcess.EVENT_RING_MIN
#: Time given to an agent to exit after stdin is closed.
AGENT_STDIN_GRACE = AgentProcess.AGENT_STDIN_GRACE
#: Time given after SIGTERM.
AGENT_TERM_GRACE = AgentProcess.AGENT_TERM_GRACE
#: Time given after SIGKILL before the session is locally torn down.
AGENT_FORCE_GRACE = AgentProcess.AGENT_FORCE_GRACE
DEFAULT_HANDSHAKE_TIMEOUT = 10.0
DEFAULT_COMMAND_TIMEOUT = 30.0
#: Number of malformed records tolerated before the session is torn down.
MAX_PROTOCOL_ERRORS = 20


class AgentError(Exception):
    """An agent operation failed (protocol, timeout, or process state)."""


class AgentClosed(AgentError):
    """The agent process is gone and can no longer answer commands."""


class AgentTimeout(AgentError):
    """An agent command did not answer within its deadline."""


class AgentProtocolError(AgentError):
    """The agent did not follow the canonical protocol."""


class AgentCommandError(AgentError):
    """The agent answered a command with ``success: false``."""

    def __init__(self, command: str, message: str) -> None:
        super().__init__(f"{command}: {message}")
        self.command = command
        self.message = message


class AgentSubscriber(Protocol):
    """Receives structured events from an agent session it is attached to."""

    def offer_agent_event(self, session: AgentSession, seq: int, event: dict) -> None: ...

    def offer_agent_dialog(self, session: AgentSession, request: dict) -> None: ...

    def offer_agent_dialog_resolved(
        self, session: AgentSession, dialog_id: str, reason: str
    ) -> None: ...

    def offer_exit(self, session: AgentSession) -> None: ...


class AgentSession(AgentProcess):
    """A child process speaking the canonical JSONL agent protocol.

    Pipes, replay, timers, and the close escalation come from
    :class:`~webterm.agent.process.AgentProcess`; this dialect adds the
    ``hello`` handshake, canonical record validation, run/usage state, and the
    dialog queue.
    """

    kind = "agent"
    AGENT_STDIN_GRACE = AGENT_STDIN_GRACE
    AGENT_TERM_GRACE = AGENT_TERM_GRACE
    AGENT_FORCE_GRACE = AGENT_FORCE_GRACE

    def __init__(
        self,
        session_id: str,
        pid: int,
        stdin_fd: int,
        stdout_fd: int,
        stderr_fd: int,
        title: str,
        *,
        cwd: str,
        loop: asyncio.AbstractEventLoop,
        on_exit: Callable[[AgentSession], None] | None = None,
        on_change: Callable[[], None] | None = None,
        retain_on_exit: bool = True,
        origin: str = "user",
        parent_id: str | None = None,
        depth: int = 0,
        role: str = "agent",
        agent: str | None = None,
        task: str | None = None,
        visibility: str = "public",
        trusted: bool | None = None,
        adapter: str = "generic",
    ) -> None:
        self.adapter = adapter
        self.hello: Hello | None = None
        self.capabilities = Capabilities()
        self.agent_name: str | None = None
        self.agent_version: str | None = None
        self.agent_session_id: str | None = None
        self.session_file: str | None = None
        self._hello_event = asyncio.Event()
        self._hello_error: str | None = None
        self._dialogs: dict[str, dict] = {}
        self._dialog_timers: dict[str, asyncio.TimerHandle] = {}
        super().__init__(
            session_id,
            pid,
            stdin_fd,
            stdout_fd,
            stderr_fd,
            title,
            cwd=cwd,
            loop=loop,
            on_exit=on_exit,
            on_change=on_change,
            retain_on_exit=retain_on_exit,
            origin=origin,
            parent_id=parent_id,
            depth=depth,
            role=role,
            agent=agent,
            task=task,
            visibility=visibility,
            trusted=trusted,
        )

    # ------------------------------------------------------------- dialect

    def _closed_error(self, message: str) -> Exception:
        """Return the canonical "process is gone" exception."""
        return AgentClosed(message)

    def _dialect_error(self, message: str) -> Exception:
        """Return the canonical operation error."""
        return AgentError(message)

    @property
    def needs_input(self) -> bool:
        """Whether a canonical dialog is waiting for an answer."""
        return bool(self._dialogs)

    def info(self) -> dict[str, object]:
        info = super().info()
        info.update(
            {
                "agent_name": self.agent_name,
                "agent_version": self.agent_version,
                "agent_session_id": self.agent_session_id,
                "ready": self.ready,
                "settled": self.settled,
                "session_file": self.session_file,
                "capabilities": self.capabilities.to_dict(),
            }
        )
        return info

    def _on_marked_dead(self) -> None:
        self.ready = False

    def _wake_waiters(self) -> None:
        super()._wake_waiters()
        self._hello_event.set()

    def _cancel_deadlines(self) -> None:
        """Resolve pending dialogs as ``closed`` and drop their timers."""
        for dialog_id in list(self._dialogs):
            self._finish_dialog(dialog_id, "closed")
        for timer in self._dialog_timers.values():
            timer.cancel()
        self._dialog_timers.clear()

    def _request_shutdown(self) -> None:
        """Send the canonical ``shutdown`` command before stdin closes."""
        try:
            self._write_json({"type": "shutdown", "id": self._next_request_id()})
        except AgentError:
            pass

    # ------------------------------------------------------------ handshake

    async def start(self, timeout: float = DEFAULT_HANDSHAKE_TIMEOUT) -> None:
        """Wait for the agent's ``hello`` record.

        The child is spawned before this is called; a failed handshake is the
        caller's signal to close the session.  The error includes the stderr
        tail, which is usually where a broken agent explains itself.
        """
        if self.dead:
            raise AgentClosed(f"agent session {self.id} exited during startup")
        try:
            await asyncio.wait_for(self._hello_event.wait(), timeout)
        except TimeoutError as exc:
            detail = self._stderr_summary()
            raise AgentProtocolError(
                f"agent did not send hello within {timeout:g}s{detail}"
            ) from exc
        if self._hello_error is not None:
            raise AgentProtocolError(f"invalid hello: {self._hello_error}")
        if self.dead:
            raise AgentClosed(f"agent session {self.id} exited during startup")

    def _stderr_summary(self) -> str:
        if not self._stderr_tail:
            return ""
        return ": " + " | ".join(list(self._stderr_tail)[-3:])

    # ------------------------------------------------------------------- rpc

    async def command(
        self,
        name: str,
        /,
        *,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        **fields: Any,
    ) -> dict:
        """Send one command and return its ``data`` payload.

        The command name is positional-only so a command field may be called
        ``name`` (the prompt-library commands are).  Raises
        :class:`AgentCommandError` when the agent answers ``success: false``;
        the original command name and message are kept on the exception so
        callers can map them to request errors.
        """
        if self.dead:
            raise AgentClosed(f"agent session {self.id} is not running")
        command_id = self._next_request_id()
        request = {"type": name, "id": command_id, **fields}
        future: asyncio.Future[dict] = self._loop.create_future()
        self._pending[command_id] = future
        try:
            self._write_json(request)
            response = await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise AgentTimeout(f"{name} timed out after {timeout:g}s") from exc
        finally:
            self._pending.pop(command_id, None)

        if not response.get("success", False):
            message = response.get("error")
            if not isinstance(message, str) or not message:
                message = "command failed"
            raise AgentCommandError(name, message)
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    async def prompt(
        self,
        message: str,
        *,
        images: list[dict[str, str]] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> dict:
        """Send a prompt with optional validated images and return its disposition."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("prompt message must not be empty")
        self._settled_event.clear()
        request: dict[str, object] = {"message": message}
        if images:
            request["images"] = images
        try:
            data = await self.command("prompt", timeout=timeout, **request)
        except Exception:
            self._settled_event.set()
            raise
        disposition = data.get("disposition")
        if disposition == "handled":
            self._settled_event.set()
        return data

    async def abort(self, *, timeout: float = 5.0) -> dict:
        """Ask the agent to stop the current run."""
        return await self.command("abort", timeout=timeout)

    async def get_state(self, *, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> dict:
        return await self.command("get_state", timeout=timeout)

    async def snapshot(
        self,
        *,
        limit: int | None = 100,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> dict:
        """Return the authoritative transcript for a client attach.

        The returned ``cursor.seq`` is the sequence at the moment the request
        was made.  A client should ignore live events at or below it; events
        after it are not part of the snapshot and are applied normally.
        """
        async with self._snapshot_lock:
            cursor = self.seq
            data = await self.command("get_snapshot", timeout=timeout, limit=limit)
            snapshot = validate_snapshot(data)
            snapshot["cursor"] = {"seq": cursor}
            snapshot["dialogs"] = list(self._dialogs.values())
            snapshot["capabilities"] = self.capabilities.to_dict()
            # Adapter features travel as data so the shared view never learns a
            # backend's vocabulary: the manifest it renders, the gaps it lists.
            snapshot["commands"] = serialize_commands(
                self.hello.commands if self.hello is not None else ()
            )
            snapshot["help"] = serialize_help(
                self.hello.help if self.hello is not None else ()
            )
            agent_state = (
                dict(snapshot["state"])
                if isinstance(snapshot.get("state"), dict)
                else {}
            )
            agent_state.update(
                {
                    "streaming": self.streaming,
                    "settled": self.settled,
                    "model": self.model or agent_state.get("model"),
                    "tokens": self.tokens,
                    "cost": self.cost,
                    "session_file": self.session_file or agent_state.get("session_file"),
                }
            )
            snapshot["state"] = agent_state
            return snapshot

    async def history(
        self,
        before: str | None,
        *,
        limit: int = 100,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> dict:
        data = await self.command(
            "get_history",
            timeout=timeout,
            before=before,
            limit=limit,
        )
        return validate_page(data)

    async def models(
        self, *, refresh: bool = False, timeout: float = DEFAULT_COMMAND_TIMEOUT
    ) -> dict:
        data = await self.command("list_models", timeout=timeout, refresh=refresh)
        return validate_models(data)

    # --------------------------------------------------------------- dialogs

    def ui_snapshot(self) -> dict:
        """Return pending dialogs for attach/resync."""
        return {
            "dialogs": list(self._dialogs.values()),
            "needs_input": bool(self._dialogs),
        }

    def resolve_dialog(self, dialog_id: str, response: dict) -> None:
        """Answer a pending dialog; the first answer wins."""
        request = self._dialogs.get(dialog_id)
        if request is None:
            raise AgentError(f"unknown dialog {dialog_id!r}")
        if not isinstance(response, dict):
            raise AgentError("dialog response must be an object")
        self._send_dialog_response(dialog_id, response)
        self._finish_dialog(dialog_id, "answered")

    def _send_dialog_response(self, dialog_id: str, response: dict) -> None:
        try:
            self._write_json(
                {"type": "dialog_response", "id": dialog_id, "response": response}
            )
        except AgentClosed:
            pass

    def _finish_dialog(self, dialog_id: str, reason: str) -> None:
        if self._dialogs.pop(dialog_id, None) is None:
            return
        timer = self._dialog_timers.pop(dialog_id, None)
        if timer is not None:
            timer.cancel()
        for subscriber in list(self._subscribers):
            try:
                subscriber.offer_agent_dialog_resolved(self, dialog_id, reason)
            except Exception:  # noqa: BLE001 - one client must not break session
                log.exception("agent %s: dialog subscriber failed", self.id)
        self._changed()

    def _expire_dialog(self, dialog_id: str) -> None:
        if dialog_id not in self._dialogs:
            return
        self._send_dialog_response(dialog_id, {"cancelled": True})
        self._finish_dialog(dialog_id, "timeout")

    # --------------------------------------------------------------- inbound

    def _handle_line(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._protocol_error(f"invalid JSON record: {exc}")
            return
        if not isinstance(record, dict):
            self._protocol_error("record is not a JSON object")
            return
        kind = record.get("type")
        if kind == "hello":
            self._handle_hello(record)
        elif kind == "response":
            self._handle_response(record)
        elif kind == "event":
            self._handle_event(record)
        elif kind == "dialog_request":
            self._handle_dialog_request(record)
        elif kind == "dialog_resolved":
            self._handle_dialog_resolved(record)
        else:
            self._protocol_error(f"unknown record type {kind!r}")

    def _handle_hello(self, record: dict) -> None:
        if self.hello is not None:
            self._protocol_error("duplicate hello record")
            return
        try:
            hello = parse_hello(record)
        except ProtocolError as exc:
            self._protocol_error(str(exc))
            return
        self.hello = hello
        self.capabilities = hello.capabilities
        self.agent_name = hello.agent
        self.agent_version = hello.version
        self.agent_session_id = hello.session_id
        self.ready = True
        self._hello_event.set()
        self._changed()
        log.info(
            "agent %s (%s) ready: protocol %s, capabilities %s",
            self.id,
            hello.agent,
            hello.protocol,
            ",".join(
                name
                for name, enabled in hello.capabilities.to_dict().items()
                if enabled is True
            )
            or "minimal",
        )

    def _handle_response(self, record: dict) -> None:
        request_id = record.get("id")
        if not isinstance(request_id, str):
            self._protocol_error("response without a string id")
            return
        future = self._pending.get(request_id)
        if future is None:
            log.debug("agent %s: response for unknown command %s", self.id, request_id)
            return
        if not future.done():
            future.set_result(record)

    def _handle_event(self, record: dict) -> None:
        try:
            event = validate_event(record)
        except ProtocolError as exc:
            self._protocol_error(str(exc))
            return
        self.seq += 1
        seq = self.seq
        try:
            size = len(json.dumps(event, separators=(",", ":"), ensure_ascii=False))
        except (TypeError, ValueError):
            size = 0
        self._remember(seq, event, size)
        self._note_event(event)
        for subscriber in list(self._subscribers):
            try:
                subscriber.offer_agent_event(self, seq, event)
            except Exception:  # noqa: BLE001 - one client must not break session
                log.exception("agent %s: event subscriber failed", self.id)

    def _note_event(self, event: dict) -> None:
        name = event.get("event")
        changed = False
        if name == "run.started":
            if not self.streaming:
                self.streaming = True
                changed = True
            if self._settled_event.is_set():
                self._settled_event.clear()
                changed = True
        elif name == "run.finished":
            if self.streaming:
                self.streaming = False
                changed = True
        elif name == "run.settled":
            if self.streaming:
                self.streaming = False
                changed = True
            if not self._settled_event.is_set():
                self._settled_event.set()
                changed = True
        elif name == "usage":
            tokens = event.get("total_tokens")
            if isinstance(tokens, int) and not isinstance(tokens, bool):
                if tokens != self.tokens:
                    self.tokens = tokens
                    changed = True
            cost = event.get("cost")
            if isinstance(cost, int | float) and not isinstance(cost, bool):
                if float(cost) != self.cost:
                    self.cost = float(cost)
                    changed = True
        elif name == "session.info":
            model = event.get("model")
            if isinstance(model, str) and model != self.model:
                self.model = model
                changed = True
            session_file = event.get("session_file")
            if isinstance(session_file, str) and session_file != self.session_file:
                self.session_file = session_file
                changed = True
        # Streaming deltas arrive once per token and do not change any session
        # info; notifying for each one would rebroadcast the whole session list
        # to every client while an agent answers.
        if changed:
            self._changed()

    def _handle_dialog_request(self, record: dict) -> None:
        try:
            request = validate_dialog_request(record)
        except ProtocolError as exc:
            self._protocol_error(str(exc))
            return
        dialog_id = str(request["id"])
        if dialog_id in self._dialogs:
            self._protocol_error(f"duplicate dialog id {dialog_id!r}")
            return
        self._dialogs[dialog_id] = request
        timeout_ms = request.get("timeout_ms")
        if isinstance(timeout_ms, int) and timeout_ms > 0:
            self._dialog_timers[dialog_id] = self._loop.call_later(
                timeout_ms / 1000.0, self._expire_dialog, dialog_id
            )
        for subscriber in list(self._subscribers):
            try:
                subscriber.offer_agent_dialog(self, request)
            except Exception:  # noqa: BLE001
                log.exception("agent %s: dialog subscriber failed", self.id)
        self._changed()

    def _handle_dialog_resolved(self, record: dict) -> None:
        dialog_id = record.get("id")
        if not isinstance(dialog_id, str) or not dialog_id:
            self._protocol_error("dialog_resolved without an id")
            return
        reason = record.get("reason")
        self._finish_dialog(dialog_id, reason if isinstance(reason, str) else "agent")

    def _protocol_error(self, message: str) -> None:
        super()._protocol_error(message)
        if not self.ready and self._hello_error is None:
            # Surface a broken handshake immediately instead of making the
            # spawner wait for the full handshake timeout.
            self._hello_error = message
            self._hello_event.set()
        if self.protocol_errors == MAX_PROTOCOL_ERRORS:
            log.error(
                "agent %s: too many protocol errors; closing",
                self.id,
            )
            self.close()

    # ----------------------------------------------------------------- stderr

    def _log_stderr(self, line: str) -> None:
        if not line:
            return
        self._stderr_tail.append(line)
        log.warning("agent %s stderr: %s", self.id, line)

"""Generic structured-agent sessions.

An :class:`AgentSession` owns a child process that speaks the canonical JSONL
protocol in :mod:`webterm.agent.protocol`.  It is deliberately independent of
any particular agent: process lifecycle, command correlation, event replay,
dialogs, and state are core; an adapter can later translate a native protocol
(such as pi's RPC mode) into the same canonical records.

The class is a sibling of :class:`webterm.session.Session`, not a subclass: it
shares the lifecycle surface (``id``, ``pid``, ``attach``, ``detach``,
``close``, ``wait``, lineage metadata) but has no PTY, scrollback bytes, or
terminal resize.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import signal
import time
from collections.abc import Callable
from typing import Any, Protocol

from .protocol import (
    Capabilities,
    Hello,
    ProtocolError,
    parse_hello,
    validate_dialog_request,
    validate_event,
    validate_models,
    validate_page,
    validate_snapshot,
)

log = logging.getLogger("webterm")

READ_SIZE = 65_536
#: Largest partial stdout line retained before it is treated as a protocol
#: failure.  Tool results can be large; this bounds a broken or hostile child.
MAX_LINE = 16 * 1024 * 1024
#: Approximate byte cap for the replay ring used by short reconnects.
EVENT_RING_BYTES = 1_000_000
EVENT_RING_MIN = 64
DEFAULT_HANDSHAKE_TIMEOUT = 10.0
DEFAULT_COMMAND_TIMEOUT = 30.0
#: Time given to an agent to exit after stdin is closed.
AGENT_STDIN_GRACE = 0.5
#: Time given after SIGTERM.
AGENT_TERM_GRACE = 2.0
#: Time given after SIGKILL before the session is locally torn down.
AGENT_FORCE_GRACE = 1.5
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


class AgentSession:
    """A child process speaking the canonical JSONL agent protocol."""

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
        self.id = session_id
        self.pid = pid
        self.stdin_fd = stdin_fd
        self.stdout_fd = stdout_fd
        self.stderr_fd = stderr_fd
        self.title = title
        self.cwd = cwd
        self.cols = 0
        self.rows = 0
        self.exit_code: int | None = None
        self.dead = False
        self.closing = False
        self.ready = False
        self.retain_on_exit = retain_on_exit
        self.origin = origin
        self.parent_id = parent_id
        self.depth = depth
        self.role = role
        self.agent = agent
        self.task = task
        self.visibility = visibility
        self.trusted = trusted
        self.created_at = time.time()
        self.adapter = adapter

        self.hello: Hello | None = None
        self.capabilities = Capabilities()
        self.agent_name: str | None = None
        self.agent_version: str | None = None
        self.agent_session_id: str | None = None
        self.seq = 0
        self.streaming = False
        self.model: str | None = None
        self.tokens = 0
        self.cost = 0.0
        self.session_file: str | None = None
        self.protocol_errors = 0

        self._events: collections.deque[tuple[int, dict, int]] = collections.deque()
        self._event_bytes = 0
        self._subscribers: set[AgentSubscriber] = set()
        self._on_exit = on_exit
        self._on_change = on_change
        self._exit_event = asyncio.Event()
        #: Starts set because a fresh process is idle; prompt/run.started
        #: clears it, and run.settled sets it again.
        self._settled_event = asyncio.Event()
        self._settled_event.set()
        self._hello_event = asyncio.Event()
        self._hello_error: str | None = None
        self._loop = loop
        self._snapshot_lock = asyncio.Lock()

        self._stdin_open = True
        self._stdout_open = True
        self._stderr_open = True
        self._reaped = False
        self._stdout_buffer = bytearray()
        self._stderr_buffer = bytearray()
        self._out = bytearray()
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._command_counter = 0
        self._timers: list[asyncio.TimerHandle] = []
        self._dialogs: dict[str, dict] = {}
        self._dialog_timers: dict[str, asyncio.TimerHandle] = {}
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=50)

        for fd in (stdin_fd, stdout_fd, stderr_fd):
            os.set_blocking(fd, False)
        loop.add_reader(stdout_fd, self._on_stdout)
        loop.add_reader(stderr_fd, self._on_stderr)

    # ------------------------------------------------------------------ props

    @property
    def subscribers(self) -> set[AgentSubscriber]:
        return self._subscribers

    @property
    def settled(self) -> bool:
        return self._settled_event.is_set()

    def info(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "adapter": self.adapter,
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "agent_session_id": self.agent_session_id,
            "cols": self.cols,
            "rows": self.rows,
            "pid": self.pid,
            "alive": not self.dead,
            "ready": self.ready,
            "closing": self.closing,
            "exit_code": self.exit_code,
            "origin": self.origin,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "role": self.role,
            "agent": self.agent,
            "task": self.task,
            "visibility": self.visibility,
            "created_at": self.created_at,
            "cwd": self.cwd,
            "trusted": self.trusted,
            "needs_input": bool(self._dialogs),
            "streaming": self.streaming,
            "settled": self.settled,
            "model": self.model,
            "tokens": self.tokens,
            "cost": round(self.cost, 6),
            "session_file": self.session_file,
            "seq": self.seq,
            "capabilities": self.capabilities.to_dict(),
        }

    # ------------------------------------------------------------- lifecycle

    def attach(self, subscriber: AgentSubscriber) -> None:
        self._subscribers.add(subscriber)

    def detach(self, subscriber: AgentSubscriber) -> None:
        self._subscribers.discard(subscriber)

    def events_since(self, seq: int) -> list[tuple[int, dict]]:
        """Return retained live events with a sequence number greater than *seq*."""
        return [(event_seq, event) for event_seq, event, _ in self._events if event_seq > seq]

    def _remember(self, seq: int, event: dict, size: int) -> None:
        self._events.append((seq, event, size))
        self._event_bytes += size
        while self._event_bytes > EVENT_RING_BYTES and len(self._events) > EVENT_RING_MIN:
            _, _, old_size = self._events.popleft()
            self._event_bytes -= old_size

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

    def _write_json(self, record: dict) -> None:
        if not self._stdin_open:
            raise AgentClosed(f"agent session {self.id} stdin is closed")
        try:
            payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise AgentError(f"cannot encode command: {exc}") from exc
        self._out.extend(payload.encode("utf-8"))
        self._out.append(0x0A)
        self._flush()

    def _flush(self) -> None:
        while self._out and self._stdin_open:
            try:
                written = os.write(self.stdin_fd, self._out)
            except BlockingIOError:
                self._loop.add_writer(self.stdin_fd, self._flush)
                return
            except OSError as exc:
                self._close_stdin()
                raise AgentClosed(f"cannot write to agent {self.id}: {exc}") from exc
            if written <= 0:
                self._close_stdin()
                raise AgentClosed(f"cannot write to agent {self.id}")
            del self._out[:written]
        if not self._out:
            try:
                self._loop.remove_writer(self.stdin_fd)
            except (ValueError, OSError):
                pass

    def _close_stdin(self) -> None:
        if not self._stdin_open:
            return
        self._stdin_open = False
        try:
            self._loop.remove_writer(self.stdin_fd)
        except (ValueError, OSError):
            pass
        try:
            os.close(self.stdin_fd)
        except OSError:
            pass

    def send_command(self, command: dict) -> None:
        """Write one command without waiting for its response."""
        if self.dead:
            raise AgentClosed(f"agent session {self.id} is not running")
        if not isinstance(command, dict):
            raise AgentError("agent command must be an object")
        self._write_json(command)

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
        self._command_counter += 1
        command_id = f"webterm-{self.pid}-{self._command_counter}"
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

    async def prompt(self, message: str, *, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> dict:
        """Send a prompt and return the agent's acceptance disposition."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("prompt message must not be empty")
        self._settled_event.clear()
        try:
            data = await self.command("prompt", timeout=timeout, message=message)
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

    async def wait_for_settled(self, timeout: float | None = None) -> bool:
        """Wait until the agent will not continue automatically."""
        try:
            if timeout is None:
                await self._settled_event.wait()
            else:
                await asyncio.wait_for(self._settled_event.wait(), timeout)
            return True
        except TimeoutError:
            return False

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

    def _on_stdout(self) -> None:
        while self._stdout_open:
            try:
                data = os.read(self.stdout_fd, READ_SIZE)
            except BlockingIOError:
                return
            except OSError:
                self._stdout_open = False
                self._remove_reader(self.stdout_fd)
                return
            if not data:
                self._stdout_open = False
                self._remove_reader(self.stdout_fd)
                return
            self._feed(data)

    def _on_stdout_eof(self) -> None:
        self._stdout_open = False
        self._remove_reader(self.stdout_fd)

    def _feed(self, data: bytes) -> None:
        self._stdout_buffer.extend(data)
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._stdout_buffer[:newline]).rstrip(b"\r")
            del self._stdout_buffer[: newline + 1]
            self._handle_line(line)
        if len(self._stdout_buffer) > MAX_LINE:
            self._stdout_buffer.clear()
            self._protocol_error("stdout record exceeded the maximum line length")

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
        self.protocol_errors += 1
        log.warning("agent %s protocol error: %s", self.id, message)
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

    def _on_stderr(self) -> None:
        while self._stderr_open:
            try:
                data = os.read(self.stderr_fd, READ_SIZE)
            except BlockingIOError:
                return
            except OSError:
                self._stderr_open = False
                self._remove_reader(self.stderr_fd)
                return
            if not data:
                self._stderr_open = False
                self._remove_reader(self.stderr_fd)
                return
            self._stderr_buffer.extend(data)
            while True:
                newline = self._stderr_buffer.find(b"\n")
                if newline < 0:
                    break
                line = bytes(self._stderr_buffer[:newline]).rstrip(b"\r")
                del self._stderr_buffer[: newline + 1]
                self._log_stderr(line.decode("utf-8", "replace"))

    def _log_stderr(self, line: str) -> None:
        if not line:
            return
        self._stderr_tail.append(line)
        log.warning("agent %s stderr: %s", self.id, line)

    # ---------------------------------------------------------------- exit/close

    def _on_child_exit(self, returncode: int) -> None:
        if self._reaped:
            return
        self._reaped = True
        self.exit_code = returncode
        self._drain()
        self._mark_dead()

    def _drain(self) -> None:
        for fd, open_attr, feed in (
            (self.stdout_fd, "_stdout_open", self._feed),
            (self.stderr_fd, "_stderr_open", None),
        ):
            if not getattr(self, open_attr):
                continue
            while True:
                try:
                    data = os.read(fd, READ_SIZE)
                except (BlockingIOError, OSError):
                    break
                if not data:
                    break
                if feed is not None:
                    feed(data)
                else:
                    self._stderr_buffer.extend(data)
        # Flush complete stderr lines after the final read.
        while b"\n" in self._stderr_buffer:
            line, _, rest = self._stderr_buffer.partition(b"\n")
            self._stderr_buffer = bytearray(rest)
            self._log_stderr(line.rstrip(b"\r").decode("utf-8", "replace"))

    def _mark_dead(self) -> None:
        if self.dead:
            return
        self.dead = True
        self.ready = False
        self.streaming = False
        self._settled_event.set()
        self._hello_event.set()
        self._exit_event.set()
        self._close_stdin()
        for dialog_id in list(self._dialogs):
            self._finish_dialog(dialog_id, "closed")
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        for timer in self._dialog_timers.values():
            timer.cancel()
        self._dialog_timers.clear()
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(AgentClosed(f"agent session {self.id} exited"))
            self._pending.pop(request_id, None)
        self._remove_reader(self.stdout_fd)
        self._remove_reader(self.stderr_fd)
        for subscriber in list(self._subscribers):
            try:
                subscriber.offer_exit(self)
            except Exception:  # noqa: BLE001
                log.exception("agent %s: exit subscriber failed", self.id)
        self._changed()
        if self._on_exit is not None:
            try:
                self._on_exit(self)
            except Exception:  # noqa: BLE001
                log.exception("agent %s: exit callback failed", self.id)

    def close(self) -> None:
        """Ask the agent to exit, then escalate if it does not."""
        if self.dead:
            return
        if self.closing:
            return
        self.closing = True
        self.retain_on_exit = False
        try:
            self._command_counter += 1
            self._write_json(
                {"type": "shutdown", "id": f"webterm-{self.pid}-{self._command_counter}"}
            )
        except AgentError:
            pass
        self._close_stdin()
        self._schedule(self.AGENT_STDIN_GRACE, self._send_term)
        self._schedule(self.AGENT_STDIN_GRACE + self.AGENT_TERM_GRACE, self._send_kill)
        self._schedule(
            self.AGENT_STDIN_GRACE + self.AGENT_TERM_GRACE + self.AGENT_FORCE_GRACE,
            self._force_dead,
        )
        self._changed()

    def force_close(self) -> None:
        """Terminate immediately; used during server shutdown."""
        if self.dead:
            return
        self.closing = True
        self.retain_on_exit = False
        self._signal(signal.SIGKILL)
        self._force_dead()

    def _schedule(self, delay: float, callback: Callable[[], None]) -> None:
        self._timers.append(self._loop.call_later(delay, callback))

    def _send_term(self) -> None:
        if not self.dead:
            self._signal(signal.SIGTERM)

    def _send_kill(self) -> None:
        if not self.dead:
            self._signal(signal.SIGKILL)

    def _force_dead(self) -> None:
        if not self.dead:
            self._mark_dead()

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(self.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def _remove_reader(self, fd: int) -> None:
        try:
            self._loop.remove_reader(fd)
        except (ValueError, OSError):
            pass

    async def wait(self, timeout: float | None = None) -> int | None:
        try:
            if timeout is None:
                await self._exit_event.wait()
            else:
                await asyncio.wait_for(self._exit_event.wait(), timeout)
        except TimeoutError:
            return None
        return self.exit_code

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                log.exception("agent %s: change callback failed", self.id)

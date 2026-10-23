"""Structured pi sessions driven by ``pi --mode rpc`` over pipes.

A :class:`PiSession` is a sibling of :class:`webterm.session.Session`: it owns
a child process, fans events out to subscribers, and participates in the same
lineage and reaping model.  The difference is the transport: three pipes
(stdin/stdout/stderr) carrying JSONL records instead of one PTY carrying bytes.
The pipes, readers, event ring, timers, and two-phase close are inherited from
:class:`webterm.agent.process.AgentProcess`; this module keeps only pi's
dialect: raw record handling, extension UI state, activity, and usage.

The child is spawned with ``fork``/``execvpe`` rather than
``asyncio.create_subprocess_exec`` so :class:`SessionManager` keeps a single
``waitpid``-based reaper.  An asyncio child watcher would race the existing
SIGCHLD handler and could lose an exit status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from ...agent.process import AgentProcess

if TYPE_CHECKING:
    from ...session import SessionManager

log = logging.getLogger("webterm")

#: Transport tuning lives on :class:`~webterm.agent.process.AgentProcess`.
READ_SIZE = AgentProcess.READ_SIZE
MAX_LINE = AgentProcess.MAX_LINE
EVENT_RING_BYTES = AgentProcess.EVENT_RING_BYTES
EVENT_RING_MIN = AgentProcess.EVENT_RING_MIN
#: Time given to pi to exit after its stdin is closed.
PI_STDIN_GRACE = 0.5
#: Time given to pi to exit after SIGTERM.
PI_TERM_GRACE = 2.0
#: Time given after SIGKILL before the session is locally torn down.
PI_FORCE_GRACE = 1.5
PI_RPC_TIMEOUT = 30.0
#: Newest entries included in an attach/resync snapshot.  Older entries are
#: fetched on demand so a long session does not ship its whole transcript to
#: the browser on every attach.
SNAPSHOT_ENTRY_LIMIT = 100
#: Entries returned by one backwards history page, and the hard cap a client
#: may request.
HISTORY_ENTRY_LIMIT = 100
MAX_HISTORY_ENTRY_LIMIT = 500
#: Extension UI methods that block pi until a matching response arrives.
DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})
#: Phases of a run, reported as ``info()["activity"]``: what pi is doing right
#: now.  It is ``None`` outside a run, i.e. before ``agent_start`` and after
#: ``agent_settled``.
ACTIVITY_THINKING = "thinking"
ACTIVITY_TOOL = "tool"
ACTIVITY_ANSWERING = "answering"
#: Streamed message parts, from ``message_update`` records.
STREAM_ACTIVITY = {
    "text_start": ACTIVITY_ANSWERING,
    "text_delta": ACTIVITY_ANSWERING,
    "text_end": ACTIVITY_ANSWERING,
    "thinking_start": ACTIVITY_THINKING,
    "thinking_delta": ACTIVITY_THINKING,
    "thinking_end": ACTIVITY_THINKING,
    "toolcall_start": ACTIVITY_TOOL,
    "toolcall_delta": ACTIVITY_TOOL,
    "toolcall_end": ACTIVITY_TOOL,
}
#: Top-level tool lifecycle records.
TOOL_RECORDS = frozenset(
    {"tool_execution_start", "tool_execution_update", "tool_execution_end"}
)


class PiError(Exception):
    """A pi operation failed (protocol, timeout, or process state)."""


class PiSessionClosed(PiError):
    """The pi process is gone and can no longer answer commands."""


def _entry_page(entries: list[dict], has_more: bool) -> dict:
    """Wrap an entry slice with the pagination fields the browser expects."""
    first = entries[0].get("id") if entries and isinstance(entries[0], dict) else None
    return {
        "entries": entries,
        "hasMore": has_more,
        "firstEntryId": first if isinstance(first, str) else None,
    }


def _entry_tail(data: dict, limit: int | None) -> dict:
    """Keep only the newest *limit* entries of a ``get_entries`` payload."""
    entries = data.get("entries")
    if not isinstance(entries, list):
        return data
    if limit is not None and limit > 0 and len(entries) > limit:
        page = entries[-limit:]
        return {**data, **_entry_page(page, True)}
    return {**data, **_entry_page(entries, False)}


class PiSubscriber(Protocol):
    """Receives structured events from a pi session it is attached to."""

    def offer_pi_event(self, session: PiSession, seq: int, record: dict) -> None: ...

    def offer_pi_ui_request(self, session: PiSession, request: dict) -> None: ...

    def offer_pi_ui_resolved(
        self, session: PiSession, request_id: str, reason: str
    ) -> None: ...

    def offer_exit(self, session: PiSession) -> None: ...


class PiSession(AgentProcess):
    """A ``pi --mode rpc`` child, its JSONL stream, and its subscribers.

    pi is one adapter behind the canonical agent surface: clients see
    ``kind: "agent"`` with ``adapter: "pi"``, and the legacy ``pi_*``
    messages stay available for pi's own view.  The dialect translation lives
    in :mod:`webterm.integration.pi.canonical`; :class:`PiCodec` is the seam.
    """

    kind = "agent"
    adapter = "pi"
    PI_STDIN_GRACE = PI_STDIN_GRACE
    PI_TERM_GRACE = PI_TERM_GRACE
    PI_FORCE_GRACE = PI_FORCE_GRACE

    #: pi reports its own errors; the base uses these for lifecycle wording.
    _name = "pi"
    _label = "pi session"
    #: pi's transport never raises on a write after stdin closed; it drops it.
    _require_stdin = False
    #: pi notifies the change listener after the exit callback, not before.
    _change_before_exit = False

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
        on_exit: Callable[[PiSession], None] | None = None,
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
        session_file: str | None = None,
    ) -> None:
        #: The JSONL file this conversation lives in, once known.  A resumed
        #: session knows it at spawn; a fresh one learns it from its first
        #: ``get_state``, which every attach performs.  The server uses it to
        #: refuse a second session over the same file.
        self.session_file = session_file
        #: What the current run is doing (see the ``ACTIVITY_*`` constants), or
        #: ``None`` when pi is idle.  The sidebar dot uses it to choose between
        #: a faint and a bright pulse.
        self.activity: str | None = None
        #: Runs that produced at least one assistant message.  A client compares
        #: this against what it has shown to detect unread answers.
        self.answers = 0
        self._run_answered = False
        self._ui_requests: dict[str, dict] = {}
        self._ui_timers: dict[str, asyncio.TimerHandle] = {}
        self._ui_status: dict[str, str] = {}
        self._ui_widgets: dict[str, dict] = {}
        self._ui_title: str | None = None
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
        """Return pi's "process is gone" exception."""
        return PiSessionClosed(message)

    def _dialect_error(self, message: str) -> Exception:
        """Return pi's operation error."""
        return PiError(message)

    def _grace_timings(self) -> tuple[float, float, float]:
        """Return the pi grace periods, honouring instance overrides."""
        return self.PI_STDIN_GRACE, self.PI_TERM_GRACE, self.PI_FORCE_GRACE

    @property
    def needs_input(self) -> bool:
        """Whether an extension UI dialog is waiting for an answer."""
        return bool(self._ui_requests)

    def info(self) -> dict[str, object]:
        info = super().info()
        info.update({"activity": self.activity, "answers": self.answers})
        return info

    def _on_marked_dead(self) -> None:
        """An exited pi session is neither closing nor working."""
        self.closing = False
        self.activity = None

    def _cancel_deadlines(self) -> None:
        """Drop pending extension UI requests; pi owns their timeouts."""
        for timer in self._ui_timers.values():
            timer.cancel()
        self._ui_timers.clear()
        self._ui_requests.clear()

    def _close_read_pipes(self) -> None:
        """pi closes the stdout/stderr read ends once it is dead."""
        for fd in (self.stdout_fd, self.stderr_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _overlong_line(self) -> None:
        """Drop an unterminated pi record instead of failing the session."""
        self.protocol_errors += 1
        log.warning(
            "pi session %s: dropping overlong stdout record (%d bytes)",
            self.id,
            len(self._stdout_buffer),
        )
        self._stdout_buffer.clear()

    def _on_write_error(self, exc: OSError | None) -> None:
        """pi treats a lost stdin write as an already-closed session."""

    def _note_command(self, command: dict) -> None:
        if command.get("type") in ("prompt", "steer", "follow_up"):
            self._settled_event.clear()

    def _log_stderr(self, line: str) -> None:
        if not line.strip():
            return
        self._stderr_tail.append(line)
        log.info("pi[%s] %s", self.id, line)

    # ------------------------------------------------------------------- rpc

    async def rpc(self, command: dict, *, timeout: float = PI_RPC_TIMEOUT) -> dict:
        """Send one RPC command and return its ``response`` record."""
        if self.dead:
            raise PiSessionClosed(f"pi session {self.id} is not running")
        if not isinstance(command, dict):
            raise PiError("RPC command must be an object")
        request = dict(command)
        request_id = request.get("id")
        if not isinstance(request_id, str) or not request_id:
            request_id = self._next_request_id()
        request["id"] = request_id
        future: asyncio.Future[dict] = self._loop.create_future()
        self._pending[request_id] = future
        try:
            self._note_command(request)
            self._write_json(request)
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def snapshot(self, *, entry_limit: int | None = SNAPSHOT_ENTRY_LIMIT) -> dict:
        """Fetch the authoritative conversation state for a client attach.

        The returned ``seq`` is the last event sequence applied before the
        snapshot was made; a client should ignore live events at or below it.
        Only the newest ``entry_limit`` entries are included; the rest are
        available through :meth:`history`.
        """
        async with self._snapshot_lock:
            state = await self.rpc({"type": "get_state"})
            if not state.get("success"):
                raise PiError(str(state.get("error", "get_state failed")))
            entries = await self.rpc({"type": "get_entries"})
            if not entries.get("success"):
                raise PiError(str(entries.get("error", "get_entries failed")))
            commands = await self.rpc({"type": "get_commands"})
            if not commands.get("success"):
                log.warning(
                    "pi session %s: get_commands failed: %s",
                    self.id,
                    commands.get("error"),
                )
            info = state.get("data") or {}
            if isinstance(info.get("sessionFile"), str) and info["sessionFile"]:
                self.session_file = info["sessionFile"]
            data = entries.get("data") or {}
            return {
                "state": info,
                "entries": _entry_tail(data, entry_limit),
                "commands": commands.get("data") or {},
                "ui": self.ui_snapshot(),
                "streaming": self.streaming,
                "settled": self._settled_event.is_set(),
                "seq": self.seq,
            }

    async def history(self, before: str, limit: int = HISTORY_ENTRY_LIMIT) -> dict:
        """Return up to *limit* append-ordered entries before *before*.

        pi's ``get_entries`` only has a forward cursor (``since``), and the
        entry list is append-only, so a backwards page is a lookup plus a
        slice.  The page excludes *before* itself.
        """
        response = await self.rpc({"type": "get_entries"})
        if not response.get("success"):
            raise PiError(str(response.get("error", "get_entries failed")))
        data = response.get("data") or {}
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise PiError("get_entries returned no entry list")
        index = None
        for position, entry in enumerate(entries):
            if isinstance(entry, dict) and entry.get("id") == before:
                index = position
                break
        if index is None:
            raise PiError(f"entry not found: {before}")
        start = max(0, index - max(limit, 1))
        return _entry_page(entries[start:index], start > 0)

    def ui_snapshot(self) -> dict:
        """Return pending extension dialogs and replayable fire-and-forget state."""
        return {
            "requests": list(self._ui_requests.values()),
            "status": dict(self._ui_status),
            "widgets": dict(self._ui_widgets),
            "title": self._ui_title,
            "needsInput": bool(self._ui_requests),
        }

    def resolve_ui(self, request_id: str, response: dict) -> None:
        """Validate and write one browser response for a pending dialog."""
        request = self._ui_requests.get(request_id)
        if request is None:
            raise PiError(f"unknown extension UI request {request_id!r}")
        method = str(request.get("method", ""))
        payload: dict[str, object] = {}
        if response.get("cancelled") is True:
            payload["cancelled"] = True
        elif method == "confirm":
            confirmed = response.get("confirmed")
            if not isinstance(confirmed, bool):
                raise PiError("confirm responses need a boolean 'confirmed'")
            payload["confirmed"] = confirmed
        else:
            value = response.get("value")
            if not isinstance(value, str):
                raise PiError(f"{method} responses need a string 'value'")
            if method == "select":
                options = request.get("options") or []
                if value not in options:
                    raise PiError("select response is not one of the offered options")
            payload["value"] = value
        self._ui_requests.pop(request_id, None)
        self._cancel_ui_timer(request_id)
        self.send_command(
            {"type": "extension_ui_response", "id": request_id, **payload}
        )
        self._broadcast_ui_resolved(request_id, "response")
        self._changed()

    def _handle_ui_request(self, record: dict) -> None:
        method = record.get("method")
        request_id = record.get("id")
        if not isinstance(method, str):
            self.protocol_errors += 1
            return
        if method in DIALOG_METHODS:
            if not isinstance(request_id, str) or not request_id:
                self.protocol_errors += 1
                log.warning("pi session %s: dialog without an id", self.id)
                return
            if self.origin == "agent" and not self._subscribers:
                # Nobody is watching an agent-origin session; never leave an
                # extension blocked forever on an unattended dialog.
                try:
                    self.send_command(
                        {
                            "type": "extension_ui_response",
                            "id": request_id,
                            "cancelled": True,
                        }
                    )
                except PiSessionClosed:
                    pass
                return
            self._ui_requests[request_id] = record
            timeout_ms = record.get("timeout")
            if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
                self._cancel_ui_timer(request_id)
                self._ui_timers[request_id] = self._loop.call_later(
                    timeout_ms / 1000.0, self._expire_ui, request_id
                )
        elif method == "notify":
            pass
        elif method == "setStatus":
            key = record.get("statusKey")
            text = record.get("statusText")
            if isinstance(key, str) and key:
                if isinstance(text, str) and text:
                    self._ui_status[key] = text
                else:
                    self._ui_status.pop(key, None)
        elif method == "setWidget":
            key = record.get("widgetKey")
            lines = record.get("widgetLines")
            if isinstance(key, str) and key:
                if isinstance(lines, list) and lines:
                    self._ui_widgets[key] = {
                        "lines": [str(line) for line in lines],
                        "placement": str(record.get("widgetPlacement", "aboveEditor")),
                    }
                else:
                    self._ui_widgets.pop(key, None)
        elif method == "setTitle":
            title = record.get("title")
            self._ui_title = str(title) if isinstance(title, str) and title else None
        elif method == "set_editor_text":
            pass
        else:
            log.debug("pi session %s: unknown extension UI method %r", self.id, method)
            return
        for subscriber in list(self._subscribers):
            subscriber.offer_pi_ui_request(self, record)
        if method != "set_editor_text":
            self._changed()

    def _expire_ui(self, request_id: str) -> None:
        if self._ui_requests.pop(request_id, None) is None:
            return
        self._ui_timers.pop(request_id, None)
        # Pi resolves its own timeout too; do not write a late response.
        self._broadcast_ui_resolved(request_id, "timeout")
        self._changed()

    def _cancel_ui_timer(self, request_id: str) -> None:
        timer = self._ui_timers.pop(request_id, None)
        if timer is not None:
            timer.cancel()

    def _broadcast_ui_resolved(self, request_id: str, reason: str) -> None:
        for subscriber in list(self._subscribers):
            subscriber.offer_pi_ui_resolved(self, request_id, reason)

    # -------------------------------------------------------------- read side

    def _handle_line(self, line: bytes) -> None:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            self.protocol_errors += 1
            log.warning("pi session %s: invalid JSON record", self.id)
            return
        if not isinstance(record, dict):
            self.protocol_errors += 1
            return
        if record.get("type") == "response":
            request_id = record.get("id")
            if isinstance(request_id, str):
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(record)
            return
        if record.get("type") == "extension_ui_request":
            self._handle_ui_request(record)
            return
        changed = False
        if record.get("type") == "agent_start":
            self._settled_event.clear()
            if not self.streaming:
                self.streaming = True
                changed = True
        elif record.get("type") == "agent_settled":
            self._settled_event.set()
            if self.streaming:
                self.streaming = False
                changed = True
            if self._run_answered:
                self.answers += 1
                self._run_answered = False
                changed = True
        elif record.get("type") == "message_end":
            message = record.get("message")
            self._track_usage(message)
            if isinstance(message, dict) and message.get("role") == "assistant":
                self._run_answered = True
        if self._note_activity(record):
            changed = True
        if changed:
            self._changed()
        self.seq += 1
        self._remember(self.seq, record, len(line))
        for subscriber in list(self._subscribers):
            subscriber.offer_pi_event(self, self.seq, record)

    def _note_activity(self, record: dict) -> bool:
        """Track what pi is doing now; True when ``activity`` changed.

        Only records inside a run (``agent_start`` .. ``agent_settled``) carry a
        phase, so compaction or a retry outside a run cannot leave the
        sidebar dot pulsing forever.
        """
        kind = record.get("type")
        phase: str | None
        if kind == "agent_start":
            phase = ACTIVITY_THINKING
        elif kind == "agent_settled":
            phase = None
        elif not self.streaming:
            return False
        elif kind == "message_update":
            # ``message_start`` deliberately carries no phase: the first content
            # part (text, thinking, or a tool call) decides what pi is doing.
            event = record.get("assistantMessageEvent")
            phase = STREAM_ACTIVITY.get(event.get("type")) if isinstance(event, dict) else None
            if phase is None:
                return False
        elif kind in TOOL_RECORDS:
            # A finished tool hands the turn back to the model, which is
            # thinking again until it streams something.
            phase = ACTIVITY_THINKING if kind == "tool_execution_end" else ACTIVITY_TOOL
        else:
            return False
        if phase == self.activity:
            return False
        self.activity = phase
        return True

    def _track_usage(self, message: object) -> None:
        """Accumulate model, token, and cost totals from completed messages."""
        if not isinstance(message, dict):
            return
        usage = message.get("usage")
        changed = False
        model = message.get("model")
        if isinstance(model, str) and model and model != self.model:
            self.model = model
            changed = True
        if not isinstance(usage, dict):
            return
        total = usage.get("totalTokens")
        if not isinstance(total, (int, float)):
            total = sum(
                value
                for value in (
                    usage.get("input"),
                    usage.get("output"),
                    usage.get("cacheRead"),
                    usage.get("cacheWrite"),
                )
                if isinstance(value, (int, float))
            )
        if isinstance(total, (int, float)) and total:
            self.tokens += int(total)
            changed = True
        cost = usage.get("cost")
        if isinstance(cost, dict):
            amount = cost.get("total")
            if isinstance(amount, (int, float)) and amount:
                self.cost += float(amount)
                changed = True
        if changed:
            log.debug(
                "pi session %s: usage model=%s tokens=%d cost=%.6f",
                self.id,
                self.model,
                self.tokens,
                self.cost,
            )


def fork_pi(
    manager: SessionManager,
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None,
    counter: int,
    parent_id: str | None,
    depth: int,
    agent: str | None,
) -> tuple[int, int, int, int]:
    """Fork and exec *argv*, returning ``(pid, stdin, stdout, stderr)``.

    The child is placed in its own session/process group so a later
    ``killpg`` takes pi's bash-tool children with it.  ``manager`` provides the
    environment preparation shared with PTY sessions.
    """
    if not argv:
        raise ValueError("pi command must not be empty")
    stdin_r, stdin_w = os.pipe2(os.O_CLOEXEC)
    stdout_r, stdout_w = os.pipe2(os.O_CLOEXEC)
    stderr_r, stderr_w = os.pipe2(os.O_CLOEXEC)
    try:
        pid = os.fork()
    except OSError:
        for fd in (stdin_r, stdin_w, stdout_r, stdout_w, stderr_r, stderr_w):
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    if pid == 0:  # pragma: no cover - runs in the child process
        try:
            os.close(stdin_w)
            os.close(stdout_r)
            os.close(stderr_r)
            os.setsid()
            os.dup2(stdin_r, 0)
            os.dup2(stdout_w, 1)
            os.dup2(stderr_w, 2)
            for fd in (stdin_r, stdout_w, stderr_w):
                if fd > 2:
                    os.close(fd)
            session_id = f"{counter}-{os.getpid()}"
            child_env = manager._prepare_child(
                cwd, env, session_id, parent_id, depth, agent
            )
            os.execvpe(argv[0], argv, child_env)
        except Exception:  # noqa: BLE001 - child must not raise into the parent
            pass
        os._exit(127)
    os.close(stdin_r)
    os.close(stdout_w)
    os.close(stderr_w)
    return pid, stdin_w, stdout_r, stderr_r

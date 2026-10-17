"""Structured pi sessions driven by ``pi --mode rpc`` over pipes.

A :class:`PiSession` is a sibling of :class:`webterm.session.Session`: it owns
a child process, fans events out to subscribers, and participates in the same
lineage and reaping model.  The difference is the transport: three pipes
(stdin/stdout/stderr) carrying JSONL records instead of one PTY carrying bytes.

The child is spawned with ``fork``/``execvpe`` rather than
``asyncio.create_subprocess_exec`` so :class:`SessionManager` keeps a single
``waitpid``-based reaper.  An asyncio child watcher would race the existing
SIGCHLD handler and could lose an exit status.
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
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .session import SessionManager

log = logging.getLogger("webterm")

READ_SIZE = 65_536
#: Largest partial stdout record retained before the line is abandoned.  A
#: tool result can be large; this bounds memory for a broken or hostile child.
MAX_LINE = 16 * 1024 * 1024
#: Time given to pi to exit after its stdin is closed.
PI_STDIN_GRACE = 0.5
#: Time given to pi to exit after SIGTERM.
PI_TERM_GRACE = 2.0
#: Time given after SIGKILL before the session is locally torn down.
PI_FORCE_GRACE = 1.5
PI_RPC_TIMEOUT = 30.0
#: Approximate byte cap for the live-event ring used by short reconnects.
EVENT_RING_BYTES = 1_000_000
EVENT_RING_MIN = 64
#: Extension UI methods that block pi until a matching response arrives.
DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})


class PiError(Exception):
    """A pi operation failed (protocol, timeout, or process state)."""


class PiSessionClosed(PiError):
    """The pi process is gone and can no longer answer commands."""


class PiSubscriber(Protocol):
    """Receives structured events from a pi session it is attached to."""

    def offer_pi_event(self, session: PiSession, seq: int, record: dict) -> None: ...

    def offer_pi_ui_request(self, session: PiSession, request: dict) -> None: ...

    def offer_pi_ui_resolved(
        self, session: PiSession, request_id: str, reason: str
    ) -> None: ...

    def offer_exit(self, session: PiSession) -> None: ...


class PiSession:
    """A ``pi --mode rpc`` child, its JSONL stream, and its subscribers."""

    kind = "pi"
    PI_STDIN_GRACE = PI_STDIN_GRACE
    PI_TERM_GRACE = PI_TERM_GRACE
    PI_FORCE_GRACE = PI_FORCE_GRACE

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

        self.seq = 0
        self._events: collections.deque[tuple[int, dict, int]] = collections.deque()
        self._event_bytes = 0
        self._subscribers: set[PiSubscriber] = set()
        self._on_exit = on_exit
        self._on_change = on_change
        self._exit_event = asyncio.Event()
        #: Starts set because a fresh process is idle; prompt/steer clears it
        #: before the command is written, and agent_settled sets it again.
        self._settled_event = asyncio.Event()
        self._settled_event.set()
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
        self._rpc_counter = 0
        self._timers: list[asyncio.TimerHandle] = []
        self._ui_requests: dict[str, dict] = {}
        self._ui_timers: dict[str, asyncio.TimerHandle] = {}
        self._ui_status: dict[str, str] = {}
        self._ui_widgets: dict[str, dict] = {}
        self._ui_title: str | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=50)
        self.protocol_errors = 0

        for fd in (stdin_fd, stdout_fd, stderr_fd):
            os.set_blocking(fd, False)
        loop.add_reader(stdout_fd, self._on_stdout)
        loop.add_reader(stderr_fd, self._on_stderr)

    # ------------------------------------------------------------------ props

    @property
    def subscribers(self) -> set[PiSubscriber]:
        return self._subscribers

    def info(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "cols": self.cols,
            "rows": self.rows,
            "pid": self.pid,
            "alive": not self.dead,
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
            "needs_input": bool(self._ui_requests),
            "seq": self.seq,
        }

    # ------------------------------------------------------------- lifecycle

    def attach(self, subscriber: PiSubscriber) -> None:
        self._subscribers.add(subscriber)

    def detach(self, subscriber: PiSubscriber) -> None:
        self._subscribers.discard(subscriber)

    def events_since(self, seq: int) -> list[tuple[int, dict]]:
        """Return retained live events with a sequence number greater than *seq*."""
        return [(event_seq, record) for event_seq, record, _ in self._events if event_seq > seq]

    def _remember(self, seq: int, record: dict, size: int) -> None:
        self._events.append((seq, record, size))
        self._event_bytes += size
        while self._event_bytes > EVENT_RING_BYTES and len(self._events) > EVENT_RING_MIN:
            _, _, old_size = self._events.popleft()
            self._event_bytes -= old_size

    # ------------------------------------------------------------------- rpc

    def send_command(self, command: dict) -> None:
        """Write one RPC command without waiting for its response."""
        if self.dead:
            raise PiSessionClosed(f"pi session {self.id} is not running")
        if not isinstance(command, dict):
            raise PiError("RPC command must be an object")
        self._note_command(command)
        self._write_json(command)

    async def rpc(self, command: dict, *, timeout: float = PI_RPC_TIMEOUT) -> dict:
        """Send one RPC command and return its ``response`` record."""
        if self.dead:
            raise PiSessionClosed(f"pi session {self.id} is not running")
        if not isinstance(command, dict):
            raise PiError("RPC command must be an object")
        request = dict(command)
        request_id = request.get("id")
        if not isinstance(request_id, str) or not request_id:
            self._rpc_counter += 1
            request_id = f"webterm-{self.pid}-{self._rpc_counter}"
        request["id"] = request_id
        future: asyncio.Future[dict] = self._loop.create_future()
        self._pending[request_id] = future
        try:
            self._note_command(request)
            self._write_json(request)
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def snapshot(self) -> dict:
        """Fetch the authoritative conversation state for a client attach.

        The returned ``seq`` is the last event sequence applied before the
        snapshot was made; a client should ignore live events at or below it.
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
            return {
                "state": state.get("data") or {},
                "entries": entries.get("data") or {},
                "commands": commands.get("data") or {},
                "ui": self.ui_snapshot(),
                "seq": self.seq,
            }

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
        if self._on_change is not None:
            self._on_change()

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
            if self._on_change is not None:
                self._on_change()

    def _expire_ui(self, request_id: str) -> None:
        if self._ui_requests.pop(request_id, None) is None:
            return
        self._ui_timers.pop(request_id, None)
        # Pi resolves its own timeout too; do not write a late response.
        self._broadcast_ui_resolved(request_id, "timeout")
        if self._on_change is not None:
            self._on_change()

    def _cancel_ui_timer(self, request_id: str) -> None:
        timer = self._ui_timers.pop(request_id, None)
        if timer is not None:
            timer.cancel()

    def _broadcast_ui_resolved(self, request_id: str, reason: str) -> None:
        for subscriber in list(self._subscribers):
            subscriber.offer_pi_ui_resolved(self, request_id, reason)

    def _note_command(self, command: dict) -> None:
        if command.get("type") in ("prompt", "steer", "follow_up"):
            self._settled_event.clear()

    async def wait_for_settled(self, timeout: float | None = None) -> bool:
        """Wait until pi has no automatic work left; False on timeout."""
        if self.dead:
            return True
        try:
            await asyncio.wait_for(self._settled_event.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def _write_json(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._out.extend(line)
        self._out.extend(b"\n")
        self._flush()

    def _flush(self) -> None:
        while self._out and not self.dead and self._stdin_open:
            try:
                written = os.write(self.stdin_fd, self._out)
            except BlockingIOError:
                self._loop.add_writer(self.stdin_fd, self._flush)
                return
            except OSError:
                self._close_stdin()
                return
            del self._out[:written]
        if not self._out:
            self._remove_writer(self.stdin_fd)

    def _close_stdin(self) -> None:
        if not self._stdin_open:
            return
        self._stdin_open = False
        self._remove_writer(self.stdin_fd)
        self._out.clear()
        try:
            os.close(self.stdin_fd)
        except OSError:
            pass

    # -------------------------------------------------------------- read side

    def _on_stdout(self) -> None:
        while self._stdout_open:
            try:
                data = os.read(self.stdout_fd, READ_SIZE)
            except BlockingIOError:
                return
            except OSError:
                self._on_stdout_eof()
                return
            if not data:
                self._on_stdout_eof()
                return
            self._feed(data)

    def _on_stdout_eof(self) -> None:
        if not self._stdout_open:
            return
        self._stdout_open = False
        self._remove_reader(self.stdout_fd)

    def _feed(self, data: bytes) -> None:
        self._stdout_buffer.extend(data)
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._stdout_buffer[:newline])
            del self._stdout_buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if line:
                self._handle_line(line)
        if len(self._stdout_buffer) > MAX_LINE:
            self.protocol_errors += 1
            log.warning(
                "pi session %s: dropping overlong stdout record (%d bytes)",
                self.id,
                len(self._stdout_buffer),
            )
            self._stdout_buffer.clear()

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
        if record.get("type") == "agent_start":
            self._settled_event.clear()
        elif record.get("type") == "agent_settled":
            self._settled_event.set()
        self.seq += 1
        self._remember(self.seq, record, len(line))
        for subscriber in list(self._subscribers):
            subscriber.offer_pi_event(self, self.seq, record)

    def _on_stderr(self) -> None:
        while self._stderr_open:
            try:
                data = os.read(self.stderr_fd, READ_SIZE)
            except BlockingIOError:
                return
            except OSError:
                self._on_stderr_eof()
                return
            if not data:
                self._on_stderr_eof()
                return
            self._stderr_buffer.extend(data)
            while True:
                newline = self._stderr_buffer.find(b"\n")
                if newline < 0:
                    break
                line = bytes(self._stderr_buffer[:newline]).decode("utf-8", "replace")
                del self._stderr_buffer[: newline + 1]
                self._log_stderr(line)
        if len(self._stderr_buffer) > MAX_LINE:
            self._stderr_buffer.clear()

    def _on_stderr_eof(self) -> None:
        if not self._stderr_open:
            return
        self._stderr_open = False
        self._remove_reader(self.stderr_fd)

    def _log_stderr(self, line: str) -> None:
        if not line.strip():
            return
        self._stderr_tail.append(line)
        log.info("pi[%s] %s", self.id, line)

    # --------------------------------------------------------------- teardown

    def _on_child_exit(self, returncode: int) -> None:
        self._reaped = True
        self.exit_code = returncode
        self._cancel_timers()
        if self.dead:
            # force_close()/the watchdog already closed the fds.  Never read
            # them here: the numbers may have been reused by another session.
            return
        self._drain()
        self._mark_dead()

    def _drain(self) -> None:
        if self._stdout_open:
            self._on_stdout()
        if self._stderr_open:
            self._on_stderr()

    def close(self) -> None:
        """Ask pi to exit, escalating to SIGTERM and SIGKILL if needed."""
        if self.dead or self.closing:
            return
        self.closing = True
        self.retain_on_exit = False
        self._close_stdin()
        if self._on_change is not None:
            self._on_change()
        self._schedule(self.PI_STDIN_GRACE, self._send_term)
        self._schedule(self.PI_STDIN_GRACE + self.PI_TERM_GRACE, self._send_kill)
        self._schedule(
            self.PI_STDIN_GRACE + self.PI_TERM_GRACE + self.PI_FORCE_GRACE,
            self._force_dead,
        )

    def force_close(self) -> None:
        """Terminate immediately, used on server shutdown."""
        if self.dead:
            return
        self.retain_on_exit = False
        self._signal(signal.SIGKILL)
        self._mark_dead()

    def _schedule(self, delay: float, callback: Callable[[], None]) -> None:
        self._timers.append(self._loop.call_later(delay, callback))

    def _cancel_timers(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    def _send_term(self) -> None:
        if not self.dead and not self._reaped:
            self._signal(signal.SIGTERM)

    def _send_kill(self) -> None:
        if not self.dead and not self._reaped:
            self._signal(signal.SIGKILL)

    def _force_dead(self) -> None:
        if self.dead:
            return
        log.warning("pi session %s did not exit after SIGKILL", self.id)
        self._mark_dead()

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self.pid, sig)
            return
        except ProcessLookupError:
            pass
        except PermissionError:
            log.warning("pi session %s: no permission to signal group", self.id)
            return
        try:
            os.kill(self.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _mark_dead(self) -> None:
        if self.dead:
            return
        self.dead = True
        self.closing = False
        self._cancel_timers()
        for timer in self._ui_timers.values():
            timer.cancel()
        self._ui_timers.clear()
        self._ui_requests.clear()
        self._remove_reader(self.stdout_fd)
        self._remove_reader(self.stderr_fd)
        self._close_stdin()
        for fd in (self.stdout_fd, self.stderr_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(PiSessionClosed(f"pi session {self.id} exited"))
                future.add_done_callback(_consume_future_exception)
        self._pending.clear()
        self._settled_event.set()
        self._exit_event.set()
        for subscriber in list(self._subscribers):
            subscriber.offer_exit(self)
        if self._on_exit is not None:
            self._on_exit(self)
        if self._on_change is not None:
            self._on_change()

    def _remove_reader(self, fd: int) -> None:
        try:
            self._loop.remove_reader(fd)
        except (ValueError, OSError):
            pass

    def _remove_writer(self, fd: int) -> None:
        try:
            self._loop.remove_writer(fd)
        except (ValueError, OSError):
            pass

    async def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the child to exit, returning ``None`` on timeout."""
        if self.dead:
            return self.exit_code
        try:
            await asyncio.wait_for(self._exit_event.wait(), timeout)
        except TimeoutError:
            return None
        return self.exit_code


def _consume_future_exception(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


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

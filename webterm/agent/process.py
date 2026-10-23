"""Process plumbing and lifecycle for structured agent sessions.

:class:`AgentProcess` owns the machinery every structured agent session shares:
the fork'd pipes and their non-blocking setup, stdout line buffering, stderr
logging, the replay event ring, attach/detach fan-out, timers, the two-phase
close, ``wait()``, and the lineage/title fields reported by ``info()``.  A
dialect supplies only what its wire format needs:
:class:`~webterm.agent.session.AgentSession` implements the canonical JSONL
protocol and :class:`~webterm.integration.pi.session.PiSession` implements pi's
RPC mode.

The module is kept separate from :mod:`webterm.agent.session` so the base has
no circular import with :class:`webterm.session.SessionManager`, which owns the
child reaper and the shared child environment.
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
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..session import SessionManager

log = logging.getLogger("webterm")


def _consume_future_exception(future: asyncio.Future) -> None:
    """Retrieve a failed future's exception so asyncio does not log it as lost."""
    if not future.cancelled():
        future.exception()


class ExitSubscriber(Protocol):
    """Anything the base can tell that a process is gone."""

    def offer_exit(self, session: AgentProcess) -> None: ...


class AgentProcess:
    """A fork'd structured-agent child, its pipes, and its local lifecycle.

    The base owns the transport: three pipes, non-blocking fds, stdout line
    splitting, stderr logging, the replay ring, subscriber fan-out, timers, and
    the close escalation state machine.  It knows nothing about records; a
    dialect (``AgentSession`` or ``PiSession``) implements the hooks below and
    keeps its own parsing, translation, and extra ``info()`` fields.

    Both subclasses keep the exact public surface they had before the
    extraction: ``id``, ``pid``, ``attach()``/``detach()``, ``events_since()``,
    ``send_command()``, ``close()``, ``force_close()``, ``wait()``, and
    ``wait_for_settled()`` all live here.  Only ``info()`` is extended, and only
    with the keys that dialect adds.
    """

    kind = "agent"
    adapter = "generic"

    #: Bytes read per readiness callback.
    READ_SIZE = 65_536
    #: Largest partial stdout line retained before it is treated as a protocol
    #: failure.  Tool results can be large; this bounds a broken or hostile child.
    MAX_LINE = 16 * 1024 * 1024
    #: Approximate byte cap for the replay ring used by short reconnects.
    EVENT_RING_BYTES = 1_000_000
    EVENT_RING_MIN = 64
    #: Time given to the child to exit after stdin is closed.
    AGENT_STDIN_GRACE = 0.5
    #: Time given after SIGTERM.
    AGENT_TERM_GRACE = 2.0
    #: Time given after SIGKILL before the session is locally torn down.
    AGENT_FORCE_GRACE = 1.5

    #: Short process name used in log lines ("agent", "pi").
    _name = "agent"
    #: Wording used in lifecycle error messages ("agent session", "pi session").
    _label = "agent session"
    #: Whether a write after stdin closed is an error (canonical) or ignored (pi).
    _require_stdin = True
    #: Whether the change callback runs before or after the exit callback.
    _change_before_exit = True

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
        on_exit: Callable[[Any], None] | None = None,
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

        self.seq = 0
        self.streaming = False
        self.model: str | None = None
        self.tokens = 0
        self.cost = 0.0
        self.protocol_errors = 0

        self._events: collections.deque[tuple[int, dict, int]] = collections.deque()
        self._event_bytes = 0
        self._subscribers: set[ExitSubscriber] = set()
        self._on_exit = on_exit
        self._on_change = on_change
        self._exit_event = asyncio.Event()
        #: Starts set because a fresh process is idle; a prompt/run start
        #: clears it, and the settle notification sets it again.
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
        self._request_counter = 0
        self._timers: list[asyncio.TimerHandle] = []
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=50)

        for fd in (stdin_fd, stdout_fd, stderr_fd):
            os.set_blocking(fd, False)
        loop.add_reader(stdout_fd, self._on_stdout)
        loop.add_reader(stderr_fd, self._on_stderr)

    # ------------------------------------------------------------------ props

    @property
    def subscribers(self) -> set[ExitSubscriber]:
        return self._subscribers

    @property
    def settled(self) -> bool:
        return self._settled_event.is_set()

    @property
    def needs_input(self) -> bool:
        """Whether a blocking dialog is waiting for an answer."""
        return False

    def info(self) -> dict[str, object]:
        """Return the lineage and lifecycle record every client understands.

        Dialects extend this with their own state; the keys here are the ones
        the session list and the browser need for any structured agent.
        """
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "adapter": self.adapter,
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
            "needs_input": self.needs_input,
            "streaming": self.streaming,
            "model": self.model,
            "tokens": self.tokens,
            "cost": round(self.cost, 6),
            "seq": self.seq,
        }

    # ------------------------------------------------------------- lifecycle

    def attach(self, subscriber: ExitSubscriber) -> None:
        self._subscribers.add(subscriber)

    def detach(self, subscriber: ExitSubscriber) -> None:
        self._subscribers.discard(subscriber)

    def events_since(self, seq: int) -> list[tuple[int, dict]]:
        """Return retained live events with a sequence number greater than *seq*."""
        return [(event_seq, event) for event_seq, event, _ in self._events if event_seq > seq]

    def _remember(self, seq: int, event: dict, size: int) -> None:
        self._events.append((seq, event, size))
        self._event_bytes += size
        while (
            self._event_bytes > self.EVENT_RING_BYTES
            and len(self._events) > self.EVENT_RING_MIN
        ):
            _, _, old_size = self._events.popleft()
            self._event_bytes -= old_size

    def _next_request_id(self) -> str:
        """Return a fresh command/request id for this child."""
        self._request_counter += 1
        return f"webterm-{self.pid}-{self._request_counter}"

    async def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the child to exit, returning ``None`` on timeout."""
        if self.dead:
            return self.exit_code
        try:
            await asyncio.wait_for(self._exit_event.wait(), timeout)
        except TimeoutError:
            return None
        return self.exit_code

    async def wait_for_settled(self, timeout: float | None = None) -> bool:
        """Wait until the agent has no automatic work left; False on timeout."""
        if self.dead:
            return True
        try:
            await asyncio.wait_for(self._settled_event.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                log.exception("agent %s: change callback failed", self.id)

    # ------------------------------------------------------------- dialect seams

    def _closed_error(self, message: str) -> Exception:
        """Return this dialect's "process is gone" exception for *message*."""
        raise NotImplementedError

    def _dialect_error(self, message: str) -> Exception:
        """Return this dialect's general operation error for *message*."""
        raise NotImplementedError

    def _handle_line(self, line: bytes) -> None:
        """Consume one complete stdout line; implemented by each dialect."""
        raise NotImplementedError

    def _log_stderr(self, line: str) -> None:
        """Log one complete stderr line; implemented by each dialect."""
        raise NotImplementedError

    def _note_command(self, command: dict) -> None:
        """Observe an outgoing command; dialects that track runs override this."""

    def _request_shutdown(self) -> None:
        """Ask the child to exit on its own before stdin closes.

        The canonical dialect sends a ``shutdown`` record; pi has none.
        """

    def _grace_timings(self) -> tuple[float, float, float]:
        """Return the ``(stdin, term, force)`` grace periods for ``close()``."""
        return (
            self.AGENT_STDIN_GRACE,
            self.AGENT_TERM_GRACE,
            self.AGENT_FORCE_GRACE,
        )

    def _on_marked_dead(self) -> None:
        """Apply dialect state changes when the child is marked dead."""

    def _wake_waiters(self) -> None:
        """Wake the waiters that ``_mark_dead`` releases."""
        self._settled_event.set()
        self._exit_event.set()

    def _cancel_deadlines(self) -> None:
        """Drop the dialect's pending deadlines (dialogs) when the child dies."""

    def _close_read_pipes(self) -> None:
        """Close the read ends after death; dialects may keep them open."""

    def _on_write_error(self, exc: OSError | None) -> None:
        """Handle a failed stdin write; the canonical dialect raises."""
        detail = f": {exc}" if exc is not None else ""
        raise self._closed_error(f"cannot write to {self._name} {self.id}{detail}")

    def _protocol_error(self, message: str) -> None:
        """Count and log one malformed inbound record."""
        self.protocol_errors += 1
        log.warning("%s %s protocol error: %s", self._name, self.id, message)

    def _overlong_line(self) -> None:
        """Drop an unterminated stdout record that grew past ``MAX_LINE``."""
        self._stdout_buffer.clear()
        self._protocol_error("stdout record exceeded the maximum line length")

    # ------------------------------------------------------------------- rpc

    def send_command(self, command: dict) -> None:
        """Write one command without waiting for its response."""
        if self.dead:
            raise self._closed_error(f"{self._label} {self.id} is not running")
        if not isinstance(command, dict):
            raise self._dialect_error(f"{self._name} command must be an object")
        self._note_command(command)
        self._write_json(command)

    def _write_json(self, record: dict) -> None:
        if not self._stdin_open and self._require_stdin:
            raise self._closed_error(f"{self._label} {self.id} stdin is closed")
        try:
            payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise self._dialect_error(f"cannot encode command: {exc}") from exc
        self._out.extend(payload.encode("utf-8"))
        self._out.append(0x0A)
        self._flush()

    def _flush(self) -> None:
        while self._out and self._stdin_open and not self.dead:
            try:
                written = os.write(self.stdin_fd, self._out)
            except BlockingIOError:
                self._loop.add_writer(self.stdin_fd, self._flush)
                return
            except OSError as exc:
                self._close_stdin()
                self._on_write_error(exc)
                return
            if written <= 0:
                self._close_stdin()
                self._on_write_error(None)
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
                data = os.read(self.stdout_fd, self.READ_SIZE)
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
        """Buffer *data* and hand every complete LF-terminated line to the dialect."""
        self._stdout_buffer.extend(data)
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._stdout_buffer[:newline]).rstrip(b"\r")
            del self._stdout_buffer[: newline + 1]
            if line:
                self._handle_line(line)
        if len(self._stdout_buffer) > self.MAX_LINE:
            self._overlong_line()

    def _on_stderr(self) -> None:
        while self._stderr_open:
            try:
                data = os.read(self.stderr_fd, self.READ_SIZE)
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
                line = bytes(self._stderr_buffer[:newline]).rstrip(b"\r")
                del self._stderr_buffer[: newline + 1]
                self._log_stderr(line.decode("utf-8", "replace"))
        if len(self._stderr_buffer) > self.MAX_LINE:
            self._stderr_buffer.clear()

    def _on_stderr_eof(self) -> None:
        if not self._stderr_open:
            return
        self._stderr_open = False
        self._remove_reader(self.stderr_fd)

    # ---------------------------------------------------------------- exit/close

    def _on_child_exit(self, returncode: int) -> None:
        """Reaper callback: record the status, drain the pipes, mark dead."""
        if self._reaped:
            return
        self._reaped = True
        self.exit_code = returncode
        self._cancel_timers()
        if self.dead:
            # close()/the watchdog already closed the fds.  Never read them
            # here: the numbers may have been reused by another session.
            return
        self._drain()
        self._mark_dead()

    def _drain(self) -> None:
        """Read what is still buffered in the pipes before teardown."""
        if self._stdout_open:
            self._on_stdout()
        if self._stderr_open:
            self._on_stderr()

    def _mark_dead(self) -> None:
        """Tear the session down locally once the child is gone."""
        if self.dead:
            return
        self.dead = True
        self.streaming = False
        self._on_marked_dead()
        self._wake_waiters()
        self._cancel_timers()
        self._close_stdin()
        self._cancel_deadlines()
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(self._closed_error(f"{self._label} {self.id} exited"))
                future.add_done_callback(_consume_future_exception)
            self._pending.pop(request_id, None)
        self._remove_reader(self.stdout_fd)
        self._remove_reader(self.stderr_fd)
        self._close_read_pipes()
        for subscriber in list(self._subscribers):
            try:
                subscriber.offer_exit(self)
            except Exception:  # noqa: BLE001
                log.exception("agent %s: exit subscriber failed", self.id)
        if self._change_before_exit:
            self._changed()
        if self._on_exit is not None:
            try:
                self._on_exit(self)
            except Exception:  # noqa: BLE001
                log.exception("agent %s: exit callback failed", self.id)
        if not self._change_before_exit:
            self._changed()

    def close(self) -> None:
        """Ask the child to exit, then escalate if it does not."""
        if self.dead or self.closing:
            return
        self.closing = True
        self.retain_on_exit = False
        self._request_shutdown()
        self._close_stdin()
        stdin_grace, term_grace, force_grace = self._grace_timings()
        self._schedule(stdin_grace, self._send_term)
        self._schedule(stdin_grace + term_grace, self._send_kill)
        self._schedule(stdin_grace + term_grace + force_grace, self._force_dead)
        self._changed()

    def force_close(self) -> None:
        """Terminate immediately; used during server shutdown."""
        if self.dead:
            return
        self.closing = True
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
        """Tear the session down locally when the child ignored SIGKILL."""
        if self.dead:
            return
        log.warning("%s %s did not exit after SIGKILL", self._label, self.id)
        self._mark_dead()

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self.pid, sig)
            return
        except ProcessLookupError:
            pass
        except PermissionError:
            log.warning("%s %s: no permission to signal group", self._name, self.id)
            return
        try:
            os.kill(self.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

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


def fork_agent(
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

    The child gets its own session/process group, so a later ``killpg`` takes
    tool subprocesses with it.  ``manager`` supplies the shared environment
    preparation, including the ``WEBTERM_*`` discovery variables.
    """
    if not argv:
        raise ValueError("agent command must not be empty")
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

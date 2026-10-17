"""PTY-backed terminal sessions and their manager.

A :class:`Session` owns a pseudo-terminal master fd and the shell process
attached to it.  Output is fanned out to any number of subscribers and kept in
a bounded scrollback buffer so a browser can reattach and replay history.

Sessions also carry *lineage* (``parent_id`` / ``depth``) and role metadata, so
an agent running in one session can spawn sub-agents that stay visible in the
client's session tree and can be collected by their parent.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import re
import shlex
import signal
import struct
import termios
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from .ansi import remove_ansi
from .pi import PiSession, fork_pi

DEFAULT_SCROLLBACK = 100_000
DEFAULT_MAX_AGENT_DEPTH = 5
DEFAULT_MAX_SESSIONS = 64
DEFAULT_MAX_RETAINED = 32
DEFAULT_RETAINED_TTL = 600.0
#: How often finished one-shot sessions are swept for TTL / count limits.
GC_INTERVAL = 30.0
READ_SIZE = 65_536
#: How much decoded output a text waiter keeps for cross-chunk matching.
WAIT_WINDOW = 65_536

log = logging.getLogger("webterm")


class SessionError(Exception):
    """A session operation was rejected (unknown parent, limit, ...)."""


class DepthExceededError(SessionError):
    """Spawning a child would exceed the configured agent nesting depth."""


class SessionLimitError(SessionError):
    """The manager is at its session cap."""


def _command_title(command: list[str] | str | None) -> str | None:
    """Derive a session title from an argv list or a shell command string."""
    if not command:
        return None
    if isinstance(command, str):
        parts = command.split()
        return os.path.basename(parts[0]) if parts else None
    return os.path.basename(command[0]) if command else None


def _single_argv(shell: str, command: list[str] | str) -> list[str]:
    """Build the argv for a one-shot run.

    A command string is handed to ``$SHELL -c`` so pipes, globs, and ``&&``
    work; an argv list is executed directly.  Empty commands are rejected.
    """
    if isinstance(command, str):
        if not command.strip():
            raise ValueError("command must not be empty")
        return [shell, "-c", command]
    argv = list(command)
    if not argv:
        raise ValueError("command must not be empty")
    return argv


@runtime_checkable
class Subscriber(Protocol):
    """Receives output from a session it is attached to."""

    def offer_output(self, session: Session, data: bytes, offset: int) -> None: ...

    def offer_exit(self, session: Session) -> None: ...


class _TextWaiter:
    """Subscriber that resolves as soon as a chunk matches a regex.

    Matches against a decoded, optionally ANSI-stripped rolling window so a
    pattern may span the boundary between two PTY reads.
    """

    def __init__(self, regex: re.Pattern[str], strip_ansi: bool) -> None:
        self.regex = regex
        self.strip_ansi = strip_ansi
        self.event = asyncio.Event()
        self.matched = False
        self.match: str | None = None
        self.offset = 0
        self._window = ""

    def offer_output(self, session: Session, data: bytes, offset: int) -> None:
        if self.event.is_set():
            return
        raw = remove_ansi(data) if self.strip_ansi else data
        self._window = (self._window + raw.decode("utf-8", "replace"))[-WAIT_WINDOW:]
        found = self.regex.search(self._window)
        if found is not None:
            self.matched = True
            self.match = found.group(0)
            self.offset = offset + len(data)
            self.event.set()

    def offer_exit(self, session: Session) -> None:
        self.event.set()


class _IdleWaiter:
    """Subscriber that records when output last arrived."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.last = time.monotonic()
        self.closed = False

    def offer_output(self, session: Session, data: bytes, offset: int) -> None:
        self.last = time.monotonic()
        self.event.set()

    def offer_exit(self, session: Session) -> None:
        self.closed = True
        self.event.set()


class Session:
    """A single PTY + child process with scrollback and fan-out."""

    kind = "shell"

    def __init__(
        self,
        session_id: str,
        pid: int,
        fd: int,
        title: str,
        *,
        cols: int,
        rows: int,
        loop: asyncio.AbstractEventLoop,
        scrollback: int = DEFAULT_SCROLLBACK,
        on_exit: Callable[[Session], None] | None = None,
        retain_on_exit: bool = False,
        origin: str = "user",
        parent_id: str | None = None,
        depth: int = 0,
        role: str = "shell",
        agent: str | None = None,
        task: str | None = None,
        visibility: str = "public",
    ) -> None:
        self.id = session_id
        self.pid = pid
        self.fd = fd
        self.title = title
        self.cols = cols
        self.rows = rows
        self.exit_code: int | None = None
        self.dead = False
        self.closing = False
        # When True the session is kept (with its scrollback and exit code)
        # after the child exits so a caller can read the finished output.  A
        # plain interactive shell clears this and is removed on exit.
        self.retain_on_exit = retain_on_exit
        # Lineage and role, surfaced in ``info()`` for the client's session tree.
        self.origin = origin
        self.parent_id = parent_id
        self.depth = depth
        self.role = role
        self.agent = agent
        self.task = task
        self.visibility = visibility
        self.created_at = time.time()
        self._eof = False
        self._reaped = False

        self._loop = loop
        self._scrollback = max(scrollback, 0)
        self._buffer = bytearray()
        # Absolute number of PTY bytes emitted since the session was created.
        # ``_buffer`` holds the tail ``[buffer_start, _total)`` of that stream;
        # offsets let a client resume after a reconnect without replaying (or
        # losing) what it has already rendered.
        self._total = 0
        self._out = bytearray()
        self._subscribers: set[Subscriber] = set()
        self._on_exit = on_exit
        self._exit_event = asyncio.Event()

        os.set_blocking(fd, False)
        loop.add_reader(fd, self._on_readable)
        self.resize(cols, rows)

    # ------------------------------------------------------------------ props

    @property
    def buffer(self) -> bytes:
        """Snapshot of the scrollback for replay."""
        return bytes(self._buffer)

    @property
    def total(self) -> int:
        """Absolute offset one past the last emitted PTY byte."""
        return self._total

    @property
    def buffer_start(self) -> int:
        """Absolute offset of the first byte still retained in ``buffer``."""
        return self._total - len(self._buffer)

    def replay_since(self, offset: int) -> tuple[int, bytes]:
        """Return ``(start, data)`` for a client that has consumed ``offset``.

        ``start`` is the absolute offset of the first returned byte and is
        never earlier than :attr:`buffer_start`, so a client whose ``offset``
        has fallen out of the retained window is told where the stream now
        begins and can detect the gap.  ``data`` may be empty when the client
        is already caught up.
        """
        start = max(min(int(offset), self._total), self.buffer_start)
        return start, bytes(self._buffer[start - self.buffer_start :])

    @property
    def subscribers(self) -> set[Subscriber]:
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
        }

    # ------------------------------------------------------------- lifecycle

    def attach(self, subscriber: Subscriber) -> None:
        self._subscribers.add(subscriber)

    def detach(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    def _on_readable(self) -> None:
        while True:
            try:
                data = os.read(self.fd, READ_SIZE)
            except BlockingIOError:
                return
            except OSError:
                self._on_eof()
                return
            if not data:
                self._on_eof()
                return
            offset = self._total
            self._append(data)
            for subscriber in list(self._subscribers):
                subscriber.offer_output(self, data, offset)

    def _on_eof(self) -> None:
        """The master reported EOF/EIO: every writer has closed the slave.

        Stop the read callback -- the EOF condition is persistent and would
        otherwise busy-loop -- but wait for the SIGCHLD-driven
        :meth:`_on_child_exit` before tearing down, so the exit code is
        recorded and no final bytes are dropped.
        """
        self._eof = True
        self._remove_reader()
        if self._reaped:
            self._mark_dead()

    def _on_child_exit(self, returncode: int) -> None:
        self._reaped = True
        self.exit_code = returncode
        if self.dead:
            return
        if not self._eof:
            # SIGCHLD can arrive before the readable callback has consumed the
            # final bytes.  Drain the master now rather than closing the fd and
            # losing the tail of a fast command's output.
            self._on_readable()
        self._mark_dead()

    def _remove_reader(self) -> None:
        try:
            self._loop.remove_reader(self.fd)
        except (ValueError, OSError):
            pass

    def _append(self, data: bytes) -> None:
        self._buffer.extend(data)
        self._total += len(data)
        overflow = len(self._buffer) - self._scrollback
        if overflow <= 0:
            return
        newline = self._buffer.find(b"\n", overflow)
        if newline == -1:
            del self._buffer[:overflow]
        else:
            del self._buffer[: newline + 1]

    def _mark_dead(self) -> None:
        if self.dead:
            return
        self.dead = True
        self._remove_reader()
        try:
            self._loop.remove_writer(self.fd)
        except (ValueError, OSError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self._exit_event.set()
        for subscriber in list(self._subscribers):
            subscriber.offer_exit(self)
        if self._on_exit is not None:
            self._on_exit(self)

    def close(self) -> None:
        """Terminate the session and tear everything down."""
        if self.dead:
            return
        try:
            os.kill(self.pid, 15)  # SIGTERM
        except ProcessLookupError:
            pass
        self._mark_dead()

    # ------------------------------------------------------------- blocking

    async def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the child to exit.

        Returns the exit code, or ``None`` if *timeout* elapsed first.  Returns
        immediately (with the recorded code) for a dead session.
        """
        if self.dead:
            return self.exit_code
        try:
            await asyncio.wait_for(self._exit_event.wait(), timeout)
        except TimeoutError:
            return None
        return self.exit_code

    def capture(
        self, since: int = 0, *, strip_ansi: bool = False
    ) -> tuple[int, int, bytes]:
        """Return ``(start, end, data)`` for output at or after *since*.

        *end* is the absolute offset just past the returned bytes, ready to be
        passed back as ``since`` for the next call.
        """
        start, data = self.replay_since(since)
        end = start + len(data)
        if strip_ansi:
            data = remove_ansi(data)
        return start, end, data

    async def wait_for_text(
        self,
        pattern: str | re.Pattern[str],
        *,
        since: int = 0,
        timeout: float | None = None,
        strip_ansi: bool = True,
        flags: int = 0,
    ) -> tuple[bool, int, str | None]:
        """Wait until *pattern* appears in the output stream.

        Returns ``(matched, offset, match)`` where *offset* is the absolute byte
        offset just past the chunk that matched.  The retained window is scanned
        first, so a pattern already on screen matches immediately.
        """
        regex = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern, flags)
        _, data = self.replay_since(since)
        window = remove_ansi(data) if strip_ansi else data
        found = regex.search(window.decode("utf-8", "replace"))
        if found is not None:
            return True, self._total, found.group(0)

        waiter = _TextWaiter(regex, strip_ansi)
        self.attach(waiter)
        try:
            try:
                await asyncio.wait_for(waiter.event.wait(), timeout)
            except TimeoutError:
                return False, self._total, None
            return waiter.matched, waiter.offset or self._total, waiter.match
        finally:
            self.detach(waiter)

    async def wait_for_idle(self, quiet_ms: int, *, timeout: float | None = None) -> bool:
        """Wait until the session has produced no output for *quiet_ms*.

        Returns ``True`` when the quiet period (or the session's exit) is
        reached, ``False`` if *timeout* elapsed first.  This is the robust way
        to detect that an interactive agent has paused.
        """
        quiet = max(int(quiet_ms), 0) / 1000.0
        deadline = None if timeout is None else time.monotonic() + timeout
        waiter = _IdleWaiter()
        self.attach(waiter)
        try:
            while True:
                waiter.event.clear()
                now = time.monotonic()
                if waiter.closed:
                    return True
                quiet_left = quiet - (now - waiter.last)
                if quiet_left <= 0:
                    return True
                if deadline is not None:
                    overall_left = deadline - now
                    if overall_left <= 0:
                        return False
                    quiet_left = min(quiet_left, overall_left)
                try:
                    await asyncio.wait_for(waiter.event.wait(), quiet_left)
                except TimeoutError:
                    return True
        finally:
            self.detach(waiter)

    # ------------------------------------------------------------------- I/O

    def write(self, data: bytes) -> None:
        if not data or self.dead:
            return
        self._out.extend(data)
        self._flush()

    def _flush(self) -> None:
        while self._out and not self.dead:
            try:
                written = os.write(self.fd, self._out)
            except BlockingIOError:
                self._loop.add_writer(self.fd, self._flush)
                return
            except OSError:
                self._mark_dead()
                return
            del self._out[:written]
        if not self._out:
            try:
                self._loop.remove_writer(self.fd)
            except (ValueError, OSError):
                pass

    def resize(self, cols: int, rows: int) -> None:
        cols = max(int(cols), 1)
        rows = max(int(rows), 1)
        self.cols, self.rows = cols, rows
        if self.dead:
            return
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass


class SessionManager:
    """Owns the set of live sessions and spawns new ones."""

    #: Poll interval for the fallback child reaper used when SIGCHLD cannot be
    #: handled (the event loop runs off the main thread, as in the Tauri app).
    REAP_POLL_INTERVAL = 0.5

    def __init__(
        self,
        shell: str,
        cwd: str,
        *,
        scrollback: int = DEFAULT_SCROLLBACK,
        env: dict[str, str] | None = None,
        child_env: dict[str, str] | None = None,
        max_agent_depth: int = DEFAULT_MAX_AGENT_DEPTH,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_retained: int = DEFAULT_MAX_RETAINED,
        retained_ttl: float | None = DEFAULT_RETAINED_TTL,
        pi_command: list[str] | None = None,
    ) -> None:
        self.shell = shell
        self.cwd = cwd
        self.scrollback = scrollback
        self.env = env
        # Extra variables merged into every child (e.g. WEBTERM_URL); the
        # per-session WEBTERM_* identity is added at fork time.
        self.child_env = dict(child_env or {})
        self.max_agent_depth = max_agent_depth
        self.max_sessions = max_sessions
        self.max_retained = max_retained
        self.retained_ttl = retained_ttl
        self.pi_command = list(pi_command) if pi_command else ["pi"]
        # Filled in once the server/socket is listening (see set_control_env).
        self.control_socket: str | None = None
        self.public_url: str | None = None
        self.sessions: dict[str, Session | PiSession] = {}
        self._counter = 0
        self._listeners: list[Callable[[], None]] = []
        self._by_pid: dict[int, Session | PiSession] = {}
        self._reaper_installed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reap_task: asyncio.Task | None = None
        self._gc_task: asyncio.Task | None = None

    # ------------------------------------------------------------ maintenance

    def set_control_env(self, *, socket: str | None = None, url: str | None = None) -> None:
        """Record the control socket / URL exported to child processes."""
        if socket is not None:
            self.control_socket = socket
        if url is not None:
            self.public_url = url

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Install signal reaping and the retention sweep for *loop*."""
        self._install_reaper(loop)

    def _install_reaper(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start reaping child processes.

        Prefer SIGCHLD, which is immediate and costs nothing while idle.  When
        the loop is not on the main thread (embedded hosts such as the Tauri
        app) signal handlers are unavailable, so fall back to polling
        ``waitpid`` on a timer.
        """
        if self._reaper_installed:
            return
        self._loop = loop
        try:
            loop.add_signal_handler(signal.SIGCHLD, self._reap_children)
        except (NotImplementedError, RuntimeError, ValueError):
            self._reap_task = loop.create_task(self._reap_forever())
        self._reaper_installed = True
        if self._gc_task is None:
            self._gc_task = loop.create_task(self._gc_forever())

    async def _reap_forever(self) -> None:
        """Fallback reaper: poll for exited children on an interval."""
        while True:
            await asyncio.sleep(self.REAP_POLL_INTERVAL)
            self._reap_children()

    async def _gc_forever(self) -> None:
        """Periodically close finished one-shot sessions past TTL or cap."""
        while True:
            await asyncio.sleep(GC_INTERVAL)
            self.sweep()

    def sweep(self) -> None:
        """Close finished retained sessions that outlived TTL or the cap."""
        now = time.time()
        finished = sorted(
            (
                s
                for s in self.sessions.values()
                if s.dead and s.retain_on_exit and getattr(s, "kind", "shell") != "pi"
            ),
            key=lambda s: s.created_at,
        )
        doomed: list[Session | PiSession] = []
        if self.retained_ttl is not None:
            doomed.extend(s for s in finished if now - s.created_at >= self.retained_ttl)
            remaining = [s for s in finished if s not in doomed]
        else:
            remaining = finished
        if self.max_retained is not None and len(remaining) > self.max_retained:
            doomed.extend(remaining[: len(remaining) - self.max_retained])
        for session in doomed:
            self._drop(session)

    def _drop(self, session: Session | PiSession) -> None:
        if self.sessions.pop(session.id, None) is not None:
            log.info("dropping finished session %s (%s)", session.id, session.title)
            self.notify()

    def _remove_reaper(self) -> None:
        """Stop reaping children before the event loop closes.

        The loop's signal wakeup fd is closed on shutdown; leaving the SIGCHLD
        handler installed lets a late signal write to the closed fd, which
        Python reports as an ignored ``OSError: [Errno 9] Bad file descriptor``.
        A fallback poll task is cancelled instead.
        """
        if self._gc_task is not None:
            self._gc_task.cancel()
            self._gc_task = None
        if not self._reaper_installed:
            return
        self._reaper_installed = False
        if self._reap_task is not None:
            self._reap_task.cancel()
            self._reap_task = None
        loop = self._loop
        if loop is None:
            return
        try:
            loop.remove_signal_handler(signal.SIGCHLD)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        # Collect any children that already exited while the handler was
        # installed, so they do not linger as zombies until process exit.
        self._reap_children()

    def _reap_children(self) -> None:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            session = self._by_pid.pop(pid, None)
            if session is not None:
                try:
                    code = os.waitstatus_to_exitcode(status)
                except ValueError:
                    code = -1
                session._on_child_exit(code)

    # ---------------------------------------------------------------- queries

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def notify(self) -> None:
        for callback in list(self._listeners):
            callback()

    def list_info(self, *, include_private: bool = True) -> list[dict[str, object]]:
        return [
            session.info()
            for session in self.sessions.values()
            if include_private or session.visibility != "private"
        ]

    def get(self, session_id: str) -> Session | PiSession | None:
        return self.sessions.get(session_id)

    def children_of(self, session_id: str) -> list[Session | PiSession]:
        """Sessions whose ``parent_id`` is *session_id* (one level)."""
        return [s for s in self.sessions.values() if s.parent_id == session_id]

    def descendants_of(self, session_id: str) -> list[Session | PiSession]:
        """Every session below *session_id*, any depth, excluding it."""
        found: list[Session | PiSession] = []
        stack = [session_id]
        while stack:
            for child in self.children_of(stack.pop()):
                found.append(child)
                stack.append(child.id)
        return found

    # -------------------------------------------------------------- lifecycle

    def _guard(self, parent_id: str | None) -> int:
        """Validate limits and return the child's depth (raises on refusal)."""
        if self.max_sessions is not None and len(self.sessions) >= self.max_sessions:
            raise SessionLimitError(
                f"session limit reached ({self.max_sessions}); close one first"
            )
        if not parent_id:
            return 0
        parent = self.sessions.get(parent_id)
        if parent is None:
            return 0
        depth = parent.depth + 1
        if self.max_agent_depth is not None and depth > self.max_agent_depth:
            raise DepthExceededError(
                f"agent nesting depth {depth} exceeds the limit of {self.max_agent_depth}"
            )
        return depth

    def _register(
        self,
        pid: int,
        fd: int,
        title: str,
        *,
        counter: int,
        cols: int,
        rows: int,
        loop: asyncio.AbstractEventLoop,
        retain_on_exit: bool = False,
        origin: str = "user",
        parent_id: str | None = None,
        depth: int = 0,
        role: str = "shell",
        agent: str | None = None,
        task: str | None = None,
        visibility: str = "public",
    ) -> Session:
        """Wrap a freshly forked PTY in a :class:`Session` and publish it."""
        session_id = f"{counter}-{pid}"
        session = Session(
            session_id,
            pid,
            fd,
            title,
            cols=cols,
            rows=rows,
            loop=loop,
            scrollback=self.scrollback,
            on_exit=self._on_session_exit,
            retain_on_exit=retain_on_exit,
            origin=origin,
            parent_id=parent_id,
            depth=depth,
            role=role,
            agent=agent,
            task=task,
            visibility=visibility,
        )
        self.sessions[session_id] = session
        self._by_pid[pid] = session
        self.notify()
        return session

    def create(
        self,
        *,
        cols: int = 80,
        rows: int = 24,
        title: str | None = None,
        command: list[str] | str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        parent_id: str | None = None,
        origin: str = "user",
        role: str = "shell",
        agent: str | None = None,
        task: str | None = None,
        visibility: str = "public",
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> Session:
        loop = loop or asyncio.get_running_loop()
        self._install_reaper(loop)
        depth = self._guard(parent_id)
        self._counter += 1
        counter = self._counter
        if title is None:
            title = task or _command_title(command) or f"shell {counter}"

        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - runs in the child process
            self._exec_child(cwd or self.cwd, env, counter, parent_id, depth, agent)
            os._exit(127)

        session = self._register(
            pid,
            fd,
            title,
            counter=counter,
            cols=cols,
            rows=rows,
            loop=loop,
            origin=origin,
            parent_id=parent_id,
            depth=depth,
            role=role,
            agent=agent,
            task=task,
            visibility=visibility,
        )
        if command:
            # Type the command into the interactive shell so it behaves exactly
            # as if the user entered it: errors are reported by the shell and
            # control returns to the prompt when it finishes.  A string command
            # is sent verbatim so shell syntax (``&&``, pipes, globs, ...)
            # keeps working; an argv list is quoted argument by argument.
            line = (command if isinstance(command, str) else shlex.join(command)) + "\n"
            loop.call_later(0.2, session.write, line.encode())
        return session

    def create_single(
        self,
        command: list[str] | str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        title: str | None = None,
        cols: int = 80,
        rows: int = 24,
        parent_id: str | None = None,
        origin: str = "user",
        role: str = "shell",
        agent: str | None = None,
        task: str | None = None,
        visibility: str = "public",
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> Session:
        """Run *command* to completion in its own PTY, with no lingering shell.

        Unlike :meth:`create`, the command is executed directly (``$SHELL -c``
        for a string, ``execvpe`` for an argv list) so the session exits when
        the command does.  The finished session is retained with its scrollback
        and exit code until :meth:`kill` closes it, so a caller can still read
        the output.  This is the primitive behind one-shot runs and
        agent/sub-agent workflows.
        """
        argv = _single_argv(self.shell, command)
        loop = loop or asyncio.get_running_loop()
        self._install_reaper(loop)
        depth = self._guard(parent_id)
        self._counter += 1
        counter = self._counter
        if title is None:
            title = task or _command_title(command) or f"shell {counter}"

        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - runs in the child process
            self._exec_single(argv, cwd or self.cwd, env, counter, parent_id, depth, agent)
            os._exit(127)

        return self._register(
            pid,
            fd,
            title,
            counter=counter,
            cols=cols,
            rows=rows,
            loop=loop,
            retain_on_exit=True,
            origin=origin,
            parent_id=parent_id,
            depth=depth,
            role=role,
            agent=agent,
            task=task,
            visibility=visibility,
        )

    def create_pi(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        title: str | None = None,
        parent_id: str | None = None,
        origin: str = "user",
        role: str = "agent",
        agent: str | None = None,
        task: str | None = None,
        visibility: str = "public",
        trusted: bool | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> PiSession:
        """Start a pi RPC session from a fully resolved argv.

        The caller supplies ``--mode rpc`` and trust/session flags; the manager
        owns lineage, limits, environment preparation, and reaping.
        """
        loop = loop or asyncio.get_running_loop()
        self._install_reaper(loop)
        depth = self._guard(parent_id)
        self._counter += 1
        counter = self._counter
        session_cwd = os.path.abspath(os.path.expanduser(cwd or self.cwd))
        pid, stdin_fd, stdout_fd, stderr_fd = fork_pi(
            self,
            argv,
            cwd=session_cwd,
            env=env,
            counter=counter,
            parent_id=parent_id,
            depth=depth,
            agent=agent,
        )
        session_id = f"{counter}-{pid}"
        session = PiSession(
            session_id,
            pid,
            stdin_fd,
            stdout_fd,
            stderr_fd,
            title or task or _command_title(argv) or f"pi {counter}",
            cwd=session_cwd,
            loop=loop,
            on_exit=self._on_session_exit,
            on_change=self.notify,
            retain_on_exit=True,
            origin=origin,
            parent_id=parent_id,
            depth=depth,
            role=role,
            agent=agent,
            task=task,
            visibility=visibility,
            trusted=trusted,
        )
        self.sessions[session_id] = session
        self._by_pid[pid] = session
        self.notify()
        return session

    def spawn(
        self,
        argv: list[str],
        *,
        interactive: bool = False,
        agent: str | None = None,
        task: str | None = None,
        title: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        parent_id: str | None = None,
        visibility: str = "public",
        cols: int = 80,
        rows: int = 24,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> Session:
        """Start an agent session (a sub-agent) from a resolved argv.

        *interactive* keeps a shell around the agent (a TUI the human can take
        over); otherwise the agent runs to completion as a retained one-shot.
        """
        common = dict(
            cwd=cwd,
            env=env,
            title=title or task or _command_title(argv),
            cols=cols,
            rows=rows,
            parent_id=parent_id,
            origin="agent",
            role="agent",
            agent=agent,
            task=task,
            visibility=visibility,
            loop=loop,
        )
        if interactive:
            return self.create(command=argv, **common)
        return self.create_single(argv, **common)

    # ------------------------------------------------------------- child exec

    def _prepare_child(
        self,
        cwd: str,
        extra_env: dict[str, str] | None,
        session_id: str,
        parent_id: str | None,
        depth: int,
        agent: str | None,
    ) -> dict[str, str]:
        """Reset signals and build the child environment (runs in the child)."""
        # The server blocks SIGCHLD (for its reaper) and Python ignores SIGPIPE;
        # a child inheriting that state can misbehave (broken subprocess handling
        # in Node/TUI apps).  Give the new process a clean signal setup.
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, [])
        except (AttributeError, OSError):
            pass
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGPIPE, signal.SIGCHLD):
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (ValueError, OSError):
                pass

        env = dict(os.environ if self.env is None else self.env)
        env.update(self.child_env)
        if extra_env:
            env.update({key: str(value) for key, value in extra_env.items()})
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        # Discovery for nested agents: a process can find the control socket and
        # its own place in the session tree without any configuration.
        if self.control_socket:
            env["WEBTERM_SOCKET"] = self.control_socket
        if self.public_url:
            env["WEBTERM_URL"] = self.public_url
        env["WEBTERM_SESSION_ID"] = session_id
        env["WEBTERM_PARENT_SESSION"] = parent_id or ""
        env["WEBTERM_DEPTH"] = str(depth)
        if agent:
            env["WEBTERM_AGENT"] = agent
        try:
            os.chdir(cwd)
        except OSError:
            pass
        return env

    def _exec_child(
        self,
        cwd: str,
        extra_env: dict[str, str] | None,
        counter: int,
        parent_id: str | None,
        depth: int,
        agent: str | None,
    ) -> None:  # pragma: no cover - runs in the child process
        session_id = f"{counter}-{os.getpid()}"
        env = self._prepare_child(cwd, extra_env, session_id, parent_id, depth, agent)
        try:
            os.execvpe(self.shell, [os.path.basename(self.shell)], env)
        except OSError:
            os._exit(127)

    def _exec_single(
        self,
        argv: list[str],
        cwd: str,
        extra_env: dict[str, str] | None,
        counter: int,
        parent_id: str | None,
        depth: int,
        agent: str | None,
    ) -> None:  # pragma: no cover - runs in the child process
        session_id = f"{counter}-{os.getpid()}"
        env = self._prepare_child(cwd, extra_env, session_id, parent_id, depth, agent)
        try:
            os.execvpe(argv[0], argv, env)
        except OSError:
            os._exit(127)

    # ---------------------------------------------------------------- teardown

    def _on_session_exit(self, session: Session | PiSession) -> None:
        if session.retain_on_exit:
            # A one-shot run or pi conversation: keep the record and output
            # around so a caller can read it until the session is closed.
            log.info(
                "session %s (%s) finished with code %s (retained)",
                session.id,
                session.title,
                session.exit_code,
            )
            self.notify()
            return
        self.sessions.pop(session.id, None)
        log.info(
            "session %s (%s) exited with code %s",
            session.id,
            session.title,
            session.exit_code,
        )
        self.notify()

    def _close_one(self, session: Session | PiSession) -> bool:
        """Close or drop a single session, reporting whether it changed."""
        if session.dead:
            if self.sessions.pop(session.id, None) is None:
                return False
            self.notify()
            return True
        if session.closing:
            return False
        # Never retain a session the caller is actively terminating, even if
        # it was created as a one-shot run.
        session.retain_on_exit = False
        session.close()
        return True

    def kill(self, session_id: str, *, tree: bool = True) -> list[str]:
        """Terminate a session; by default its whole subtree.

        Returns the ids actually closed.  With ``tree=True`` descendants are
        closed before the session itself; with ``tree=False`` children are
        orphaned to the root.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return []
        targets: list[Session | PiSession] = []
        if tree:
            targets = sorted(
                self.descendants_of(session_id), key=lambda s: s.depth, reverse=True
            )
        targets.append(session)
        killed: list[str] = []
        for target in targets:
            if self._close_one(target):
                killed.append(target.id)
        return killed

    def close_all(self) -> None:
        for session in list(self.sessions.values()):
            force_close = getattr(session, "force_close", None)
            if force_close is not None:
                force_close()
            else:
                session.close()
        self._remove_reaper()

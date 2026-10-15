"""PTY-backed terminal sessions and their manager.

A :class:`Session` owns a pseudo-terminal master fd and the shell process
attached to it.  Output is fanned out to any number of subscribers and kept in
a bounded scrollback buffer so a browser can reattach and replay history.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import shlex
import signal
import struct
import termios
from collections.abc import Callable
from typing import Protocol, runtime_checkable

DEFAULT_SCROLLBACK = 100_000
READ_SIZE = 65_536

log = logging.getLogger("webterm")


def _command_title(command: list[str] | str | None) -> str | None:
    """Derive a session title from an argv list or a shell command string."""
    if not command:
        return None
    if isinstance(command, str):
        parts = command.split()
        return os.path.basename(parts[0]) if parts else None
    return os.path.basename(command[0]) if command else None


@runtime_checkable
class Subscriber(Protocol):
    """Receives output from a session it is attached to."""

    def offer_output(self, session: Session, data: bytes) -> None: ...

    def offer_exit(self, session: Session) -> None: ...


class Session:
    """A single PTY + child process with scrollback and fan-out."""

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
    ) -> None:
        self.id = session_id
        self.pid = pid
        self.fd = fd
        self.title = title
        self.cols = cols
        self.rows = rows
        self.exit_code: int | None = None
        self.dead = False

        self._loop = loop
        self._scrollback = max(scrollback, 0)
        self._buffer = bytearray()
        self._out = bytearray()
        self._subscribers: set[Subscriber] = set()
        self._on_exit = on_exit

        os.set_blocking(fd, False)
        loop.add_reader(fd, self._on_readable)
        self.resize(cols, rows)

    # ------------------------------------------------------------------ props

    @property
    def buffer(self) -> bytes:
        """Snapshot of the scrollback for replay."""
        return bytes(self._buffer)

    @property
    def subscribers(self) -> set[Subscriber]:
        return self._subscribers

    def info(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "cols": self.cols,
            "rows": self.rows,
            "pid": self.pid,
            "alive": not self.dead,
            "exit_code": self.exit_code,
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
                self._mark_dead()
                return
            if not data:
                self._mark_dead()
                return
            self._append(data)
            for subscriber in list(self._subscribers):
                subscriber.offer_output(self, data)

    def _on_child_exit(self, returncode: int) -> None:
        self.exit_code = returncode
        self._mark_dead()

    def _append(self, data: bytes) -> None:
        self._buffer.extend(data)
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
        try:
            self._loop.remove_reader(self.fd)
        except (ValueError, OSError):
            pass
        try:
            self._loop.remove_writer(self.fd)
        except (ValueError, OSError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
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

    def __init__(
        self,
        shell: str,
        cwd: str,
        *,
        scrollback: int = DEFAULT_SCROLLBACK,
        env: dict[str, str] | None = None,
    ) -> None:
        self.shell = shell
        self.cwd = cwd
        self.scrollback = scrollback
        self.env = env
        self.sessions: dict[str, Session] = {}
        self._counter = 0
        self._listeners: list[Callable[[], None]] = []
        self._by_pid: dict[int, Session] = {}
        self._reaper_installed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def _install_reaper(self, loop: asyncio.AbstractEventLoop) -> None:
        """Reap child processes via SIGCHLD (asyncio removed child watchers)."""
        if self._reaper_installed:
            return
        try:
            loop.add_signal_handler(signal.SIGCHLD, self._reap_children)
        except (NotImplementedError, RuntimeError, ValueError):
            return
        self._loop = loop
        self._reaper_installed = True

    def _remove_reaper(self) -> None:
        """Uninstall the SIGCHLD handler before the event loop closes.

        The loop's signal wakeup fd is closed on shutdown; leaving the handler
        installed lets a late SIGCHLD write to the closed fd, which Python
        reports as an ignored ``OSError: [Errno 9] Bad file descriptor``.
        """
        if not self._reaper_installed:
            return
        self._reaper_installed = False
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

    def list_info(self) -> list[dict[str, object]]:
        return [session.info() for session in self.sessions.values()]

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def create(
        self,
        *,
        cols: int = 80,
        rows: int = 24,
        title: str | None = None,
        command: list[str] | str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> Session:
        loop = loop or asyncio.get_running_loop()
        self._install_reaper(loop)
        self._counter += 1
        if title is None:
            title = _command_title(command) or f"shell {self._counter}"

        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - runs in the child process
            self._exec_child()
            os._exit(127)

        session_id = f"{self._counter}-{pid}"
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
        )
        self.sessions[session_id] = session
        self._by_pid[pid] = session
        if command:
            # Type the command into the interactive shell so it behaves exactly
            # as if the user entered it: errors are reported by the shell and
            # control returns to the prompt when it finishes.  A string command
            # is sent verbatim so shell syntax (``&&``, pipes, globs, ...)
            # keeps working; an argv list is quoted argument by argument.
            line = (command if isinstance(command, str) else shlex.join(command)) + "\n"
            loop.call_later(0.2, session.write, line.encode())
        self.notify()
        return session

    def _exec_child(self) -> None:  # pragma: no cover - runs in the child process
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
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        try:
            os.chdir(self.cwd)
        except OSError:
            pass
        try:
            os.execvpe(self.shell, [os.path.basename(self.shell)], env)
        except OSError:
            os._exit(127)

    def _on_session_exit(self, session: Session) -> None:
        self.sessions.pop(session.id, None)
        log.info(
            "session %s (%s) exited with code %s",
            session.id,
            session.title,
            session.exit_code,
        )
        self.notify()

    def kill(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        session.close()
        return True

    def close_all(self) -> None:
        for session in list(self.sessions.values()):
            session.close()
        self._remove_reaper()

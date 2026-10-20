"""Process plumbing for structured agent sessions.

Kept separate from :mod:`webterm.agent.session` so the session class has no
circular import with :class:`webterm.session.SessionManager`, which owns the
child reaper and the shared child environment.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..session import SessionManager


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

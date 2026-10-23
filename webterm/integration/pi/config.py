"""pi integration configuration and CLI defaults.

The core still imports this module directly; these are ordinary helpers, not a
backend plugin interface.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .storage import prepare_pi_session_dir

if TYPE_CHECKING:
    from ...session import SessionManager


def default_session_dir() -> str:
    """Default webterm-managed root for persisted pi session files."""
    explicit = os.environ.get("WEBTERM_PI_SESSION_DIR")
    if explicit:
        return explicit
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "webterm", "pi-sessions")


def command() -> list[str]:
    """Return the default pi argv used for browser-created sessions."""
    return [shutil.which("pi") or "pi"]


def available() -> bool:
    """Return True when a pi executable is on PATH."""
    return shutil.which("pi") is not None


#: The pi release webterm is developed and tested against.  A different version
#: is not blocked — pi's RPC surface is version-agnostic enough that the reducer
#: ignores unknown records — but it is reported so a protocol change is not
#: mistaken for a webterm bug.
TESTED_VERSION = "1.3.0"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def probe_version(command: list[str] | str | None = None) -> str | None:
    """Return the installed pi version, or None when it cannot be read.

    Called once at startup, before the event loop runs: ``pi --version`` is a
    short-lived synchronous call, and probing lazily inside a coroutine would
    block the loop.
    """
    argv = list(command) if isinstance(command, list) and command else command()
    try:
        result = subprocess.run(
            [*argv, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(result.stdout or "")
    return match.group(0) if match else None


def version_is_tested(version: str | None) -> bool:
    """Return True when *version* is the release this webterm was tested with."""
    return version == TESTED_VERSION


def extension_path() -> str | None:
    """Return the bundled pi extension path, if it is present."""
    path = Path(__file__).with_name("extension.ts")
    return str(path) if path.is_file() else None


def configure(manager: SessionManager, session_dir: str | None) -> str:
    """Create the pi session root and attach pi options to *manager*."""
    root = prepare_pi_session_dir(session_dir or default_session_dir())
    manager.pi_session_dir = root
    extension = extension_path()
    if extension is not None:
        manager.pi_extension = extension
    return root


def frontend_config(
    session_dir: str | None,
    cwd: str,
    *,
    is_available: bool | None = None,
    default_model: str | None = None,
    version: str | None = None,
) -> dict[str, object]:
    """Return the pi block exposed through ``/config.js``."""
    return {
        "available": available() if is_available is None else is_available,
        "cwd": os.path.abspath(os.path.expanduser(cwd)),
        "sessionDir": session_dir,
        "defaultModel": default_model,
        "version": version,
        "testedVersion": TESTED_VERSION,
    }


def default_agent_profiles(
    extension: str | None = None,
) -> dict[str, dict[str, list[str] | str]]:
    """Built-in pi agent profile when pi is installed.

    Every mode loads the bundled extension, so a pi process webterm starts always
    has the webterm tools and the prompt context that describes them.  Children
    inherit it, which is what lets a sub-agent spawn its own children.
    """
    pi = shutil.which("pi")
    if not pi:
        return {}
    extension_args = ["-e", extension] if extension else []
    rpc = [pi, "--mode", "rpc", "--no-session", "--no-approve", *extension_args]
    return {
        "pi": {
            "print": [pi, *extension_args, "-p", "{task}"],
            "interactive": [pi, *extension_args, "{task}"],
            "rpc": rpc,
        }
    }


def default_launcher(extension: str | None = None) -> list[dict[str, object]]:
    """The default sidebar launcher for the pi TUI, when pi is installed."""
    pi = shutil.which("pi")
    if not pi:
        return []
    command = [pi, "-e", extension] if extension else [pi]
    return [{"label": "pi agent", "command": command}]

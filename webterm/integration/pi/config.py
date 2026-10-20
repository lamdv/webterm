"""pi integration configuration and CLI defaults.

The core still imports this module directly; these are ordinary helpers, not a
backend plugin interface.
"""

from __future__ import annotations

import os
import shutil
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
) -> dict[str, object]:
    """Return the pi block exposed through ``/config.js``."""
    return {
        "available": available() if is_available is None else is_available,
        "cwd": os.path.abspath(os.path.expanduser(cwd)),
        "sessionDir": session_dir,
        "defaultModel": default_model,
    }


def default_agent_profiles(
    extension: str | None = None,
) -> dict[str, dict[str, list[str]]]:
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

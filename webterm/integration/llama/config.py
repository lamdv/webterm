"""Configuration helpers for the llama-server agent profile."""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Thinking levels accepted by ``--thinking`` and the canonical ``set_thinking``
#: command.  ``default`` leaves the server's own setting untouched; the rest map
#: onto llama-server's ``reasoning_effort`` values.
THINKING_LEVELS = (
    "default",
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def default_url() -> str | None:
    """Return the configured llama server base URL, if any."""
    return _env("WEBTERM_LLAMA_URL") or _env("LLAMA_SWAP_URL")


def default_model() -> str | None:
    """Return the configured default model, if any."""
    return _env("WEBTERM_LLAMA_MODEL")


def default_system_prompt() -> str | None:
    """Return the configured default system prompt, if any."""
    return _env("WEBTERM_LLAMA_SYSTEM_PROMPT")


def default_thinking() -> str | None:
    """Return the configured default thinking level, if any."""
    return _env("WEBTERM_LLAMA_THINKING")


def default_prompt_dir(override: str | None = None) -> Path:
    """Return the system-prompt library directory.

    An explicit *override* wins, then ``$WEBTERM_PROMPT_DIR``, then
    ``$XDG_CONFIG_HOME/webterm/prompts``, then ``~/.config/webterm/prompts``.
    This is a pure lookup: it reads the environment when called and never
    creates the directory.
    """
    for value in (override, _env("WEBTERM_PROMPT_DIR")):
        if value and value.strip():
            return Path(value.strip()).expanduser()
    xdg = _env("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "webterm" / "prompts"
    return Path.home() / ".config" / "webterm" / "prompts"


def agent_profile(
    url: str,
    model: str | None = None,
    system_prompt: str | None = None,
    thinking: str | None = None,
) -> dict[str, object]:
    """Build the ``--agent-rpc`` style profile for a llama server.

    The adapter runs in the webterm interpreter, so an installed webterm works
    without requiring a separate console script.  ``adapter`` tells the server
    to use the canonical codec rather than pi's RPC dialect.
    """
    argv = [sys.executable, "-m", "webterm.integration.llama", "--url", url]
    if model:
        argv.extend(["--model", model])
    if system_prompt:
        argv.extend(["--system-prompt", system_prompt])
    if thinking:
        argv.extend(["--thinking", thinking])
    return {"rpc": argv, "adapter": "llama"}

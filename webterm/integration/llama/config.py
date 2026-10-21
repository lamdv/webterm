"""Configuration helpers for the llama-server agent profile."""

from __future__ import annotations

import os
import sys


def default_url() -> str | None:
    """Return the configured llama server base URL, if any."""
    value = os.environ.get("WEBTERM_LLAMA_URL") or os.environ.get("LLAMA_SWAP_URL")
    return value.strip() if value and value.strip() else None


def default_model() -> str | None:
    """Return the configured default model, if any."""
    value = os.environ.get("WEBTERM_LLAMA_MODEL")
    return value.strip() if value and value.strip() else None


def agent_profile(url: str, model: str | None = None) -> dict[str, object]:
    """Build the ``--agent-rpc`` style profile for a llama server.

    The adapter runs in the webterm interpreter, so an installed webterm works
    without requiring a separate console script.  ``adapter`` tells the server
    to use the canonical codec rather than pi's RPC dialect.
    """
    argv = [sys.executable, "-m", "webterm.integration.llama", "--url", url]
    if model:
        argv.extend(["--model", model])
    return {"rpc": argv, "adapter": "llama"}

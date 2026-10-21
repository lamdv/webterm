"""llama-server / llama-swap integration.

This package contains a canonical agent adapter that turns a local
OpenAI-compatible llama server into a webterm chat session.  It has no
tools or shell access: it is a chat partner, not an autonomous worker.
"""

from __future__ import annotations

from .adapter import LlamaAgent, main

__all__ = ["LlamaAgent", "main"]

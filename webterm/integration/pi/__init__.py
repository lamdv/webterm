"""pi integration.

This package is the pi-specific vertical slice: the RPC session, session-file
storage, CLI/config helpers, and the bundled pi extension.  The rest of webterm
imports it directly.  It is deliberately not a plugin interface.
"""

from __future__ import annotations

from .session import (
    HISTORY_ENTRY_LIMIT,
    MAX_HISTORY_ENTRY_LIMIT,
    SNAPSHOT_ENTRY_LIMIT,
    PiError,
    PiSession,
    PiSessionClosed,
    PiSubscriber,
    fork_pi,
)

__all__ = [
    "HISTORY_ENTRY_LIMIT",
    "MAX_HISTORY_ENTRY_LIMIT",
    "SNAPSHOT_ENTRY_LIMIT",
    "PiError",
    "PiSession",
    "PiSessionClosed",
    "PiSubscriber",
    "fork_pi",
]

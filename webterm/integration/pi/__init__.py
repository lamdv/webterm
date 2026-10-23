"""pi integration.

This package is the pi-specific vertical slice: the RPC session, session-file
storage, CLI/config helpers, and the bundled pi extension.  The rest of webterm
imports it directly.  It is deliberately not a plugin interface.
"""

from __future__ import annotations

from .canonical import (
    PI_CAPABILITIES,
    PI_COMMANDS,
    PI_DECLARED_COMMANDS,
    PI_DIRECT_COMMANDS,
    PI_HELP,
    PI_SESSION_COMMANDS,
    CanonicalCommandError,
    PiCanonicalTranslator,
    canonical_command_data,
    canonical_response,
    pi_capabilities,
    pi_command_from_canonical,
    pi_command_images,
    pi_declared_result,
    pi_declared_rpc,
    pi_dialog_response_to_pi,
    pi_dialog_to_canonical,
    pi_export,
    pi_history_to_canonical,
    pi_models_to_canonical,
    pi_snapshot_to_canonical,
    pi_state_to_canonical,
    pi_ui_request_to_events,
    pi_ui_resolved_to_canonical,
)
from .models import PiModelCache, normalize_models
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
    "PI_CAPABILITIES",
    "PI_COMMANDS",
    "PI_DECLARED_COMMANDS",
    "PI_DIRECT_COMMANDS",
    "PI_HELP",
    "PI_SESSION_COMMANDS",
    "CanonicalCommandError",
    "PiCanonicalTranslator",
    "MAX_HISTORY_ENTRY_LIMIT",
    "SNAPSHOT_ENTRY_LIMIT",
    "PiError",
    "PiModelCache",
    "PiSession",
    "PiSessionClosed",
    "PiSubscriber",
    "fork_pi",
    "normalize_models",
    "canonical_command_data",
    "canonical_response",
    "pi_capabilities",
    "pi_command_from_canonical",
    "pi_command_images",
    "pi_dialog_response_to_pi",
    "pi_declared_result",
    "pi_declared_rpc",
    "pi_dialog_to_canonical",
    "pi_export",
    "pi_history_to_canonical",
    "pi_models_to_canonical",
    "pi_snapshot_to_canonical",
    "pi_state_to_canonical",
    "pi_ui_request_to_events",
    "pi_ui_resolved_to_canonical",
]

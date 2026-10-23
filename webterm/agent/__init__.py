"""Agent-agnostic structured sessions and the canonical JSONL protocol."""

from __future__ import annotations

from .process import AgentProcess, fork_agent
from .protocol import (
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOLS,
    Capabilities,
    Hello,
    ProtocolError,
    parse_hello,
)
from .session import (
    AGENT_FORCE_GRACE,
    AGENT_STDIN_GRACE,
    AGENT_TERM_GRACE,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_HANDSHAKE_TIMEOUT,
    AgentClosed,
    AgentCommandError,
    AgentError,
    AgentProtocolError,
    AgentSession,
    AgentSubscriber,
    AgentTimeout,
)

__all__ = [
    "AGENT_FORCE_GRACE",
    "AGENT_STDIN_GRACE",
    "AGENT_TERM_GRACE",
    "AgentClosed",
    "AgentCommandError",
    "AgentError",
    "AgentProcess",
    "AgentProtocolError",
    "AgentSession",
    "AgentSubscriber",
    "AgentTimeout",
    "Capabilities",
    "DEFAULT_COMMAND_TIMEOUT",
    "DEFAULT_HANDSHAKE_TIMEOUT",
    "Hello",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "SUPPORTED_PROTOCOLS",
    "fork_agent",
    "parse_hello",
]

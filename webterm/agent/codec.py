"""Codecs: the adapter seam between webterm and a structured agent child.

The server speaks one vocabulary — the canonical protocol in
:mod:`webterm.agent.protocol` — to browsers, scripts, and the control socket.
A codec turns that vocabulary into whatever an adapter's child process
understands and normalizes the answers back:

* :class:`NativeCodec` talks to an :class:`~webterm.agent.session.AgentSession`,
  whose child already speaks the canonical protocol, so nothing is translated.
* ``PiCodec`` (in ``webterm/integration/pi/codec.py``) talks to a ``PiSession``,
  whose child speaks pi's RPC dialect;
  :mod:`webterm.integration.pi.canonical` owns that mapping.

Adding an adapter means adding a codec, not touching ``server.py``: the server
looks the codec up once per session from its ``adapter`` attribute.
"""

from __future__ import annotations

from typing import Any

from .protocol import (
    CANONICAL_COMMANDS,
    CanonicalCommandError,
    response_record,
)
from .session import (
    DEFAULT_COMMAND_TIMEOUT,
    AgentCommandError,
    AgentError,
    AgentSession,
)

#: Default timeout for one canonical command round trip.
DEFAULT_AGENT_COMMAND_TIMEOUT = 30.0


class CodecError(Exception):
    """A canonical operation an adapter cannot perform."""


class UnknownDialog(CodecError):
    """No dialog with that id is pending; the caller gets ``not_found``."""


class AgentCodec:
    """The client-facing operations of one agent dialect."""

    #: Session attribute value this codec serves (``session.adapter``).
    adapter = ""
    #: Canonical command names the codec accepts.
    commands: frozenset[str] = CANONICAL_COMMANDS

    def capabilities(self, session: Any) -> dict:
        """Return the canonical capability declaration for *session*."""
        raise NotImplementedError

    async def snapshot(self, session: Any, *, limit: int | None = None) -> dict:
        """Return the canonical snapshot, including its ``cursor``."""
        raise NotImplementedError

    async def history(self, session: Any, before: str, limit: int) -> dict:
        """Return one backwards page of transcript entries."""
        raise NotImplementedError

    async def models(
        self, session: Any, manager: Any, *, refresh: bool = False
    ) -> dict:
        """Return the canonical model list for the session's environment."""
        raise NotImplementedError

    def resolve_dialog(self, session: Any, dialog_id: str, response: dict) -> None:
        """Answer a pending dialog; raise :class:`UnknownDialog` if it is gone."""
        raise NotImplementedError

    async def command(
        self,
        session: Any,
        name: str,
        command: dict,
        command_id: str,
        *,
        timeout: float | None = None,
    ) -> dict:
        """Run one validated command and return a canonical ``response``."""
        raise NotImplementedError

    async def result(self, session: Any, *, settled: bool) -> dict:
        """Return the structured result of a sub-agent spawn."""
        raise NotImplementedError


class NativeCodec(AgentCodec):
    """The child speaks the canonical protocol itself."""

    adapter = "generic"

    def capabilities(self, session: AgentSession) -> dict:
        return session.capabilities.to_dict()

    async def snapshot(self, session: AgentSession, *, limit: int | None = None) -> dict:
        return await session.snapshot(limit=limit)

    async def history(self, session: AgentSession, before: str, limit: int) -> dict:
        return await session.history(before, limit=limit)

    async def models(
        self, session: AgentSession, manager: Any, *, refresh: bool = False
    ) -> dict:
        return await session.models(refresh=refresh)

    def resolve_dialog(self, session: AgentSession, dialog_id: str, response: dict) -> None:
        try:
            session.resolve_dialog(dialog_id, response)
        except AgentError as exc:
            raise UnknownDialog(str(exc)) from exc

    async def command(
        self,
        session: AgentSession,
        name: str,
        command: dict,
        command_id: str,
        *,
        timeout: float | None = None,
    ) -> dict:
        timeout = DEFAULT_COMMAND_TIMEOUT if timeout is None else timeout
        if name == "shutdown":
            session.close()
            return response_record(name, command_id)
        fields = {
            key: value
            for key, value in command.items()
            if key not in ("type", "id", "timeout")
        }
        try:
            if name == "prompt":
                message = fields.pop("message", None)
                if not isinstance(message, str) or not message.strip():
                    raise CanonicalCommandError(
                        "prompt message must be a non-empty string"
                    )
                data = await session.prompt(message, timeout=timeout)
            else:
                data = await session.command(name, timeout=timeout, **fields)
        except AgentCommandError as exc:
            return response_record(name, command_id, error=exc.message)
        except ValueError as exc:
            raise CanonicalCommandError(str(exc)) from exc
        return response_record(name, command_id, data=data)

    async def result(self, session: AgentSession, *, settled: bool) -> dict:
        result: dict[str, object] = {
            "session_id": session.id,
            "settled": settled,
            "exit_code": session.exit_code,
        }
        if session.dead:
            return result
        try:
            result["state"] = await session.get_state(timeout=5.0)
        except (AgentError, TimeoutError) as exc:
            result["error"] = str(exc)
        return result


NATIVE_CODEC = NativeCodec()

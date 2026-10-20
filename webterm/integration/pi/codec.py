"""The pi codec: canonical commands over pi's RPC dialect.

:class:`PiSession` owns the pi child process and its RPC correlation.  This
module is the adapter seam: it presents that session through the canonical
surface the server uses for every structured agent, so ``server.py`` no longer
needs a pi branch.  The record-level translation stays in
:mod:`webterm.integration.pi.canonical`.
"""

from __future__ import annotations

from typing import Any

from ...agent.codec import (
    DEFAULT_AGENT_COMMAND_TIMEOUT,
    AgentCodec,
    UnknownDialog,
)
from .canonical import (
    PI_CAPABILITIES,
    PI_COMMANDS,
    canonical_response,
    pi_command_from_canonical,
    pi_dialog_response_to_pi,
    pi_history_to_canonical,
    pi_models_to_canonical,
    pi_snapshot_to_canonical,
)
from .session import PiError, PiSession, PiSessionClosed


def _dialog_kind(session: PiSession, dialog_id: str) -> str | None:
    """Return the pi extension UI method for a pending dialog."""
    for request in session.ui_snapshot()["requests"]:
        if request.get("id") == dialog_id:
            method = request.get("method")
            return method if isinstance(method, str) else None
    return None


class PiCodec(AgentCodec):
    """Canonical surface over a pi RPC child."""

    adapter = "pi"
    commands = PI_COMMANDS

    def capabilities(self, session: PiSession) -> dict:
        return PI_CAPABILITIES.to_dict()

    async def snapshot(self, session: PiSession, *, limit: int | None = None) -> dict:
        snapshot = await session.snapshot(entry_limit=limit)
        return pi_snapshot_to_canonical(
            snapshot, tokens=session.tokens, cost=session.cost
        )

    async def history(self, session: PiSession, before: str, limit: int) -> dict:
        return pi_history_to_canonical(await session.history(before, limit))

    async def models(
        self, session: PiSession, manager: Any, *, refresh: bool = False
    ) -> dict:
        payload = await manager.pi_models(session.cwd, refresh=refresh)
        return pi_models_to_canonical(payload)

    def resolve_dialog(self, session: PiSession, dialog_id: str, response: dict) -> None:
        kind = _dialog_kind(session, dialog_id)
        if kind is None:
            raise UnknownDialog(f"unknown dialog {dialog_id!r}")
        try:
            session.resolve_ui(dialog_id, pi_dialog_response_to_pi(kind, response))
        except PiError as exc:
            raise UnknownDialog(str(exc)) from exc

    async def command(
        self,
        session: PiSession,
        name: str,
        command: dict,
        command_id: str,
        *,
        timeout: float | None = None,
    ) -> dict:
        response = await session.rpc(
            pi_command_from_canonical(command),
            timeout=timeout if timeout is not None else DEFAULT_AGENT_COMMAND_TIMEOUT,
        )
        return canonical_response(
            name, command_id, response, tokens=session.tokens, cost=session.cost
        )

    async def result(self, session: PiSession, *, settled: bool) -> dict:
        result: dict[str, object] = {
            "session_id": session.id,
            "settled": settled,
            "exit_code": session.exit_code,
        }
        if session.dead:
            return result
        try:
            text = await session.rpc({"type": "get_last_assistant_text"}, timeout=5.0)
            if text.get("success"):
                result["text"] = (text.get("data") or {}).get("text")
            state = await session.rpc({"type": "get_state"}, timeout=5.0)
            if state.get("success"):
                result["state"] = state.get("data") or {}
            stats = await session.rpc({"type": "get_session_stats"}, timeout=5.0)
            if stats.get("success"):
                result["stats"] = stats.get("data") or {}
        except (PiError, PiSessionClosed, TimeoutError) as exc:
            result["error"] = str(exc)
        return result


PI_CODEC = PiCodec()

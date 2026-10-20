"""Model discovery for the new-session picker.

The browser picks a model *before* a pi session exists, so it cannot use the
RPC ``get_available_models`` of a running session.  pi resolves models itself —
credentials in ``auth.json``, ``models.json`` providers and overrides, catalog
updates, extension-provided models — so webterm must not re-derive that list
from configuration files.  Instead a throwaway ``pi --mode rpc --no-session
--no-approve`` child is asked, and the answer is cached.

The cache is a convenience, never a source of truth: a stale entry only means
the picker is out of date until the next refresh.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .session import PiError, PiSessionClosed

if TYPE_CHECKING:
    from ...session import SessionManager

#: Model fields forwarded to the browser.  Everything else in pi's model object
#: is dropped on purpose: provider credentials, endpoints and any field pi adds
#: later must not reach a client by default.
MODEL_FIELDS = (
    "id",
    "provider",
    "name",
    "contextWindow",
    "maxTokens",
    "reasoning",
    "input",
)

#: How long a probe result is reused before the picker asks pi again.
DEFAULT_TTL = 60.0
#: Upper bound for the probe's RPC calls; startup on a cold machine is slow.
PROBE_TIMEOUT = 20.0


def normalize_models(models: object) -> list[dict]:
    """Return the browser-safe subset of pi's model list, sorted for display."""
    if not isinstance(models, list):
        return []
    normalized: list[dict] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        identifier = model.get("id")
        provider = model.get("provider")
        if not isinstance(identifier, str) or not identifier:
            continue
        if not isinstance(provider, str) or not provider:
            continue
        normalized.append({key: model[key] for key in MODEL_FIELDS if key in model})
    normalized.sort(key=lambda item: (str(item["provider"]).casefold(), str(item["id"]).casefold()))
    return normalized


def _normalize_model(model: object) -> dict | None:
    """Return one normalized model, or ``None`` when it is not usable."""
    normalized = normalize_models([model])
    return normalized[0] if normalized else None


def probe_argv(manager: SessionManager) -> list[str]:
    """Return argv for a probe process.

    The bundled extension is deliberately not loaded: model listing needs no
    tools, and the probe must not touch a session or the project.
    """
    argv = list(manager.pi_command)
    argv.extend(["--mode", "rpc", "--no-session", "--no-approve"])
    return argv


class PiModelCache:
    """The last successful model probe, keyed by working directory.

    ``cwd`` is part of the key because project-level configuration and trust
    affect what pi resolves; a list probed elsewhere is not reused.
    """

    def __init__(self, ttl: float = DEFAULT_TTL) -> None:
        self.ttl = ttl
        self.models: list[dict] = []
        self.default: dict | None = None
        self.fetched_at: float | None = None
        self.cwd: str | None = None
        self._lock = asyncio.Lock()

    def fresh(self, cwd: str) -> bool:
        """Return True when the cached result may be reused for *cwd*."""
        if self.fetched_at is None or self.cwd != cwd:
            return False
        return self.ttl < 0 or time.time() - self.fetched_at < self.ttl

    def payload(self, *, cached: bool) -> dict:
        """Return the wire payload for one probe result."""
        return {
            "models": self.models,
            "default": self.default,
            "fetchedAt": self.fetched_at,
            "cwd": self.cwd,
            "cached": cached,
        }

    async def get(
        self, manager: SessionManager, cwd: str, *, refresh: bool = False
    ) -> dict:
        """Return the model list for *cwd*, probing pi when the cache is cold.

        A failed refresh falls back to the previous result for the same
        directory so a transient probe error does not empty the picker.
        """
        async with self._lock:
            if not refresh and self.fresh(cwd):
                return self.payload(cached=True)
            try:
                models, default = await self._probe(manager, cwd)
            except (PiError, PiSessionClosed, TimeoutError, OSError) as exc:
                if self.cwd == cwd and self.fetched_at is not None:
                    payload = self.payload(cached=True)
                    payload["error"] = str(exc) or exc.__class__.__name__
                    return payload
                raise
            self.models = models
            self.default = default
            self.fetched_at = time.time()
            self.cwd = cwd
            return self.payload(cached=False)

    async def _probe(self, manager: SessionManager, cwd: str) -> tuple[list[dict], dict | None]:
        """Ask a throwaway pi process which models it offers.

        The child is never published as a session, but it is registered for
        reaping so :class:`SessionManager`'s single ``waitpid`` path owns it.
        """
        session = manager.create_pi(
            probe_argv(manager),
            cwd=cwd,
            title="pi models",
            ephemeral=True,
        )
        try:
            response = await session.rpc(
                {"type": "get_available_models"}, timeout=PROBE_TIMEOUT
            )
            if not response.get("success", True):
                raise PiError(str(response.get("error") or "get_available_models failed"))
            data = response.get("data")
            models = normalize_models(data.get("models") if isinstance(data, dict) else None)
            return models, await self._probe_default(session)
        finally:
            session.close()

    async def _probe_default(self, session) -> dict | None:
        """Return the model a session would start with, if pi reports one."""
        try:
            response = await session.rpc({"type": "get_state"}, timeout=PROBE_TIMEOUT)
        except (PiError, PiSessionClosed, TimeoutError):
            return None
        data = response.get("data")
        if not isinstance(data, dict):
            return None
        return _normalize_model(data.get("model"))

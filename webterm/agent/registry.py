"""Discoverable agent backends.

The browser knows shapes, never a backend list: ``/config.js`` carries an
``agentAdapters`` list, built here from state the server actually has.  Each
entry says which adapter id a session can carry, whether that adapter is usable
right now, and which browser view renders it.

Availability is discovered, not declared.  A backend is advertised because a
codec names it (:data:`webterm.agent.codec.NATIVE_CODEC`'s ``adapter``,
``PiCodec.adapter``) or because an agent profile (``--agent`` /
``--agent-rpc``) configures it, and it is *available* when that profile carries
an ``rpc`` command — a structured child webterm can really start — plus
``SessionManager.pi_available`` for pi.  Nothing here probes a network service
or runs a binary: a backend with no browser view falls back to the generic
canonical view, so an adapter nobody has heard of still works.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..integration.pi.codec import PI_CODEC
from .codec import NATIVE_CODEC

if TYPE_CHECKING:
    from ..session import SessionManager

#: Adapter id of the fallback codec.  Any name without a codec of its own is
#: served by :data:`NATIVE_CODEC`, so it is a dialect marker rather than a
#: backend and is never advertised as an entry of its own.
GENERIC_ADAPTER = NATIVE_CODEC.adapter

#: Browser views webterm ships, keyed by adapter id.  A view is a same-origin
#: classic script the shell loads to register itself with the shared agent
#: view; an adapter without an entry here is rendered by the generic canonical
#: view, which is what every native (canonical JSONL) backend uses.
VIEWS: dict[str, str] = {PI_CODEC.adapter: "/pi-view.js"}

#: Human labels for adapters webterm has met.  Anything else is labelled with
#: its adapter id, so a profile adds a named backend without a webterm release.
LABELS: dict[str, str] = {
    PI_CODEC.adapter: "pi",
    "llama": "llama-server",
    "mva": "mva",
}

#: Adapters advertised even when no profile configures them, in the order they
#: are listed.  The first is the dialect webterm translates itself; the rest
#: are backends webterm knows by name and can run once configured.  This order
#: is never a statement about availability.
KNOWN_ADAPTERS: tuple[str, ...] = (PI_CODEC.adapter, "llama", "mva")


def _adapter_name(name: str, profile: object) -> str:
    """Return the adapter id a profile configures; its ``adapter`` key wins."""
    if isinstance(profile, Mapping):
        declared = profile.get("adapter")
        if isinstance(declared, str) and declared:
            return declared
    return name


def _configured(
    profiles: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, bool]]:
    """Collect the launcher flags each configured profile justifies."""
    configured: dict[str, dict[str, bool]] = {}
    for name, profile in profiles.items():
        adapter = _adapter_name(name, profile)
        if adapter == GENERIC_ADAPTER:
            continue
        flags = configured.setdefault(adapter, {"rpc": False, "interactive": False})
        if isinstance(profile, Mapping):
            flags["rpc"] = flags["rpc"] or bool(profile.get("rpc"))
            flags["interactive"] = flags["interactive"] or bool(profile.get("interactive"))
    return configured


def _available(
    adapter: str, flags: dict[str, bool] | None, pi_available: bool
) -> bool:
    """Return True when a structured session for *adapter* can be started."""
    if not flags or not flags.get("rpc"):
        return False
    if adapter == PI_CODEC.adapter:
        return pi_available
    return True


def agent_adapters(
    manager: SessionManager | None = None,
    profiles: Mapping[str, Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Return the agent backends to advertise through ``/config.js``.

    The order is stable: :data:`KNOWN_ADAPTERS` first, then any adapter that
    only a configured profile names, in profile order.  A profile whose
    ``adapter`` key names another adapter contributes to that entry instead of
    its own name, so one backend can be configured under several profile names.

    Each entry is ``{"name", "label", "available", "view", "launcher"}``:
    ``name`` is the adapter id a session can carry, ``label`` its human name,
    ``available`` the discovered state described in the module docstring,
    ``view`` the same-origin script a browser loads to render it (``None``
    means the generic view), and ``launcher`` whether the profile also has an
    interactive command, i.e. whether a terminal session can be launched.

    *manager* is only read for :attr:`SessionManager.pi_available`; ``None``
    means "pi is not usable" and keeps the function callable from tests and
    embedded hosts without a manager.  *profiles* are the parsed ``--agent`` /
    ``--agent-rpc`` profiles, exactly as ``parse_agents`` returns them.
    """
    profiles = profiles or {}
    pi_available = bool(getattr(manager, "pi_available", False))
    configured = _configured(profiles)
    names = list(KNOWN_ADAPTERS) + [
        name for name in configured if name not in KNOWN_ADAPTERS
    ]
    return [
        {
            "name": name,
            "label": LABELS.get(name, name),
            "available": _available(name, configured.get(name), pi_available),
            "view": VIEWS.get(name),
            "launcher": bool(configured.get(name, {}).get("interactive", False)),
        }
        for name in names
    ]

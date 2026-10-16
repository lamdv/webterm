"""PyTauri host for the webterm desktop app.

The Tauri main thread runs the window/webview event loop; the webterm asyncio
server runs on PyTauri's existing anyio/asyncio portal loop in a worker thread,
with an ephemeral loopback port.  The webview is pointed at that loopback URL so
the existing browser client stays same-origin with ``/ws``.

Set ``WEBTERM_DESKTOP_URL`` to attach to an already-running server instead of
starting one in-process (handy for debugging against ``uv run webterm``).
"""

from __future__ import annotations

import os
from os import environ

# pytauri requires this to be set before any pytauri import.  It selects the
# prebuilt pytauri-wheel native libraries instead of the Cargo extension.
environ["_PYTAURI_DIST"] = "pytauri-wheel"

from pathlib import Path  # noqa: E402

from anyio.from_thread import start_blocking_portal  # noqa: E402
from pytauri import Commands  # noqa: E402
from pytauri_wheel.lib import builder_factory, context_factory  # noqa: E402
from webterm.server import start_server  # noqa: E402
from webterm.session import DEFAULT_SCROLLBACK, SessionManager  # noqa: E402

#: Directory holding ``Tauri.toml``, ``capabilities/``, ``icons/`` and the
#: frontend assets.  ``context_factory`` reads the Tauri config from here.
PACKAGE_DIR = Path(__file__).parent.absolute()

commands = Commands()


def _default_shell() -> str:
    return os.environ.get("SHELL") or "/bin/sh"


def _tauri_config(url: str) -> dict:
    # Point the webview at the loopback server instead of bundled assets.
    return {"build": {"frontendDist": url}}


def main() -> int:
    """Run the desktop app and return its exit code."""
    external_url = environ.get("WEBTERM_DESKTOP_URL")

    with start_blocking_portal("asyncio") as portal:
        handle = None
        if external_url:
            url = external_url
        else:
            manager = SessionManager(
                shell=_default_shell(),
                cwd=os.path.expanduser("~"),
                scrollback=DEFAULT_SCROLLBACK,
            )
            handle = portal.call(start_server, "127.0.0.1", 0, manager, {})
            url = handle.url + "/"

        try:
            app = builder_factory().build(
                context=context_factory(PACKAGE_DIR, tauri_config=_tauri_config(url)),
                invoke_handler=commands.generate_handler(portal),
            )
            return app.run_return()
        finally:
            if handle is not None:
                # Runs the server's teardown on the portal loop: close the
                # listener, disconnect clients, and kill the sessions.
                portal.call(handle.stop)

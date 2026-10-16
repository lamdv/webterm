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
import secrets
from os import environ

# pytauri requires this to be set before any pytauri import.  It selects the
# prebuilt pytauri-wheel native libraries instead of the Cargo extension.
environ["_PYTAURI_DIST"] = "pytauri-wheel"

from pathlib import Path  # noqa: E402

from anyio.from_thread import start_blocking_portal  # noqa: E402
from pytauri import AppHandle, Commands, Manager  # noqa: E402
from pytauri.menu import Menu, MenuItem, PredefinedMenuItem, Submenu  # noqa: E402
from pytauri_wheel.lib import builder_factory, context_factory  # noqa: E402
from webterm.server import start_server  # noqa: E402
from webterm.session import DEFAULT_SCROLLBACK, SessionManager  # noqa: E402

#: Directory holding ``Tauri.toml``, ``capabilities/``, ``icons/`` and the
#: frontend assets.  ``context_factory`` reads the Tauri config from here.
PACKAGE_DIR = Path(__file__).parent.absolute()

commands = Commands()


def _default_shell() -> str:
    return os.environ.get("SHELL") or "/bin/sh"


#: Native menu items that forward to the client-side bridge in ``app.js``.
#: Keyboard accelerators are intentionally left to the web client for now: it
#: already implements Ctrl+Shift+N / Ctrl+Shift+B and must keep working in a
#: plain browser, and duplicating them risks double-firing a toggle.
MENU_ACTIONS = {
    "new-session": "window.__webterm && window.__webterm.newSession()",
    "close-session": "window.__webterm && window.__webterm.closeActiveSession()",
    "toggle-sidebar": "window.__webterm && window.__webterm.toggleSidebar()",
}


def _build_menu(app_handle: AppHandle) -> Menu:
    """Build the application menu (a no-op fallback if the platform ignores it)."""
    file_menu = Submenu.with_items(
        app_handle,
        "File",
        True,
        [
            MenuItem.with_id(app_handle, "new-session", "New Session", True),
            MenuItem.with_id(app_handle, "close-session", "Close Session", True),
            PredefinedMenuItem.separator(app_handle),
            PredefinedMenuItem.quit(app_handle, "Quit"),
        ],
    )
    edit_menu = Submenu.with_items(
        app_handle,
        "Edit",
        True,
        [
            PredefinedMenuItem.copy(app_handle),
            PredefinedMenuItem.paste(app_handle),
            PredefinedMenuItem.select_all(app_handle),
        ],
    )
    view_menu = Submenu.with_items(
        app_handle,
        "View",
        True,
        [
            MenuItem.with_id(app_handle, "toggle-sidebar", "Toggle Sidebar", True),
            PredefinedMenuItem.fullscreen(app_handle),
        ],
    )
    help_menu = Submenu.with_items(
        app_handle, "Help", True, [PredefinedMenuItem.about(app_handle)]
    )
    return Menu.with_items(app_handle, [file_menu, edit_menu, view_menu, help_menu])


def _setup_menu(app_handle: AppHandle) -> None:
    """Attach the menu and route item clicks to the client bridge (runs in setup)."""
    menu = _build_menu(app_handle)
    window = Manager.get_webview_window(app_handle, "main")
    if window is not None:
        window.set_menu(menu)
    else:
        app_handle.set_menu(menu)

    def on_menu(handle: AppHandle, event_id: str) -> None:
        script = MENU_ACTIONS.get(event_id)
        if script is None:
            return
        target = Manager.get_webview_window(handle, "main")
        if target is not None:
            target.eval(script)

    app_handle.on_menu_event(on_menu)


def _tauri_config(url: str) -> dict:
    # Point the webview at the loopback server instead of bundled assets.
    return {"build": {"frontendDist": url}}


async def _start_in_process(manager: SessionManager, token: str):
    # ``portal.call`` has no keyword arguments, so bind the token here.
    return await start_server("127.0.0.1", 0, manager, {}, token=token)


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
            # A fresh secret per launch: the port is ephemeral and any local
            # process could otherwise reach the shell.  The webview exchanges
            # it for an HttpOnly cookie on first navigation.
            token = secrets.token_urlsafe(32)
            handle = portal.call(_start_in_process, manager, token)
            url = f"{handle.url}/?bootstrap={token}"

        try:
            app = builder_factory().build(
                context=context_factory(PACKAGE_DIR, tauri_config=_tauri_config(url)),
                invoke_handler=commands.generate_handler(portal),
                setup=_setup_menu,
            )
            return app.run_return()
        finally:
            if handle is not None:
                # Runs the server's teardown on the portal loop: close the
                # listener, disconnect clients, and kill the sessions.
                portal.call(handle.stop)

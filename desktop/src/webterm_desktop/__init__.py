"""PyTauri host for the webterm desktop app.

The Tauri main thread runs the window/webview event loop; the webterm asyncio
server runs on PyTauri's existing anyio/asyncio portal loop in a worker thread,
with an ephemeral loopback port.  The webview is pointed at that loopback URL so
the existing browser client stays same-origin with ``/ws``.

Set ``WEBTERM_DESKTOP_URL`` to attach to an already-running server instead of
starting one in-process (handy for debugging against ``uv run webterm``).
"""

from __future__ import annotations

import ctypes.util
import logging
import os
import secrets
import sys
from os import environ

# pytauri requires this to be set before any pytauri import.  It selects the
# prebuilt pytauri-wheel native libraries instead of the Cargo extension.
environ["_PYTAURI_DIST"] = "pytauri-wheel"

from pathlib import Path  # noqa: E402

from anyio.from_thread import start_blocking_portal  # noqa: E402
from pytauri import AppHandle, Commands, Manager  # noqa: E402
from pytauri.image import Image  # noqa: E402
from pytauri.menu import Menu, MenuItem, PredefinedMenuItem, Submenu  # noqa: E402
from pytauri.tray import TrayIcon, TrayIconEvent  # noqa: E402
from pytauri_plugins import single_instance, window_state  # noqa: E402
from pytauri_wheel.lib import builder_factory, context_factory  # noqa: E402
from webterm.server import start_server  # noqa: E402
from webterm.session import DEFAULT_SCROLLBACK, SessionManager  # noqa: E402

log = logging.getLogger("webterm.desktop")

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


def _focus_main_window(app_handle: AppHandle) -> None:
    window = Manager.get_webview_window(app_handle, "main")
    if window is not None:
        window.show()
        window.set_focus()


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


#: Keep the tray wrapper alive; dropping it could unregister the tray icon.
_tray: TrayIcon | None = None


def _build_tray_menu(app_handle: AppHandle) -> Menu:
    return Menu.with_items(
        app_handle,
        [
            MenuItem.with_id(app_handle, "tray-show", "Show Window", True),
            MenuItem.with_id(app_handle, "tray-new", "New Session", True),
            PredefinedMenuItem.separator(app_handle),
            MenuItem.with_id(app_handle, "tray-quit", "Quit", True),
        ],
    )


def _on_tray_menu(app_handle: AppHandle, event_id: str) -> None:
    if event_id == "tray-show":
        _focus_main_window(app_handle)
    elif event_id == "tray-new":
        _focus_main_window(app_handle)
        window = Manager.get_webview_window(app_handle, "main")
        if window is not None:
            window.eval(MENU_ACTIONS["new-session"])
    elif event_id == "tray-quit":
        app_handle.exit(0)


def _on_tray_icon(app_handle: AppHandle, event: TrayIconEvent) -> None:
    if isinstance(event, TrayIconEvent.Click):
        _focus_main_window(app_handle)


def _setup_tray(app_handle: AppHandle) -> None:
    """Add a status tray with show/new-session/quit (best effort)."""
    global _tray
    tray = TrayIcon.with_id(app_handle, "webterm-tray")
    icon = None
    try:
        icon = Image.from_path(PACKAGE_DIR / "icons" / "icon.png")
    except Exception:  # noqa: BLE001 - fall back to the app icon
        icon = app_handle.default_window_icon()
    if icon is not None:
        tray.set_icon(icon)
    tray.set_tooltip("webterm")
    tray.set_menu(_build_tray_menu(app_handle))
    tray.set_show_menu_on_left_click(False)
    tray.on_menu_event(_on_tray_menu)
    tray.on_tray_icon_event(_on_tray_icon)
    _tray = tray


def _on_second_instance(app_handle: AppHandle, _args: list[str], _cwd: str) -> None:
    """A second launch focused the running instance instead of starting one."""
    _focus_main_window(app_handle)


def _tray_available() -> bool:
    """Whether a status-tray backend is present.

    Tauri's tray on Linux needs libayatana-appindicator3 (or libappindicator3);
    without it the Rust side panics in a way Python cannot reliably catch, so
    check for the library up front.  macOS and Windows always have a backend.
    """
    if sys.platform != "linux":
        return True
    return any(
        ctypes.util.find_library(name)
        for name in ("ayatana-appindicator3", "appindicator3")
    )


def _setup(app_handle: AppHandle) -> None:
    _setup_menu(app_handle)
    if not _tray_available():
        log.info(
            "system tray disabled: install libayatana-appindicator3 or "
            "libappindicator3 to enable it"
        )
        return
    try:
        _setup_tray(app_handle)
    except Exception:  # noqa: BLE001 - a missing tray backend must not break the app
        log.exception("could not create the system tray")


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
                setup=_setup,
                plugins=(
                    # single-instance must be registered first to work correctly.
                    single_instance.init(_on_second_instance),
                    window_state.Builder.build(),
                ),
            )
            return app.run_return()
        finally:
            if handle is not None:
                # Runs the server's teardown on the portal loop: close the
                # listener, disconnect clients, and kill the sessions.
                portal.call(handle.stop)

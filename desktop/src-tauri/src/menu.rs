//! The native menu bar and the actions behind its items.
//!
//! A menu is native chrome: it lives outside the webview, so it behaves the
//! same while the main window is showing the picker and while it is showing a
//! remote server page we do not control.  No webview code has to know it
//! exists.

use tauri::menu::{Menu, MenuBuilder, MenuEvent, MenuItemBuilder, SubmenuBuilder};
use tauri::{AppHandle, Manager, Runtime};

use crate::commands;

/// Build the application menu.
///
/// `Builder::menu` calls this once, before any window exists.
pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let servers = MenuItemBuilder::with_id("servers", "Servers")
        .accelerator("CmdOrCtrl+Shift+S")
        .build(app)?;
    let reload = MenuItemBuilder::with_id("reload", "Reload Window")
        .accelerator("CmdOrCtrl+R")
        .build(app)?;

    let server = SubmenuBuilder::new(app, "Server")
        .item(&servers)
        .item(&reload)
        .separator()
        .quit()
        .build()?;

    let window = SubmenuBuilder::new(app, "Window")
        .minimize()
        .fullscreen()
        .close_window()
        .build()?;

    MenuBuilder::new(app).item(&server).item(&window).build()
}

/// Dispatch a clicked menu item.
///
/// Custom items are matched by the id we gave them; predefined items such as
/// Quit and Close Window are handled by the platform and never reach here.
pub fn handle<R: Runtime>(app: &AppHandle<R>, event: MenuEvent) {
    match event.id().as_ref() {
        "servers" => {
            let _ = commands::show_picker(app);
        }
        "reload" => reload_main_window(app),
        _ => {}
    }
}

/// Reload the main window, whichever view it is showing.
fn reload_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.reload();
    }
}

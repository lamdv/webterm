//! The native menu bar and the actions behind its items.
//!
//! Chapter 6.  A menu is native chrome: it lives outside the webview, so it
//! behaves the same in the picker and in a server window that loaded a remote
//! page we do not control.  No webview code has to know it exists.

use tauri::menu::{Menu, MenuBuilder, MenuEvent, MenuItemBuilder, SubmenuBuilder};
use tauri::{AppHandle, Manager, Runtime};

/// Build the application menu.
///
/// `Builder::menu` calls this once, before any window exists.
pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let new_server = MenuItemBuilder::with_id("new-server", "New Server")
        .accelerator("CmdOrCtrl+N")
        .build(app)?;
    let reload = MenuItemBuilder::with_id("reload", "Reload Window")
        .accelerator("CmdOrCtrl+R")
        .build(app)?;

    let server = SubmenuBuilder::new(app, "Server")
        .item(&new_server)
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
        "new-server" => focus_picker(app),
        "reload" => reload_focused(app),
        _ => {}
    }
}

/// Bring the picker forward and put the caret in the add form.
fn focus_picker<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        // The menu is native; this is the one seam where it reaches the page.
        let _ = window.eval("document.querySelector('#name')?.focus()");
    }
}

/// Reload whichever window has focus, falling back to the picker.
fn reload_focused<R: Runtime>(app: &AppHandle<R>) {
    let focused = app
        .webview_windows()
        .into_values()
        .find(|window| window.is_focused().unwrap_or(false));

    if let Some(window) = focused.or_else(|| app.get_webview_window("main")) {
        let _ = window.reload();
    }
}

//! Tauri shell for the webterm desktop client.
//!
//! This crate is deliberately thin.  Anything that can be tested without a
//! window lives in the `webterm-client` crate; here we only wire up the window
//! system, the IPC commands and the plugins.

use tauri::Manager;

/// Build and run the application.  Called exactly once, from `main`.
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // A second launch focuses the picker instead of starting a second
            // copy of the app.
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .run(tauri::generate_context!())
        .expect("error while running the webterm desktop app");
}

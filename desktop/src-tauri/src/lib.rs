//! Tauri shell for the webterm desktop client.
//!
//! This crate is deliberately thin.  Anything that can be tested without a
//! window lives in the `webterm-client` crate; here we only wire up the window
//! system, the profile store, the IPC commands and the plugins.

mod commands;
#[cfg(desktop)]
mod menu;

use tauri::Manager;

/// Build and run the application.  Called exactly once, from `main`.
pub fn run() {
    let builder = tauri::Builder::default()
        // The HTTP plugin gives us the `reqwest` client used to probe servers.
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // A second launch focuses the picker instead of starting a second
            // copy of the app.
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            // Store the profiles in the per-app config directory, e.g.
            // `~/.config/dev.webterm.desktop/servers.json` on Linux.
            let path = app.path().app_config_dir()?.join("servers.json");
            app.manage(commands::Profiles::load(path)?);
            app.manage(commands::HttpClient::new()?);
            app.manage(commands::PickerUrl::new());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::list_profiles,
            commands::add_profile,
            commands::update_profile,
            commands::remove_profile,
            commands::connect,
            commands::probe_all,
        ]);

    // Menus and window-state are desktop-only plugins; a future mobile build
    // simply skips them.
    #[cfg(desktop)]
    let builder = builder
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .menu(menu::build)
        .on_menu_event(menu::handle);

    builder
        .run(tauri::generate_context!())
        .expect("error while running the webterm desktop app");
}

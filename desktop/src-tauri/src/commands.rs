//! IPC commands for the picker.
//!
//! The picker window is the only window with IPC permission (see
//! `capabilities/default.json`).  Every command here reads or writes the
//! [`ProfileStore`] that `setup` put in Tauri's managed state.
//!
//! Commands are deliberately small and synchronous: Tauri runs them on its
//! async runtime and hands the result back to JavaScript as a promise, so the
//! UI thread is never blocked by a few kilobytes of JSON.

use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{Manager, State, WebviewUrl, WebviewWindowBuilder};
use webterm_client::{Profile, ProfileStore, StoreError};

/// The profile store, wrapped so Tauri can share it between command calls.
///
/// The `Mutex` is required because commands can run concurrently on the async
/// runtime.  Each command locks, does its work and drops the guard, so there is
/// no long-lived borrow.
#[derive(Debug)]
pub struct Profiles(Mutex<ProfileStore>);

impl Profiles {
    /// Load the store from disk, creating an empty one if the file is absent.
    pub fn load(path: PathBuf) -> Result<Self, StoreError> {
        Ok(Self(Mutex::new(ProfileStore::load(path)?)))
    }
}

/// Turn a store error into the string the picker will display.  `StoreError`'s
/// `Display` already says what went wrong and why.
fn describe(error: StoreError) -> String {
    error.to_string()
}

/// A poisoned lock means another command panicked while holding it; there is no
/// safe way to continue.
fn poisoned() -> String {
    "the profile store could not be locked".to_string()
}

/// Return every saved server, in the order it was added.
#[tauri::command]
pub fn list_profiles(state: State<'_, Profiles>) -> Result<Vec<Profile>, String> {
    let store = state.0.lock().map_err(|_| poisoned())?;
    Ok(store.profiles().to_vec())
}

/// Add a profile and persist the list.
#[tauri::command]
pub fn add_profile(state: State<'_, Profiles>, profile: Profile) -> Result<(), String> {
    let mut store = state.0.lock().map_err(|_| poisoned())?;
    store.add(profile).map_err(describe)
}

/// Remove a profile.  Returns whether anything was removed.
#[tauri::command]
pub fn remove_profile(state: State<'_, Profiles>, id: String) -> Result<bool, String> {
    let mut store = state.0.lock().map_err(|_| poisoned())?;
    store
        .remove(&id)
        .map(|removed| removed.is_some())
        .map_err(describe)
}

/// Open a window pointed at the profile's server.
///
/// The window loads the server's own web client, not ours.  It is created
/// without a capability, so the remote page cannot call any of these commands.
#[tauri::command]
pub fn connect(
    app: tauri::AppHandle,
    state: State<'_, Profiles>,
    id: String,
) -> Result<(), String> {
    let profile = {
        let store = state.0.lock().map_err(|_| poisoned())?;
        store
            .get(&id)
            .cloned()
            .ok_or_else(|| format!("no server with id {id:?}"))?
    };
    open_server_window(&app, &profile)
}

fn open_server_window(app: &tauri::AppHandle, profile: &Profile) -> Result<(), String> {
    let label = server_label(&profile.id);

    // Clicking Connect twice should focus the existing window, not stack a
    // second copy of the same server.
    if let Some(window) = app.get_webview_window(&label) {
        window.set_focus().map_err(|error| error.to_string())?;
        return Ok(());
    }

    let url = tauri::Url::parse(&profile.url)
        .map_err(|error| format!("{:?} is not a valid server URL: {error}", profile.url))?;

    WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
        .title(&profile.name)
        .inner_size(900.0, 600.0)
        .build()
        .map_err(|error| error.to_string())?;
    Ok(())
}

/// Tauri window labels may only contain letters, digits, `-`, `_`, `.` and
/// `:`. Profile ids come from the webview, so sanitise rather than trust them.
fn server_label(id: &str) -> String {
    let safe: String = id
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.') {
                c
            } else {
                '-'
            }
        })
        .collect();
    format!("server-{safe}")
}

#[cfg(test)]
mod tests {
    use super::server_label;

    #[test]
    fn server_labels_are_sanitised() {
        assert_eq!(server_label("ok-1_2.3"), "server-ok-1_2.3");
        assert_eq!(server_label("a/b c"), "server-a-b-c");
    }
}

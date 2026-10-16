//! IPC commands for the picker.
//!
//! The picker window is the only window with IPC permission (see
//! `capabilities/default.json`).  Every command here reads or writes the
//! [`ProfileStore`] that `setup` put in Tauri's managed state.
//!
//! Commands are deliberately small.  Most are synchronous: Tauri runs them on
//! its async runtime and hands the result back to JavaScript as a promise, so
//! the UI thread is never blocked by a few kilobytes of JSON.  `probe_all` is
//! `async` because it awaits the network.

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use tauri::{Manager, State, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_http::reqwest;
use webterm_client::{Profile, ProfileStore, ServerInfo, StoreError};

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

/// A shared HTTP client for probing servers.
///
/// The client is built once so its connection pool and TLS configuration are
/// reused.  The timeout is short: a probe is a status hint, not a request the
/// user waits on.
#[derive(Debug)]
pub struct HttpClient(reqwest::Client);

impl HttpClient {
    pub fn new() -> Result<Self, reqwest::Error> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .user_agent(concat!("webterm-desktop/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(Self(client))
    }
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

/// Replace an existing profile.  Returns whether one was found.
#[tauri::command]
pub fn update_profile(state: State<'_, Profiles>, profile: Profile) -> Result<bool, String> {
    let mut store = state.0.lock().map_err(|_| poisoned())?;
    store
        .update(profile)
        .map(|updated| updated.is_some())
        .map_err(describe)
}

/// The outcome of probing one server, as the picker sees it.
#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProbeStatus {
    /// Reachable and protocol-compatible.
    Online,
    /// Reachable, but speaking a protocol this client does not know.
    Incompatible,
    /// Unreachable, or not a webterm server.
    Offline,
}

/// One row of the probe result.
#[derive(Debug, Serialize)]
pub struct ProbeResult {
    pub id: String,
    pub status: ProbeStatus,
    /// Present when the server answered with a discovery document.
    pub server: Option<ServerInfo>,
    /// Present when the probe failed; a message for the user.
    pub error: Option<String>,
}

impl ProbeResult {
    fn online(id: String, server: ServerInfo) -> Self {
        Self {
            id,
            status: ProbeStatus::Online,
            server: Some(server),
            error: None,
        }
    }

    fn incompatible(id: String, server: ServerInfo) -> Self {
        Self {
            id,
            status: ProbeStatus::Incompatible,
            server: Some(server),
            error: None,
        }
    }

    fn offline(id: String, error: String) -> Self {
        Self {
            id,
            status: ProbeStatus::Offline,
            server: None,
            error: Some(error),
        }
    }
}

/// Probe every saved server and return its status.
///
/// The probes run one after another.  With a five-second timeout that is fine
/// for the handful of servers a picker holds, and it keeps the code simple; a
/// future change can fan them out without touching the frontend.
#[tauri::command]
pub async fn probe_all(
    profiles: State<'_, Profiles>,
    http: State<'_, HttpClient>,
) -> Result<Vec<ProbeResult>, String> {
    let profiles = {
        let store = profiles.0.lock().map_err(|_| poisoned())?;
        store.profiles().to_vec()
    };

    let mut results = Vec::with_capacity(profiles.len());
    for profile in profiles {
        results.push(probe_one(&http.0, profile).await);
    }
    Ok(results)
}

async fn probe_one(client: &reqwest::Client, profile: Profile) -> ProbeResult {
    let id = profile.id.clone();
    match fetch_server_info(client, &profile).await {
        Ok(info) if info.is_compatible() => ProbeResult::online(id, info),
        Ok(info) => ProbeResult::incompatible(id, info),
        Err(error) => ProbeResult::offline(id, error),
    }
}

/// Fetch and parse a profile's discovery document.
///
/// The document is served before authentication, so no credential is sent.
async fn fetch_server_info(
    client: &reqwest::Client,
    profile: &Profile,
) -> Result<ServerInfo, String> {
    let url = webterm_client::discovery_url(profile).map_err(|error| error.to_string())?;

    let response = client
        .get(url)
        .send()
        .await
        .map_err(|error| error.to_string())?;

    let status = response.status();
    if !status.is_success() {
        return Err(format!("server replied {status}"));
    }

    let body = response.text().await.map_err(|error| error.to_string())?;
    serde_json::from_str(&body).map_err(|error| format!("unexpected discovery document: {error}"))
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

    let url = webterm_client::connect_url(profile)
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

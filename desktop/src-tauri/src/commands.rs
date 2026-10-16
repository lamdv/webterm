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
use tauri::{Manager, State};
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

/// Where the picker lives.
///
/// It is recorded just before the main window navigates to a server, so the
/// Servers menu can navigate back.  The picker's URL is platform-specific
/// (`tauri://localhost` or `http://tauri.localhost`), so it is captured rather
/// than reconstructed.
#[derive(Debug, Default)]
pub struct PickerUrl(Mutex<Option<tauri::Url>>);

impl PickerUrl {
    pub fn new() -> Self {
        Self::default()
    }
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

/// Open a profile's server **in the main window**.
///
/// The window navigates to the server's own web client, not ours.  Reusing the
/// one window (rather than creating one) is what keeps the app single-window,
/// and because it is a top-level navigation the server's session cookie and
/// WebSocket stay first-party, so OAuth2 proxies still work.
///
/// Once the window is on the server origin the page is remote, and Tauri's ACL
/// denies it every command in `capabilities/default.json`; `show_picker` brings
/// the picker back.
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

    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "the main window is missing".to_string())?;

    // This command runs in the picker, so the window's current URL is the
    // picker's.  Remember it before we leave it behind.
    if let Ok(url) = window.url() {
        if let Ok(mut picker) = app.state::<PickerUrl>().0.lock() {
            picker.replace(url);
        }
    }

    let url = webterm_client::connect_url(&profile)
        .map_err(|error| format!("{:?} is not a valid server URL: {error}", profile.url))?;
    window.navigate(url).map_err(|error| error.to_string())
}

/// Navigate the main window back to the picker.
///
/// The picker is also where servers are added, so there is no separate
/// "new server" action: returning to the list is enough.
pub fn show_picker<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<(), String> {
    let Some(window) = app.get_webview_window("main") else {
        return Err("the main window is missing".to_string());
    };

    let remembered = app
        .state::<PickerUrl>()
        .0
        .lock()
        .map_err(|_| poisoned())?
        .clone();

    let Some(mut url) = remembered else {
        // No server has been opened yet, so the picker is already on screen.
        return Ok(());
    };

    url.set_fragment(None);
    window.navigate(url).map_err(|error| error.to_string())
}

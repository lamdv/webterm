# Chapter 5 — Server status

**Goal:** before you click Connect, show whether each server is reachable and
speaks a protocol this client understands. Until now a saved URL was a promise;
this chapter makes the picker tell the truth about it.

The code:

- `webterm/server.py` — discovery now advertises `protocol`
- `crates/webterm-client/src/discovery.rs` — `ServerInfo` and `discovery_url`
- `src-tauri/src/commands.rs` — `HttpClient` state and the `probe_all` command
- `frontend/app.js` — a status pill per row

```bash
cargo test -p webterm-client
cargo test -p webterm-desktop
cargo tauri dev
```

## What the server tells us

The previous server commit added an unauthenticated document at
`/.well-known/webterm.json`. It is served *before* `check_auth`, so a client can
classify a server without a credential. This chapter adds one field to it:

```json
{
  "service": "webterm",
  "version": "0.4.1",
  "protocol": 1,
  "auth_required": true,
  "auth_methods": ["basic", "bearer", "cookie"],
  "websocket_path": "/ws"
}
```

`protocol` is a *wire-protocol* version, distinct from the server's release
version. A client that does not know the protocol should refuse the connection
rather than send frames the server will misread. The Python side now has
`DISCOVERY_PROTOCOL = 1`, and the test suite pins the field.

## The document's shape is portable; the request is not

Parsing this JSON and deciding compatibility needs no window or socket, so it
lives in `webterm-client`:

```rust
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServerInfo {
    #[serde(default)]
    pub service: String,
    #[serde(default)]
    pub version: String,
    #[serde(default = "default_protocol")]
    pub protocol: u32,
    #[serde(default)]
    pub auth_required: bool,
    #[serde(default)]
    pub auth_methods: Vec<String>,
}
```

The `#[serde(default)]` on every field is deliberate: a server from an older
release omits `protocol` entirely, and that must not fail the parse.
`default_protocol` supplies `PROTOCOL_VERSION` (1) for exactly that case, so
"field absent" and "protocol 1" mean the same thing. Unknown fields such as
`websocket_path` are ignored, which is what lets the server add fields later.

`is_compatible` is one comparison:

```rust
pub fn is_compatible(&self) -> bool {
    self.protocol == PROTOCOL_VERSION
}
```

Building the discovery URL is also portable:

```rust
pub fn discovery_url(profile: &Profile) -> Result<Url, url::ParseError> {
    Url::parse(&profile.url)?.join(DISCOVERY_PATH.trim_start_matches('/'))
}
```

`Url::join` resolves the path *relative* to the base. This matters for a server
behind a mount point: `https://host/webterm/` yields
`https://host/webterm/.well-known/webterm.json`, not the proxy root. Two tests
pin the root and mounted cases.

## A real HTTP client, in Rust

The request itself is not portable (it needs TLS and the shell's runtime), so it
lives in `commands.rs`. We get an HTTP client from `tauri-plugin-http`, which
re-exports the `reqwest` it uses:

```rust
use tauri_plugin_http::reqwest;
```

The client is built once in `setup` and managed as state, so its connection pool
and TLS configuration are reused:

```rust
#[derive(Debug)]
pub struct HttpClient(reqwest::Client);

impl HttpClient::new() -> Result<Self, reqwest::Error> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .user_agent(concat!("webterm-desktop/", env!("CARGO_PKG_VERSION")))
        .build()?;
    Ok(Self(client))
}
```

The five-second timeout is a product decision, not a default: a probe is a hint,
and a hanging probe must become "offline" quickly.

### The first async command

Every command so far was synchronous. A request must be awaited, so `probe_all`
is `async`:

```rust
#[tauri::command]
pub async fn probe_all(
    profiles: State<'_, Profiles>,
    http: State<'_, HttpClient>,
) -> Result<Vec<ProbeResult>, String> {
    let profiles = {
        let store = profiles.0.lock().map_err(|_| poisoned())?;
        store.profiles().to_vec()
    };
    ...
}
```

Read the block around the lock. `ProfileStore` is behind a `Mutex`, and a
`MutexGuard` is **not `Send`**: holding it across an `.await` would make the
future unable to run on Tauri's multi-threaded runtime and fail to compile. So
we take what we need (`to_vec()`), let the guard drop at the end of the block,
and only then await. This is the standard shape for "async command + mutex".

### Turning failures into three states

`probe_one` collapses the result into a small enum the frontend can switch on:

```rust
async fn probe_one(client: &reqwest::Client, profile: Profile) -> ProbeResult {
    let id = profile.id.clone();
    match fetch_server_info(client, &profile).await {
        Ok(info) if info.is_compatible() => ProbeResult::online(id, info),
        Ok(info) => ProbeResult::incompatible(id, info),
        Err(error) => ProbeResult::offline(id, error),
    }
}
```

`fetch_server_info` reports every non-success answer as a string: a DNS failure,
a refused connection, a TLS error, a `401`, or JSON that is not a discovery
document. The picker only needs "offline, and here is why" for the tooltip.

The probes run one at a time. For the handful of servers a picker holds, a
sequential loop is simpler than spawning tasks and joining them, and the
five-second timeout bounds the worst case. A later chapter can fan them out
without changing the frontend.

## The pill

`app.js` keeps the latest results in a `Map` keyed by profile id and derives a
class and label from each one:

```js
function probeState(probe) {
  if (!probe) return { className: "checking", label: "checking…" };
  switch (probe.status) {
    case "online": {
      const version = probe.server?.version;
      return { className: "online", label: version ? `v${version}` : "online" };
    }
    case "incompatible":
      return { className: "warn", label: `needs protocol ${probe.server?.protocol}` };
    default:
      return { className: "offline", label: "offline" };
  }
}
```

Because `probes` starts empty, every row renders as "checking…" first, then
updates when `probe_all` returns. Editing, adding or removing a profile calls
`load()`, which re-renders and re-probes, so a corrected token or URL gets a
fresh status.

## Security notes

- The discovery document is unauthenticated **by design** and reveals only the
  service name, version, protocol and credential types. Never put anything
  sensitive in it.
- `reqwest` verifies TLS certificates. A self-signed remote server therefore
  probes as offline, which is honest — the browser client would also refuse it.
  Certificate pinning is a separate, larger feature.
- The probe sends no credential, so a malicious server learns nothing beyond
  "a webterm client looked at me".

## Exercise

1. Add a `latency_ms` field to `ProbeResult`, measured around the request with
   `std::time::Instant`, and show it in the pill's tooltip.
2. Make `probe_all` concurrent. `tauri::async_runtime::spawn` can run each
   `probe_one`, but joining the handles without a `futures` dependency is the
   interesting part — try a `tokio::sync::mpsc` channel instead.
3. Make the picker re-probe only the row that changed instead of the whole list.
4. Run `cargo fmt`, `cargo clippy`, `cargo test -p webterm-client`,
   `cargo test -p webterm-desktop`, and the Python suite (`uv run python -m
   unittest discover -s tests -t .`).

## Checkpoint

- Each row shows checking / online (with version) / incompatible / offline.
- A probe failure is explained in the tooltip, never a panic.
- The discovery document is parsed leniently: missing fields default rather
  than fail.
- `connect_url` and `discovery_url` are both unit tested in `webterm-client`.

Next: Chapter 6 gives the app a native menu bar and keyboard shortcuts — New
Server, Connect, and Quit — delivered by the shell rather than the webview.

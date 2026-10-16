# Chapter 3 — The picker

**Goal:** the app finally shows something. This chapter connects the three
pieces that have existed separately so far — the `ProfileStore` from Chapter 2,
the Tauri shell, and the HTML frontend — so you can list, add and remove
servers, and click one to open its terminal.

The code:

- `src-tauri/src/commands.rs` — managed state and the four IPC commands
- `src-tauri/src/lib.rs` — registering the state and the commands
- `frontend/index.html`, `frontend/app.js`, `frontend/ipc.js` — the picker

Run the tests and the app:

```bash
cargo test -p webterm-client   # the store tests still pass
cargo test -p webterm-desktop  # the label sanitiser
cargo tauri dev                # open a window and click around
```

## The shell's job: turn the store into state

The store has to survive between IPC calls, so it goes into Tauri's **managed
state**, a type map the app owns:

```rust
.setup(|app| {
    let path = app.path().app_config_dir()?.join("servers.json");
    app.manage(commands::Profiles::load(path)?);
    Ok(())
})
```

- `setup` runs once, after the app is built but before the window is shown. It
  is the right place for anything that can fail at startup.
- `app.path().app_config_dir()` is Tauri's platform answer to "where do app
  files live?" — `~/.config/dev.webterm.desktop` on Linux, `~/Library/
  Application Support/...` on macOS. We do not hard-code a dotfile path.
- `app.manage(..)` stores one value of a type; commands retrieve it by type.

The value is a `Profiles` newtype around `Mutex<ProfileStore>`:

```rust
pub struct Profiles(Mutex<ProfileStore>);
```

Why a `Mutex`? Commands can arrive at the same time — a fast double-click, or
a menu action landing while a click is in flight — and `ProfileStore::add`
takes `&mut self`. The mutex serialises them, exactly as it would in any
multi-threaded program. Each command locks,
does its work, and drops the guard before returning, so no borrow is held across
calls.

The `.setup` closure returns `Result<(), Box<dyn std::error::Error>>`. Both
`StoreError` and Tauri's path error implement `Error`, so the single `?` works
for both. A failure here aborts startup, which is right: the app cannot manage
servers without its store.

## Commands: functions the webview can call

A command is an ordinary function with an attribute:

```rust
#[tauri::command]
pub fn list_profiles(state: State<'_, Profiles>) -> Result<Vec<Profile>, String> {
    let store = state.0.lock().map_err(|_| poisoned())?;
    Ok(store.profiles().to_vec())
}
```

Three things to notice:

- `State<'_, Profiles>` is *injected*, not passed by JavaScript. Tauri matches
  the type to the value from `app.manage`. The lifetime is the usual "borrowed
  for the duration of the call".
- The return type is `Result<Vec<Profile>, String>`. Tauri serialises the `Ok`
  into the JavaScript promise's value and the `Err` into its rejection. A
  `String` is the simplest error channel; the store's `Display` already
  explains the problem.
- The list is cloned with `.to_vec()`. The command could not return
  `&[Profile]` even if it wanted to: the value crosses the IPC boundary as
  JSON, so it must be owned and `Serialize`. (If we returned a borrowed slice,
  the mutex guard would have to outlive the function — impossible.)

The commands are registered once:

```rust
.invoke_handler(tauri::generate_handler![
    commands::list_profiles,
    commands::add_profile,
    commands::remove_profile,
    commands::connect,
])
```

`generate_handler!` builds the dispatcher: it maps an incoming command name to
the right function and deserialises its arguments. Forget a command here and the
frontend gets "not found", even though the function exists.

A command runs off the UI thread: Tauri's default execution context wraps even a
synchronous function in an async task on its runtime, so a small file write
never freezes the window. For genuinely long work you would reach for an
`async fn` or `#[tauri::command(blocking)]`.

### Opening a server in the main window

`connect` looks the profile up and navigates the app's **one** window to the
server:

```rust
let window = app.get_webview_window("main").ok_or("the main window is missing")?;

// This command runs in the picker, so the window's current URL is the picker's.
// Remember it before we navigate away, so the Servers menu can come back.
if let Ok(url) = window.url() {
    app.state::<PickerUrl>().0.lock()?.replace(url);
}

let url = webterm_client::connect_url(&profile)?;
window.navigate(url)?;
```

There are three ways to show a server, and only one of them works here:

- **Navigate the existing window** (what we do). The webview's top-level document
  becomes the server origin, so its cookie and WebSocket are first-party and an
  OAuth2 proxy can run its normal login. The cost is that the picker is replaced
  while you are connected, which is exactly why `PickerUrl` exists and why the
  native **Servers** menu (Chapter 6) is the way back.
- **An `<iframe>` in the picker.** It would keep the list visible, but a
  third-party iframe cannot use the `SameSite=Strict` bootstrap cookie, and
  proxies commonly send `X-Frame-Options: DENY`. It would break the
  authentication built in Chapter 4.
- **A second window per server** (the previous design). Handy for watching two
  servers at once, but the point of this change is a single window.

The URL is chosen by Rust from the profile, so the remote page never gets to
choose its own origin.

### Making "no IPC" actually true

Tauri does **not** check capabilities for your own commands unless the app
declares an ACL manifest. With no manifest, a custom command is callable from
*every* window — including a remote server page that found
`window.__TAURI_INTERNALS__`. That is the opposite of what this app wants, so
`build.rs` declares the commands:

```rust
tauri_build::try_build(
    tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "list_profiles",
            "add_profile",
            "remove_profile",
            "connect",
        ]),
    ),
)
```

This generates `permissions/autogenerated/*.toml` (`allow-list-profiles`,
`deny-connect`, ...) and switches on per-command ACL checks. The capability then
names the allowed ones:

```json
"permissions": [
  "core:default",
  "allow-list-profiles",
  "allow-add-profile",
  "allow-remove-profile",
  "allow-connect"
]
```

Crucially, the capability has no `remote` scope, so it applies only to the
app's **local** origin. The picker is local and can call every allowed command;
the moment the window navigates to a server it is a remote origin, and Tauri has
no matching context, so every command is denied. The remote page is not "a
window we left out of the capability" any more — it is an origin the capability
was never for. This is the difference between a comment about security and an
enforced one.

## How JavaScript reaches those commands

Tauri injects `window.__TAURI_INTERNALS__` into each window it creates. The
official `@tauri-apps/api` package's `invoke` is essentially this:

```js
window.__TAURI_INTERNALS__.invoke(command, args)
```

Rather than vendor that package (no bundler here), `frontend/ipc.js` exposes the
one function we need:

```js
export function invoke(command, args = {}) {
  const internals = window.__TAURI_INTERNALS__;
  if (!internals) {
    return Promise.reject(new Error("the Tauri IPC bridge is unavailable in this window"));
  }
  return internals.invoke(command, args);
}
```

`app.js` is an ES module (`<script type="module" src="app.js">`), so it can
`import { invoke } from "./ipc.js"` with no build step.

Arguments become a JSON object whose keys match the command's parameters:

```js
await invoke("add_profile", {
  profile: { id: newId(), name, url, auth },
});
```

That `profile` object is deserialised straight into the `Profile` from
Chapter 1 — the `Serialize`/`Deserialize` derives are doing real work now. A
field the Rust struct does not know about is ignored; a missing required field
rejects the promise.

## Rendering without string injection

Profile names and URLs are user input, so the picker builds DOM nodes and sets
`.textContent`, never `innerHTML`:

```js
const name = document.createElement("span");
name.textContent = profile.name;
```

A server named `<img src=x onerror=alert(1)>` is then displayed as those literal
characters. This is the frontend version of the rule from `profile.rs`: keep
data and code separate. `list.replaceChildren(...profiles.map(serverRow))`
swaps the whole list in one step.

Errors from Rust are shown in a banner rather than swallowed:

```js
async function guard(action) {
  clearError();
  try {
    await action();
  } catch (error) {
    showError(error);
  }
}
```

Every button handler is wrapped in `guard`, so a rejected promise becomes a
visible message.

## Exercise

1. Add a **Rename** button to each row. That needs a new command
   `rename_profile(id, name)` in `commands.rs` and a `ProfileStore` method that
   changes a profile's name and saves (the store's `update` from the Chapter 2
   exercise, if you wrote it, is exactly this).
2. Register it in `generate_handler!` and call it from `app.js`, then refresh
   the list.
3. Run `cargo fmt`, `cargo clippy`, and both test suites before moving on.
4. Stretch goal: make the picker show a short "connecting…" state on the button
   while `connect` runs, then reset it whether the promise resolves or rejects.

## Checkpoint

- The picker lists every saved server and can add and remove one.
- Connect navigates the main window to the server's own client; the native
  **Servers** menu comes back to the picker.
- Once the window is on a server origin, the page has no IPC capabilities.
- `cargo test -p webterm-client` and `cargo test -p webterm-desktop` pass.

Next: [Chapter 4 — Credentials](04-credentials.md) makes `AuthKind` matter:
token profiles bootstrap a session cookie, and profiles can be edited in place.

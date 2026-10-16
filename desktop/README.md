# webterm desktop client

A Tauri client for remote webterm servers. It stores server profiles and opens
one window per server, pointed at that server's own web client — so the app and
the browser show exactly the same UI.

This app does **not** run a server. `localhost` is simply one more profile.

## Layout

```
Cargo.toml              cargo workspace (two members)
rust-toolchain.toml     pins stable + rustfmt + clippy
crates/webterm-client/  portable logic: profiles, probing, auth (no Tauri)
src-tauri/              the Tauri shell: windows, IPC, plugins
frontend/               the picker page (plain HTML/CSS/JS, no build step)
tutorial/               read this to learn the codebase and Rust
```

The split matters: anything testable without a window goes in
`crates/webterm-client`, which is what will let the same code compile for iPad
and Android later.

## Build

Prerequisites: `rustup`, the Tauri CLI, and the platform webview libraries
(Linux: `webkit2gtk-4.1`). See [tutorial/00-orientation.md](tutorial/00-orientation.md)
for the exact commands.

```bash
cargo test -p webterm-client   # fast: no window, no Tauri
cargo tauri dev                # run the app
cargo tauri build              # produce bundles
```

## Why the terminal UI is not in this repository

When you connect, the window navigates to the server's origin and loads the
server's `index.html` / `app.js` / `style.css`. Keeping the webview on the
server origin (rather than building our own terminal UI) is deliberate: it keeps
the session cookie and the WebSocket first-party and same-origin, which is what
makes OAuth2 proxies such as oauth2-proxy work without any server-side change.

The picker therefore cannot share a stylesheet with the server (different
origin, no CORS), so `frontend/picker.css` carries its own copy of the palette,
intentionally matching `webterm/static/style.css`. If you change one palette,
change the other.

## Security

- `capabilities/default.json` grants IPC to the `main` (picker) window only.
  Server windows are not listed, so a remote page cannot reach the app's IPC.
- `withGlobalTauri` is off, so there is no ambient `window.__TAURI__` to abuse.
- Remote windows are given exactly one server and navigate to it; they receive
  no app-level privileges.

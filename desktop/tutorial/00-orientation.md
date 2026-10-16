# Chapter 0 — Orientation

Welcome. This directory is a Rust project, and this `tutorial/` tree is both
its documentation and its syllabus. Each chapter adds one working slice of the
app and explains the Rust you need to read it. By the end you will have a Tauri
client that connects to any webterm server, and you will have written all of it.

If you are new to Rust, read the chapters in order. If you only want the
architecture, skip to `README.md`.

## What we are building

A **remote-only client**. The previous version of this app bundled the whole
Python webterm server and ran it in-process. That is gone. Now the app is a thin
shell around the browser client that already exists: it stores server profiles,
and when you pick one it navigates its one window to that server.

Two consequences follow, and they shape every decision after this:

1. **The terminal UI is not ours.** The window navigates to the server's origin
   and loads the server's own `index.html` / `app.js`. There is no terminal code
   in this app. What the browser sees and what the app shows are the same files.
2. **The app's own UI is only the picker** — the list of servers. That is the
   only thing in `frontend/`. The native menu is what gets you back to it once
   the window is showing a server.

## Why a workspace, not one crate

`desktop/Cargo.toml` is a *virtual workspace* with two members:

```
crates/webterm-client   portable logic — no Tauri, no window, fast to test
src-tauri               the Tauri shell — windows, IPC, plugins
```

The rule is simple: **if it can be tested without a window, it belongs in
`webterm-client`.** The shell stays thin. This is not ceremony — it is what lets
us run `cargo test -p webterm-client` in milliseconds, and it is what will let
the same logic compile for iPad and Android later without change.

## Install the toolchain

`rustup` is the toolchain manager; `cargo` is the build tool that ships with it.
On this machine (Arch/CachyOS):

```bash
# Rust itself, plus the native libraries Tauri links against.
sudo pacman -S --needed rustup webkit2gtk-4.1

# A tray icon would also need this; we use no tray, so it is optional.
#   sudo pacman -S --needed libayatana-appindicator

rustup default stable
```

On other systems, `rustup` is installed with:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Then install the Tauri CLI, which is what turns this project into an app:

```bash
# Fast path (Node is already installed):
npm install --global @tauri-apps/cli@^2

# Or the pure-Rust path, which compiles the CLI (a few minutes):
cargo install tauri-cli --version "^2" --locked
```

The `rust-toolchain.toml` in this directory pins the channel to `stable` and
asks for `rustfmt` and `clippy`, so `cargo` will fetch them on first use.

## The five commands you will live in

```bash
cargo check          # type-check only: fastest feedback while editing
cargo build          # compile without running
cargo test           # compile and run every #[test]
cargo fmt            # format (writes the files)
cargo clippy         # extra lints; treat warnings as a to-do list
```

Run them from `desktop/`. To run a single crate's tests, add `-p`:

```bash
cargo test -p webterm-client
```

To actually run the app in a window:

```bash
cargo tauri dev      # if you installed the CLI with cargo
npx tauri dev        # if you installed it with npm
```

## Reading your first compiler error

Rust's compiler is the best teacher here. Errors are long on purpose: they name
the exact span, explain what rule was broken, and usually show the fix. Read the
*first* error and ignore the cascade after it — later errors are frequently
consequences of the first. Two you will meet immediately:

- `cannot find value ... in this scope` — a typo, or a missing `use`.
- `borrow of moved value` — you handed ownership away and then used the
  original. Chapter 2 explains this properly; for now, note where it happened.

## What is on disk right now

```
Cargo.toml                      workspace root
rust-toolchain.toml             pins stable + rustfmt + clippy
crates/webterm-client/
  Cargo.toml
  src/lib.rs                    crate root; re-exports the public API
  src/profile.rs                Chapter 1: Profile + AuthKind
  src/store.rs                  Chapter 2: ProfileStore + StoreError
  src/connect.rs                Chapter 4: connect_url
  src/discovery.rs              Chapter 5: ServerInfo + discovery_url
src-tauri/
  Cargo.toml
  build.rs                      generates Tauri's context at compile time
  src/main.rs                   trivial: calls webterm_desktop_lib::run()
  src/lib.rs                    builds the app, registers state and commands
  src/commands.rs               Chapter 3: ProfileStore state + IPC commands
  src/menu.rs                   Chapter 6: native menu bar and shortcuts
  tauri.conf.json               window + bundle configuration
  capabilities/default.json     IPC permissions — picker window only
frontend/
  index.html                    the picker page
  app.js                        Chapter 3: list/add/remove/connect
  ipc.js                        Chapter 3: the invoke bridge
  picker.css                    self-contained styling
```

## Exercise

1. From `desktop/`, run `cargo test -p webterm-client`. It should compile and
   report four passing tests. Read `crates/webterm-client/src/profile.rs` to see
   what they assert — that file is Chapter 1.
2. Run `cargo clippy -p webterm-client` and confirm it is quiet.
3. Change the window `title` in `src-tauri/tauri.conf.json` to `webterm
   (chapter 0)`, then run `cargo tauri dev` and confirm the title bar changes.

## Checkpoint

- `cargo test -p webterm-client` passes.
- `cargo clippy` reports no warnings.
- `cargo tauri dev` opens a window showing the picker placeholder.

Next: [Chapter 1 — Modeling a server](01-modeling-a-server.md).

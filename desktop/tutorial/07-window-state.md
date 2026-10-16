# Chapter 7 — Remembering windows

**Goal:** a window should come back where you left it. Move the picker to a
second monitor, resize it, quit, relaunch — it should reappear in the same
place. The same should hold for every server window you had open.

The code is one plugin registration in `src-tauri/src/lib.rs`.

```bash
cargo tauri dev
```

## Why this is a plugin and not code

Window geometry is one of those problems that is trivial on one platform and
fiddly on the others: monitor coordinates can be negative, displays change
between launches, a window may be maximized, minimized or fullscreen, and the
"position before maximization" has to be remembered separately so un-maximizing
returns to the right spot. That is exactly the kind of platform plumbing Tauri
plugins exist to absorb.

```rust
#[cfg(desktop)]
let builder = builder
    .plugin(tauri_plugin_window_state::Builder::default().build())
    .menu(menu::build)
    .on_menu_event(menu::handle);
```

Two lines of intent (`.build()` and `.plugin(...)`) hide a `StateFlags` bitfield,
a JSON cache, five window-event handlers and a `RunEvent::Exit` hook.

The `#[cfg(desktop)]` is not optional: the plugin's crate is compiled out on
Android and iOS (`#![cfg(not(any(target_os = "android", target_os = "ios")))]`),
so referring to it there would not compile. This is the second desktop/mobile
split in the shell; see Chapter 6.

## What it does with those two lines

The plugin:

1. reads `app_config_dir()/.window-state.json` during setup — the same
   directory as `servers.json`, so all app state lives together;
2. for **each window as it becomes ready**, looks up its label in that file and
   restores size, position, maximized and fullscreen;
3. listens for `Moved` and `Resized` and keeps an in-memory copy current;
4. writes the file back on `RunEvent::Exit`.

The "for each window as it becomes ready" part is what makes this chapter more
than a one-liner. The picker exists from `tauri.conf.json`, but server windows
are created at runtime by `open_server_window` (Chapter 3). The plugin's
`on_window_ready` hook fires for them too, keyed by their label:

```rust
WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
```

That label is `server-<profile id>`, and Chapter 1 was explicit that a profile's
id survives renames and is *not* derived from its name. So a server window
opens at the remembered spot even after you rename the profile, and two
different servers never share a window slot. If the id had been derived from the
name, this chapter would have needed a migration.

Removing a profile does not prune its state entry, so `.window-state.json` can
accumulate a few stale labels. That file is an internal cache, not user data;
leaving dead keys is harmless and much simpler than reconciling it on every
delete.

## Choosing what to restore

The default is every flag:

```rust
StateFlags::SIZE | StateFlags::POSITION | StateFlags::MAXIMIZED
    | StateFlags::VISIBLE | StateFlags::DECORATIONS | StateFlags::FULLSCREEN
```

`VISIBLE` and `DECORATIONS` are the two worth thinking about. Restoring
`VISIBLE` means a window that was hidden — not closed — when the app exited
starts hidden. This app never hides a window, so the default is fine; an app
with a tray icon (which does hide windows) would want to drop that flag. Change
it with `with_state_flags`:

```rust
tauri_plugin_window_state::Builder::default()
    .with_state_flags(StateFlags::SIZE | StateFlags::POSITION | StateFlags::MAXIMIZED)
    .build()
```

The plugin also exposes `skip_initial_state(label)` to leave one window to the
configuration file, and `map_label(...)` to rewrite labels before they are used
as keys.

## Trying it

The save happens on a clean exit, so the workflow is:

1. `cargo tauri dev`, move and resize the picker.
2. Quit the app from the **Server → Quit** menu (Chapter 6), not `Ctrl+C` in the
   terminal — that kills the process before `RunEvent::Exit` fires.
3. `cargo tauri dev` again; the window should return to where you put it.
4. Connect to a server, move *that* window, quit, and re-connect.

## Exercise

1. Restrict the flags to size, position and maximized, then verify that
   maximizing and quitting restores the maximized window.
2. Use `skip_initial_state("main")` so the picker always opens at the size from
   `tauri.conf.json`, while server windows are still restored.
3. Read `~/.config/dev.webterm.desktop/.window-state.json` after a run. It is
   plain JSON keyed by window label; explain why `prev_x`/`prev_y` exist.
4. Run `cargo fmt`, `cargo clippy`, and both test suites.

## Checkpoint

- The picker and every server window restore their geometry across launches.
- Repeated connects to the same profile reuse one remembered position.
- The state file lives beside `servers.json` in the app config directory.
- The plugin is compiled out on mobile targets.

Next: Chapter 8 gives the app a tray icon with a session count and a
quit-when-idle action, so it can live in the background without a visible
window.

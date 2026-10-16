# Chapter 7 — Remembering windows

**Goal:** the window should come back where you left it. Move it to a second
monitor, resize it, quit, relaunch — it should reappear in the same place. With
a single window (Chapter 6) that is the whole requirement; there is no separate
per-server geometry to track.

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

With a single window this is almost anticlimactic: one label, `main`, one saved
geometry. The interesting property is a negative one — the picker and every
server share that geometry, because they are the same window at different times.
Move the window while a server is open, and the picker appears there next
launch.

(The earlier multi-window design keyed state by `server-<profile id>`, so each
server remembered its own spot. That `WebviewWindowBuilder` code is gone with the
extra windows. The profile id still matters, but only for the store.)

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
4. Connect to a server (the same window navigates), move it, quit, and relaunch.

## Exercise

1. Restrict the flags to size, position and maximized, then verify that
   maximizing and quitting restores the maximized window.
2. Use `with_state_flags(StateFlags::POSITION)` alone so the window always opens
   at the size from `tauri.conf.json` but remembers where you put it.
3. Read `~/.config/dev.webterm.desktop/.window-state.json` after a run. It is
   plain JSON keyed by window label; explain why `prev_x`/`prev_y` exist.
4. Run `cargo fmt`, `cargo clippy`, and both test suites.

## Checkpoint

- The window restores its geometry across launches, whichever view it was
  showing when it quit.
- The picker and every server share that one geometry, because they are the same
  window.
- The state file lives beside `servers.json` in the app config directory.
- The plugin is compiled out on mobile targets.

Next: there is no further chapter for now. The app closes and exits with its
one window (there is no tray icon), and the remaining work is packaging —
see the Roadmap in `desktop/README.md`.

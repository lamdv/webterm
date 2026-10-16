# Chapter 6 — Menus and shortcuts

**Goal:** give the app a real menu bar and keyboard shortcuts. A menu is native
chrome, so it works even in a server window that is showing a remote page we do
not control — a webview-only toolbar could not do that.

The code is `src-tauri/src/menu.rs`, wired in from `lib.rs`.

```bash
cargo test -p webterm-client
cargo test -p webterm-desktop
cargo tauri dev
```

## Why native

There are two windows in play. The **picker** is ours; a **server window** loads
whatever the server serves. If the toolbar lived in `frontend/app.js`, a server
window could never have it. A native menu is built in Rust and attached by the
window system, so a Quit or Reload item is available everywhere with no
cooperation from the page. That is the whole reason to add one.

## Building the menu

`Builder::menu` takes a closure that runs once, before any window exists:

```rust
let builder = builder.menu(menu::build).on_menu_event(menu::handle);
```

`build` returns a `Menu`, assembled from builders:

```rust
pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let new_server = MenuItemBuilder::with_id("new-server", "New Server")
        .accelerator("CmdOrCtrl+N")
        .build(app)?;
    let reload = MenuItemBuilder::with_id("reload", "Reload Window")
        .accelerator("CmdOrCtrl+R")
        .build(app)?;

    let server = SubmenuBuilder::new(app, "Server")
        .item(&new_server)
        .item(&reload)
        .separator()
        .quit()
        .build()?;

    let window = SubmenuBuilder::new(app, "Window")
        .minimize()
        .fullscreen()
        .close_window()
        .build()?;

    MenuBuilder::new(app).item(&server).item(&window).build()
}
```

Three kinds of thing are being combined:

- **`MenuItem`** — a custom item, identified by the id we choose. We need our own
  id because we will match on it.
- **`PredefinedMenuItem`** — a platform item with platform behaviour. `.quit()`,
  `.minimize()`, `.fullscreen()` and `.close_window()` are builder shortcuts for
  it. The operating system handles them; they never reach our event handler.
- **`Submenu`** — a labelled group. On macOS a top-level `Menu` may only contain
  submenus, which is why "Server" and "Window" are submenus rather than loose
  items.

Builders borrow the menu items they add (`.item(&new_server)`), so the locals
must outlive the `build` call; they do, because the submenu's `build()` runs
inside the same function.

### Accelerators and a terminal-shaped trap

`.accelerator("CmdOrCtrl+N")` is parsed by Tauri and registered with the window
system, so the shortcut fires even when focus is inside the webview.

It is tempting to add predefined Copy/Paste/Select-All items next. Resist:
`PredefinedMenuItem::copy` carries the platform accelerator, which is
**Ctrl+C** on Linux and Windows. In a terminal that is SIGINT, and a menu
accelerator is handled *before* the webview sees the key — the menu would eat
every interrupt. A real terminal needs explicit Copy items with a different
accelerator (Ctrl+Shift+C) or none at all. Leaving a helpful item out is a
decision, not an omission, and this is the kind of thing a native menu forces
you to notice.

## Handling a click

`on_menu_event` receives every menu click:

```rust
pub fn handle<R: Runtime>(app: &AppHandle<R>, event: MenuEvent) {
    match event.id().as_ref() {
        "new-server" => focus_picker(app),
        "reload" => reload_focused(app),
        _ => {}
    }
}
```

`event.id()` is a `MenuId`, which dereferences to the `&str` we passed to
`with_id`. Predefined items are handled by the platform and never arrive here,
so the catch-all arm is genuinely empty.

`focus_picker` brings the picker forward and then reaches into the page:

```rust
let _ = window.eval("document.querySelector('#name')?.focus()");
```

That `eval` is the one seam between native chrome and the web UI. It is fine for
a one-line nudge like focusing a field. Anything larger belongs in an IPC
command, where the types are checked and the capability system applies.

`reload_focused` acts on whichever window the user is looking at:

```rust
let focused = app
    .webview_windows()
    .into_values()
    .find(|window| window.is_focused().unwrap_or(false));

if let Some(window) = focused.or_else(|| app.get_webview_window("main")) {
    let _ = window.reload();
}
```

`webview_windows()` returns a map, `into_values` gives owned windows, and `find`
picks the focused one. The fallback to the picker covers the moment when no
window reports focus (for instance while a menu is open).

## The menu must be attached to new windows

On macOS the menu is owned by the application and shown for whichever window is
active. On Windows and Linux it belongs to *each window*. The picker is created
from `tauri.conf.json` after `.menu()` has run, so it gets one; server windows
are created at runtime, so `open_server_window` passes the app menu explicitly:

```rust
let builder = WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
    .title(&profile.name)
    .inner_size(900.0, 600.0);

#[cfg(desktop)]
let builder = match app.menu() {
    Some(menu) => builder.menu(menu),
    None => builder,
};

builder.build()?;
```

## Desktop-only, by construction

`Builder::menu`, `on_menu_event` and `WebviewWindowBuilder::menu` are all behind
`#[cfg(desktop)]`, because mobile platforms have no menu bar. `lib.rs` gates the
module and the two builder calls:

```rust
#[cfg(desktop)]
mod menu;

// ...

#[cfg(desktop)]
let builder = builder.menu(menu::build).on_menu_event(menu::handle);
```

The rest of `run` is shared with a future mobile build. This is the first place
the desktop/mobile split actually appears in code rather than in a comment.

## Exercise

1. Add a **Connect to…** item that opens the picker, focuses the first row, and
   presses its Connect button: `document.querySelector('.server button')?.click()`.
   Notice how quickly `eval` moves from a nudge to a hack — and where the IPC
   alternative would be.
2. Add a **Copy** item with `accelerator("CmdOrCtrl+Shift+C")` and dispatch it
   to the focused window with `document.execCommand('copy')`. Try it in a server
   window running a terminal; it should copy the selection without stealing
   Ctrl+C.
3. Give the "Server" submenu an id and use `app.menu()` to find it later.
4. Run `cargo fmt`, `cargo clippy`, and both test suites.

## Checkpoint

- A native menu with Server and Window submenus appears in both the picker and
  server windows.
- `CmdOrCtrl+N` focuses the picker and its add form; `CmdOrCtrl+R` reloads the
  focused window.
- Predefined Quit, Minimize, Fullscreen and Close Window work without any Rust
  handling.
- The menu code is compiled out on mobile targets.

Next: Chapter 7 remembers where every window was, so the app restores its layout
across launches.

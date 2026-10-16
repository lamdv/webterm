# Chapter 6 — Menus and shortcuts

**Goal:** give the app a real menu bar and keyboard shortcuts. A menu is native
chrome, so it works whether the main window is showing the picker or a remote
server page we do not control — a webview-only toolbar could not do that.

The code is `src-tauri/src/menu.rs`, wired in from `lib.rs`.

```bash
cargo test -p webterm-client
cargo test -p webterm-desktop
cargo tauri dev
```

## Why native

Here the one window shows one of two things: our **picker**, or a remote server
page. If the toolbar lived in `frontend/app.js`, it would vanish the moment the
window navigated to a server. A native menu is built in Rust and attached by the
window system, so a Quit, Servers or Reload item is always available with no
cooperation from the page. That is the whole reason to add one.

## Building the menu

`Builder::menu` takes a closure that runs once, before any window exists:

```rust
let builder = builder.menu(menu::build).on_menu_event(menu::handle);
```

`build` returns a `Menu`, assembled from builders:

```rust
pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let servers = MenuItemBuilder::with_id("servers", "Servers")
        .accelerator("CmdOrCtrl+Shift+S")
        .build(app)?;
    let reload = MenuItemBuilder::with_id("reload", "Reload Window")
        .accelerator("CmdOrCtrl+R")
        .build(app)?;

    let server = SubmenuBuilder::new(app, "Server")
        .item(&servers)
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
  id because we will match on it. `servers` is the one custom item, and it is
  load-bearing: it is the only way back from a server to the picker, because a
  remote page cannot draw a "back" button for us.
- **`PredefinedMenuItem`** — a platform item with platform behaviour. `.quit()`,
  `.minimize()`, `.fullscreen()` and `.close_window()` are builder shortcuts for
  it. The operating system handles them; they never reach our event handler.
- **`Submenu`** — a labelled group. On macOS a top-level `Menu` may only contain
  submenus, which is why "Server" and "Window" are submenus rather than loose
  items.

Builders borrow the menu items they add (`.item(&servers)`), so the locals must
outlive the `build` call; they do, because the submenu's `build()` runs inside
the same function.

### Accelerators and a terminal-shaped trap

`.accelerator("CmdOrCtrl+Shift+S")` is parsed by Tauri and registered with the
window system, so the shortcut fires even when focus is inside the webview.

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
        "servers" => {
            let _ = commands::show_picker(app);
        }
        "reload" => reload_main_window(app),
        _ => {}
    }
}
```

`event.id()` is a `MenuId`, which dereferences to the `&str` we passed to
`with_id`. Predefined items are handled by the platform and never arrive here,
so the catch-all arm is genuinely empty.

`show_picker` is the interesting one. When the window is on a server origin the
page is remote and has no IPC (Chapter 3), so it cannot offer a "back" button.
The only code that can bring the picker back is native Rust. `connect` recorded
the picker's URL before navigating away; `show_picker` navigates back to it:

```rust
pub fn show_picker<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let window = app.get_webview_window("main").ok_or("the main window is missing")?;
    let Some(url) = app.state::<PickerUrl>().0.lock()?.clone() else {
        return Ok(()); // never left the picker
    };
    window.navigate(url).map_err(|error| error.to_string())
}
```

This is the counterpart to Chapter 3's `connect`: one function navigates to a
server, the other back. Together they are the whole single-window model — there
is no separate "New Server" item, because the picker already shows the add form.

`reload_main_window` is even simpler now that there is only one window:

```rust
fn reload_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.reload();
    }
}
```

## One window, one menu

An earlier version of this app opened a window per server. On Windows and Linux
a menu belongs to a window, so each runtime-created window had to be handed the
app menu explicitly. With a single window that problem disappears:
`Builder::menu` installs the app menu before the `main` window is created, and
that window keeps the same native menu as it navigates between the picker and
any number of servers.

## Desktop-only, by construction

`Builder::menu` and `on_menu_event` are behind `#[cfg(desktop)]`, because
mobile platforms have no menu bar. `lib.rs` gates the module and the two
builder calls:

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

1. Try to re-add a **New Server** item (Cmd/Ctrl+N) that goes to the picker and
   focuses the add form. The awkward part is crossing the navigation: a URL
   fragment such as `#new` that the picker reads on load is one option. Notice
   that native-to-web signalling is the awkward direction, while web-to-native
   is just `invoke`.
2. Add a **Copy** item with `accelerator("CmdOrCtrl+Shift+C")` and dispatch it
   with `document.execCommand('copy')`. Try it while a server is showing a
   terminal; it should copy the selection without stealing Ctrl+C.
3. Give the "Server" submenu an id and use `app.menu()` to find it later.
4. Run `cargo fmt`, `cargo clippy`, and both test suites.

## Checkpoint

- A native menu with Server and Window submenus appears in the single window,
  whichever view it is showing.
- **Servers** (Cmd/Ctrl+Shift+S) navigates back to the picker from a server;
  **Reload Window** (Cmd/Ctrl+R) reloads the current view.
- Predefined Quit, Minimize, Fullscreen and Close Window work without any Rust
  handling.
- The menu code is compiled out on mobile targets.

Next: [Chapter 7 — Remembering windows](07-window-state.md) persists the
window's size and position across launches.

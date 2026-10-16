# Chapter 4 — Credentials

**Goal:** make `AuthKind` mean something. Until now the picker stored whether a
server wants a token, a browser login, or nothing — and then ignored it when you
clicked Connect. This chapter applies the credential, and adds profile editing
so a mistyped token can be fixed without deleting the server.

The code:

- `crates/webterm-client/src/connect.rs` — `connect_url`, the portable rule
- `crates/webterm-client/src/store.rs` — `ProfileStore::update`
- `src-tauri/src/commands.rs` — `connect` uses `connect_url`; new
  `update_profile`
- `frontend/app.js` — the form doubles as an editor

As always:

```bash
cargo test -p webterm-client
cargo test -p webterm-desktop
cargo tauri dev
```

## Where the credential decision belongs

The rule "which URL do we open for this profile?" depends only on a `Profile`:
no window, no Tauri, no network. That puts it in the portable crate, where it is
fast to test:

```rust
pub fn connect_url(profile: &Profile) -> Result<Url, url::ParseError> {
    let mut url = Url::parse(&profile.url)?;
    if let AuthKind::Token { token } = &profile.auth {
        url.query_pairs_mut().append_pair("bootstrap", token);
    }
    Ok(url)
}
```

`Url` comes from the `url` crate, which the workspace now depends on. Parsing
rather than concatenating matters: it accepts `https://host`, `https://host:8443`,
and `https://host/path?x=1` uniformly, and it knows how to append a query
parameter to a URL that already has one.

The `if let` pattern is the concise form of a one-arm `match`. We borrow the
auth kind (`&profile.auth`) and match the `Token` variant; `None` and `Browser`
fall through and the URL is returned untouched. This is the payoff of the
`AuthKind` enum from Chapter 1: adding a fourth credential later makes the
compiler point here, because `connect_url` will need to say what it does for it
— or a `_ => {}` will be needed, which is a deliberate choice rather than an
oversight.

## The bootstrap flow

Why put the token in a query parameter at all? A webview on the server's origin
authenticates its WebSocket with cookies, and Tauri cannot set a cookie from
Rust in a portable way. So the server offers a one-shot exchange, implemented in
`webterm/server.py`:

1. The window opens `https://server/?bootstrap=<token>`.
2. If the token matches, the server replies
   `302 Found`, `Set-Cookie: webterm_session=…; HttpOnly; SameSite=Strict;
   Path=/`, and `Location: /`.
3. The webview follows the redirect; from then on every request, including the
   `/ws` upgrade, carries the cookie. `SameSite=Strict` also means another site
   cannot ride that cookie in a cross-site WebSocket — the CSWSH gap.

The token therefore appears in the window's first URL. It is a real tradeoff, so
be honest about it in code and docs: the response is `Cache-Control: no-store`,
the redirect happens immediately, the desktop window draws no URL bar, and the
secret is never repeated. A future chapter could replace this with an
`Authorization: Bearer` header if Tauri grows a cookie API; the server already
accepts Bearer for exactly that reason.

`AuthKind::Browser` intentionally sends *no* credential. The URL points at an
identity-aware proxy (oauth2-proxy, Authelia), and letting the proxy run its own
login in the window is the whole feature.

> **Untested.** This is the one credential path that has not been run against a
> real proxy. It depends on the provider's login page rendering in the embedded
> webview, which several IdPs refuse. Testing it against oauth2-proxy is a
> tracked roadmap item in `desktop/README.md`, along with the follow-ups it will
decide (sign-in-again, a "login required" status, popup handling).

## Editing, and the `update` method

The store gets the method from the Chapter 2 exercise:

```rust
pub fn update(&mut self, profile: Profile) -> Result<Option<Profile>, StoreError> {
    let Some(index) = self.profiles.iter().position(|p| p.id == profile.id) else {
        return Ok(None);
    };
    let previous = std::mem::replace(&mut self.profiles[index], profile);
    if let Err(error) = self.save() {
        self.profiles[index] = previous;
        return Err(error);
    }
    Ok(Some(previous))
}
```

`std::mem::replace` swaps the new profile in and hands back the old one *in one
step*, without cloning. The returned value is used two ways: to restore the old
profile if the save fails, and to tell the caller what was replaced. The borrow
checker is happy because the two uses are on opposite branches.

The command is three lines, exactly like `remove_profile`:

```rust
#[tauri::command]
pub fn update_profile(state: State<'_, Profiles>, profile: Profile) -> Result<bool, String> {
    let mut store = state.0.lock().map_err(|_| poisoned())?;
    store.update(profile).map(|updated| updated.is_some()).map_err(describe)
}
```

### Adding a command touches three places

`connect` now calls `webterm_client::connect_url(profile)` instead of parsing
the URL itself, and `update_profile` joins the command set. Registering it is
not one edit but three, and forgetting any one fails differently:

| Place | What it does | Symptom if you forget |
|---|---|---|
| `generate_handler!` in `lib.rs` | dispatch by name | "command not found" from JS |
| `build.rs` `AppManifest::commands` | generates `allow-update-profile` | "not allowed by ACL" |
| `capabilities/default.json` | grants it to the `main` window | "not allowed by ACL" |

The middle two are the ACL from Chapter 3. The permission files under
`permissions/autogenerated/` are written by `build.rs`; `update_profile.toml`
appeared the first time the app was built after the command was declared.

## The form becomes an editor

The frontend keeps one piece of module state:

```js
let editingId = null;
```

`startEdit(profile)` fills the fields and switches the submit button to "Save
changes"; `stopEdit()` resets everything. On submit, the id decides the command:

```js
const profile = { id: editingId ?? newId(), name, url, auth };
if (editingId === null) {
    await invoke("add_profile", { profile });
} else {
    await invoke("update_profile", { profile });
}
```

The `??` is the nullish-coalescing operator: use `editingId` when it is set,
otherwise mint a new id. Because the id is carried through an edit unchanged,
the profile keeps its identity — and if you later namespace per-server state (a
remembered window size, say), an edit will not orphan it. That is the promise
`Profile::id` made back in Chapter 1.

## Exercise

1. Add a **Test** button per row that runs `connect_url`'s logic through a new
   `#[tauri::command] fn probe(id) -> Result<String, String>` and returns the
   URL that would be opened, so a token that fails to encode is visible before
   you click Connect.
2. Notice that `Url` is not `Serialize` in a useful way here. Return
   `url.to_string()` from the command and explain to yourself why the `Profile`
   type crosses IPC as JSON but a `Url` does not.
3. Try to make `connect_url` return `&str` instead of `Url`. The compiler will
   explain that the owned URL is built locally and cannot be borrowed out; the
   fix is to keep returning `Url`.
4. Run `cargo fmt`, `cargo clippy`, `cargo test -p webterm-client` and
   `cargo test -p webterm-desktop`.

## Checkpoint

- Connecting to a token profile bootstraps a session cookie; the terminal then
  authenticates without the token in the WebSocket URL.
- Browser-login and open profiles still open their URL unchanged.
- A profile can be edited in place, keeping its id.
- `connect_url` has unit tests for open, browser, token, encoding and invalid
  URLs.

Next: [Chapter 5 — Server status](05-server-status.md) probes each server's
`/.well-known/webterm.json` discovery document, so reachability, the version
and protocol compatibility are visible before you connect.

# Chapter 1 — Modeling a server

**Goal:** describe a connectable server as data, so that the rest of the app
never has to think about JSON, files, or the network. The code for this chapter
is `crates/webterm-client/src/profile.rs`.

Everything here is plain Rust with one dependency, `serde`. No windows, no
Tauri. Run the chapter's tests with:

```bash
cargo test -p webterm-client
```

## A `struct` is a named bundle of fields

```rust
pub struct Profile {
    pub id: String,
    pub name: String,
    pub url: String,
    pub auth: AuthKind,
}
```

Four things to notice, because they recur constantly:

- `pub` on the struct and on each field. Rust makes everything private by
  default; `pub` is opt-in, field by field.
- `String`, not `&str`. A `String` owns its bytes and can grow or move freely; a
  `&str` is a borrowed view into someone else's bytes and carries a lifetime.
  For data that outlives a function call — like a stored profile — you want
  `String`. This is the first appearance of *ownership*, which Chapter 2 makes
  concrete.
- The type is a **plain old data** type: no behavior, no hidden state. That is
  why we can clone it, compare it, and serialise it so easily.
- `id` is separate from `name` and is explicitly *not* derived from it. A user
  renaming "Dev box" to "Prod box" must not orphan its window label or its
  keychain entry.

## `#[derive(...)]` generates the boring impls

```rust
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Profile { /* ... */ }
```

Each name is a *derive macro* that writes code for you:

| Derive | What you get | Why we want it |
|---|---|---|
| `Debug` | `{:?}` formatting | `assert_eq!` failures print the value; `dbg!` works |
| `Clone` | `.clone()` | hand a copy to the UI and keep the original |
| `PartialEq` / `Eq` | `==` | compare a round-tripped value to the original |
| `Serialize` | value → JSON | write profiles to disk, send them over IPC |
| `Deserialize` | JSON → value | read profiles back |

`Serialize` and `Deserialize` come from `serde`. The workspace enables serde's
`derive` feature in `Cargo.toml`, which is what makes those two names available
as derives.

## Enums model "one of several shapes"

Authentication is not a string — it is one of a few genuinely different things.
Rust's `enum` is built for exactly this, and unlike enums in many languages, each
variant can carry its own data:

```rust
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AuthKind {
    #[default]
    None,
    Token { token: String },
    Browser,
}
```

- `None` and `Browser` are *unit* variants: markers with no data.
- `Token { token: String }` is a *struct* variant: it carries named fields.
- `#[default]` (with the derived `Default`) marks which variant `AuthKind::default()`
  produces. That pairs with `#[serde(default)]` on `Profile::auth` so a file
  written before this field existed still loads.

### The serde tags, and the trap

By default serde writes an enum *externally tagged*: `Token { token }` becomes
`{"token": {"token": "..."}}`. We asked for `#[serde(tag = "kind")]` instead,
which produces the flatter, friendlier:

```json
{ "kind": "token", "token": "s3cret" }
```

`rename_all = "snake_case"` lowercases the variant names for the wire, so the
Rust `Browser` is JSON `"browser"`.

> **The trap:** serde *cannot* internally tag a newtype variant such as
> `Token(String)`. There is no place to put the `"kind"` key inside a bare
> string, and the compiler will say so. The fix is always the same — give the
> variant named fields, as we did.

## `match` and returning borrowed text

```rust
impl AuthKind {
    pub fn label(&self) -> &'static str {
        match self {
            AuthKind::None => "open",
            AuthKind::Token { .. } => "token",
            AuthKind::Browser => "browser login",
        }
    }
}
```

- `match` must be *exhaustive*: every variant is listed, and the compiler
  refuses to build if you add a variant and forget a arm. That is a feature —
  adding `AuthKind::Mtls` later will point you at every place that needs
  updating.
- `Token { .. }` ignores the fields; we only care which variant it is.
- `&'static str` says the returned text lives for the whole program. The strings
  are baked into the binary, so nothing is allocated and no lifetime is tied to
  `&self`.

## Tests live next to the code

The bottom of `profile.rs` is a `#[cfg(test)] mod tests`. That module is
compiled only for `cargo test`, so it costs nothing in the shipped binary, and
it can reach private items via `use super::*`.

The interesting one asserts the *wire shape*, not just a round-trip:

```rust
#[test]
fn auth_is_internally_tagged() {
    let value = serde_json::to_value(AuthKind::Token { token: "s3cret".into() })
        .expect("serialise");
    assert_eq!(value, json!({ "kind": "token", "token": "s3cret" }));
}
```

`"s3cret".into()` converts a `&str` into a `String`; `.expect(...)` unwraps a
`Result` or panics with your message. Round-tripping alone would not catch a
change to the tag name, because serialise and deserialise would still agree with
each other — pinning the literal JSON does.

## Exercise

1. Add a variant `AuthKind::Mtls { fingerprint: String }` for pinning a client
   certificate. Then run `cargo test -p webterm-client` and watch the compiler
   tell you that `label`'s `match` is no longer exhaustive.
2. Add the missing arm, returning `"client certificate"`.
3. Add a test that serialises it and asserts the exact JSON, mirroring
   `auth_is_internally_tagged`.
4. Run `cargo fmt` and `cargo clippy -p webterm-client` before moving on.

If you want a stretch goal: the round-trip test compares whole `Profile` values
with `==`. Explain to yourself why `Eq` is required on `AuthKind` for that to
compile, and why `f64` fields would have made it impossible.

## Checkpoint

- `Profile` and `AuthKind` serialise to stable, human-readable JSON.
- `cargo test -p webterm-client` passes, including the JSON-shape tests.
- The model has no dependency on Tauri, files, or the network.

Next: [Chapter 2 — Persisting the list](02-persisting-the-list.md) turns this
value into a persisted `Vec<Profile>`, the file system, and the `Result`/`?`
error-handling style.

# Chapter 2 — Persisting the list

**Goal:** keep more than one server, and keep it across restarts. Chapter 1
gave us one `Profile` as data; this chapter turns it into a list stored in a
JSON file. The code is `crates/webterm-client/src/store.rs`.

Run its tests, as always, with:

```bash
cargo test -p webterm-client
```

This is the chapter where the two ideas Chapter 0 warned about — *ownership*
and *borrow of moved value* — stop being abstract. Read it with
`store.rs` open beside you.

## `Vec<T>` owns, `&[T]` borrows

A `Vec<Profile>` is a growable list that **owns** its `Profile`s. A `&[Profile]`
is a *slice*: a borrowed window onto someone else's list. The store owns the
`Vec`; the rest of the app gets a slice:

```rust
pub fn profiles(&self) -> &[Profile] {
    &self.profiles
}
```

Why not return `&Vec<Profile>`? A slice is more general — it can point at a
`Vec`, an array, or part of one — and it cannot be grown or shrunk by the
caller. The rule of thumb: **take the least powerful type you can work with.**
`&[Profile]` says "read these, do not change the list", and that is all the
picker needs.

That `&` before `self` is `profiles`'s whole interface contract: it borrows the
store. You can call it many times at once, and none of those calls can mutate
anything.

## `&self` and `&mut self`

Adding to the store changes it, so `add` takes `&mut self`:

```rust
pub fn add(&mut self, profile: Profile) -> Result<(), StoreError> {
    if self.get(&profile.id).is_some() {
        return Err(StoreError::DuplicateId(profile.id));
    }
    self.profiles.push(profile);
    ...
}
```

Read the borrow checker's mind here:

- `&mut self` means `add` has *exclusive* access to the store for the duration
  of the call. No one else can be holding a `&Profile` into it.
- `self.get(&profile.id)` borrows the store immutably and returns an
  `Option<&Profile>`. That borrow ends at the end of the `if` line.
- `self.profiles.push(profile)` then takes a mutable borrow and **moves**
  `profile` into the list.

That word *moves* is the whole trick. `Profile` is not `Copy` (it owns a
`String`), so `push(profile)` transfers ownership: after that line, the local
`profile` is gone, and the compiler would refuse to let you use it. If you need
it again, `.clone()` it first — at the cost of copying its strings.

The duplicate check is also why `StoreError::DuplicateId` carries a `String`
and not a `&str`. We want to report the id *after* `profile` has been consulted;
a borrowed `&str` would keep borrowing `profile`, which we are about to move.
Owning the id sidesteps the conflict.

Finally, note the rollback:

```rust
if let Err(error) = self.save() {
    self.profiles.pop();
    return Err(error);
}
```

If the disk write fails, the in-memory list is put back the way it was, so the
store never claims to hold something the file does not.

## `Option`: "not found" is not an error

An unknown id is an ordinary outcome, so `get` returns `Option`, not `Result`:

```rust
pub fn get(&self, id: &str) -> Option<&Profile> {
    self.profiles.iter().find(|profile| profile.id == id)
}
```

`iter()` walks the list by reference; `find` takes a closure (`|profile| ...`)
and returns the first match — `Some(&Profile)`, or `None`. Both the search and
the result borrow the store, which is why the returned reference is tied to
`&self`'s lifetime without us writing one.

`remove` shows `Option` *inside* `Result`, which reads as "a fallible operation
whose success may still find nothing":

```rust
let Some(index) = self.profiles.iter().position(|profile| profile.id == id) else {
    return Ok(None);
};
```

`let ... else` is a newer, flatter way to handle the `None` arm: the `else`
block *must* diverge (`return`, `break`, `panic!`), which reassures the
compiler that `index` is valid afterwards. `position` gives the index so we can
put the item back if saving fails.

> `Option::unwrap` panics. Use it only where the value is provably there; the
> library code above never does, and neither should you.

## `Result`, `?`, and one error type

Loading can fail for several reasons, so the store defines its own error:

```rust
pub enum StoreError {
    Io(io::Error),
    Json(serde_json::Error),
    DuplicateId(String),
}
```

Each variant keeps the *underlying* error type, so nothing is lossy. A
`StoreError` implements `Display` (for humans), `Error` (`source()` lets a
caller walk the cause chain) and two `From` impls. Those `From`s are the secret
behind `?`:

```rust
pub fn load(path: impl Into<PathBuf>) -> Result<Self, StoreError> {
    let path = path.into();
    let profiles = match fs::read_to_string(&path) {
        Ok(text) => serde_json::from_str(&text)?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => Vec::new(),
        Err(error) => return Err(error.into()),
    };
    Ok(Self { path, profiles })
}
```

- `serde_json::from_str(&text)?` returns a `serde_json::Error`; the `?` sees
  that `load` wants a `StoreError`, finds `From<serde_json::Error> for
  StoreError`, and converts automatically.
- The `Err(error) if ...` arm is a *match guard*. A missing file is the normal
  first run, so it becomes an empty list instead of an error. Every other read
  failure is propagated with `.into()`.
- Nothing is unwrapped: callers decide what to tell the user.

Why not `Box<dyn Error>`, the usual "any error" shortcut? Because a caller
cannot `match` on a trait object. The desktop shell will want to tell the user
"that file is corrupt" differently from "that file belongs to root", and an
enum keeps both reachable.

## Paths: `PathBuf` and `Path`

We could have used `String`, but paths are not text: they have components,
extensions and platform rules. Rust splits the owned path (`PathBuf`) from the
borrowed view (`Path`), exactly like `String` and `&str`:

```rust
pub struct ProfileStore {
    path: PathBuf,       // owned
    profiles: Vec<Profile>,
}
```

`load(path: impl Into<PathBuf>)` accepts a `&str`, a `Path`, or a `PathBuf` — all
three convert. `write_atomic` takes `&Path` because it only reads the path.
`path.parent()` and `path.with_extension("tmp")` do the component surgery that
string concatenation would get subtly wrong.

## Writing a file without losing the old one

If we truncated `servers.json` and then ran out of disk, we would lose every
saved server. Instead we write a sibling and rename it over the destination:

```rust
let temporary = path.with_extension("tmp");
fs::write(&temporary, bytes)?;
set_owner_only(&temporary)?;
...
fs::rename(&temporary, path)?;
```

On POSIX, `rename` is atomic: readers see either the old file or the new one,
never a half-written one. The helper also `create_dir_all`s the parent, so the
first save creates `~/.config/webterm/` if needed. The `#[cfg(windows)]` block
removes the destination first, because Windows, unlike POSIX, refuses to rename
over an existing file. Conditional compilation (`#[cfg(...)]`) is how one
codebase carries platform differences without `if`s at runtime.

Profiles may hold tokens, so the file is chmodded to `0o600` — owner read/write
only. On non-Unix the function body is empty; there is nothing to restrict.

## Tests that clean up after themselves

The store talks to the file system, so the tests need a throwaway directory. To
keep the crate dependency-free, the test module writes its own guard instead of
pulling in a crate:

```rust
struct TempDir(PathBuf);

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
```

`Drop` is Rust's destructor. It runs when the value goes out of scope, **even
when the test panics**, so temp directories do not pile up. A tuple struct
(`struct TempDir(PathBuf)`) is a one-field wrapper; `.0` reaches the path. A
`static AtomicUsize` and the process id keep concurrent test runs from colliding
on the same directory name.

The most valuable test is the round-trip:

```rust
store.add(profile("dev")).expect("add");
let reloaded = ProfileStore::load(&path).expect("reload");
assert_eq!(reloaded.profiles(), &[profile("dev")]);
```

It exercises the exact path the app uses: mutate, persist, reopen, compare. The
others pin the *behaviours*, not the implementation: missing file is empty,
duplicate ids are rejected, removal reports what it removed, malformed JSON is
reported as `StoreError::Json`, and (on Unix) the file is `0o600`.

## Exercise

1. Add a method that replaces an existing profile by id and persists the list:

   ```rust
   pub fn update(&mut self, profile: Profile) -> Result<Option<Profile>, StoreError>
   ```

   It should return the previous profile when one was replaced and `Ok(None)`
   when the id was unknown — and, like `add` and `remove`, roll the in-memory
   list back if `save` fails.
2. Add a test that updates a profile, reloads from disk and asserts the new
   name stuck.
3. Add a test that `update` on an unknown id returns `Ok(None)` and writes no
   duplicate.
4. Run `cargo fmt`, `cargo clippy -p webterm-client` and
   `cargo test -p webterm-client` before moving on.

Stretch goal: explain to yourself why `profiles()` returning `&Vec<Profile>`
would compile but be worse API design than `&[Profile]`.

## Checkpoint

- `ProfileStore` loads, adds, removes and saves profiles.
- A malformed or unreadable file produces a typed `StoreError`, never a panic.
- Saving is atomic and the file is owner-only.
- `cargo test -p webterm-client` passes, including the new store tests.

Next: [Chapter 3 — The picker](03-the-picker.md) hands the store to the Tauri
shell and renders the list, so the app finally shows something you can click.

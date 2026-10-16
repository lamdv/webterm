//! The [`ProfileStore`]: the list of saved servers, backed by one JSON file.
//!
//! Chapter 2 of the tutorial.  This is where the pure [`Profile`] value from
//! Chapter 1 meets the file system, and where you meet three things Rust
//! newcomers have to internalise early:
//!
//! * **ownership and borrowing** — `&self`, `&mut self` and `&[Profile]`;
//! * **`Result` and `?`** — errors are values, propagated deliberately;
//! * **`Option`** — a lookup that may find nothing is not an error.

use std::error::Error;
use std::fmt;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use crate::Profile;

/// Everything that can go wrong while loading or saving the store.
///
/// An `enum` rather than a `String` so callers can tell the cases apart: a
/// malformed file should tell the user to fix it, while a permissions error
/// should tell them to check who owns the file.  `io::Error` and
/// `serde_json::Error` each keep their own type so nothing is lost.
#[derive(Debug)]
pub enum StoreError {
    /// The file could not be read or written.
    Io(io::Error),
    /// The file is not the JSON array of profiles we expect.
    Json(serde_json::Error),
    /// A profile with this id is already present.
    DuplicateId(String),
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StoreError::Io(error) => {
                write!(f, "could not read or write the profile store: {error}")
            }
            StoreError::Json(error) => write!(f, "the profile store is not valid JSON: {error}"),
            StoreError::DuplicateId(id) => {
                write!(f, "a profile with id {id:?} already exists")
            }
        }
    }
}

impl Error for StoreError {
    /// The underlying error, if any.  `None` here means the error is entirely
    /// ours (a duplicate id), and there is nothing more specific to unwrap.
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            StoreError::Io(error) => Some(error),
            StoreError::Json(error) => Some(error),
            StoreError::DuplicateId(_) => None,
        }
    }
}

/// `From` is what lets `?` convert an `io::Error` into a `StoreError` for us.
/// Implement it once here instead of spelling out `map_err(...)` at every call.
impl From<io::Error> for StoreError {
    fn from(error: io::Error) -> Self {
        StoreError::Io(error)
    }
}

impl From<serde_json::Error> for StoreError {
    fn from(error: serde_json::Error) -> Self {
        StoreError::Json(error)
    }
}

/// The saved list of [`Profile`]s, together with the file it came from.
///
/// The struct *owns* its path (`PathBuf`) and its profiles (`Vec<Profile>`), so
/// it can outlive the function that built it.  Mutation is explicit and every
/// mutation writes the file back, so an in-memory change and an on-disk change
/// can never drift apart unnoticed.
#[derive(Debug)]
pub struct ProfileStore {
    path: PathBuf,
    profiles: Vec<Profile>,
}

impl ProfileStore {
    /// Load the store at `path`.
    ///
    /// A *missing* file is the normal first-run case, not an error: it yields
    /// an empty store.  Any other read failure, and any malformed file, is
    /// reported.
    pub fn load(path: impl Into<PathBuf>) -> Result<Self, StoreError> {
        let path = path.into();
        let profiles = match fs::read_to_string(&path) {
            Ok(text) => serde_json::from_str(&text)?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => Vec::new(),
            Err(error) => return Err(error.into()),
        };
        Ok(Self { path, profiles })
    }

    /// The profiles, in the order they were saved.
    ///
    /// Returning `&[Profile]` — a *borrowed slice* — lets callers read every
    /// profile without copying any of them, and without being able to change
    /// the list behind the store's back.
    pub fn profiles(&self) -> &[Profile] {
        &self.profiles
    }

    /// Look up one profile by its id.
    ///
    /// An unknown id is `None`, not an error: "not found" is an ordinary
    /// answer, which is exactly what `Option` is for.
    pub fn get(&self, id: &str) -> Option<&Profile> {
        self.profiles.iter().find(|profile| profile.id == id)
    }

    /// Add a profile, then persist the list.
    ///
    /// Takes `&mut self`: saving mutates the stored list, and the compiler
    /// enforces that nobody else is reading it at the same time.
    pub fn add(&mut self, profile: Profile) -> Result<(), StoreError> {
        if self.get(&profile.id).is_some() {
            return Err(StoreError::DuplicateId(profile.id));
        }
        self.profiles.push(profile);
        // If the write fails, roll the in-memory list back so it keeps
        // describing what is actually on disk.
        if let Err(error) = self.save() {
            self.profiles.pop();
            return Err(error);
        }
        Ok(())
    }

    /// Remove the profile with `id`, returning it if it was there.
    ///
    /// `Ok(None)` means "there was nothing to remove" — again an ordinary
    /// answer rather than a failure.
    pub fn remove(&mut self, id: &str) -> Result<Option<Profile>, StoreError> {
        let Some(index) = self.profiles.iter().position(|profile| profile.id == id) else {
            return Ok(None);
        };
        let removed = self.profiles.remove(index);
        if let Err(error) = self.save() {
            self.profiles.insert(index, removed);
            return Err(error);
        }
        Ok(Some(removed))
    }

    /// Replace the profile with the same id, returning the previous value.
    ///
    /// `Ok(None)` means no profile with that id exists.  Like `add` and
    /// `remove`, a failed save restores the old profile so memory and disk
    /// stay in step.
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

    /// Write the current list back to disk.
    ///
    /// `&self`, not `&mut self`: saving reads the list, it does not change it.
    pub fn save(&self) -> Result<(), StoreError> {
        let json = serde_json::to_string_pretty(&self.profiles)?;
        write_atomic(&self.path, json.as_bytes())?;
        Ok(())
    }
}

/// Write `bytes` to `path` by writing a sibling file and renaming it over the
/// destination.
///
/// A partly written profile file would be unreadable and could lose every
/// saved server, so we never truncate the real file: `rename` replaces it in
/// one step on POSIX.  When the store holds tokens, the file is also chmodded
/// to owner-only.
fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), io::Error> {
    // A bare filename has no parent; treat it as the current directory.
    let parent = match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent,
        _ => Path::new("."),
    };
    fs::create_dir_all(parent)?;

    let temporary = path.with_extension("tmp");
    fs::write(&temporary, bytes)?;
    set_owner_only(&temporary)?;

    // Windows refuses to rename over an existing file, so clear the way first.
    #[cfg(windows)]
    if path.exists() {
        fs::remove_file(path)?;
    }

    if let Err(error) = fs::rename(&temporary, path) {
        let _ = fs::remove_file(&temporary);
        return Err(error);
    }
    Ok(())
}

/// Restrict a file to its owner.  Profiles may carry tokens, so no other user
/// on the machine should be able to read them.
#[cfg(unix)]
fn set_owner_only(path: &Path) -> Result<(), io::Error> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
}

/// On platforms without POSIX permissions there is nothing to do.
#[cfg(not(unix))]
fn set_owner_only(_path: &Path) -> Result<(), io::Error> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// A throwaway directory that deletes itself when the test ends.  Written
    /// by hand so the crate keeps its "no runtime dependencies" promise; the
    /// `Drop` impl runs even when a test panics.
    struct TempDir(PathBuf);

    impl TempDir {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let unique = format!(
                "webterm-store-test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed),
            );
            let path = std::env::temp_dir().join(unique);
            fs::create_dir_all(&path).expect("create temp dir");
            Self(path)
        }

        fn file(&self) -> PathBuf {
            self.0.join("servers.json")
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn profile(id: &str) -> Profile {
        Profile {
            id: id.to_string(),
            name: format!("{id} box"),
            url: format!("https://{id}.example.com"),
            auth: crate::AuthKind::None,
        }
    }

    #[test]
    fn loading_a_missing_file_yields_an_empty_store() {
        let dir = TempDir::new();
        let store = ProfileStore::load(dir.file()).expect("load");
        assert!(store.profiles().is_empty());
        assert!(store.get("nope").is_none());
    }

    #[test]
    fn add_persists_across_loads() {
        let dir = TempDir::new();
        let path = dir.file();

        let mut store = ProfileStore::load(&path).expect("load");
        store.add(profile("dev")).expect("add");

        let reloaded = ProfileStore::load(&path).expect("reload");
        assert_eq!(reloaded.profiles(), &[profile("dev")]);
        assert_eq!(
            reloaded.get("dev").map(|p| p.name.as_str()),
            Some("dev box")
        );
    }

    #[test]
    fn add_rejects_a_duplicate_id() {
        let dir = TempDir::new();
        let mut store = ProfileStore::load(dir.file()).expect("load");
        store.add(profile("dev")).expect("first add");

        let error = store.add(profile("dev")).expect_err("duplicate must fail");
        assert!(matches!(error, StoreError::DuplicateId(id) if id == "dev"));
        assert_eq!(store.profiles().len(), 1);
    }

    #[test]
    fn remove_persists_and_reports_what_it_removed() {
        let dir = TempDir::new();
        let path = dir.file();
        let mut store = ProfileStore::load(&path).expect("load");
        store.add(profile("dev")).expect("add");
        store.add(profile("prod")).expect("add");

        let removed = store.remove("dev").expect("remove");
        assert_eq!(removed.as_ref().map(|p| p.id.as_str()), Some("dev"));
        assert!(store.remove("dev").expect("remove missing").is_none());

        let reloaded = ProfileStore::load(&path).expect("reload");
        assert_eq!(reloaded.profiles(), &[profile("prod")]);
    }

    #[test]
    fn update_replaces_by_id_and_persists() {
        let dir = TempDir::new();
        let path = dir.file();
        let mut store = ProfileStore::load(&path).expect("load");
        store.add(profile("dev")).expect("add");

        let mut renamed = profile("dev");
        renamed.name = "Renamed".to_string();
        let previous = store.update(renamed).expect("update");
        assert_eq!(previous.map(|p| p.name), Some("dev box".to_string()));

        assert!(store
            .update(profile("missing"))
            .expect("update missing")
            .is_none());

        let reloaded = ProfileStore::load(&path).expect("reload");
        assert_eq!(
            reloaded.get("dev").map(|p| p.name.as_str()),
            Some("Renamed")
        );
        assert_eq!(reloaded.profiles().len(), 1);
    }

    #[test]
    fn a_malformed_file_is_reported_as_json() {
        let dir = TempDir::new();
        fs::write(dir.file(), b"this is not json").expect("seed bad file");

        let error = ProfileStore::load(dir.file()).expect_err("must fail");
        assert!(matches!(error, StoreError::Json(_)));
    }

    #[cfg(unix)]
    #[test]
    fn save_restricts_the_file_to_its_owner() {
        use std::os::unix::fs::PermissionsExt;

        let dir = TempDir::new();
        let path = dir.file();
        let store = ProfileStore::load(&path).expect("load");
        store.save().expect("save");

        let mode = fs::metadata(&path).expect("stat").permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }
}

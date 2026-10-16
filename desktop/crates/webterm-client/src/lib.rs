//! Portable core of the webterm client.
//!
//! Nothing in this crate knows that Tauri, a window, or a GUI exists.  The same
//! code therefore backs the desktop app, the future iOS/Android app, and plain
//! `cargo test`.  If a change here needs a window or a webview, it belongs in
//! `src-tauri` instead.
//!
//! The tutorial in `desktop/tutorial/` grows this crate one chapter at a time.
//! Chapter 1 introduces [`Profile`] and [`AuthKind`]; Chapter 2 adds
//! [`ProfileStore`], which keeps them in a JSON file.

pub mod profile;
pub mod store;

pub use profile::{AuthKind, Profile};
pub use store::{ProfileStore, StoreError};

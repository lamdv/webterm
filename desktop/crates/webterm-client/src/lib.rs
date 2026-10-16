//! Portable core of the webterm client.
//!
//! Nothing in this crate knows that Tauri, a window, or a GUI exists.  The same
//! code therefore backs the desktop app, the future iOS/Android app, and plain
//! `cargo test`.  If a change here needs a window or a webview, it belongs in
//! `src-tauri` instead.
//!
//! The tutorial in `desktop/tutorial/` grows this crate one chapter at a time.
//! Chapter 1 (this one) introduces [`Profile`] and [`AuthKind`].

pub mod profile;

pub use profile::{AuthKind, Profile};

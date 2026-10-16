//! The [`Profile`]: one server the client can connect to.

use serde::{Deserialize, Serialize};

/// A saved connection to a single webterm server.
///
/// A profile is *pure data*: a name, a URL and how to authenticate.  It holds
/// no socket, no window handle and no secret store — which is exactly what lets
/// the store, the picker and the window registry all pass it around freely.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Profile {
    /// Stable identifier, used for window labels and keychain keys.  It must
    /// survive renames, so it is *not* derived from `name`.
    pub id: String,
    /// Human-readable label shown in the picker.
    pub name: String,
    /// Base URL of the server, e.g. `https://webterm.example.com`.
    pub url: String,
    /// How to authenticate.  `#[serde(default)]` means files written by an
    /// older build (with no `auth` key) still load.
    #[serde(default)]
    pub auth: AuthKind,
}

/// How the client authenticates to a profile's server.
///
/// `#[serde(tag = "kind")]` makes this an *internally tagged* enum: it
/// serialises as `{"kind": "token", "token": "..."}` rather than as a bare
/// string or an externally tagged `{"token": "..."}` object.  The tag keeps the
/// JSON readable and lets new variants be added without breaking old files.
///
/// Every variant carries *named* fields.  Serde cannot internally tag a newtype
/// variant such as `Token(String)`, because a bare string has nowhere to put
/// the `kind` key — a rule worth remembering the first time it bites you.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AuthKind {
    /// The server is open, or the network already protects it (local bind,
    /// Tailscale, a VPN).
    #[default]
    None,
    /// A shared secret validated by webterm's `--token` flag.
    Token {
        /// The secret itself.  Chapter 6 moves this into the OS keychain and
        /// leaves only a reference here; for now it lives in the profile file.
        token: String,
    },
    /// An identity-aware proxy (oauth2-proxy, Authelia, ...).  The client opens
    /// a login window and captures the session cookie the proxy sets.
    Browser,
}

impl AuthKind {
    /// A short description for the picker to show next to a server.
    ///
    /// Returning `&'static str` is the idiomatic way to hand back one of a
    /// fixed set of strings: the text lives in the binary, so no allocation and
    /// no lifetime tied to `self`.
    pub fn label(&self) -> &'static str {
        match self {
            AuthKind::None => "open",
            AuthKind::Token { .. } => "token",
            AuthKind::Browser => "browser login",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn sample() -> Profile {
        Profile {
            id: "dev-box".to_string(),
            name: "Dev box".to_string(),
            url: "https://webterm.example.com".to_string(),
            auth: AuthKind::Browser,
        }
    }

    #[test]
    fn profile_round_trips_through_json() {
        let profile = sample();
        let text = serde_json::to_string(&profile).expect("serialise");
        let back: Profile = serde_json::from_str(&text).expect("deserialise");
        assert_eq!(profile, back);
    }

    #[test]
    fn auth_is_internally_tagged() {
        let value = serde_json::to_value(AuthKind::Token {
            token: "s3cret".into(),
        })
        .expect("serialise");
        assert_eq!(value, json!({ "kind": "token", "token": "s3cret" }));
    }

    #[test]
    fn missing_auth_field_defaults_to_none() {
        let text = r#"{"id":"a","name":"A","url":"http://127.0.0.1:8765"}"#;
        let profile: Profile = serde_json::from_str(text).expect("deserialise");
        assert_eq!(profile.auth, AuthKind::None);
    }

    #[test]
    fn labels_describe_the_auth_kind() {
        assert_eq!(AuthKind::None.label(), "open");
        assert_eq!(AuthKind::Token { token: "x".into() }.label(), "token");
        assert_eq!(AuthKind::Browser.label(), "browser login");
    }
}

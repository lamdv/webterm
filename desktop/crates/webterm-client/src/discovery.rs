//! Probing a server: the discovery document and the URL that serves it.
//!
//! Chapter 5.  The server serves `/.well-known/webterm.json` without
//! authentication, so a client can confirm it is talking to webterm, learn
//! which credentials it accepts, and refuse a protocol it does not understand —
//! all before it holds a secret.
//!
//! The network call lives in the shell; this module owns the *shape* of the
//! document and the URL rule, which is the part worth testing in isolation.

use serde::{Deserialize, Serialize};
use url::Url;

use crate::Profile;

/// The wire-protocol version this client speaks.
pub const PROTOCOL_VERSION: u32 = 1;

/// Path of the unauthenticated discovery document.
pub const DISCOVERY_PATH: &str = "/.well-known/webterm.json";

/// The JSON served at [`DISCOVERY_PATH`].
///
/// Every field is `#[serde(default)]` so a newer server can add fields and an
/// older one can omit them without breaking the parse.  Unknown fields are
/// ignored by default.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServerInfo {
    /// Service name; `"webterm"` for a genuine server.
    #[serde(default)]
    pub service: String,
    /// The server's own version string.
    #[serde(default)]
    pub version: String,
    /// Wire-protocol version.  A server predating the field is assumed to be
    /// protocol 1, the only version that has existed.
    #[serde(default = "default_protocol")]
    pub protocol: u32,
    /// Whether the server requires a credential.
    #[serde(default)]
    pub auth_required: bool,
    /// Accepted credential types, e.g. `["basic", "bearer", "cookie"]`.
    #[serde(default)]
    pub auth_methods: Vec<String>,
}

fn default_protocol() -> u32 {
    PROTOCOL_VERSION
}

impl ServerInfo {
    /// Whether this client can talk to the server.
    pub fn is_compatible(&self) -> bool {
        self.protocol == PROTOCOL_VERSION
    }
}

/// Build the discovery URL for `profile`.
///
/// `Url::join` resolves the path relative to the profile's base, so a server
/// served under a mount point (`https://host/webterm/`) is probed under that
/// mount rather than at the proxy's root.
pub fn discovery_url(profile: &Profile) -> Result<Url, url::ParseError> {
    Url::parse(&profile.url)?.join(DISCOVERY_PATH.trim_start_matches('/'))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile(url: &str) -> Profile {
        Profile {
            id: "dev".to_string(),
            name: "Dev box".to_string(),
            url: url.to_string(),
            auth: crate::AuthKind::None,
        }
    }

    #[test]
    fn parses_the_servers_document() {
        let json = r#"{
            "service": "webterm",
            "version": "0.4.1",
            "protocol": 1,
            "auth_required": true,
            "auth_methods": ["basic", "bearer", "cookie"],
            "websocket_path": "/ws"
        }"#;
        let info: ServerInfo = serde_json::from_str(json).expect("parse");
        assert_eq!(info.service, "webterm");
        assert_eq!(info.version, "0.4.1");
        assert_eq!(info.protocol, 1);
        assert!(info.auth_required);
        assert_eq!(info.auth_methods, ["basic", "bearer", "cookie"]);
        assert!(info.is_compatible());
    }

    #[test]
    fn a_missing_protocol_defaults_to_compatible() {
        let info: ServerInfo =
            serde_json::from_str(r#"{"service":"webterm","version":"old"}"#).expect("parse");
        assert_eq!(info.protocol, PROTOCOL_VERSION);
        assert!(info.is_compatible());
    }

    #[test]
    fn a_newer_protocol_is_incompatible() {
        let info: ServerInfo =
            serde_json::from_str(r#"{"protocol": 999, "version": "future"}"#).expect("parse");
        assert!(!info.is_compatible());
    }

    #[test]
    fn discovery_url_joins_the_base() {
        assert_eq!(
            discovery_url(&profile("https://webterm.example.com"))
                .expect("url")
                .as_str(),
            "https://webterm.example.com/.well-known/webterm.json"
        );
        assert_eq!(
            discovery_url(&profile("https://webterm.example.com/webterm/"))
                .expect("url")
                .as_str(),
            "https://webterm.example.com/webterm/.well-known/webterm.json"
        );
    }

    #[test]
    fn an_invalid_base_url_is_an_error() {
        assert!(discovery_url(&profile("not a url")).is_err());
    }
}

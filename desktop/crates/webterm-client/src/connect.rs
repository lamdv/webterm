//! Turning a [`Profile`] into the URL a client window should open.
//!
//! Chapter 4 of the tutorial.  So far `AuthKind` has been data with no effect;
//! this is where it starts to matter.

use url::Url;

use crate::{AuthKind, Profile};

/// Build the URL to open for `profile`.
///
/// A **token** profile goes through the server's bootstrap flow: the secret
/// rides in the `bootstrap` query parameter once, the server trades it for an
/// `HttpOnly`, `SameSite=Strict` cookie and redirects to `/`.  The webview then
/// sends that cookie on the WebSocket upgrade, so the terminal authenticates
/// without the token appearing in the WebSocket URL.
///
/// `AuthKind::None` and `AuthKind::Browser` open the profile's URL unchanged;
/// the `Browser` case is what lets an identity-aware proxy run its own login in
/// the window.
///
/// The token does appear in the window's first URL.  The server marks that
/// response `no-store` and redirects immediately, the app draws no URL bar, and
/// the token is not sent again, so it is not exposed to later requests.
pub fn connect_url(profile: &Profile) -> Result<Url, url::ParseError> {
    let mut url = Url::parse(&profile.url)?;
    if let AuthKind::Token { token } = &profile.auth {
        url.query_pairs_mut().append_pair("bootstrap", token);
    }
    Ok(url)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{AuthKind, Profile};

    fn profile(auth: AuthKind) -> Profile {
        Profile {
            id: "dev".to_string(),
            name: "Dev box".to_string(),
            url: "https://webterm.example.com".to_string(),
            auth,
        }
    }

    #[test]
    fn open_and_browser_profiles_open_unchanged() {
        assert_eq!(
            connect_url(&profile(AuthKind::None))
                .expect("open")
                .as_str(),
            "https://webterm.example.com/"
        );
        assert_eq!(
            connect_url(&profile(AuthKind::Browser))
                .expect("browser")
                .as_str(),
            "https://webterm.example.com/"
        );
    }

    #[test]
    fn token_profiles_carry_a_bootstrap_parameter() {
        let auth = AuthKind::Token {
            token: "s3cret".to_string(),
        };
        let url = connect_url(&profile(auth)).expect("token");
        assert_eq!(
            url.as_str(),
            "https://webterm.example.com/?bootstrap=s3cret"
        );
    }

    #[test]
    fn tokens_are_percent_encoded() {
        let auth = AuthKind::Token {
            token: "a b&c".to_string(),
        };
        let url = connect_url(&profile(auth)).expect("token");
        assert_eq!(
            url.as_str(),
            "https://webterm.example.com/?bootstrap=a+b%26c"
        );
    }

    #[test]
    fn an_existing_query_is_preserved() {
        let mut profile = profile(AuthKind::Token {
            token: "t".to_string(),
        });
        profile.url = "https://webterm.example.com/webterm/?theme=dark".to_string();

        let url = connect_url(&profile).expect("token");
        assert_eq!(url.path(), "/webterm/");
        assert_eq!(url.query(), Some("theme=dark&bootstrap=t"));
    }

    #[test]
    fn an_invalid_url_is_an_error_not_a_panic() {
        let mut profile = profile(AuthKind::None);
        profile.url = "not a url".to_string();
        assert!(connect_url(&profile).is_err());
    }
}

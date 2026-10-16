# Remote access: reverse proxies, OAuth2, and Tailscale

webterm intentionally ships no authentication beyond an optional shared token
([`--token`](../README.md#configuration)). It is a tool that hands out shells, so
the safe pattern is to keep it bound to `127.0.0.1` and put an identity-aware,
TLS-terminating layer in front of it. This guide covers the two common ways to
do that:

- [Tailscale](#tailscale-private-tailnet) — private tailnet, least setup.
- [OAuth2 reverse proxy](#oauth2-reverse-proxy) — browser login via an identity
  provider (Google, GitHub, Keycloak, Okta, ...).

Both keep webterm's lack of built-in auth out of the equation and avoid adding
any Python dependency.

## Why a proxy at all

`--token` is HTTP Basic auth. Over plain HTTP it is base64 on the wire, so
anyone who can observe the connection can read it and take a shell. Two things
fix that:

1. **Encryption + server authentication**, so the token and terminal stream
   cannot be read or tampered with. TLS does this.
2. **Per-user authentication**, so a shared secret is not the only gate. OAuth2
   or a tailnet identity does this.

A proxy can provide both, and it can gate the WebSocket upgrade (`/ws`) with the
same rule as ordinary HTTP, so you do not need to secure the two paths
separately. The browser client picks `wss://` automatically whenever the page is
served over `https://`, so no frontend configuration is required.

> **Always keep the localhost bind.** `--host 0.0.0.0` exposes webterm to the
> LAN (and any other interface) *in addition* to the proxy, bypassing all of the
> protections below. Bind `127.0.0.1` (the default) and let the proxy be the
> only route in.

> **Consider keeping `--token` too.** It is a cheap second layer. It also
> protects the localhost port from other users on the same machine, who can
> reach `127.0.0.1:8765` directly without going through the proxy.

## Quick reference

| Setup | Encryption | Identity | WebSocket forwarded | New Python deps |
| --- | --- | --- | --- | --- |
| Tailscale `serve` | WireGuard + TLS in tailscaled | tailnet device / ACLs | yes | none |
| oauth2-proxy as reverse proxy | upstream TLS (proxy) | OIDC provider | yes | none |
| nginx/Caddy `forward_auth` + oauth2-proxy | proxy TLS | OIDC provider | yes | none |

## Tailscale (private tailnet)

[Tailscale](https://tailscale.com) gives every device a WireGuard address.
`tailscale serve` proxies a local port to your tailnet over HTTPS, so only your
own devices can connect and there is no certificate to manage.

### 1. Install and enable HTTPS

Install Tailscale and run `tailscale up` on the host and on each client. Enable
**MagicDNS** and **HTTPS Certificates** for the tailnet in the admin console;
`serve` needs them to mint a trusted certificate.

### 2. Start webterm on localhost

```bash
uv run webterm --token "$WEBTERM_TOKEN"
# listens on http://127.0.0.1:8765 — do not pass --host 0.0.0.0
```

### 3. Proxy it to the tailnet

```bash
tailscale serve --bg 8765      # tailnet HTTPS -> http://127.0.0.1:8765
tailscale serve status         # verify it shows an https:// URL
```

Open the printed `https://<host>.<tailnet>.ts.net/` URL from any device in your
tailnet. TLS terminates in tailscaled and is proxied to localhost; the token, if
set, travels encrypted.

### 4. Restrict access with ACLs

By default every device in a tailnet can reach every other device. If your
tailnet has devices you do not fully trust, add an ACL so only specific
users/devices can reach the port. The port for `serve` defaults to `443` on the
host's Tailscale IP:

```jsonc
{
  "tagOwners": {
    "tag:webterm": ["autogroup:admin"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["user@example.com"],
      "dst": ["hostname:443"]
    }
  ]
}
```

Tag the host (`tailscale up --advertise-tags=tag:webterm`) and tighten `src`.
See the Tailscale ACL docs for the full syntax.

### 5. Automate it

Run webterm under your process manager and `tailscale serve --bg` once; serve
config persists across restarts. `tailscale serve reset` removes it.

### Caveats

- **Use `serve`, not `funnel`.** `funnel` publishes the service to the *public
  internet*; never do that for webterm without TLS and a real auth layer.
- **Local users bypass the tailnet.** Anything on the host can reach
  `127.0.0.1:8765` directly. Keep `--token` on shared machines.
- Confirm the URL is `https://`. If HTTPS certs are disabled, `serve` may fall
  back to plain HTTP on the tailnet, which makes the token plaintext again.

## OAuth2 reverse proxy

This pattern puts an OAuth2/OIDC gatekeeper in front of webterm and lets the
provider handle login. The examples use
[oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) because it can act
as either the reverse proxy itself or an auth backend for nginx/Caddy.

### 1. Register an OAuth client

Create an OIDC application at your provider (Google, GitHub, Keycloak, Okta,
Authentik, ...) and set the redirect URI to:

```
https://webterm.example.com/oauth2/callback
```

Note the issuer URL, client ID, and client secret.

### 2. Start webterm on localhost

```bash
uv run webterm
```

No `--token` here: with oauth2-proxy as the reverse proxy it is the gate, and
webterm is reachable only through it. (oauth2-proxy's `--upstream` mode cannot
add a static `Authorization` header, so combining the two would just earn a
`401` and an HTTP Basic prompt *after* the OAuth login. If you want the token as
a second layer, use the nginx `auth_request` shape in 4a, where nginx can
`proxy_set_header Authorization`.)

### 3. Run oauth2-proxy

Generate a cookie secret (`openssl rand -base64 32`) and run:

```bash
oauth2-proxy \
  --provider=oidc \
  --oidc-issuer-url=https://accounts.example.com \
  --client-id=webterm \
  --client-secret=... \
  --cookie-secret=... \
  --cookie-secure=true \
  --email-domain=example.com \
  --http-address=127.0.0.1:4180 \
  --redirect-url=https://webterm.example.com/oauth2/callback \
  --upstream=http://127.0.0.1:8765
```

With `--upstream` set, oauth2-proxy is the reverse proxy: it authenticates the
browser, then forwards both HTTP and the WebSocket upgrade (`/ws`) to webterm,
including the session cookie. Put TLS in front of it (your load balancer, Caddy,
or oauth2-proxy's own `--tls-cert-file`/`--tls-key-file`).

Restrict who may log in with `--email-domain`, `--allowed-group`, or
`--allowed-role` as appropriate.

### 4a. Alternative: nginx `auth_request`

If you already run nginx for TLS, run oauth2-proxy on 4180 as above but **without**
`--upstream`, then use it for authorization only:

```nginx
server {
    listen 443 ssl;
    server_name webterm.example.com;
    # ssl_certificate ...; ssl_certificate_key ...;

    location /oauth2/ {
        proxy_pass http://127.0.0.1:4180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Scheme $scheme;
    }

    location = /oauth2/auth {
        internal;
        proxy_pass http://127.0.0.1:4180;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-URI $request_uri;
    }

    location / {
        auth_request /oauth2/auth;
        error_page 401 = /oauth2/sign_in;

        auth_request_set $user  $upstream_http_x_auth_request_user;
        auth_request_set $email $upstream_http_x_auth_request_email;
        proxy_set_header X-User  $user;
        proxy_set_header X-Email $email;

        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

The `Upgrade`/`Connection` headers in `location /` are what let the WebSocket
through; without them the terminal connects and then drops.

### 4b. Alternative: Caddy `forward_auth`

Caddy handles TLS and WebSocket upgrades automatically:

```caddyfile
webterm.example.com {
    forward_auth 127.0.0.1:4180 {
        uri /oauth2/auth
        copy_headers X-Auth-Request-User X-Auth-Request-Email
        @unauthorized status 401
        handle_response @unauthorized {
            redir * /oauth2/sign_in?rd={scheme}://{host}{uri}
        }
    }

    reverse_proxy 127.0.0.1:8765
}
```

The `/oauth2/*` routes must also be proxied to oauth2-proxy (Caddy's
`forward_auth` sends the sign-in redirect, but the callback and sign-in pages
need a matching reverse-proxy route); add one if your setup does not already
have it.

### 5. Verify

- `curl -I https://webterm.example.com/` returns a redirect to the provider.
- In the browser, complete the login and confirm the terminal connects (the
  status dot turns green and `wss://` appears in devtools).
- Confirm a logged-out browser is refused, and that the WebSocket, not just the
  HTML page, requires login.

## Generic proxy notes

Whatever proxy you use, the requirements are the same:

- Terminate TLS and forward to `http://127.0.0.1:8765`.
- Enable HTTP/1.1 and forward `Upgrade`/`Connection` headers so `/ws` upgrades.
- Apply authentication to **all** paths, including `/ws` and `/config.js`, not
  just `/`. The one intentional exception is `/.well-known/webterm.json`, which
  webterm serves unauthenticated so a client can identify it; gate it at the
  proxy if even that should be private.
- Leave webterm bound to `127.0.0.1`.
- Optionally retain `--token` as defense in depth. This applies to the
  reverse-proxy/`auth_request` shape (4a), where the proxy can inject the
  credential upstream: webterm accepts `Authorization: Bearer <token>`, so for
  nginx that is `proxy_set_header Authorization "Bearer $WEBTERM_TOKEN";`.
  oauth2-proxy's own `--upstream` mode *cannot* set a static header, so do not
  pair `--token` with it. Without the injected header webterm answers `401` and
  the browser prompts for HTTP Basic *after* the OAuth login.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Page loads, terminal never connects | WebSocket upgrade headers not forwarded by the proxy. |
| Login loop | `--redirect-url`/callback URI does not match the provider registration, or the auth cookie is not `Secure` under HTTPS. |
| `wss://` connection rejected but HTTP works | Auth is applied to `/ws` inconsistently, or the cookie is not forwarded on the upgrade. |
| Works until the machine reboots | `tailscale serve` or the proxy is not configured to start automatically. |
| Tailnet URL is `http://` | HTTPS certificates are not enabled for the tailnet. |

## See also

- [README: Exposing it safely](../README.md#exposing-it-safely-reverse-proxy--tailscale)
- [Architecture: security model](architecture.md#security-model)
- [Backend: `check_auth` and `serve`](backend.md)

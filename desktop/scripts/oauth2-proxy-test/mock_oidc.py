#!/usr/bin/env python3
"""A tiny OIDC provider for testing browser-login auth against oauth2-proxy.

Stdlib + the `openssl` CLI only: it signs RS256 ID tokens with a local key so
oauth2-proxy (given --oidc-public-key-file) can verify them without a JWKS.

Not a real identity provider.  It logs in one hard-coded user.
"""

from __future__ import annotations

import base64
import http.server
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5556
KEY = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("key.pem")
ISSUER = f"http://127.0.0.1:{PORT}"
CLIENT_ID = "webterm-test"
CLIENT_SECRET = "test-secret"
EMAIL = "tester@example.com"

# code -> (nonce, redirect_uri)
CODES: dict[str, str] = {}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_id_token(redirect_uri: str) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
    payload = {
        "iss": ISSUER,
        "sub": "tester",
        "aud": CLIENT_ID,
        "exp": now + 600,
        "iat": now,
        "email": EMAIL,
        "email_verified": True,
    }
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}"
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(KEY)],
        input=signing_input.encode(),
        capture_output=True,
        check=True,
    ).stdout
    return f"{signing_input}.{b64url(signature)}"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, status: int, body: bytes, ctype: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict) -> None:
        self._send(200, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/.well-known/openid-configuration":
            self._json(
                {
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/auth",
                    "token_endpoint": f"{ISSUER}/token",
                    "userinfo_endpoint": f"{ISSUER}/userinfo",
                    "jwks_uri": f"{ISSUER}/keys",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "scopes_supported": ["openid", "email", "profile"],
                }
            )
            return

        if parsed.path == "/keys":
            self._json({"keys": []})  # verification uses --oidc-public-key-file
            return

        if parsed.path == "/auth":
            # The page a real provider would show.  Automation skips straight
            # to /approve below, but a browser (or the desktop webview) sees
            # this and the button.
            approve = urllib.parse.urlencode(
                {
                    "redirect_uri": query.get("redirect_uri", [""])[0],
                    "state": query.get("state", [""])[0],
                }
            )
            body = (
                "<!doctype html><title>Mock sign in</title>"
                "<h1>Mock identity provider</h1>"
                f"<p>Sign in as {EMAIL}</p>"
                f'<p><a id="approve" href="/approve?{approve}">Sign in</a></p>'
            ).encode()
            self._send(200, body, "text/html")
            return

        if parsed.path == "/approve":
            redirect_uri = query.get("redirect_uri", [""])[0]
            state = query.get("state", [""])[0]
            code = base64.urlsafe_b64encode(str(time.time_ns()).encode()).decode()
            CODES[code] = redirect_uri
            location = f"{redirect_uri}?code={code}&state={state}"
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return

        if parsed.path == "/userinfo":
            self._json({"sub": "tester", "email": EMAIL, "email_verified": True})
            return

        self._send(404, b"not found")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if urllib.parse.urlparse(self.path).path != "/token":
            self._send(404, b"not found")
            return

        if (
            form.get("client_id", [""])[0] != CLIENT_ID
            or form.get("client_secret", [""])[0] != CLIENT_SECRET
        ):
            self._send(401, b"bad client")
            return

        code = form.get("code", [""])[0]
        redirect_uri = CODES.pop(code, "")
        if not redirect_uri:
            self._send(400, b"bad code")
            return

        self._json(
            {
                "access_token": "mock-access-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": make_id_token(redirect_uri),
            }
        )


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mock oidc on {ISSUER}", flush=True)
    server.serve_forever()

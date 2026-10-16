#!/usr/bin/env bash
# End-to-end check of the desktop app's "browser login" auth path against a real
# oauth2-proxy placed in front of a local webterm.
#
# It cannot drive the Tauri webview, so it verifies the half that can be
# automated: an unauthenticated client is redirected to the IdP, the sign-in
# page serves, the callback sets a session cookie, and that cookie is required
# for both the UI and the `/ws` upgrade.  A human still has to watch the webview
# do the same dance — see the Roadmap in desktop/README.md.
#
# Requirements: oauth2-proxy on PATH (or set $O2P), plus openssl, python3, curl,
# and uv.  oauth2-proxy releases:
#   https://github.com/oauth2-proxy/oauth2-proxy/releases
#
# Usage:
#   desktop/scripts/oauth2-proxy-test/run.sh
#
# Env overrides: O2P, WEBTERM_PORT, IDP_PORT, PROXY_PORT.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../../.." && pwd)

O2P=${O2P:-oauth2-proxy}
WEBTERM_PORT=${WEBTERM_PORT:-8767}
IDP_PORT=${IDP_PORT:-5556}
PROXY_PORT=${PROXY_PORT:-4180}
PROXY="http://127.0.0.1:$PROXY_PORT"
IDP="http://127.0.0.1:$IDP_PORT"
CLIENT_ID=webterm-test
CLIENT_SECRET=test-secret
COOKIE_SECRET=0123456789abcdef0123456789abcdef

command -v "$O2P" >/dev/null 2>&1 || {
  echo "error: oauth2-proxy not found; install it or set O2P=/path/to/oauth2-proxy" >&2
  exit 1
}
command -v openssl >/dev/null 2>&1 || { echo "error: openssl is required" >&2; exit 1; }

WORK=$(mktemp -d)
JAR="$WORK/jar.txt"

cleanup() {
  kill "$IDP_PID" "$PROXY_PID" 2>/dev/null || true
  pkill -f "webterm --port $WEBTERM_PORT" 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  echo "--- webterm ---" >&2; tail -n 20 "$WORK/webterm.log" >&2 || true
  echo "--- oauth2-proxy ---" >&2; tail -n 20 "$WORK/o2p.log" >&2 || true
  exit 1
}

pass() { echo "ok   - $*"; }

openssl genrsa -out "$WORK/key.pem" 2048 2>/dev/null
openssl rsa -in "$WORK/key.pem" -pubout -out "$WORK/pub.pem" 2>/dev/null

# A fresh webterm, with its control socket disabled so it does not collide with
# a dev instance the developer may already be running.
(cd "$REPO_ROOT" && uv run webterm --port "$WEBTERM_PORT" --no-control) \
  >"$WORK/webterm.log" 2>&1 &
python3 "$SCRIPT_DIR/mock_oidc.py" "$IDP_PORT" "$WORK/key.pem" >"$WORK/idp.log" 2>&1 &
IDP_PID=$!
"$O2P" --provider=oidc --client-id="$CLIENT_ID" --client-secret="$CLIENT_SECRET" \
  --cookie-secret="$COOKIE_SECRET" --cookie-secure=false --email-domain='*' \
  --upstream="http://127.0.0.1:$WEBTERM_PORT" --http-address="127.0.0.1:$PROXY_PORT" \
  --redirect-url="$PROXY/oauth2/callback" --oidc-issuer-url="$IDP" \
  --skip-oidc-discovery --login-url="$IDP/auth" --redeem-url="$IDP/token" \
  --oidc-public-key-file="$WORK/pub.pem" \
  --insecure-oidc-skip-issuer-verification --oidc-enabled-signing-alg=RS256 \
  --skip-provider-button --pass-user-headers >"$WORK/o2p.log" 2>&1 &
PROXY_PID=$!

# Wait for the three ports to answer.
for _ in $(seq 1 30); do
  curl -sS --max-time 1 -o /dev/null "$IDP/.well-known/openid-configuration" 2>/dev/null && break
  sleep 0.5
done
sleep 1

echo "== unauthenticated request is redirected to the IdP =="
auth=$(curl -sS --max-time 10 -o /dev/null -w '%{redirect_url}' -c "$JAR" -b "$JAR" "$PROXY/")
[[ -n "$auth" ]] || fail "no redirect; the proxy answered the unauthenticated request"
pass "redirected to $auth"

echo "== the sign-in page renders =="
curl -sS --max-time 10 -c "$JAR" -b "$JAR" -o "$WORK/login.html" "$auth"
grep -q 'id="approve"' "$WORK/login.html" || fail "sign-in page did not render a Sign in control"
pass "sign-in page served"

echo "== approving sets a session cookie =="
approve=$(python3 - "$auth" <<PY
import sys, urllib.parse
q = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[1]).query)
print("$IDP/approve?" + urllib.parse.urlencode(
    {"redirect_uri": q["redirect_uri"][0], "state": q["state"][0]}))
PY
)
callback=$(curl -sS --max-time 10 -o /dev/null -w '%{redirect_url}' -c "$JAR" -b "$JAR" "$approve")
[[ -n "$callback" ]] || fail "the IdP did not redirect back to the proxy"
curl -sS --max-time 10 -o /dev/null -c "$JAR" -b "$JAR" "$callback"
grep -q _oauth2_proxy "$JAR" || fail "the proxy set no session cookie"
pass "session cookie set"

echo "== the authenticated UI reaches webterm =="
title=$(curl -sS --max-time 10 -b "$JAR" "$PROXY/" | grep -m1 -oE '<title>[^<]*' || true)
[[ "$title" == *webterm* ]] || fail "authenticated request did not reach webterm (title: $title)"
pass "GET / served the webterm client"

echo "== /ws is gated by the proxy cookie =="
ws_status() {
  curl -sS --max-time 3 -i "$@" -o "$WORK/ws.out" \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    -H 'Sec-WebSocket-Version: 13' "$PROXY/ws" 2>/dev/null || true
  head -n 1 "$WORK/ws.out" || true
}
without=$(ws_status)
with=$(ws_status -b "$JAR")
[[ "$without" == *" 302 "* ]] || fail "unauthenticated /ws expected 302, got: $without"
[[ "$with" == *" 101 "* ]] || fail "authenticated /ws expected 101, got: $with"
pass "no cookie -> 302, cookie -> 101"

echo
echo "All browser-login checks passed."

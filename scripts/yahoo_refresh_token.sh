#!/usr/bin/env bash
#
# Mint a Yahoo Fantasy refresh token and load the three credentials the
# `yahoo-draft-assistant` flow needs into the `yahoo-sports` KV store.
#
# Yahoo requires PKCE, so the `code_verifier` generated in step 1 has to be
# carried into the token exchange in step 3 - which is the whole reason this is
# a script rather than two curl commands.
#
# The client secret is read from a hidden prompt and never appears in argv,
# shell history, or this file. Run it again whenever the refresh token needs
# regenerating (Yahoo refresh tokens are long-lived but not eternal).
#
# Usage: scripts/yahoo_refresh_token.sh

set -euo pipefail

NAMESPACE="${YAHOO_KESTRA_NAMESPACE:-yahoo-sports}"
REDIRECT_URI="${YAHOO_REDIRECT_URI:-oob}"
# Not defaulted in-repo: this is a public repository, and the consumer key
# decodes to the Yahoo App ID. Export YAHOO_CLIENT_ID to skip the prompt.
CLIENT_ID="${YAHOO_CLIENT_ID:-}"
TOKEN_URL="https://api.login.yahoo.com/oauth2/get_token"
AUTH_URL="https://api.login.yahoo.com/oauth2/request_auth"

command -v kestractl >/dev/null || { echo "kestractl not on PATH" >&2; exit 1; }
command -v openssl   >/dev/null || { echo "openssl not on PATH" >&2; exit 1; }

# Read a KV value, or print nothing if the key is absent.
kv_read() {
  kestractl kv get "$NAMESPACE" "$1" -o json 2>/dev/null \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("value","") or "")
except Exception: pass' 2>/dev/null
}

# Credentials come from the environment, else the KV store, else a prompt. The
# KV lookup means a re-run after the app credentials are already loaded needs no
# retyping - and nothing sensitive is defaulted into this public repo.
[ -n "$CLIENT_ID" ] || CLIENT_ID=$(kv_read YAHOO_CLIENT_ID)
if [ -n "$CLIENT_ID" ]; then
  echo "Using YAHOO_CLIENT_ID from ${NAMESPACE} KV store."
else
  echo "Client ID (Consumer Key) from https://developer.yahoo.com/apps/ :"
  printf '> '
  read -r CLIENT_ID
  [ -n "$CLIENT_ID" ] || { echo "No client id entered." >&2; exit 1; }
fi

CLIENT_SECRET="${YAHOO_CLIENT_SECRET:-}"
[ -n "$CLIENT_SECRET" ] || CLIENT_SECRET=$(kv_read YAHOO_CLIENT_SECRET)
if [ -n "$CLIENT_SECRET" ]; then
  echo "Using YAHOO_CLIENT_SECRET from ${NAMESPACE} KV store."
else
  printf 'Client secret (hidden): '
  read -rs CLIENT_SECRET
  printf '\n'
  [ -n "$CLIENT_SECRET" ] || { echo "No client secret entered." >&2; exit 1; }
fi

# 1. PKCE pair. The verifier is the secret half and stays in this process.
VERIFIER=$(openssl rand -base64 60 | tr -d '\n=+/' | cut -c1-64)
CHALLENGE=$(printf %s "$VERIFIER" \
  | openssl dgst -binary -sha256 \
  | openssl base64 | tr '+/' '-_' | tr -d '=\n')

# 2. Send the user to Yahoo to authorize, and collect the code they get back.
cat <<EOF

Open this URL, sign in, and approve access:

${AUTH_URL}?client_id=${CLIENT_ID}&redirect_uri=${REDIRECT_URI}&response_type=code&language=en-us&code_challenge=${CHALLENGE}&code_challenge_method=S256

EOF
if [ "$REDIRECT_URI" = "oob" ]; then
  echo "Yahoo will show a short code on screen once you approve."
else
  echo "Yahoo will redirect to ${REDIRECT_URI}?code=... - copy the 'code' value."
fi
printf '\nAuthorization code: '
read -r AUTH_CODE
[ -n "$AUTH_CODE" ] || { echo "No code entered." >&2; exit 1; }
printf '\n'

# 3. Exchange the code for tokens. Credentials go in the Basic auth header,
#    the verifier proves this is the same client that started the flow.
RESPONSE=$(curl -sS -X POST "$TOKEN_URL" \
  -u "${CLIENT_ID}:${CLIENT_SECRET}" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=authorization_code' \
  --data-urlencode "code=${AUTH_CODE}" \
  --data-urlencode "redirect_uri=${REDIRECT_URI}" \
  --data-urlencode "code_verifier=${VERIFIER}")

REFRESH_TOKEN=$(printf %s "$RESPONSE" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit("Yahoo did not return JSON")
if "refresh_token" not in d:
    sys.exit("Yahoo returned no refresh_token: %s" % json.dumps(d))
print(d["refresh_token"])
')

# 4. Load all three into the KV store the flow reads with kv().
kestractl kv set "$NAMESPACE" STRING YAHOO_CLIENT_ID     "$CLIENT_ID"     >/dev/null
kestractl kv set "$NAMESPACE" STRING YAHOO_CLIENT_SECRET "$CLIENT_SECRET" >/dev/null
kestractl kv set "$NAMESPACE" STRING YAHOO_REFRESH_TOKEN "$REFRESH_TOKEN" >/dev/null

echo "Loaded YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET and YAHOO_REFRESH_TOKEN into '${NAMESPACE}'."
kestractl kv list "$NAMESPACE"

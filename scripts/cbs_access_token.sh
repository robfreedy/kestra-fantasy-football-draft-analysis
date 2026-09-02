#!/usr/bin/env bash
#
# Mint a CBS Sports Fantasy league access token and load the credentials the
# `cbs-draft-assistant` flow needs into the `cbs-sports` KV store.
#
# CBS's token flow is two hops - a request token bound to your CBS account,
# then the access token it is exchanged for - which is the whole reason this is
# a script rather than one curl command.
#
# The client secret is read from a hidden prompt, so it is not echoed and does
# not land in shell history or this file. It does pass through curl's argv and
# the request URL, because that is the only form CBS's token endpoints accept -
# see the comment on step 1.
#
# Run it again whenever CBS starts rejecting the token; CBS publishes no expiry
# for these, so that is the only signal.
#
# Usage: scripts/cbs_access_token.sh

set -euo pipefail

NAMESPACE="${CBS_KESTRA_NAMESPACE:-cbs-sports}"
TOKEN_URL="${CBS_TOKEN_URL:-https://api.cbssports.com/general/oauth}"
BASE_URL="${CBS_BASE_URL:-https://api.cbssports.com/fantasy}"

command -v kestractl >/dev/null || { echo "kestractl not on PATH" >&2; exit 1; }
command -v curl      >/dev/null || { echo "curl not on PATH" >&2; exit 1; }

# Read a KV value, or print nothing if the key is absent.
kv_read() {
  kestractl kv get "$NAMESPACE" "$1" -o json 2>/dev/null \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("value","") or "")
except Exception: pass' 2>/dev/null
}

# Pull one field out of a CBS response body, failing loudly on the errors block
# CBS returns with HTTP 200 rather than a status code.
cbs_field() {
  python3 -c '
import json, sys
field = sys.argv[1]
raw = sys.stdin.read()
try:
    body = json.loads(raw).get("body") or {}
except Exception:
    sys.exit("CBS did not return JSON: %s" % raw[:300])
errors = (body.get("errors") or {}).get("error")
if errors:
    if not isinstance(errors, str):
        errors = "; ".join(str(e) for e in errors)
    sys.exit("CBS returned an error: %s" % errors)
if field not in body:
    sys.exit("CBS returned no %s: %s" % (field, json.dumps(body)[:300]))
print(body[field])
' "$1"
}

# Credentials come from the environment, else the KV store, else a prompt. The
# KV lookup means a re-run after they are already loaded needs no retyping -
# and nothing sensitive is defaulted into this public repo.
#
# The portal that used to issue v3.0 API credentials is gone, and CBS's token
# endpoints accept any non-empty pair - so these name your application rather
# than authenticating it. Pick something stable.
CLIENT_ID="${CBS_CLIENT_ID:-}"
[ -n "$CLIENT_ID" ] || CLIENT_ID=$(kv_read CBS_CLIENT_ID)
if [ -n "$CLIENT_ID" ]; then
  echo "Using CBS_CLIENT_ID from ${NAMESPACE} KV store."
else
  echo "Client id for your application (any stable string, e.g. kestra-draft-assistant):"
  printf '> '
  read -r CLIENT_ID
  [ -n "$CLIENT_ID" ] || { echo "No client id entered." >&2; exit 1; }
fi

CLIENT_SECRET="${CBS_CLIENT_SECRET:-}"
[ -n "$CLIENT_SECRET" ] || CLIENT_SECRET=$(kv_read CBS_CLIENT_SECRET)
if [ -n "$CLIENT_SECRET" ]; then
  echo "Using CBS_CLIENT_SECRET from ${NAMESPACE} KV store."
else
  printf 'Client secret (hidden): '
  read -rs CLIENT_SECRET
  printf '\n'
  [ -n "$CLIENT_SECRET" ] || { echo "No client secret entered." >&2; exit 1; }
fi

USER_ID="${CBS_USER_ID:-}"
[ -n "$USER_ID" ] || USER_ID=$(kv_read CBS_USER_ID)
if [ -n "$USER_ID" ]; then
  echo "Using CBS_USER_ID from ${NAMESPACE} KV store."
else
  echo "The email address you sign in to CBS Sports with:"
  printf '> '
  read -r USER_ID
  [ -n "$USER_ID" ] || { echo "No user id entered." >&2; exit 1; }
fi

LEAGUE_ID="${CBS_LEAGUE_ID:-}"
if [ -z "$LEAGUE_ID" ]; then
  echo "Your league's CBS subdomain - the 'myleague' in"
  echo "https://myleague.football.cbssports.com/ (blank to skip the check):"
  printf '> '
  read -r LEAGUE_ID
fi

# 1. A request token bound to the CBS account. GET with the credentials in the
#    query string is the only form CBS accepts here - the same request as a
#    POST with a form body answers HTTP 500. Its resource endpoints take
#    `access_token` as a query parameter too, so this API puts secrets in URLs
#    by design.
REQUEST_TOKEN=$(curl -sS -G "${TOKEN_URL}/request_token" \
  --data-urlencode 'response_format=json' \
  --data-urlencode "client_id=${CLIENT_ID}" \
  --data-urlencode "client_secret=${CLIENT_SECRET}" \
  --data-urlencode "user_id=${USER_ID}" \
  | cbs_field token)

# 2. Exchange it for the access token the flow uses.
ACCESS_TOKEN=$(curl -sS -G "${TOKEN_URL}/access_token" \
  --data-urlencode 'response_format=json' \
  --data-urlencode "client_id=${CLIENT_ID}" \
  --data-urlencode "client_secret=${CLIENT_SECRET}" \
  --data-urlencode "request_token=${REQUEST_TOKEN}" \
  | cbs_field access_token)

# 3. Prove the token actually reaches the league before storing it. CBS hands
#    out tokens freely and only refuses at the resource, so skipping this would
#    mean discovering the problem mid-draft.
if [ -n "$LEAGUE_ID" ]; then
  echo
  echo "Checking the token against league '${LEAGUE_ID}'..."
  CHECK=$(curl -sS -G "${BASE_URL}/league/details" \
    --data-urlencode 'version=3.0' \
    --data-urlencode 'SPORT=football' \
    --data-urlencode 'response_format=json' \
    --data-urlencode "league_id=${LEAGUE_ID}" \
    --data-urlencode "access_token=${ACCESS_TOKEN}")
  if ! printf %s "$CHECK" | grep -q '"league_details"'; then
    echo "CBS did not return league details:" >&2
    printf '  %s\n' "$(printf %s "$CHECK" | head -c 300)" >&2
    echo >&2
    echo "The token was not stored. Check that ${USER_ID} is a member of" >&2
    echo "'${LEAGUE_ID}', and that the league id is the subdomain of the" >&2
    echo "league URL rather than a team or league name." >&2
    exit 1
  fi
  printf %s "$CHECK" | python3 -c '
import json, sys
d = json.load(sys.stdin)["body"]["league_details"]
print("  %s - %s teams, draft state %s" % (
    d.get("name"), d.get("num_teams"), d.get("draft_state")))
'
fi

# 4. Load the credentials and the token into the KV store the flow reads.
kestractl kv set "$NAMESPACE" STRING CBS_CLIENT_ID     "$CLIENT_ID"     >/dev/null
kestractl kv set "$NAMESPACE" STRING CBS_CLIENT_SECRET "$CLIENT_SECRET" >/dev/null
kestractl kv set "$NAMESPACE" STRING CBS_USER_ID       "$USER_ID"       >/dev/null
# Cached in the same shape the flow writes after minting one itself, so the two
# paths are interchangeable.
kestractl kv set "$NAMESPACE" JSON cbs_access_token \
  "{\"access_token\":\"${ACCESS_TOKEN}\",\"minted\":true}" >/dev/null

echo
echo "Loaded CBS_CLIENT_ID, CBS_CLIENT_SECRET, CBS_USER_ID and"
echo "cbs_access_token into '${NAMESPACE}'."
kestractl kv list "$NAMESPACE"

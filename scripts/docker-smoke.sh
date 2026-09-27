#!/usr/bin/env bash
# Docker smoke test: prove a fresh clone can reach a working gateway with one command.
#
#   ./scripts/docker-smoke.sh
#
# Builds the image, starts it on a throwaway port with a throwaway key, and asserts that health,
# auth, and the error envelope all behave. Nothing here talks to Google: no account is connected,
# so the only successful path exercised is the unauthenticated health check.

set -euo pipefail

PORT="${GMAIL_AUTOMATOR_SMOKE_PORT:-18080}"
IMAGE="gmail-automator:smoke"
NAME="gmail-automator-smoke-$$"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY="$(python3 -c 'import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
ADMIN_KEY="smoke-admin-key"
BASE="http://127.0.0.1:${PORT}"
FAILED=0

log()  { printf '\n== %s\n' "$1"; }
pass() { printf '   ok   %s\n' "$1"; }
fail() { printf '   FAIL %s\n' "$1"; FAILED=1; }

check_status() {
  local expected="$1" path="$2" actual
  actual="$(curl -s -o /dev/null -w '%{http_code}' "${@:3}" "${BASE}${path}")"
  if [[ "${actual}" == "${expected}" ]]; then
    pass "${path} -> ${actual}"
  else
    fail "${path} -> ${actual} (expected ${expected})"
  fi
}

cleanup() {
  docker rm -f "${NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "building ${IMAGE}"
docker build -q -t "${IMAGE}" "${ROOT}"

log "starting ${NAME} on port ${PORT}"
docker run -d --name "${NAME}" \
  -p "127.0.0.1:${PORT}:8000" \
  -e "GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY=${KEY}" \
  -e GMAIL_AUTOMATOR_AUTH_MODE=api_key \
  -e "GMAIL_AUTOMATOR_BOOTSTRAP_ADMIN_KEY=${ADMIN_KEY}" \
  "${IMAGE}" serve >/dev/null

log "waiting for /health"
for _ in $(seq 1 60); do
  if curl -fsS "${BASE}/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! curl -fsS "${BASE}/health" >/dev/null 2>&1; then
  echo "gateway never became healthy; container logs:" >&2
  docker logs "${NAME}" >&2
  exit 1
fi

AUTH=(-H "authorization: Bearer ${ADMIN_KEY}")
JSON=(-H 'content-type: application/json')
SSE=(-H 'accept: application/json, text/event-stream')

log "asserting the auth gate"
check_status 200 /health
check_status 401 /v1/accounts
check_status 401 /v1/send -X POST "${JSON[@]}" -d '{"to":["a@example.com"],"subject":"s","body":"b"}'
check_status 401 /mcp -X POST "${JSON[@]}" "${SSE[@]}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
check_status 401 /v1/accounts -H 'authorization: Bearer wrong-key'

log "asserting the authenticated surface"
check_status 200 /v1/accounts "${AUTH[@]}"
check_status 200 /v1/quota "${AUTH[@]}"
check_status 200 /mcp -X POST "${AUTH[@]}" "${JSON[@]}" "${SSE[@]}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
check_status 400 /v1/send -X POST "${AUTH[@]}" "${JSON[@]}" \
  -d '{"to":[],"subject":"s","body":"b"}'
check_status 404 /v1/nope "${AUTH[@]}"
check_status 405 /v1/send "${AUTH[@]}"
check_status 404 /v1/accounts/nobody@example.com "${AUTH[@]}"

if curl -fsS "${BASE}/health" | grep -q '"status":"ok"'; then
  pass "health payload reports ok"
else
  fail "health payload missing status=ok"
fi

if docker exec "${NAME}" id -u | grep -q '^10001$'; then
  pass "container runs as uid 10001"
else
  fail "container is not running as uid 10001"
fi

if docker exec "${NAME}" sh -c 'ls /data' >/dev/null 2>&1; then
  pass "/data is writable by the runtime user"
else
  fail "/data is not usable"
fi

# Token columns exist in the schema, so look for token *values*: Google access tokens start with
# "ya29." and refresh tokens with "1//". Neither may appear in the database file.
if docker exec "${NAME}" sh -c 'grep -a -o -E "ya29\.|1//" /data/*.db' >/dev/null 2>&1; then
  fail "a plaintext Google token was found in the database"
else
  pass "no plaintext Google tokens in the database"
fi

if docker exec "${NAME}" sh -c 'grep -a -c "access_token_enc" /data/*.db' >/dev/null 2>&1; then
  pass "token columns are stored encrypted (access_token_enc)"
else
  fail "expected encrypted token columns in the schema"
fi

if docker exec "${NAME}" sh -c 'grep -a -o -E "ya29\.|1//" /app/migrations/*.py' >/dev/null 2>&1; then
  fail "a plaintext token literal is present in the image"
else
  pass "no plaintext token literals in the image"
fi

log "result"
if [[ "${FAILED}" == "0" ]]; then
  echo "   smoke test passed"
else
  echo "   smoke test failed" >&2
fi
exit "${FAILED}"

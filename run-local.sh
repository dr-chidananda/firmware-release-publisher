#!/bin/bash
# Local (no-Docker) dev loop for the Firmware Release Publisher task, macOS arm64.
#
# Recreates what environment/Dockerfile does — signing keypairs + npm deps — then
# starts the gateway, runs the publisher twice, and checks the golden output and
# idempotency. Run it from anywhere:
#
#     bash run-local.sh ~/firmware-task
#
set -euo pipefail

TASK_ROOT="${1:-$HOME/firmware-task}"
APP="$TASK_ROOT/environment"
GATEWAY="$APP/distribution-gateway"
PORT="${PORT:-7070}"

[ -d "$APP" ] || { echo "!! $APP not found — pass the task root as \$1"; exit 1; }

# ---------------------------------------------------------------- toolchain ---
# duckdb@1.1.3 ships prebuilt binaries only up to Node 20's ABI; on Node 22+ it
# falls back to a source build and fails. Node 20 is also what the image uses.
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$NODE_MAJOR" != "20" ]; then
  echo "!! Node 20 required (found: $(node -v 2>/dev/null || echo none))."
  echo "   brew install node@20 && export PATH=\"/opt/homebrew/opt/node@20/bin:\$PATH\""
  exit 1
fi

# macOS ships LibreSSL as /usr/bin/openssl, which has no working `cms` subcommand.
# Both the publisher and the gateway shell out to whatever `openssl` is on PATH,
# so real OpenSSL 3 has to come first.
if ! openssl version 2>/dev/null | grep -q '^OpenSSL'; then
  BREW_SSL="$(brew --prefix openssl@3 2>/dev/null || echo /opt/homebrew/opt/openssl@3)"
  if [ -x "$BREW_SSL/bin/openssl" ]; then
    export PATH="$BREW_SSL/bin:$PATH"
  else
    echo "!! Real OpenSSL 3 not found (LibreSSL can't do 'cms'). Run: brew install openssl@3"
    exit 1
  fi
fi
echo "== toolchain: $(node -v), $(openssl version)"

# --------------------------------------------------- keypairs (Dockerfile parity)
if [ ! -f "$APP/keys/current/current.key.pem" ]; then
  echo "== generating signing keypairs"
  mkdir -p "$APP/keys/current" "$APP/keys/revoked"
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
    -keyout "$APP/keys/current/current.key.pem" \
    -out    "$APP/keys/current/current.cert.pem" \
    -subj "/CN=fw-signing-2026-current/O=ReleaseEng/C=US" 2>/dev/null
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
    -keyout "$APP/keys/revoked/revoked.key.pem" \
    -out    "$APP/keys/revoked/revoked.cert.pem" \
    -subj "/CN=fw-signing-2025-revoked/O=ReleaseEng/C=US" 2>/dev/null
fi

# ------------------------------------------------------------------- deps ------
[ -d "$APP/node_modules" ]     || (echo "== npm install (publisher)"; cd "$APP" && npm install --no-audit --no-fund)
[ -d "$GATEWAY/node_modules" ] || (echo "== npm install (gateway)";   cd "$GATEWAY" && npm install --no-audit --no-fund)

[ -f "$APP/publisher/release-publisher.mjs" ] || {
  echo "!! $APP/publisher/release-publisher.mjs is missing — drop the publisher there first."; exit 1; }

# ------------------------------------------------------------------ gateway ----
# The gateway defaults to the in-image cert path, so point it at the local one.
echo "== starting gateway on :$PORT"
rm -f "$APP/releases.duckdb" "$GATEWAY/data/gateway.json"
CURRENT_CERT_PATH="$APP/keys/current/current.cert.pem" PORT="$PORT" \
  node "$GATEWAY/server.js" > /tmp/fw-gateway.log 2>&1 &
GW_PID=$!
trap 'kill $GW_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break
  sleep 0.25
done
curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null || { echo "!! gateway never came up"; cat /tmp/fw-gateway.log; exit 1; }

# -------------------------------------------------------------------- runs -----
cd "$APP"
echo "== run 1"; npm run --silent report | tee /tmp/fw-run1.txt
echo "== run 2"; npm run --silent report > /tmp/fw-run2.txt

mask() { sed -E 's/RECEIPT=[^ ]+/RECEIPT=<id>/' "$1"; }

echo
echo "== golden diff (RECEIPT masked)"
if diff <(mask reports/publications.expected.txt) <(mask /tmp/fw-run1.txt); then
  echo "   OK — matches publications.expected.txt"
else
  echo "   FAIL — output does not match the golden file"; exit 1
fi

echo "== idempotency"
if diff -q /tmp/fw-run1.txt /tmp/fw-run2.txt >/dev/null; then
  echo "   OK — second run byte-identical"
else
  echo "   FAIL — reruns differ"; exit 1
fi

LEDGER=$(node -e "console.log(Object.keys(require('$GATEWAY/data/gateway.json').publications).length)")
echo "== gateway ledger holds $LEDGER publications (expected 3)"
[ "$LEDGER" = "3" ] || { echo "   FAIL — duplicate publications on the gateway"; exit 1; }

echo "== revoked-key trap"
printf '%s' '{"artifact_count":1,"bundle_id":"BND-TRAP","total_bytes":100}' > /tmp/fw-trap.bin
openssl cms -sign -in /tmp/fw-trap.bin \
  -signer "$APP/keys/revoked/revoked.cert.pem" \
  -inkey  "$APP/keys/revoked/revoked.key.pem" \
  -md sha256 -outform PEM -binary > /tmp/fw-trap-sig.pem
TRAP=$(node -e '
const fs = require("fs");
fetch("http://127.0.0.1:'"$PORT"'/v1/publications", {
  method: "POST", headers: {"content-type": "application/json"},
  body: JSON.stringify({
    descriptor: fs.readFileSync("/tmp/fw-trap.bin", "utf8"),
    signature: fs.readFileSync("/tmp/fw-trap-sig.pem", "utf8"),
    request_token: "token-BND-TRAP",
  }),
}).then(r => r.json()).then(j => console.log(j.error || j.status));')
[ "$TRAP" = "UNTRUSTED_SIGNATURE" ] && echo "   OK — revoked key rejected" \
  || { echo "   FAIL — revoked key returned: $TRAP"; exit 1; }

echo
echo "ALL CHECKS PASSED"

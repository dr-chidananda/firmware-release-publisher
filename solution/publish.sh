#!/bin/bash
# Reference-solution entrypoint.
#
# The grader runs this script inside the built task image (WORKDIR /app) to
# demonstrate Proof B ("solution scores 1"). Its only job is to install the
# reference publisher exactly where a candidate would put it themselves:
#
#     /app/publisher/release-publisher.mjs
#
# environment/publisher/ ships empty on purpose (see CANDIDATE_GUIDE.md 2) --
# the real implementation lives here, next to this script, as
# release-publisher.mjs, and is copied into place rather than baked into the
# image. tests/test.sh (run separately by the grader right after this script)
# is what actually exercises it and computes the reward; this script does not
# run the report or start the gateway itself.
set -euo pipefail

SOLUTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-/app}"

SRC="$SOLUTION_DIR/release-publisher.mjs"
DEST_DIR="$APP_ROOT/publisher"
DEST="$DEST_DIR/release-publisher.mjs"

[ -f "$SRC" ] || { echo "!! $SRC not found next to publish.sh"; exit 1; }
[ -d "$APP_ROOT" ] || { echo "!! $APP_ROOT not found -- is this running inside the task image?"; exit 1; }

mkdir -p "$DEST_DIR"
cp "$SRC" "$DEST"

# Fail fast on a syntax error rather than letting the grader discover it via a
# confusing runtime crash. Requires only Node itself, no dependencies.
node --check "$DEST"

echo "== installed reference publisher at $DEST"

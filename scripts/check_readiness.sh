#!/bin/sh
# Ask the running application whether it can serve, and believe only the answer.
#
#   check_readiness.sh [url]
#
# Default: http://127.0.0.1:8000/health/ready - the application's own port,
# from inside its own container. The deploy pipeline runs this through
# `docker compose exec -T api`, so the request never leaves the compose network
# and never passes through nginx.
#
# **Why this exists as a script rather than a line in a workflow.** The check it
# replaces was
#
#     curl -fsS --max-time 10 http://127.0.0.1/health/ready
#
# and port 80 is nginx, whose only non-ACME rule is a 301 to HTTPS. `curl -f`
# fails on 4xx and 5xx; a 301 without `-L` exits **zero**. So the final gate of
# every deployment passed whenever nginx was running - regardless of whether the
# API was up, the database reachable or the migrations applied. The workflow's
# own comment stated the intent exactly: "A readiness check here is what turns
# 'the command exited zero' into 'the release works'." It did not.
#
# Two rules follow from that, and both are why the exit code alone is not
# enough:
#
#   1. **The status must be a success, not merely not-an-error.** A redirect is
#      not an application answering; it is a proxy declining to. Anything
#      outside 2xx fails here.
#   2. **The body must say the application is ready.** A 200 from something that
#      is not this endpoint - a maintenance page, a stale container, a proxy
#      returning a cached root document - satisfies rule 1 and proves nothing.
#
# Being a file rather than a string is what makes it testable:
# `tests/unit/test_readiness_gate.py` runs *this script* against a stub server
# that serves each of those shapes, instead of searching the workflow's YAML for
# a substring - which is what the test it replaces did.
set -eu

URL="${1:-http://127.0.0.1:8000/health/ready}"
TIMEOUT_SECONDS="${READINESS_TIMEOUT_SECONDS:-10}"
# What `/health/ready` answers when every dependency it needs is reachable. The
# 503 body says `degraded` and is refused by the status rule above; this is the
# guard against a 200 that is not this endpoint at all.
EXPECTED='"status":"ok"'

fail() {
    echo "readiness: $*" >&2
    exit 1
}

# `--fail-with-body` would be tidier and is not portable enough to rely on, so
# the status is read explicitly and the body is captured whole. `-o` and `-w`
# rather than `-f`: this needs to tell 301 from 200 from 503, and `-f` collapses
# two of those into the same exit code while passing the third.
body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT

status="$(
    curl -sS \
        --max-time "$TIMEOUT_SECONDS" \
        --output "$body_file" \
        --write-out '%{http_code}' \
        "$URL"
)" || fail "could not reach $URL"

case "$status" in
    2??) ;;
    *) fail "expected a 2xx, got $status" ;;
esac

body="$(tr -d ' \n\r' < "$body_file")"
case "$body" in
    *"$EXPECTED"*) ;;
    *) fail "answered $status but did not report ready" ;;
esac

echo "readiness: $URL reported ready"

#!/usr/bin/env bash
# End-to-end smoke test: fleet-sim -> internal collector -> fake backend.
# Passes once the fake backend has received traces, metrics and logs.
set -euo pipefail

cd "$(dirname "$0")/.."
# Unique project per run, so leftovers from an interrupted run (and their
# logs) can never satisfy this run's checks.
COMPOSE=(docker compose -p "fleet-sim-smoke-$$-${RANDOM}" -f docker-compose.yml -f ci/docker-compose.smoke.yml)
TIMEOUT="${SMOKE_TIMEOUT:-90}"

# Compose requires this at interpolation time; the overlay replaces it.
export UPSTREAM_OTLP_ENDPOINT="${UPSTREAM_OTLP_ENDPOINT:-http://fake-upstream:4318}"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    "${COMPOSE[@]}" logs --no-color --tail=100 || true
  fi
  "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

"${COMPOSE[@]}" up -d ${SMOKE_BUILD:+--build}

deadline=$((SECONDS + TIMEOUT))
for signal in Traces Metrics Logs; do
  until "${COMPOSE[@]}" logs --no-color fake-upstream | grep "info[[:space:]]*${signal}" >/dev/null; do
    # (not grep -q: exiting early can SIGPIPE `compose logs` and, with
    # pipefail, turn a match into a failure)
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "FAIL: fake backend received no ${signal} within ${TIMEOUT}s" >&2
      exit 1
    fi
    sleep 2
  done
  echo "ok: ${signal} reached the backend"
done

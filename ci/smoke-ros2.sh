#!/usr/bin/env bash
# End-to-end smoke test for the optional ros2ebpf stack:
#   ROS 2 nodes -(eBPF uprobes)-> ros2ebpfd -(scrape)-> collector -> fake backend
# Needs Linux with BTF and permission to run privileged containers.
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT="fleet-sim-ros2-smoke-$$-${RANDOM}"
COMPOSE=(docker compose -p "$PROJECT"
  -f docker-compose.yml -f ci/docker-compose.smoke.yml
  -f docker-compose.ros2.yml -f ci/docker-compose.smoke-ros2.yml)
TIMEOUT="${SMOKE_TIMEOUT:-180}"
TOPIC=/amr_01/chatter

export UPSTREAM_OTLP_ENDPOINT="${UPSTREAM_OTLP_ENDPOINT:-http://fake-upstream:4318}"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    "${COMPOSE[@]}" logs --no-color --tail=60 ros2ebpfd ros2-robot otel-collector || true
  fi
  "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

backend_has() {
  # Full read, no grep -q: see ci/smoke.sh.
  "${COMPOSE[@]}" logs --no-color fake-upstream | grep -E "$1" >/dev/null
}

ros2ebpf_resource_has() {
  # Only the resource attributes printed right after service.name=ros2ebpf,
  # so fleet-sim's own robot.id / service.namespace cannot satisfy the check.
  "${COMPOSE[@]}" logs --no-color fake-upstream \
    | grep -A8 "service\.name: Str(ros2ebpf)" | grep -E "$1" >/dev/null
}

wait_for() {
  local what=$1 pattern=$2 check=${3:-backend_has}
  until "$check" "$pattern"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "FAIL: backend never received ${what} within ${TIMEOUT}s" >&2
      exit 1
    fi
    sleep 3
  done
  echo "ok: ${what}"
}

"${COMPOSE[@]}" up -d ${SMOKE_BUILD:+--build}
deadline=$((SECONDS + TIMEOUT))

wait_for "ros2_rmw_publish_total" "Name: ros2_rmw_publish_total"
wait_for "ros2_rmw_take_total" "Name: ros2_rmw_take_total"
wait_for "ros2_rmw_service_request_total" "Name: ros2_rmw_service_request_total"
wait_for "topic ${TOPIC}" "topic: Str\(${TOPIC}\)"
wait_for "robot.id on the ros2ebpf resource" "robot\.id: Str\(amr-01\)" ros2ebpf_resource_has
wait_for "service.namespace on the ros2ebpf resource" "service\.namespace: Str\(robotics\)" ros2ebpf_resource_has

# The talker publishes once a second and the listener takes every message,
# so the two counters must stay close. A large gap means publishes are being
# counted more than once (see docker-compose.ros2.yml).
metrics=$(docker run --rm --network "${PROJECT}_default" curlimages/curl:8.17.0 -sf ros2ebpfd:9477/metrics)
pub=$(awk -v t="topic=\"${TOPIC}\"" '/^ros2_rmw_publish_total/ && index($0, t) {print $NF}' <<<"$metrics")
take=$(awk -v t="topic=\"${TOPIC}\"" '/^ros2_rmw_take_total/ && index($0, t) {print $NF}' <<<"$metrics")
echo "publish=${pub:-?} take=${take:-?} on ${TOPIC}"
if [ -z "$pub" ] || [ -z "$take" ] || [ "$take" -eq 0 ] || [ "$pub" -gt $(( take * 3 / 2 + 5 )) ]; then
  echo "FAIL: publish and take counters disagree" >&2
  exit 1
fi
echo "ok: publish and take counters agree"

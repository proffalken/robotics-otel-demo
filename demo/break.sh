#!/usr/bin/env bash
# Break the demo on purpose, so the check rules fire and Agent0 has
# something to investigate. Run it on the host running the stack.
#
#   demo/break.sh <scenario>
#
# Scenarios:
#   ros2-subscriber-down  kill the listener in ros2-robot  -> ROS2TopicDeliveryGap
#   ros2-publisher-down   kill the talker in ros2-robot    -> ROS2TopicSilent
#   fleet-faults          fleet-sim with 5x fault rates    -> RobotOperationErrorRate / RobotTaskFailureRate
#   restore               put everything back
#
# Compose files default to the ros2ebpf stack; override with COMPOSE_FILES,
# e.g. COMPOSE_FILES="-f docker-compose.yml" for fleet-sim only.
set -euo pipefail

cd "$(dirname "$0")/.."
read -r -a FILES <<<"${COMPOSE_FILES:--f docker-compose.yml -f docker-compose.ros2.yml}"
COMPOSE=(docker compose "${FILES[@]}")

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
  exit 2
}

# The ROS 2 image has no pkill, so match processes through /proc. The
# pattern travels in an env var, not argv: otherwise this shell (and its
# subshells) would have it in their own cmdline and match themselves.
kill_ros2_node() {
  "${COMPOSE[@]}" exec -T -e NODE_PATTERN="/lib/$1 " ros2-robot bash -c '
    found=0
    for d in /proc/[0-9]*; do
      cmd=$({ tr "\0" " " < "$d/cmdline"; } 2>/dev/null) || continue
      case "$cmd" in *"$NODE_PATTERN"*) kill "${d#/proc/}" && found=1 ;; esac
    done
    [ "$found" = 1 ] || { echo "no running process matching $NODE_PATTERN" >&2; exit 1; }
  '
}

case "${1:-}" in
  ros2-subscriber-down)
    kill_ros2_node "demo_nodes_py/listener"
    echo "listener killed: /amr_01/chatter is still published but nothing takes it."
    echo "ROS2TopicDeliveryGap should fire within ~3 minutes."
    ;;
  ros2-publisher-down)
    kill_ros2_node "demo_nodes_cpp/talker"
    echo "talker killed: /amr_01/chatter has stopped publishing."
    echo "ROS2TopicSilent should fire within ~3 minutes."
    ;;
  fleet-faults)
    FLEET_FAULT_SCALE=5 "${COMPOSE[@]}" up -d --no-deps fleet-sim
    echo "fleet-sim restarted with FLEET_FAULT_SCALE=5."
    echo "RobotOperationErrorRate / RobotTaskFailureRate should fire within ~12 minutes."
    ;;
  restore)
    "${COMPOSE[@]}" restart ros2-robot 2>/dev/null || true
    FLEET_FAULT_SCALE="${FLEET_FAULT_SCALE_DEFAULT:-1.0}" "${COMPOSE[@]}" up -d --no-deps fleet-sim
    echo "restored: ros2-robot restarted, fleet-sim back to normal fault rates."
    ;;
  *) usage ;;
esac

"""Generate dashboards/robot-correlation.yaml (Perses format, for `dash0 apply`).

One robot's application telemetry (fleet-sim metrics, spans, logs) next to
its ROS 2 middleware telemetry (ros2ebpf), on the same time axis. Both sides
carry robot_id, so a single Robot filter drives every panel. Application
panels sit on the left (x=0), ROS 2 panels on the right (x=12).

The two sides are separate programs that share only the robot_id label:
the dashboard lines them up, it does not imply that one drives the other.

Run: uv run python dashboards/correlation.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate import NS, ROBOT, grid, m, p95, query, timeseries  # noqa: E402
from generate import stat as _stat  # noqa: E402


def stat(name, description, promql, unit="decimal", decimals=0, label="selected range"):
    """A stat tile with a short series label, so the tile doesn't print its PromQL."""
    tile = _stat(name, description, promql, unit, decimals)
    tile["spec"]["queries"][0]["spec"]["plugin"]["spec"]["seriesNameFormat"] = label
    return tile

# Application side
HANDLED = m("robot.commands.handled", "sum", f"{NS}, {ROBOT}")
FAILED_TASKS = m("robot.commands.handled", "sum", f'{NS}, outcome = "failure", {ROBOT}')
SPANS = m("dash0.spans", "sum", f"{NS}, {ROBOT}")
ERROR_SPANS = m("dash0.spans", "sum", f'{NS}, otel_span_status_code = "ERROR", {ROBOT}')
LOGS = m("dash0.logs", "sum", f"{NS}, {ROBOT}")
ERROR_LOGS = m("dash0.logs", "sum", f'{NS}, otel_log_severity_range =~ "ERROR|FATAL", {ROBOT}')
BATTERY = m("robot.battery.level", "gauge", f"{NS}, {ROBOT}")
TEMP = m("robot.cpu.temperature", "gauge", f"{NS}, {ROBOT}")
STEP_DUR = m("robot.step.duration", "histogram", f"{NS}, {ROBOT}")

# ROS 2 side (ros2ebpf)
ROS_PUB = m("ros2_rmw_publish_total", "sum", f"{NS}, {ROBOT}")
ROS_TAKE = m("ros2_rmw_take_total", "sum", f"{NS}, {ROBOT}")
ROS_REQ = m("ros2_rmw_service_request_total", "sum", f'{NS}, side = "server", {ROBOT}')
ROS_RESP = m("ros2_rmw_service_response_total", "sum", f'{NS}, side = "server", {ROBOT}')
ROS_PERIOD = m("ros2_rmw_publish_period_seconds", "histogram", f"{NS}, {ROBOT}")
ROS_PROC = m("ros2_process_info", "gauge", f"{NS}, {ROBOT}")
ROS_LOST = m("ros2_rmw_take_seq_lost_total", "sum", f"{NS}, {ROBOT}")
ROS_GAP = m("ros2_rmw_take_seq_gap_total", "sum", f"{NS}, {ROBOT}")
EBPF_ATTACHED = m("ros2ebpf_probes_attached", "gauge", f"{NS}, {ROBOT}")
EBPF_CONFIGURED = m("ros2ebpf_probes_configured", "gauge", f"{NS}, {ROBOT}")
EBPF_DROPPED = m("ros2ebpf_events_dropped_total", "sum", f"{NS}, {ROBOT}")
EBPF_UNRESOLVED = m("ros2ebpf_unresolved_endpoints", "gauge", f"{NS}, {ROBOT}")
SCRAPE_UP = m("up", "gauge", f"{NS}, {ROBOT}")


def ratio(part: str, whole: str, window: str = "$__range") -> str:
    """100 * part / whole, reading 0 (not "no data") when part has no series."""
    return (f"100 * (sum(increase({part}[{window}])) or sum(increase({whole}[{window}])) * 0)"
            f" / sum(increase({whole}[{window}]))")


R = "$__rate_interval"

panels = {
    # Health at a glance: application tiles first, then ROS 2
    "taskFailure": stat(
        "Task failure rate", "Tasks the robot reported as failed, as a share of tasks handled",
        ratio(FAILED_TASKS, HANDLED), unit="percent", decimals=1,
    ),
    "spanErrors": stat(
        "Span error rate", "Spans from this robot with status ERROR, as a share of all its spans",
        ratio(ERROR_SPANS, SPANS), unit="percent", decimals=1,
    ),
    "rosPublish": stat(
        "ROS 2 messages / s", "rmw_publish calls per second across this robot's topics",
        f"sum(rate({ROS_PUB}[$__range]))", decimals=2,
    ),
    "rosDelivery": stat(
        "Takes per publish", "Messages taken per message published; below 100% means messages "
        "are published but not taken",
        ratio(ROS_TAKE, ROS_PUB), unit="percent",
    ),
    "rosAnswered": stat(
        "Service calls answered", "Server responses as a share of server requests",
        ratio(ROS_RESP, ROS_REQ), unit="percent",
    ),
    "probeCoverage": stat(
        "Probe coverage", "eBPF probes attached as a share of probes configured; below 100% "
        "means some ROS 2 calls are not counted",
        f"100 * sum({EBPF_ATTACHED}) / sum({EBPF_CONFIGURED})", unit="percent", label="now",
    ),
    # Application vs ROS 2 middleware
    "appTasks": timeseries(
        "Tasks handled by outcome", "Tasks the robot finished per interval, success vs failure",
        [query(f"sum by (outcome) (increase({HANDLED}[{R}]))", "{{outcome}}")],
        display="bar", stack=True,
    ),
    "rosFlow": timeseries(
        "Published vs taken per topic", "Messages per second published and taken on each topic; "
        "the lines separate when a subscriber stops or falls behind",
        [
            query(f"sum by (topic) (rate({ROS_PUB}[{R}]))", "{{topic}} published"),
            query(f"sum by (topic) (rate({ROS_TAKE}[{R}]))", "{{topic}} taken"),
        ],
        decimals=2,
    ),
    "appErrors": timeseries(
        "Error spans and error logs", "Spans with status ERROR and ERROR/FATAL log records per interval",
        [
            query(f"sum(increase({ERROR_SPANS}[{R}]))", "error spans"),
            query(f"sum(increase({ERROR_LOGS}[{R}]))", "error logs"),
        ],
        display="bar",
    ),
    "rosServices": timeseries(
        "Service requests vs responses", "Server side requests taken and responses sent per second",
        [
            query(f"sum by (service) (rate({ROS_REQ}[{R}]))", "{{service}} requests"),
            query(f"sum by (service) (rate({ROS_RESP}[{R}]))", "{{service}} responses"),
        ],
        decimals=2,
    ),
    # Robot state vs ROS 2 runtime
    "battery": timeseries(
        "Battery level", "State of charge reported by the robot",
        [query(f"max({BATTERY})", "battery")], unit="percent",
    ),
    "rosProcesses": timeseries(
        "ROS 2 processes", "Distinct PIDs per process name seen by ros2ebpf in the last minute; "
        "a name dropping to zero means that node exited. Short-lived clients that respawn show "
        "several PIDs",
        [query(f"count by (comm) (max by (comm, pid) (last_over_time({ROS_PROC}[1m])))", "{{comm}}")],
    ),
    "temperature": timeseries(
        "CPU temperature (°C)", "Onboard computer temperature",
        [query(f"max({TEMP})", "cpu")], decimals=1,
    ),
    "rosJitter": timeseries(
        "Publish interval p99 by topic", "99th percentile time between consecutive publishes; "
        "spikes are jitter or stalls in the publishing node",
        [query(f"histogram_quantile(0.99, sum by (le, topic) (increase({ROS_PERIOD}[{R}])))", "{{topic}}")],
        unit="seconds", decimals=3,
    ),
    "stepDuration": timeseries(
        "Task step duration", "Median and 95th percentile duration of task steps on the robot, "
        "across all step types",
        [
            query(f"histogram_quantile(0.5, sum by (le) (increase({STEP_DUR}[{R}])))", "p50"),
            query(p95(STEP_DUR, ""), "p95"),
        ],
        unit="seconds", decimals=2,
    ),
    "rosLoss": timeseries(
        "Messages lost or out of order", "Per interval: messages a subscription never received "
        "(lost) and sequence jumps it saw (gaps)",
        [
            query(f"sum by (topic) (increase({ROS_LOST}[{R}]))", "{{topic}} lost"),
            query(f"sum by (topic) (increase({ROS_GAP}[{R}]))", "{{topic}} gaps"),
        ],
        display="bar",
    ),
    # Telemetry pipeline: is a gap the robot, or the monitoring?
    "appTelemetry": timeseries(
        "fleet-sim telemetry received", "Spans and log records from this robot reaching Dash0 per interval; "
        "a drop to zero means the application or its export stopped",
        [
            query(f"sum(increase({SPANS}[{R}]))", "spans"),
            query(f"sum(increase({LOGS}[{R}]))", "logs"),
        ],
    ),
    "rosScrape": timeseries(
        "ros2ebpf scrape and resolution", "Collector scrape of ros2ebpfd (1 = up) and endpoints "
        "ros2ebpf could not yet tie to a process",
        [
            query(f"max({SCRAPE_UP})", "scrape up"),
            query(f"max({EBPF_UNRESOLVED})", "unresolved endpoints"),
        ],
    ),
    "ebpfDropped": timeseries(
        "ros2ebpf events dropped", "Ring buffer overflows per interval; affected processes stay "
        "unresolved until the next GC",
        [query(f"sum(increase({EBPF_DROPPED}[{R}]))", "dropped")], display="bar",
    ),
}

layouts = [
    grid("Health at a glance", [(k, i * 4, 0, 4, 4) for i, k in enumerate(
        ["taskFailure", "spanErrors", "rosPublish", "rosDelivery", "rosAnswered", "probeCoverage"])]),
    grid("Application vs ROS 2 middleware", [
        ("appTasks", 0, 0, 12, 8), ("rosFlow", 12, 0, 12, 8),
        ("appErrors", 0, 8, 12, 8), ("rosServices", 12, 8, 12, 8),
    ]),
    grid("Robot state vs ROS 2 runtime", [
        ("battery", 0, 0, 12, 8), ("rosProcesses", 12, 0, 12, 8),
        ("temperature", 0, 8, 12, 8), ("rosJitter", 12, 8, 12, 8),
        ("stepDuration", 0, 16, 12, 8), ("rosLoss", 12, 16, 12, 8),
    ]),
    grid("Telemetry pipeline", [
        ("appTelemetry", 0, 0, 8, 8), ("rosScrape", 8, 0, 8, 8), ("ebpfDropped", 16, 0, 8, 8),
    ]),
]


def robot_filter() -> dict:
    # Defaults to amr-01, the robot the ROS 2 demo stack runs as
    # (ROS2EBPF_ROBOT). Pick another robot to see only its fleet-sim side.
    return {
        "kind": "Dash0FilterVariable",
        "spec": {
            "name": "robot_id", "attributeKey": "robot_id", "operator": "is_one_of", "values": ["amr-01"],
            "dependsOn": [], "capturingRegexp": "",
            "display": {"name": "Robot", "description": "", "hidden": False},
        },
    }


def build() -> dict:
    return {
        "kind": "Dashboard",
        "metadata": {"name": "robot-correlation", "dash0Extensions": {"id": "robot-correlation"}},
        "spec": {
            "display": {
                "name": "Robot Correlation",
                "description": "One robot's application telemetry (fleet-sim tasks, spans, logs) beside its "
                "ROS 2 middleware telemetry (ros2ebpf), on one time axis. Application on the left, ROS 2 on "
                "the right.",
            },
            "duration": "30m",
            "variables": [robot_filter()],
            "panels": panels,
            "layouts": layouts,
        },
    }


def main() -> None:
    out = Path(__file__).with_name("robot-correlation.yaml")
    out.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

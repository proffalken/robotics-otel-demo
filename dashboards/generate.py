"""Generate dashboards/fleet-sim.yaml (Perses format, for `dash0 apply`).

Run: uv run python dashboards/generate.py
"""

import json
from pathlib import Path


def m(name: str, kind: str, extra: str = "") -> str:
    sel = f'otel_metric_name = "{name}", otel_metric_type = "{kind}"'
    return "{" + sel + (", " + extra if extra else "") + "}"


ROBOT = "$robot_id"
TASK = "$fleet_task_type"
SENT = m("fleet.commands.sent", "sum", f"{ROBOT}, {TASK}")
FAILED = m("fleet.commands.failed", "sum", f"{ROBOT}, {TASK}")
TASK_DUR = m("fleet.task.duration", "histogram", TASK)
LATENCY = m("fleet.command.latency", "histogram", ROBOT)
ROBOTS = m("fleet.robots", "gauge")
QUEUE = m("fleet.task.queue.depth", "gauge")
BATTERY = m("robot.battery.level", "gauge", ROBOT)
TEMP = m("robot.cpu.temperature", "gauge", ROBOT)
DISTANCE = m("robot.distance.travelled", "sum", ROBOT)
HANDLED = m("robot.commands.handled", "sum", f"{ROBOT}, {TASK}")
STEP_DUR = m("robot.step.duration", "histogram", f"{ROBOT}, {TASK}")

# ROS 2 middleware statistics from ros2ebpf, scraped by the collector
# (collector/ros2ebpf.yaml stamps service.namespace and robot.id on them).
NS = 'service_namespace = "robotics"'
ROS_PUB = m("ros2_rmw_publish_total", "sum", f"{NS}, {ROBOT}")
ROS_TAKE = m("ros2_rmw_take_total", "sum", f"{NS}, {ROBOT}")
ROS_REQ = m("ros2_rmw_service_request_total", "sum", f'{NS}, side = "server", {ROBOT}')
ROS_RESP = m("ros2_rmw_service_response_total", "sum", f'{NS}, side = "server", {ROBOT}')
ROS_PERIOD = m("ros2_rmw_publish_period_seconds", "histogram", f"{NS}, {ROBOT}")
ROS_PROC = m("ros2_process_info", "gauge", f"{NS}, {ROBOT}")
EBPF_ATTACHED = m("ros2ebpf_probes_attached", "gauge", NS)
EBPF_CONFIGURED = m("ros2ebpf_probes_configured", "gauge", NS)
EBPF_DROPPED = m("ros2ebpf_events_dropped_total", "sum", NS)
EBPF_UNRESOLVED = m("ros2ebpf_unresolved_endpoints", "gauge", NS)


def fmt(unit: str = "decimal", decimals: int = 0) -> dict:
    return {"unit": unit, "decimalPlaces": decimals, "shortValues": True}


def query(promql: str, legend: str = "") -> dict:
    return {
        "kind": "TimeSeriesQuery",
        "spec": {
            "plugin": {
                "kind": "PrometheusTimeSeriesQuery",
                "spec": {"query": promql, "seriesNameFormat": legend},
            }
        },
    }


def panel(name: str, description: str, plugin: dict, queries: list[dict]) -> dict:
    return {
        "kind": "Panel",
        "spec": {"display": {"name": name, "description": description}, "plugin": plugin, "queries": queries},
    }


def stat(name, description, promql, unit="decimal", decimals=0):
    plugin = {"kind": "StatChart", "spec": {"calculation": "last-number", "format": fmt(unit, decimals)}}
    return panel(name, description, plugin, [query(promql)])


def timeseries(name, description, queries, unit="decimal", decimals=0, display="line", stack=False, area=0.0):
    plugin = {
        "kind": "TimeSeriesChart",
        "spec": {
            "legend": {"mode": "list", "position": "bottom", "showLegend": True},
            "visual": {
                "display": display,
                "lineWidth": 1.5,
                "areaOpacity": area,
                "connectNulls": True,
                "pointRadius": 0,
                "stack": "all" if stack else "none",
                "palette": {"mode": "categorical"},
            },
            "yAxis": {"show": True, "min": 0, "format": fmt(unit, decimals)},
        },
    }
    return panel(name, description, plugin, queries)


def table(name, description, promql, legend):
    plugin = {"kind": "TimeSeriesTable", "spec": {"format": fmt()}}
    return panel(name, description, plugin, [query(promql, legend)])


def p95(selector: str, by: str, window: str = "$__rate_interval") -> str:
    group = f"le, {by}" if by else "le"
    return f"histogram_quantile(0.95, sum by ({group}) (increase({selector}[{window}])))"


panels = {
    # Overview
    "commandsPerMin": stat(
        "Commands / min", "Commands dispatched to robots per minute",
        f"sum(rate({SENT}[$__range])) * 60", decimals=1,
    ),
    "failureRate": stat(
        "Command failure rate", "Failed or timed-out commands as a share of commands sent",
        f"100 * sum(increase({FAILED}[$__range])) / sum(increase({SENT}[$__range]))", unit="percent", decimals=1,
    ),
    "taskP95": stat(
        "Task duration p95", "95th percentile end-to-end task duration",
        p95(TASK_DUR, "", "$__range"), unit="seconds", decimals=2,
    ),
    "robotsWorking": stat(
        "Robots working", "Robots currently busy, charging or updating",
        "sum(" + m("fleet.robots", "gauge", 'robot_status =~ "busy|charging|updating"') + ")",
    ),
    "robotsEstopped": stat(
        "Robots e-stopped", "Robots halted by an emergency stop",
        "sum(" + m("fleet.robots", "gauge", 'robot_status = "estopped"') + ")",
    ),
    "avgBattery": stat(
        "Average battery", "Mean battery level across the fleet",
        f"avg({BATTERY})", unit="percent", decimals=0,
    ),
    # Commands
    "commandsByTask": timeseries(
        "Commands sent by task", "Commands dispatched per interval, by task type",
        [query(f"sum by (fleet_task_type) (increase({SENT}[$__rate_interval]))", "{{fleet_task_type}}")],
        display="bar", stack=True,
    ),
    "failuresByError": timeseries(
        "Failures by error type", "Failed commands per interval, by error.type",
        [query(f"sum by (error_type) (increase({FAILED}[$__rate_interval]))", "{{error_type}}")],
        display="bar", stack=True,
    ),
    "taskDurationByTask": timeseries(
        "Task duration p95 by task", "95th percentile task duration as seen by the controller",
        [query(p95(TASK_DUR, "fleet_task_type"), "{{fleet_task_type}}")],
        unit="seconds", decimals=2,
    ),
    "commandLatency": timeseries(
        "Command round-trip latency", "Network round trip between controller and robot",
        [
            query(f"histogram_quantile(0.5, sum by (le) (increase({LATENCY}[$__rate_interval])))", "p50"),
            query(p95(LATENCY, ""), "p95"),
        ],
        unit="seconds", decimals=3,
    ),
    # Fleet state
    "robotsByStatus": timeseries(
        "Robots by status", "Number of robots in each status",
        [query(f"sum by (robot_status) ({ROBOTS})", "{{robot_status}}")],
        stack=True, area=0.6,
    ),
    "queueDepth": timeseries(
        "Task queue depth", "Tasks waiting to be scheduled",
        [query(f"max({QUEUE})", "queue")],
        area=0.2,
    ),
    # Robot health
    "battery": timeseries(
        "Battery level", "State of charge per robot",
        [query(f"max by (robot_id) ({BATTERY})", "{{robot_id}}")],
        unit="percent",
    ),
    "temperature": timeseries(
        "CPU temperature (°C)", "Onboard computer temperature per robot",
        [query(f"max by (robot_id) ({TEMP})", "{{robot_id}}")],
        decimals=1,
    ),
    "distance": timeseries(
        "Distance travelled (m)", "Metres driven per interval, per robot",
        [query(f"sum by (robot_id) (increase({DISTANCE}[$__rate_interval]))", "{{robot_id}}")],
        display="bar", stack=True,
    ),
    "stepDuration": timeseries(
        "Step duration p95 by step", "95th percentile duration of each task step on the robot",
        [query(p95(STEP_DUR, "robot_step"), "{{robot_step}}")],
        unit="seconds", decimals=2,
    ),
    "handledByRobot": table(
        "Commands handled by robot", "Commands executed per robot and outcome over the selected time range",
        f"sum by (robot_id, outcome) (increase({HANDLED}[$__range]))", "{{robot_id}} {{outcome}}",
    ),
    # ROS 2 middleware (ros2ebpf)
    "rosProcesses": stat(
        "ROS 2 processes", "ROS 2 processes with publishers, subscriptions or services",
        f"count({ROS_PROC})",
    ),
    "rosTopics": stat(
        "Topics publishing", "Topics with at least one publish over the selected time range",
        f"count(sum by (robot_id, topic) (rate({ROS_PUB}[$__range])) > 0)",
    ),
    "rosPublishRate": stat(
        "Messages published / s", "rmw_publish calls per second across all topics",
        f"sum(rate({ROS_PUB}[$__range]))", decimals=1,
    ),
    "rosDelivery": stat(
        "Takes per publish", "Messages taken by subscriptions per message published; "
        "100% means every message was taken once, more means several subscribers",
        f"100 * sum(rate({ROS_TAKE}[$__range])) / sum(rate({ROS_PUB}[$__range]))", unit="percent",
    ),
    "rosPublishByTopic": timeseries(
        "Publish rate by topic", "Messages published per second, per topic and publishing process",
        [query(f"sum by (topic, comm) (rate({ROS_PUB}[$__rate_interval]))", "{{topic}} ({{comm}})")],
        decimals=2,
    ),
    "rosTakeByTopic": timeseries(
        "Take rate by topic", "Messages taken per second, per topic and subscribing process",
        [query(f"sum by (topic, comm) (rate({ROS_TAKE}[$__rate_interval]))", "{{topic}} ({{comm}})")],
        decimals=2,
    ),
    "rosServices": timeseries(
        "Service requests vs responses", "Server side: requests taken and responses sent per second. "
        "Responses falling behind requests means calls go unanswered",
        [
            query(f"sum by (service) (rate({ROS_REQ}[$__rate_interval]))", "{{service}} requests"),
            query(f"sum by (service) (rate({ROS_RESP}[$__rate_interval]))", "{{service}} responses"),
        ],
        decimals=2,
    ),
    "rosJitter": timeseries(
        "Publish interval p99 by topic", "99th percentile time between consecutive publishes: "
        "spikes are jitter or stalls in the publishing node",
        [query(f"histogram_quantile(0.99, sum by (le, topic) (increase({ROS_PERIOD}[$__rate_interval])))",
               "{{topic}}")],
        unit="seconds", decimals=3,
    ),
    "rosProcessTable": table(
        "ROS 2 processes", "Processes seen by ros2ebpf, with their RMW, container and ROS_DOMAIN_ID",
        f"max by (robot_id, comm, rmw, container, domain_id) ({ROS_PROC})",
        "{{robot_id}} {{comm}} {{rmw}} domain {{domain_id}} {{container}}",
    ),
    # ros2ebpf health
    "ebpfProbes": timeseries(
        "Probes attached vs configured", "eBPF probe links per host; attached below configured "
        "means some ROS 2 calls are not being counted",
        [
            query(f"max by (host) ({EBPF_ATTACHED})", "{{host}} attached"),
            query(f"max by (host) ({EBPF_CONFIGURED})", "{{host}} configured"),
        ],
    ),
    "ebpfDropped": timeseries(
        "Events dropped", "Ring buffer overflows: affected processes stay unresolved until the next GC",
        [query(f"sum by (host) (increase({EBPF_DROPPED}[$__rate_interval]))", "{{host}}")],
        display="bar",
    ),
    "ebpfUnresolved": timeseries(
        "Unresolved endpoints", "Endpoints skipped at scrape time because their process identity is pending",
        [query(f"max by (host) ({EBPF_UNRESOLVED})", "{{host}}")],
    ),
}


def grid(title: str, items: list[tuple[str, int, int, int, int]]) -> dict:
    return {
        "kind": "Grid",
        "spec": {
            "display": {"title": title, "collapse": {"open": True}},
            "items": [
                {"x": x, "y": y, "width": w, "height": h, "content": {"$ref": f"#/spec/panels/{key}"}}
                for key, x, y, w, h in items
            ],
        },
    }


layouts = [
    grid("Overview", [(k, i * 4, 0, 4, 4) for i, k in enumerate(
        ["commandsPerMin", "failureRate", "taskP95", "robotsWorking", "robotsEstopped", "avgBattery"])]),
    grid("Commands", [
        ("commandsByTask", 0, 0, 12, 8), ("failuresByError", 12, 0, 12, 8),
        ("taskDurationByTask", 0, 8, 12, 8), ("commandLatency", 12, 8, 12, 8),
    ]),
    grid("Fleet state", [("robotsByStatus", 0, 0, 16, 8), ("queueDepth", 16, 0, 8, 8)]),
    grid("Robot health", [
        ("battery", 0, 0, 12, 8), ("temperature", 12, 0, 12, 8),
        ("distance", 0, 8, 12, 8), ("stepDuration", 12, 8, 12, 8),
        ("handledByRobot", 0, 16, 24, 8),
    ]),
    grid("ROS 2 middleware", [
        ("rosProcesses", 0, 0, 6, 4), ("rosTopics", 6, 0, 6, 4),
        ("rosPublishRate", 12, 0, 6, 4), ("rosDelivery", 18, 0, 6, 4),
        ("rosPublishByTopic", 0, 4, 12, 8), ("rosTakeByTopic", 12, 4, 12, 8),
        ("rosServices", 0, 12, 12, 8), ("rosJitter", 12, 12, 12, 8),
        ("rosProcessTable", 0, 20, 24, 8),
    ]),
    grid("ros2ebpf health", [
        ("ebpfProbes", 0, 0, 8, 8), ("ebpfDropped", 8, 0, 8, 8), ("ebpfUnresolved", 16, 0, 8, 8),
    ]),
]


def filter_var(key: str, label: str) -> dict:
    return {
        "kind": "Dash0FilterVariable",
        "spec": {
            "name": key, "attributeKey": key, "operator": "is_any", "values": [], "dependsOn": [],
            "capturingRegexp": "", "display": {"name": label, "description": "", "hidden": False},
        },
    }


def build() -> dict:
    return {
        "kind": "Dashboard",
        # dash0Extensions.id makes `dash0 apply` update this dashboard in place
        # instead of creating a new copy on every run.
        "metadata": {"name": "robot-fleet-simulator", "dash0Extensions": {"id": "robot-fleet-simulator"}},
        "spec": {
            "display": {
                "name": "Robot Fleet Simulator",
                "description": "Fleet controller commands, failures and robot health from fleet-sim, "
                "plus ROS 2 middleware statistics from ros2ebpf",
            },
            "duration": "30m",
            "variables": [filter_var("robot_id", "Robot"), filter_var("fleet_task_type", "Task type")],
            "panels": panels,
            "layouts": layouts,
        },
    }


def main() -> None:
    out = Path(__file__).with_name("fleet-sim.yaml")
    # JSON is valid YAML, so no YAML dependency is needed.
    out.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

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
]


def filter_var(key: str, label: str) -> dict:
    return {
        "kind": "Dash0FilterVariable",
        "spec": {
            "name": key, "attributeKey": key, "operator": "is_any", "values": [], "dependsOn": [],
            "capturingRegexp": "", "display": {"name": label, "description": "", "hidden": False},
        },
    }


dashboard = {
    "kind": "Dashboard",
    "metadata": {"name": "robot-fleet-simulator"},
    "spec": {
        "display": {
            "name": "Robot Fleet Simulator",
            "description": "Fleet controller commands, failures and robot health from fleet-sim",
        },
        "duration": "30m",
        "variables": [filter_var("robot_id", "Robot"), filter_var("fleet_task_type", "Task type")],
        "panels": panels,
        "layouts": layouts,
    },
}

out = Path(__file__).with_name("fleet-sim.yaml")
# JSON is valid YAML, so no YAML dependency is needed.
out.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n")
print(f"wrote {out}")

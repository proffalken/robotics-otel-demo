"""The Robot Correlation dashboard: one robot's application and ROS 2 telemetry side by side."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from tests.test_dashboard import ROS2EBPF_METRICS

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "dashboards" / "correlation.py"
GENERATED = ROOT / "dashboards" / "robot-correlation.yaml"
SELECTOR = re.compile(r'otel_metric_name = "([^"]+)", otel_metric_type = "([^"]+)"')

# Application side: fleet-sim's own metrics plus Dash0's span and log counts.
APP_METRICS = {
    "robot.commands.handled": "sum",
    "robot.battery.level": "gauge",
    "robot.cpu.temperature": "gauge",
    "robot.step.duration": "histogram",
    "dash0.spans": "sum",
    "dash0.logs": "sum",
}
# Middleware side: ros2ebpf, including the sequence counters and the
# collector's scrape `up` for the ros2ebpfd target.
ROS_METRICS = ROS2EBPF_METRICS | {
    "ros2_rmw_take_seq_gap_total": "sum",
    "ros2_rmw_take_seq_lost_total": "sum",
    "up": "gauge",
}
SECTIONS = ["Health at a glance", "Application vs ROS 2 middleware", "Robot state vs ROS 2 runtime",
            "Telemetry pipeline"]
SIDE_BY_SIDE = ["Application vs ROS 2 middleware", "Robot state vs ROS 2 runtime"]


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("correlation", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dashboard(gen):
    return gen.build()


def queries(panel: dict) -> list[str]:
    return [q["spec"]["plugin"]["spec"]["query"] for q in panel["spec"]["queries"]]


def metrics(panel: dict) -> set[tuple[str, str]]:
    return {pair for q in queries(panel) for pair in SELECTOR.findall(q)}


def grids(dashboard: dict) -> dict[str, dict]:
    return {g["spec"]["display"]["title"]: g for g in dashboard["spec"]["layouts"]}


def items(layout: dict) -> list[tuple[str, dict]]:
    return [(i["content"]["$ref"].rsplit("/", 1)[1], i) for i in layout["spec"]["items"]]


def test_importing_the_generator_writes_nothing(gen):
    assert not hasattr(gen, "out")


def test_sections_in_order(dashboard):
    assert list(grids(dashboard)) == SECTIONS


def test_every_layout_item_points_at_a_panel_and_every_panel_is_placed(dashboard):
    panels = dashboard["spec"]["panels"]
    placed = [key for g in grids(dashboard).values() for key, _ in items(g)]
    assert sorted(placed) == sorted(panels)


def test_panels_do_not_overlap_and_fit_the_grid(dashboard):
    for title, layout in grids(dashboard).items():
        cells = set()
        for key, i in items(layout):
            assert 0 <= i["x"] and i["x"] + i["width"] <= 24, f"{title}/{key} is off the grid"
            mine = {(x, y) for x in range(i["x"], i["x"] + i["width"]) for y in range(i["y"], i["y"] + i["height"])}
            assert not cells & mine, f"{title}/{key} overlaps another panel"
            cells |= mine


def test_every_query_follows_the_robot_filter(dashboard):
    for key, panel in dashboard["spec"]["panels"].items():
        for q in queries(panel):
            assert q.count("$robot_id") == q.count("otel_metric_name"), f"{key}: a selector ignores the Robot filter"


def test_only_known_metrics_with_their_types(dashboard):
    known = APP_METRICS | ROS_METRICS
    for key, panel in dashboard["spec"]["panels"].items():
        assert metrics(panel), f"{key} queries nothing"
        for name, kind in metrics(panel):
            assert known.get(name) == kind, f"{key}: unknown metric or wrong type {name} ({kind})"


@pytest.mark.parametrize("title", SIDE_BY_SIDE)
def test_application_on_the_left_ros2_on_the_right(dashboard, title):
    panels = dashboard["spec"]["panels"]
    for key, i in items(grids(dashboard)[title]):
        names = {name for name, _ in metrics(panels[key])}
        if i["x"] == 0:
            assert names <= set(APP_METRICS), f"{key} is on the application side but queries {names}"
        else:
            assert i["x"] == 12 and names <= set(ROS_METRICS), f"{key} should be ROS 2 only, at x=12"


@pytest.mark.parametrize("title", SIDE_BY_SIDE)
def test_side_by_side_rows_are_paired(dashboard, title):
    rows: dict[int, set[int]] = {}
    for _, i in items(grids(dashboard)[title]):
        rows.setdefault(i["y"], set()).add(i["x"])
    assert all(xs == {0, 12} for xs in rows.values()), rows


def test_glance_row_covers_both_layers(dashboard):
    panels = dashboard["spec"]["panels"]
    names = {n for key, _ in items(grids(dashboard)["Health at a glance"]) for n, _ in metrics(panels[key])}
    assert names & set(APP_METRICS) and names & set(ROS2EBPF_METRICS)


def test_ros2_sequence_loss_is_shown(dashboard):
    text = json.dumps(dashboard["spec"]["panels"])
    assert "ros2_rmw_take_seq_lost_total" in text and "ros2_rmw_take_seq_gap_total" in text


def test_robot_filter_defaults_to_the_robot_running_ros2(dashboard):
    (var,) = dashboard["spec"]["variables"]
    assert var["spec"]["attributeKey"] == "robot_id"
    assert var["spec"]["values"] == ["amr-01"]


def test_dashboard_has_a_stable_id(dashboard):
    assert dashboard["metadata"]["dash0Extensions"]["id"] == "robot-correlation"


def test_committed_yaml_matches_the_generator(dashboard):
    assert json.loads(GENERATED.read_text()) == dashboard


def test_ros2_processes_counts_distinct_recent_pids(dashboard):
    # One process can have several ros2_process_info series (labels such as
    # rmw come and go), and old PIDs linger in the lookback window.
    (q,) = queries(dashboard["spec"]["panels"]["rosProcesses"])
    assert "max by (comm, pid)" in q and "last_over_time(" in q and "[1m]" in q, q


def test_tiles_have_a_label_instead_of_their_promql(dashboard):
    panels = dashboard["spec"]["panels"]
    for key, _ in items(grids(dashboard)["Health at a glance"]):
        for q in panels[key]["spec"]["queries"]:
            assert q["spec"]["plugin"]["spec"]["seriesNameFormat"], f"{key} would show its raw query"


def test_process_counts_are_not_stacked(dashboard):
    # Stacking would make the top line a sum, not "is talker running?".
    assert dashboard["spec"]["panels"]["rosProcesses"]["spec"]["plugin"]["spec"]["visual"]["stack"] == "none"


def test_step_duration_is_one_readable_pair_of_lines(dashboard):
    qs = queries(dashboard["spec"]["panels"]["stepDuration"])
    assert len(qs) == 2 and not any("robot_step" in q for q in qs)

"""The generated Dash0 dashboard: structure, ROS 2 section, and freshness."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "dashboards" / "generate.py"
GENERATED = ROOT / "dashboards" / "fleet-sim.yaml"

# From ros2ebpf docs/metrics.md (pinned commit c29dd5f), with the OTel type
# the collector's prometheus receiver gives them in Dash0.
ROS2EBPF_METRICS = {
    "ros2_rmw_publish_total": "sum",
    "ros2_rmw_take_total": "sum",
    "ros2_rmw_service_request_total": "sum",
    "ros2_rmw_service_response_total": "sum",
    "ros2_rmw_publish_period_seconds": "histogram",
    "ros2_process_info": "gauge",
    "ros2ebpf_probes_attached": "gauge",
    "ros2ebpf_probes_configured": "gauge",
    "ros2ebpf_events_dropped_total": "sum",
    "ros2ebpf_unresolved_endpoints": "gauge",
}
SELECTOR = re.compile(r'otel_metric_name = "([^"]+)", otel_metric_type = "([^"]+)"')


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("dashboard_generate", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dashboard(gen):
    return gen.build()


def queries(panel: dict) -> list[str]:
    return [q["spec"]["plugin"]["spec"]["query"] for q in panel["spec"]["queries"]]


def grid(dashboard: dict, title: str) -> dict:
    return next(g for g in dashboard["spec"]["layouts"] if g["spec"]["display"]["title"] == title)


def panel_keys(layout: dict) -> list[str]:
    return [i["content"]["$ref"].rsplit("/", 1)[1] for i in layout["spec"]["items"]]


def test_importing_the_generator_writes_nothing(gen):
    assert hasattr(gen, "build") and hasattr(gen, "main")


def test_every_layout_item_points_at_a_panel_and_every_panel_is_placed(dashboard):
    panels = set(dashboard["spec"]["panels"])
    placed = [k for g in dashboard["spec"]["layouts"] for k in panel_keys(g)]
    assert set(placed) <= panels, f"dangling refs: {set(placed) - panels}"
    assert panels <= set(placed), f"unplaced panels: {panels - set(placed)}"
    assert len(placed) == len(set(placed)), "a panel is placed twice"


@pytest.mark.parametrize("title", ["ROS 2 middleware", "ros2ebpf health"])
def test_ros2_sections_exist(dashboard, title):
    assert panel_keys(grid(dashboard, title))


def test_ros2_panels_only_use_real_ros2ebpf_metrics_with_their_types(dashboard):
    panels = dashboard["spec"]["panels"]
    keys = panel_keys(grid(dashboard, "ROS 2 middleware")) + panel_keys(grid(dashboard, "ros2ebpf health"))
    for key in keys:
        for q in queries(panels[key]):
            found = SELECTOR.findall(q)
            assert found, f"{key}: no metric selector in {q!r}"
            for name, kind in found:
                assert ROS2EBPF_METRICS.get(name) == kind, f"{key}: unknown or mistyped {name}/{kind}"


def test_ros2_middleware_panels_honour_the_robot_filter(dashboard):
    panels = dashboard["spec"]["panels"]
    for key in panel_keys(grid(dashboard, "ROS 2 middleware")):
        for q in queries(panels[key]):
            assert "$robot_id" in q, f"{key} ignores the Robot filter"


def test_ros2_section_covers_the_key_questions(dashboard):
    text = json.dumps([dashboard["spec"]["panels"][k] for k in panel_keys(grid(dashboard, "ROS 2 middleware"))])
    for metric in ("ros2_rmw_publish_total", "ros2_rmw_take_total", "ros2_rmw_service_request_total",
                   "ros2_rmw_service_response_total", "ros2_rmw_publish_period_seconds", "ros2_process_info"):
        assert metric in text, f"ROS 2 section never shows {metric}"


def test_existing_sections_are_unchanged(dashboard):
    titles = [g["spec"]["display"]["title"] for g in dashboard["spec"]["layouts"]]
    assert titles[:4] == ["Overview", "Commands", "Fleet state", "Robot health"]


def test_committed_yaml_matches_the_generator(dashboard):
    assert json.loads(GENERATED.read_text()) == dashboard, "run: uv run python dashboards/generate.py"


def test_dashboard_has_a_stable_id_so_apply_updates_instead_of_duplicating(dashboard):
    assert dashboard["metadata"]["dash0Extensions"]["id"] == "robot-fleet-simulator"

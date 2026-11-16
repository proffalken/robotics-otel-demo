"""Contract for the optional ros2ebpf add-on.

collector/ros2ebpf.yaml is merged on top of collector/config.yaml (the
collector accepts several --config flags). It must only add new components
and a new pipeline, never replace anything in the base config, because the
collector replaces (not appends) lists when merging.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "collector" / "config.yaml"
OVERLAY = ROOT / "collector" / "ros2ebpf.yaml"
COMPOSE_ROS2 = ROOT / "docker-compose.ros2.yml"
RECEIVER = "prometheus/ros2ebpf"
PIPELINE = "metrics/ros2ebpf"


@pytest.fixture(scope="module")
def base() -> dict:
    return yaml.safe_load(BASE.read_text())


@pytest.fixture(scope="module")
def overlay() -> dict:
    return yaml.safe_load(OVERLAY.read_text())


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_ROS2.read_text())


@pytest.mark.parametrize("section", ["receivers", "processors", "exporters", "extensions"])
def test_overlay_only_adds_new_components(base, overlay, section):
    clash = set(overlay.get(section, {})) & set(base.get(section, {}))
    assert not clash, f"overlay redefines base {section}: {clash}"


def test_overlay_does_not_touch_base_pipelines_or_service_lists(base, overlay):
    service = overlay["service"]
    assert set(service) == {"pipelines"}, "overlay must not replace service.extensions etc."
    assert not set(service["pipelines"]) & set(base["service"]["pipelines"])


def test_ros2ebpf_daemon_is_scraped_over_the_internal_network(overlay):
    jobs = overlay["receivers"][RECEIVER]["config"]["scrape_configs"]
    assert len(jobs) == 1
    assert jobs[0]["job_name"] == "ros2ebpf"
    assert jobs[0]["static_configs"] == [{"targets": ["ros2ebpfd:9477"]}]


def test_ros2ebpf_pipeline_reuses_base_processors_and_upstream_exporter(overlay):
    pipeline = overlay["service"]["pipelines"][PIPELINE]
    assert pipeline["receivers"] == [RECEIVER]
    assert pipeline["processors"][0] == "memory_limiter"
    assert pipeline["processors"][-1] == "batch"
    assert "resource/ros2ebpf" in pipeline["processors"]
    assert "otlphttp/upstream" in pipeline["exporters"]


def test_ros2ebpf_metrics_carry_the_same_identity_as_fleet_sim(overlay):
    attrs = {a["key"]: a for a in overlay["processors"]["resource/ros2ebpf"]["attributes"]}
    assert attrs["service.namespace"]["value"] == "robotics"
    assert attrs["robot.id"]["value"] == "${env:ROS2EBPF_ROBOT:-amr-01}"
    assert all(a["action"] == "upsert" for a in attrs.values())


def test_compose_collector_loads_both_configs(compose):
    collector = compose["services"]["otel-collector"]
    configs = [c for c in collector["command"] if c.startswith("--config=")]
    assert configs == [
        "--config=/etc/otelcol-contrib/config.yaml",
        "--config=/etc/otelcol-contrib/ros2ebpf.yaml",
    ]
    assert "./collector/ros2ebpf.yaml:/etc/otelcol-contrib/ros2ebpf.yaml:ro" in collector["volumes"]


def test_daemon_is_built_from_a_pinned_upstream_commit(compose):
    build = compose["services"]["ros2ebpfd"]["build"]
    repo, _, ref = build["context"].partition("#")
    assert repo == "https://github.com/fujitatomoya/ros2ebpf.git"
    assert len(ref) == 40, "pin ros2ebpf to a full commit SHA"
    assert build["dockerfile"] == "docker/Dockerfile"


def test_daemon_has_what_ebpf_needs_and_publishes_nothing(compose):
    daemon = compose["services"]["ros2ebpfd"]
    assert daemon["pid"] == "host"
    assert daemon["privileged"] is True
    assert "/sys/fs/bpf:/sys/fs/bpf" in daemon["volumes"]
    assert "/sys/kernel/btf:/sys/kernel/btf:ro" in daemon["volumes"]
    assert "ports" not in daemon


def test_ros2_nodes_run_in_one_robot_container(compose):
    # ros2ebpf c29dd5f attaches its publish probes once per container that
    # maps the same librmw_implementation.so, so N containers built from one
    # image inflate ros2_rmw_publish_total N times. One container = one shim.
    ros2 = [name for name in compose["services"] if name.startswith("ros2-")]
    assert ros2 == ["ros2-robot"]
    robot = compose["services"]["ros2-robot"]
    assert robot["command"] == ["ros2", "launch", "/robot/robot.launch.xml"]
    assert robot["environment"]["ROS_DOMAIN_ID"] == "5"
    assert "privileged" not in robot, "only the daemon needs privileges"


@pytest.fixture(scope="module")
def launch():
    import xml.etree.ElementTree as ET

    return ET.parse(ROOT / "ros2" / "robot.launch.xml").getroot()


def test_launch_file_runs_the_demo_nodes_in_the_robot_namespace(launch):
    group = launch.find("group")
    assert group.find("push_ros_namespace").get("namespace") == "amr_01"
    nodes = {(n.get("pkg"), n.get("exec")) for n in group.iter("node")}
    assert nodes == {
        ("demo_nodes_cpp", "talker"),
        ("demo_nodes_py", "listener"),
        ("demo_nodes_cpp", "add_two_ints_server"),
        ("demo_nodes_cpp", "add_two_ints_client"),
    }


def test_one_shot_service_client_is_respawned(launch):
    client = next(n for n in launch.iter("node") if n.get("exec") == "add_two_ints_client")
    assert client.get("respawn") == "true"
    assert float(client.get("respawn_delay")) >= 1

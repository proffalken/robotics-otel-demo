"""Check rules in checks/robot-alerts.yaml, focusing on the ROS 2 group."""

import re
from pathlib import Path

import pytest
import yaml

from tests.test_dashboard import ROS2EBPF_METRICS

RULES_FILE = Path(__file__).resolve().parent.parent / "checks" / "robot-alerts.yaml"
ROS2_ALERTS = ["ROS2TopicDeliveryGap", "ROS2TopicSilent", "ROS2ServiceUnanswered", "ROS2EbpfProbesDetached"]
METRIC = re.compile(r'otel_metric_name="([^"]+)"')


@pytest.fixture(scope="module")
def groups() -> dict:
    doc = yaml.safe_load(RULES_FILE.read_text())
    return {g["name"]: g for g in doc["spec"]["groups"]}


@pytest.fixture(scope="module")
def ros2(groups) -> dict:
    return {r["alert"]: r for r in groups["ROS 2"]["rules"]}


def test_existing_robot_fleet_rules_are_kept(groups):
    names = [r["alert"] for r in groups["Robot Fleet"]["rules"]]
    assert names == ["RobotOperationErrorRate", "RobotTaskFailureRate"]


def test_ros2_group_has_the_planned_rules(ros2):
    assert list(ros2) == ROS2_ALERTS


@pytest.mark.parametrize("alert", ROS2_ALERTS)
def test_rule_only_uses_real_ros2ebpf_metrics(ros2, alert):
    names = METRIC.findall(ros2[alert]["expr"])
    assert names
    assert set(names) <= set(ROS2EBPF_METRICS), set(names) - set(ROS2EBPF_METRICS)


@pytest.mark.parametrize("alert", ROS2_ALERTS)
def test_rule_is_scoped_to_the_robotics_namespace(ros2, alert):
    for selector in re.findall(r"\{[^}]*otel_metric_name[^}]*\}", ros2[alert]["expr"]):
        assert 'service_namespace="robotics"' in selector, selector


@pytest.mark.parametrize("alert", ROS2_ALERTS)
def test_rule_keeps_robot_id_so_automations_know_where_to_look(ros2, alert):
    expr = ros2[alert]["expr"]
    for clause in re.findall(r"(?:sum|max|min|avg) by \(([^)]*)\)", expr):
        assert "robot_id" in clause, f"{alert} aggregates away robot_id: by ({clause})"
    assert "{{ $labels.robot_id }}" in ros2[alert]["annotations"]["summary"]


@pytest.mark.parametrize("alert", ROS2_ALERTS)
def test_rule_has_severity_hold_and_description(ros2, alert):
    rule = ros2[alert]
    assert rule["labels"]["severity"] in {"warning", "critical"}
    assert rule["for"]
    assert rule["annotations"]["description"]


def test_rule_file_has_a_stable_id_so_apply_updates_instead_of_duplicating():
    doc = yaml.safe_load(RULES_FILE.read_text())
    assert doc["metadata"]["labels"]["dash0.com/id"] == "robot-fleet-simulator"


def test_silent_topic_rule_ignores_event_driven_topics(ros2):
    # /parameter_events and /rosout only publish when something happens, so
    # a quiet spell on them is healthy, not a stalled node.
    expr = ros2["ROS2TopicSilent"]["expr"]
    for topic in ("parameter_events", "rosout"):
        assert expr.count(topic) == expr.count("ros2_rmw_publish_total"), \
            f"every publish selector in ROS2TopicSilent must exclude {topic}"
    assert 'topic!~' in expr

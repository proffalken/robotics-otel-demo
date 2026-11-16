import random

import pytest
from opentelemetry.trace import SpanKind, StatusCode

from fleet_sim.controller import FleetController
from fleet_sim.model import Robot, robot_service_name
from fleet_sim.robot import RobotAgent
from fleet_sim.telemetry import make_service_telemetry

from .conftest import no_sleep


def build(captures, *, fault_scale: float, comms_timeout_rate: float = 0.0, seed: int = 7):
    rng = random.Random(seed)
    robot = Robot("amr-01", "AMR-500", "zone-a", battery=90.0)
    ctrl_cap = captures("fleet-controller", "controller-1")
    robot_cap = captures(robot_service_name(robot.robot_id), robot.robot_id)
    agent = RobotAgent(robot, robot_cap.telemetry, rng=rng, sleep=no_sleep, fault_scale=fault_scale)
    controller = FleetController(
        {robot.robot_id: agent},
        ctrl_cap.telemetry,
        rng=rng,
        sleep=no_sleep,
        comms_timeout_rate=comms_timeout_rate,
    )
    return controller, robot, ctrl_cap, robot_cap


def test_successful_task_produces_one_trace_across_both_services(captures):
    controller, robot, ctrl_cap, robot_cap = build(captures, fault_scale=0.0)

    result = controller.run_task(robot.robot_id, "move_to_waypoint")
    assert result.ok

    ctrl_spans = ctrl_cap.spans.get_finished_spans()
    robot_spans = robot_cap.spans.get_finished_spans()

    root = next(s for s in ctrl_spans if s.parent is None)
    client = next(s for s in ctrl_spans if s.kind == SpanKind.CLIENT)
    server = next(s for s in robot_spans if s.kind == SpanKind.SERVER)

    assert root.name == "schedule move_to_waypoint"
    # RPC semconv: CLIENT and SERVER spans are named after rpc.method
    assert client.name == "move_to_waypoint"
    assert server.name == "move_to_waypoint"

    trace_ids = {s.context.trace_id for s in ctrl_spans + robot_spans}
    assert trace_ids == {root.context.trace_id}
    assert server.parent.span_id == client.context.span_id
    assert server.resource.attributes["service.name"] == "robot-amr-01"
    assert server.resource.attributes["service.instance.id"] == "amr-01"
    assert root.attributes["robot.id"] == "amr-01"
    # Step spans under the robot's server span
    assert len([s for s in robot_spans if s.kind == SpanKind.INTERNAL]) >= 2
    assert root.status.status_code != StatusCode.ERROR


def test_robot_fault_marks_spans_as_errors_and_logs_it(captures):
    controller, robot, ctrl_cap, robot_cap = build(captures, fault_scale=1000.0)

    result = controller.run_task(robot.robot_id, "pick_item")
    assert not result.ok
    assert result.error_type

    server = next(s for s in robot_cap.spans.get_finished_spans() if s.kind == SpanKind.SERVER)
    root = next(s for s in ctrl_cap.spans.get_finished_spans() if s.parent is None)
    assert server.status.status_code == StatusCode.ERROR
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["error.type"] == result.error_type
    assert any(e.name == "exception" for s in robot_cap.spans.get_finished_spans() for e in s.events)

    error_logs = [
        r for r in robot_cap.logs.get_finished_logs() if r.log_record.severity_text in ("ERROR", "WARN", "WARNING")
    ]
    assert error_logs
    assert error_logs[0].log_record.trace_id == root.context.trace_id


def test_comms_timeout_is_retried_with_a_second_dispatch(captures):
    controller, robot, ctrl_cap, _ = build(captures, fault_scale=0.0, comms_timeout_rate=1.0)

    result = controller.run_task(robot.robot_id, "status_ping")
    assert not result.ok
    assert result.error_type == "comms_timeout"
    clients = [s for s in ctrl_cap.spans.get_finished_spans() if s.kind == SpanKind.CLIENT]
    assert len(clients) == 2


def test_metrics_are_recorded_for_a_task(captures):
    controller, robot, ctrl_cap, robot_cap = build(captures, fault_scale=0.0)
    controller.run_task(robot.robot_id, "move_to_waypoint")

    sent = ctrl_cap.metric_points("fleet.commands.sent")
    assert sum(p.value for p in sent) == 1
    durations = ctrl_cap.metric_points("fleet.task.duration")
    assert sum(p.count for p in durations) == 1
    assert robot_cap.metric_points("robot.battery.level")
    assert robot_cap.metric_points("robot.distance.travelled")
    assert ctrl_cap.metric_points("fleet.robots")


def test_failed_task_counts_a_failure_metric(captures):
    controller, robot, ctrl_cap, _ = build(captures, fault_scale=1000.0)
    result = controller.run_task(robot.robot_id, "pick_item")

    failed = ctrl_cap.metric_points("fleet.commands.failed")
    assert sum(p.value for p in failed) == 1
    assert failed[0].attributes["error.type"] == result.error_type


def test_charge_task_restores_battery(captures):
    controller, robot, _, _ = build(captures, fault_scale=0.0)
    robot.battery = 15.0
    controller.run_task(robot.robot_id, "dock_and_charge")
    assert robot.battery > 15.0


def test_run_once_picks_a_robot_and_a_task(captures):
    controller, _, ctrl_cap, _ = build(captures, fault_scale=0.0)
    controller.run_once()
    assert any(s.parent is None for s in ctrl_cap.spans.get_finished_spans())


def test_otlp_telemetry_requires_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        make_service_telemetry(service_name="fleet-controller", instance_id="c1")


def test_duration_histograms_use_fine_grained_buckets(captures):
    controller, robot, ctrl_cap, robot_cap = build(captures, fault_scale=0.0)
    controller.run_task(robot.robot_id, "move_to_waypoint")

    for cap, name in (
        (ctrl_cap, "fleet.task.duration"),
        (ctrl_cap, "fleet.command.latency"),
        (robot_cap, "robot.step.duration"),
    ):
        bounds = cap.metric_points(name)[0].explicit_bounds
        # Sub-second resolution, not the SDK default (0, 5, 10, 25, ...)
        assert len([b for b in bounds if 0 < b <= 1.0]) >= 4, name


def test_dispatch_spans_do_not_name_a_peer_address(captures):
    # A CLIENT span with server.address and no matching SERVER span (e.g. on a
    # comms timeout) shows up as a phantom external service on service maps.
    controller, robot, ctrl_cap, _ = build(captures, fault_scale=0.0, comms_timeout_rate=1.0)
    controller.run_task(robot.robot_id, "status_ping")
    clients = [s for s in ctrl_cap.spans.get_finished_spans() if s.kind == SpanKind.CLIENT]
    assert clients
    assert all("server.address" not in s.attributes for s in clients)


def test_command_spans_follow_rpc_semantic_conventions(captures):
    controller, robot, ctrl_cap, robot_cap = build(captures, fault_scale=0.0)
    controller.run_task(robot.robot_id, "pick_item")

    client = next(s for s in ctrl_cap.spans.get_finished_spans() if s.kind == SpanKind.CLIENT)
    server = next(s for s in robot_cap.spans.get_finished_spans() if s.kind == SpanKind.SERVER)
    for span in (client, server):
        assert span.attributes["rpc.system.name"] == "jsonrpc"
        assert span.attributes["rpc.method"] == "pick_item"
        assert span.attributes["jsonrpc.protocol.version"] == "2.0"
        assert span.attributes["jsonrpc.request.id"] == client.attributes["fleet.command.id"]
        # Deprecated in current semconv
        for old in ("rpc.system", "rpc.service", "rpc.jsonrpc.version", "rpc.jsonrpc.request_id"):
            assert old not in span.attributes


def test_failed_command_reports_rpc_status_code(captures):
    controller, robot, ctrl_cap, robot_cap = build(captures, fault_scale=1000.0)
    controller.run_task(robot.robot_id, "pick_item")

    client = next(s for s in ctrl_cap.spans.get_finished_spans() if s.kind == SpanKind.CLIENT)
    server = next(s for s in robot_cap.spans.get_finished_spans() if s.kind == SpanKind.SERVER)
    assert server.attributes["rpc.response.status_code"] == "-32000"
    assert client.attributes["rpc.response.status_code"] == "-32000"


def test_failed_task_is_requeued_for_the_same_robot(captures):
    controller, robot, ctrl_cap, _ = build(captures, fault_scale=1000.0)
    controller.requeue_rate = 1.0
    first = controller.run_task(robot.robot_id, "pick_item")
    assert not first.ok
    retried = controller.run_once()
    assert (retried.robot_id, retried.task) == (robot.robot_id, "pick_item")


def test_successful_task_is_not_requeued(captures):
    controller, robot, _, _ = build(captures, fault_scale=0.0)
    controller.requeue_rate = 1.0
    controller.run_task(robot.robot_id, "pick_item")
    assert controller._requeued is None

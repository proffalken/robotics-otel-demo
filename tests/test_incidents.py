import random

from fleet_sim.model import DEFAULT_INCIDENTS, TASKS, Incident, Robot, robot_service_name
from fleet_sim.robot import Command, RobotAgent

from .conftest import no_sleep


def make_agent(captures, incidents, now=0.0):
    robot = Robot("amr-02", "AMR-500", "zone-a", battery=90.0)
    cap = captures(robot_service_name(robot.robot_id), robot.robot_id)
    agent = RobotAgent(
        robot,
        cap.telemetry,
        rng=random.Random(3),
        sleep=no_sleep,
        fault_scale=0.0,
        incidents=incidents,
        clock=lambda: now,
    )
    return agent


def cmd(task):
    return Command("c1", task, "amr-02", 1, {})


def test_permanent_incident_is_always_active():
    assert Incident("amr-02", "pick_item", "gripper_fault").active(12345.0)


def test_intermittent_incident_follows_its_cycle():
    inc = Incident("amr-04", "place_item", "gripper_fault", on_s=600, off_s=1200)
    assert inc.active(0)
    assert inc.active(599)
    assert not inc.active(600)
    assert not inc.active(1799)
    assert inc.active(1800)


def test_active_incident_fails_only_its_task(captures):
    agent = make_agent(captures, [Incident("amr-02", "pick_item", "gripper_fault")])
    for _ in range(10):
        response = agent.handle(cmd("pick_item"))
        assert not response.ok
        assert response.error_type == "gripper_fault"
    assert agent.handle(cmd("move_to_waypoint")).ok


def test_inactive_incident_does_not_fail(captures):
    inc = Incident("amr-02", "pick_item", "gripper_fault", on_s=600, off_s=1200)
    agent = make_agent(captures, [inc], now=700.0)
    assert agent.handle(cmd("pick_item")).ok


def test_incidents_for_other_robots_are_ignored(captures):
    agent = make_agent(captures, [Incident("amr-05", "pick_item", "gripper_fault")])
    assert agent.handle(cmd("pick_item")).ok


def test_default_incidents_hit_two_to_three_robots_across_operations():
    robots = {i.robot_id for i in DEFAULT_INCIDENTS}
    assert 2 <= len(robots) <= 3
    assert len({i.robot_id for i in DEFAULT_INCIDENTS if i.on_s is None}) >= 2
    assert len({i.task for i in DEFAULT_INCIDENTS}) >= 2
    for inc in DEFAULT_INCIDENTS:
        step_faults = {f.error_type for s in TASKS[inc.task].steps for f in s.faults}
        assert inc.error_type in step_faults, inc

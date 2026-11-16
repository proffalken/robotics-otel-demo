import random

from fleet_sim.model import TASKS, Robot, choose_task, make_fleet, robot_service_name


def test_make_fleet_creates_unique_robots(rng):
    fleet = make_fleet(6, rng)
    assert len(fleet) == 6
    assert len({r.robot_id for r in fleet}) == 6
    assert all(r.robot_id.startswith("amr-") for r in fleet)


def test_low_battery_robot_is_sent_to_charge(rng):
    robot = Robot("amr-01", "AMR-500", "zone-a", battery=10.0)
    assert choose_task(robot, rng) == "dock_and_charge"


def test_estopped_robot_gets_estop_cleared(rng):
    robot = Robot("amr-01", "AMR-500", "zone-a", status="estopped")
    assert choose_task(robot, rng) == "clear_estop"


def test_healthy_robot_gets_a_known_random_task():
    rng = random.Random(1)
    robot = Robot("amr-01", "AMR-500", "zone-a", battery=90.0)
    picked = {choose_task(robot, rng) for _ in range(500)}
    assert picked <= set(TASKS)
    # Variety: most task types should come up over 500 picks.
    assert len(picked) >= 6
    assert "clear_estop" not in picked


def test_battery_stays_within_bounds():
    robot = Robot("amr-01", "AMR-500", "zone-a", battery=5.0)
    robot.drain(50)
    assert robot.battery == 0.0
    robot.charge(500)
    assert robot.battery == 100.0


def test_each_robot_is_its_own_service():
    assert robot_service_name("amr-01") == "robot-amr-01"

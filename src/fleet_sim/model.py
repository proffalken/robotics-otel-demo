"""Robots, task definitions and task selection. No telemetry in here."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

MODELS = ("AMR-500", "AMR-500", "AMR-900", "PickBot-X2")
ZONES = ("zone-a", "zone-b", "zone-c", "dock")


@dataclass(frozen=True)
class FaultSpec:
    error_type: str
    probability: float
    message: str
    level: str = "ERROR"  # log level used when this fault fires


@dataclass(frozen=True)
class StepSpec:
    name: str
    duration_s: tuple[float, float]
    faults: tuple[FaultSpec, ...] = ()


@dataclass(frozen=True)
class TaskSpec:
    name: str
    weight: float
    steps: tuple[StepSpec, ...]
    battery_cost: tuple[float, float] = (0.5, 2.0)
    distance_m: tuple[float, float] = (0.0, 0.0)
    random_pick: bool = True  # False = only chosen by rules (e.g. clear_estop)


OBSTACLE = FaultSpec("obstacle_blocked", 0.06, "Path blocked by obstacle, navigation aborted", "WARNING")
LOCALISATION = FaultSpec("localisation_lost", 0.02, "Localisation confidence dropped below threshold")
GRIPPER = FaultSpec("gripper_fault", 0.05, "Gripper failed to close: torque limit exceeded")
GRASP = FaultSpec("grasp_slip", 0.04, "Item slipped during grasp", "WARNING")
CHARGER = FaultSpec("dock_alignment_failed", 0.05, "Could not align with charging contacts", "WARNING")
FIRMWARE = FaultSpec("firmware_checksum_mismatch", 0.08, "Firmware image checksum mismatch, rolled back")

TASKS: dict[str, TaskSpec] = {
    t.name: t
    for t in (
        TaskSpec(
            "move_to_waypoint",
            weight=30,
            steps=(
                StepSpec("plan_path", (0.02, 0.15)),
                StepSpec("navigate", (0.3, 1.5), (OBSTACLE, LOCALISATION)),
                StepSpec("confirm_arrival", (0.01, 0.05)),
            ),
            battery_cost=(1.0, 3.0),
            distance_m=(5.0, 60.0),
        ),
        TaskSpec(
            "pick_item",
            weight=18,
            steps=(
                StepSpec("navigate", (0.2, 0.8), (OBSTACLE,)),
                StepSpec("scan_barcode", (0.05, 0.2)),
                StepSpec("grasp", (0.2, 0.6), (GRIPPER, GRASP)),
            ),
            battery_cost=(1.5, 3.5),
            distance_m=(2.0, 25.0),
        ),
        TaskSpec(
            "place_item",
            weight=15,
            steps=(
                StepSpec("navigate", (0.2, 0.8), (OBSTACLE,)),
                StepSpec("release", (0.1, 0.3), (GRIPPER,)),
            ),
            battery_cost=(1.0, 2.5),
            distance_m=(2.0, 25.0),
        ),
        TaskSpec(
            "status_ping",
            weight=20,
            steps=(StepSpec("collect_diagnostics", (0.01, 0.06)),),
            battery_cost=(0.0, 0.1),
        ),
        TaskSpec(
            "return_to_base",
            weight=6,
            steps=(
                StepSpec("plan_path", (0.02, 0.1)),
                StepSpec("navigate", (0.4, 1.5), (OBSTACLE,)),
            ),
            battery_cost=(1.0, 2.5),
            distance_m=(10.0, 80.0),
        ),
        TaskSpec(
            "dock_and_charge",
            weight=5,
            steps=(
                StepSpec("navigate", (0.3, 1.0), (OBSTACLE,)),
                StepSpec("align_dock", (0.1, 0.4), (CHARGER,)),
                StepSpec("charge", (0.8, 2.0)),
            ),
            battery_cost=(0.0, 0.0),
            distance_m=(5.0, 40.0),
        ),
        TaskSpec(
            "firmware_update",
            weight=2,
            steps=(
                StepSpec("download_image", (0.3, 1.0)),
                StepSpec("verify_checksum", (0.05, 0.2), (FIRMWARE,)),
                StepSpec("reboot", (0.5, 1.5)),
            ),
            battery_cost=(0.5, 1.0),
        ),
        TaskSpec(
            "emergency_stop",
            weight=1.5,
            steps=(StepSpec("halt_motors", (0.005, 0.03)),),
            battery_cost=(0.0, 0.0),
        ),
        TaskSpec(
            "clear_estop",
            weight=0,
            steps=(
                StepSpec("safety_check", (0.1, 0.4)),
                StepSpec("enable_motors", (0.05, 0.2)),
            ),
            battery_cost=(0.0, 0.1),
            random_pick=False,
        ),
    )
}

LOW_BATTERY = 25.0


@dataclass(frozen=True)
class Incident:
    """A robot that keeps failing one task, so alerts have something to fire on.

    Permanent when on_s is None; otherwise active for on_s seconds, then quiet
    for off_s seconds, repeating (aligned to the wall clock).
    """

    robot_id: str
    task: str
    error_type: str
    on_s: float | None = None
    off_s: float | None = None

    def active(self, now: float) -> bool:
        if self.on_s is None:
            return True
        return (now % (self.on_s + (self.off_s or 0))) < self.on_s


# Two robots fail constantly and a third flaps (10 min on, 20 min off), so
# 2-3 robots always have an alert, each on a different operation.
DEFAULT_INCIDENTS: tuple[Incident, ...] = (
    Incident("amr-02", "pick_item", "gripper_fault"),
    Incident("amr-05", "move_to_waypoint", "localisation_lost"),
    Incident("amr-04", "place_item", "gripper_fault", on_s=600, off_s=1200),
)

# Histogram buckets (seconds) sized for these simulated timings.
DURATION_BUCKETS_S = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
LATENCY_BUCKETS_S = (0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0)


@dataclass
class Robot:
    robot_id: str
    model: str
    zone: str
    battery: float = 100.0
    temperature: float = 42.0
    status: str = "idle"  # idle | busy | charging | updating | estopped | faulted
    firmware: str = "2.4.1"
    distance_m: float = field(default=0.0)

    def drain(self, amount: float) -> None:
        self.battery = max(0.0, self.battery - amount)

    def charge(self, amount: float) -> None:
        self.battery = min(100.0, self.battery + amount)

    def bump_firmware(self) -> None:
        major, minor, patch = (int(p) for p in self.firmware.split("."))
        self.firmware = f"{major}.{minor}.{patch + 1}"


def robot_service_name(robot_id: str) -> str:
    """Each robot reports as its own service so it gets its own node on service maps."""
    return f"robot-{robot_id}"


def make_fleet(size: int, rng: random.Random) -> list[Robot]:
    return [
        Robot(
            robot_id=f"amr-{i:02d}",
            model=rng.choice(MODELS),
            zone=rng.choice(ZONES[:-1]),
            battery=rng.uniform(40, 100),
            temperature=rng.uniform(38, 48),
        )
        for i in range(1, size + 1)
    ]


def choose_task(robot: Robot, rng: random.Random) -> str:
    """Pick the next task for a robot: safety and battery rules first, then weighted random."""
    if robot.status == "estopped":
        return "clear_estop"
    if robot.battery < LOW_BATTERY:
        return "dock_and_charge"
    candidates = [t for t in TASKS.values() if t.random_pick]
    return rng.choices(candidates, weights=[t.weight for t in candidates])[0].name

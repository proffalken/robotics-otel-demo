"""Entry point: run the fleet simulation forever, exporting everything over OTLP."""

from __future__ import annotations

import os
import random
import signal
import sys
import threading

from fleet_sim.controller import FleetController
from fleet_sim.model import DEFAULT_INCIDENTS, make_fleet, robot_service_name
from fleet_sim.robot import RobotAgent
from fleet_sim.telemetry import make_service_telemetry


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def main() -> int:
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        print(
            "OTEL_EXPORTER_OTLP_ENDPOINT is not set. Example:\n"
            "  OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 fleet-sim",
            file=sys.stderr,
        )
        return 2

    seed = os.environ.get("FLEET_SEED")
    rng = random.Random(int(seed)) if seed else random.Random()
    size = int(os.environ.get("FLEET_ROBOTS", "6"))
    interval = (_env_float("FLEET_INTERVAL_MIN", 1.0), _env_float("FLEET_INTERVAL_MAX", 3.0))
    fault_scale = _env_float("FLEET_FAULT_SCALE", 1.0)
    incidents = DEFAULT_INCIDENTS if os.environ.get("FLEET_INCIDENTS", "1") != "0" else ()

    telemetries = []
    agents = {}
    for robot in make_fleet(size, rng):
        tel = make_service_telemetry(
            service_name=robot_service_name(robot.robot_id),
            instance_id=robot.robot_id,
            extra_resource={"robot.id": robot.robot_id, "robot.model": robot.model},
        )
        telemetries.append(tel)
        agents[robot.robot_id] = RobotAgent(robot, tel, rng=rng, fault_scale=fault_scale, incidents=incidents)

    ctrl_tel = make_service_telemetry(service_name="fleet-controller", instance_id="fleet-controller-1")
    telemetries.append(ctrl_tel)
    controller = FleetController(agents, ctrl_tel, rng=rng)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    try:
        controller.run_forever(stop, interval)
    finally:
        for tel in telemetries:  # flush everything that is still buffered
            tel.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""A simulated on-robot agent that executes commands from the fleet controller."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from opentelemetry import propagate
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind, Status, StatusCode

from fleet_sim.model import DURATION_BUCKETS_S, TASKS, FaultSpec, Incident, Robot
from fleet_sim.telemetry import ServiceTelemetry


@dataclass
class Command:
    command_id: str
    task: str
    robot_id: str
    attempt: int
    headers: dict[str, str]  # W3C trace context travels here, like a message header


@dataclass
class CommandResponse:
    ok: bool
    error_type: str | None = None
    message: str = ""


# JSON-RPC 2.0 "server error" code, reported when a robot fails a command.
JSONRPC_SERVER_ERROR = "-32000"


def rpc_attributes(task: str, command_id: str) -> dict[str, str]:
    """RPC semantic-convention attributes for a controller -> robot command (JSON-RPC over MQTT)."""
    return {
        "rpc.system.name": "jsonrpc",
        "rpc.method": task,
        "jsonrpc.protocol.version": "2.0",
        "jsonrpc.request.id": command_id,
        "network.protocol.name": "mqtt",
    }


class RobotFault(Exception):
    def __init__(self, fault: FaultSpec):
        super().__init__(fault.message)
        self.fault = fault


class RobotAgent:
    def __init__(
        self,
        robot: Robot,
        telemetry: ServiceTelemetry,
        *,
        rng: random.Random,
        sleep: Callable[[float], None] = time.sleep,
        fault_scale: float = 1.0,
        incidents: Sequence[Incident] = (),
        clock: Callable[[], float] = time.time,
    ):
        self.robot = robot
        self.incidents = [i for i in incidents if i.robot_id == robot.robot_id]
        self.clock = clock
        self.telemetry = telemetry
        self.rng = rng
        self.sleep = sleep
        self.fault_scale = fault_scale
        self.log = telemetry.logger

        meter = telemetry.meter
        self._distance = meter.create_counter(
            "robot.distance.travelled", unit="m", description="Distance driven by the robot"
        )
        self._commands = meter.create_counter(
            "robot.commands.handled", unit="{command}", description="Commands executed by the robot"
        )
        self._step_duration = meter.create_histogram(
            "robot.step.duration",
            unit="s",
            description="Duration of each step of a task",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS_S,
        )
        meter.create_observable_gauge(
            "robot.battery.level", [self._observe_battery], unit="%", description="Battery state of charge"
        )
        meter.create_observable_gauge(
            "robot.cpu.temperature", [self._observe_temperature], unit="Cel", description="Onboard computer temperature"
        )

    # -- metric callbacks -------------------------------------------------
    def _attrs(self) -> dict[str, str]:
        return {"robot.id": self.robot.robot_id, "robot.model": self.robot.model, "robot.zone": self.robot.zone}

    def _observe_battery(self, _options: CallbackOptions):
        yield Observation(round(self.robot.battery, 2), self._attrs())

    def _observe_temperature(self, _options: CallbackOptions):
        yield Observation(round(self.robot.temperature, 2), self._attrs())

    # -- command handling -------------------------------------------------
    def handle(self, command: Command) -> CommandResponse:
        spec = TASKS[command.task]
        parent_ctx = propagate.extract(command.headers)
        robot = self.robot
        attrs = {**self._attrs(), "fleet.task.type": command.task, "fleet.command.id": command.command_id}

        with self.telemetry.tracer.start_as_current_span(
            command.task,
            context=parent_ctx,
            kind=SpanKind.SERVER,
            attributes={**attrs, **rpc_attributes(command.task, command.command_id)},
        ) as span:
            span.set_attribute("robot.battery.level", round(robot.battery, 1))
            span.set_attribute("robot.firmware.version", robot.firmware)
            self.log.info(
                "Received %s command %s (attempt %d)",
                command.task,
                command.command_id,
                command.attempt,
                extra={"fleet.task.type": command.task, "fleet.command.id": command.command_id},
            )
            previous_status = robot.status
            robot.status = {"dock_and_charge": "charging", "firmware_update": "updating"}.get(command.task, "busy")
            try:
                for step in spec.steps:
                    self._run_step(command, step)
                self._apply_success(command.task)
            except RobotFault as exc:
                fault = exc.fault
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, fault.message))
                span.set_attribute("error.type", fault.error_type)
                span.set_attribute("rpc.response.status_code", JSONRPC_SERVER_ERROR)
                robot.status = "idle" if previous_status != "estopped" else "estopped"
                self.log.log(
                    logging.getLevelNamesMapping()[fault.level],
                    "%s failed on %s: %s",
                    command.task,
                    robot.robot_id,
                    fault.message,
                    extra={"fleet.task.type": command.task, "error.type": fault.error_type},
                )
                self._commands.add(1, {**self._attrs(), "fleet.task.type": command.task, "outcome": "failure"})
                return CommandResponse(ok=False, error_type=fault.error_type, message=fault.message)
            finally:
                cost = self.rng.uniform(*spec.battery_cost)
                robot.drain(cost)
                distance = self.rng.uniform(*spec.distance_m)
                if distance:
                    robot.distance_m += distance
                    self._distance.add(distance, self._attrs())
                self._drift_temperature()

            self._commands.add(1, {**self._attrs(), "fleet.task.type": command.task, "outcome": "success"})
            self.log.info(
                "Completed %s (battery %.0f%%)",
                command.task,
                robot.battery,
                extra={"fleet.task.type": command.task, "fleet.command.id": command.command_id},
            )
            return CommandResponse(ok=True)

    def _run_step(self, command: Command, step) -> None:
        with self.telemetry.tracer.start_as_current_span(step.name, attributes={"robot.id": self.robot.robot_id}):
            duration = self.rng.uniform(*step.duration_s)
            self.sleep(duration)
            self._step_duration.record(
                duration,
                {"robot.model": self.robot.model, "fleet.task.type": command.task, "robot.step": step.name},
            )
            forced = self._incident_fault(command.task)
            for fault in step.faults:
                if fault.error_type == forced:
                    raise RobotFault(fault)
                if self.rng.random() < min(1.0, fault.probability * self.fault_scale):
                    raise RobotFault(fault)

    def _incident_fault(self, task: str) -> str | None:
        now = self.clock()
        for incident in self.incidents:
            if incident.task == task and incident.active(now):
                return incident.error_type
        return None

    def _apply_success(self, task: str) -> None:
        robot = self.robot
        if task == "dock_and_charge":
            robot.charge(self.rng.uniform(45, 75))
            robot.zone = "dock"
            robot.status = "idle"
        elif task == "return_to_base":
            robot.zone = "dock"
            robot.status = "idle"
        elif task in ("move_to_waypoint", "pick_item", "place_item"):
            robot.zone = self.rng.choice(("zone-a", "zone-b", "zone-c"))
            robot.status = "idle"
        elif task == "firmware_update":
            old = robot.firmware
            robot.bump_firmware()
            robot.status = "idle"
            self.log.info("Firmware updated %s -> %s", old, robot.firmware)
        elif task == "emergency_stop":
            robot.status = "estopped"
            self.log.warning("EMERGENCY STOP engaged on %s, motors halted", robot.robot_id)
        elif task == "clear_estop":
            robot.status = "idle"
            self.log.info("E-stop cleared on %s, resuming operations", robot.robot_id)
        else:
            robot.status = "idle"

        if robot.battery < 30:
            self.log.warning("Battery low on %s: %.0f%%", robot.robot_id, robot.battery)

    def _drift_temperature(self) -> None:
        target = 55.0 if self.robot.status in ("busy", "updating") else 42.0
        t = self.robot.temperature
        self.robot.temperature = t + (target - t) * 0.2 + self.rng.gauss(0, 0.8)

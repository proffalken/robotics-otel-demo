"""The central fleet controller: picks tasks and dispatches them to robots."""

from __future__ import annotations

import random
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from opentelemetry import propagate
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind, Status, StatusCode

from fleet_sim.model import DURATION_BUCKETS_S, LATENCY_BUCKETS_S, choose_task
from fleet_sim.robot import JSONRPC_SERVER_ERROR, Command, CommandResponse, RobotAgent, rpc_attributes
from fleet_sim.telemetry import ServiceTelemetry

STATUSES = ("idle", "busy", "charging", "updating", "estopped")
MAX_ATTEMPTS = 2
COMMS_TIMEOUT_S = 0.5


@dataclass
class TaskResult:
    task: str
    robot_id: str
    ok: bool
    error_type: str | None
    duration_s: float


class FleetController:
    def __init__(
        self,
        agents: dict[str, RobotAgent],
        telemetry: ServiceTelemetry,
        *,
        rng: random.Random,
        sleep: Callable[[float], None] = time.sleep,
        comms_timeout_rate: float = 0.03,
        requeue_rate: float = 0.6,
    ):
        self.agents = agents
        self.telemetry = telemetry
        self.rng = rng
        self.sleep = sleep
        self.comms_timeout_rate = comms_timeout_rate
        self.requeue_rate = requeue_rate  # chance a failed task is retried next
        self._requeued: tuple[str, str] | None = None
        self.log = telemetry.logger

        meter = telemetry.meter
        self._sent = meter.create_counter(
            "fleet.commands.sent", unit="{command}", description="Commands dispatched to robots"
        )
        self._failed = meter.create_counter(
            "fleet.commands.failed", unit="{command}", description="Commands that failed or timed out"
        )
        self._task_duration = meter.create_histogram(
            "fleet.task.duration",
            unit="s",
            description="End-to-end task duration as seen by the controller",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS_S,
        )
        self._latency = meter.create_histogram(
            "fleet.command.latency",
            unit="s",
            description="Network round trip to the robot",
            explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
        )
        meter.create_observable_gauge(
            "fleet.robots", [self._observe_robots], unit="{robot}", description="Robots by status"
        )
        meter.create_observable_gauge(
            "fleet.task.queue.depth", [self._observe_queue], unit="{task}", description="Tasks waiting to be scheduled"
        )
        self._queue_depth = 5

    # -- metric callbacks -------------------------------------------------
    def _observe_robots(self, _options: CallbackOptions):
        counts = Counter(a.robot.status for a in self.agents.values())
        for status in STATUSES:
            yield Observation(counts.get(status, 0), {"robot.status": status})

    def _observe_queue(self, _options: CallbackOptions):
        yield Observation(self._queue_depth)

    # -- scheduling -------------------------------------------------------
    def run_once(self) -> TaskResult:
        if self._requeued is not None:
            robot_id, task = self._requeued
            self._requeued = None
            self.log.info("Retrying requeued %s on %s", task, robot_id, extra={"robot.id": robot_id})
            return self.run_task(robot_id, task)
        agent = self.rng.choice(list(self.agents.values()))
        task = choose_task(agent.robot, self.rng)
        self._queue_depth = max(0, self._queue_depth + self.rng.randint(-2, 2))
        return self.run_task(agent.robot.robot_id, task)

    def run_forever(self, stop: threading.Event, interval_s: tuple[float, float] = (1.0, 3.0)) -> None:
        self.log.info("Fleet controller online, managing %d robots", len(self.agents))
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:  # keep the demo running whatever happens
                self.log.exception("Unexpected error while running task")
            stop.wait(self.rng.uniform(*interval_s))
        self.log.info("Fleet controller shutting down")

    def run_task(self, robot_id: str, task: str) -> TaskResult:
        agent = self.agents[robot_id]
        robot = agent.robot
        task_id = uuid.uuid4().hex[:12]
        base = {"robot.id": robot_id, "robot.model": robot.model, "fleet.task.type": task}
        started = time.perf_counter()

        with self.telemetry.tracer.start_as_current_span(
            f"schedule {task}", kind=SpanKind.INTERNAL, attributes={**base, "fleet.task.id": task_id}
        ) as root:
            self.log.info("Scheduling %s for %s (task %s)", task, robot_id, task_id, extra={**base})
            response = CommandResponse(ok=False, error_type="not_dispatched")
            for attempt in range(1, MAX_ATTEMPTS + 1):
                response = self._dispatch(agent, task, task_id, attempt, base)
                if response.ok or response.error_type != "comms_timeout":
                    break
                if attempt < MAX_ATTEMPTS:
                    self.log.warning("Retrying %s for %s after comms timeout", task, robot_id, extra={**base})

            outcome = "success" if response.ok else "failure"
            root.set_attribute("fleet.task.outcome", outcome)
            if not response.ok:
                root.set_attribute("error.type", response.error_type)
                root.set_status(Status(StatusCode.ERROR, response.message or response.error_type))
                self.log.error(
                    "Task %s (%s) failed on %s: %s",
                    task_id,
                    task,
                    robot_id,
                    response.error_type,
                    extra={**base, "error.type": response.error_type},
                )
            else:
                self.log.info("Task %s (%s) completed on %s", task_id, task, robot_id, extra={**base})

        duration = time.perf_counter() - started
        attrs = {"fleet.task.type": task, "robot.model": robot.model, "fleet.task.outcome": outcome}
        if not response.ok:
            attrs["error.type"] = response.error_type
        self._task_duration.record(duration, attrs)
        if not response.ok and response.error_type != "comms_timeout" and self.rng.random() < self.requeue_rate:
            self._requeued = (robot_id, task)
        return TaskResult(task, robot_id, response.ok, response.error_type, duration)

    def _dispatch(
        self, agent: RobotAgent, task: str, task_id: str, attempt: int, base: dict[str, str]
    ) -> CommandResponse:
        command_id = f"{task_id}-{attempt}"
        with self.telemetry.tracer.start_as_current_span(
            task,
            kind=SpanKind.CLIENT,
            attributes={
                **base,
                "fleet.command.id": command_id,
                "fleet.command.attempt": attempt,
                **rpc_attributes(task, command_id),
            },
        ) as span:
            headers: dict[str, str] = {}
            propagate.inject(headers)
            self._sent.add(1, {"fleet.task.type": task, "robot.id": agent.robot.robot_id})

            if self.rng.random() < self.comms_timeout_rate:
                self.sleep(COMMS_TIMEOUT_S)
                span.set_attribute("error.type", "comms_timeout")
                span.set_status(Status(StatusCode.ERROR, "No ack from robot within timeout"))
                self.log.warning(
                    "No ack from %s within %.1fs (attempt %d)",
                    agent.robot.robot_id,
                    COMMS_TIMEOUT_S,
                    attempt,
                    extra={**base, "error.type": "comms_timeout"},
                )
                self._failed.add(
                    1, {"fleet.task.type": task, "robot.id": agent.robot.robot_id, "error.type": "comms_timeout"}
                )
                return CommandResponse(ok=False, error_type="comms_timeout", message="No ack from robot")

            rtt = self.rng.uniform(0.005, 0.06)
            self.sleep(rtt / 2)
            response = agent.handle(Command(command_id, task, agent.robot.robot_id, attempt, headers))
            self.sleep(rtt / 2)
            self._latency.record(rtt, {"robot.id": agent.robot.robot_id})

            if not response.ok:
                span.set_attribute("error.type", response.error_type)
                span.set_attribute("rpc.response.status_code", JSONRPC_SERVER_ERROR)
                span.set_status(Status(StatusCode.ERROR, response.message))
                self._failed.add(
                    1, {"fleet.task.type": task, "robot.id": agent.robot.robot_id, "error.type": response.error_type}
                )
            return response

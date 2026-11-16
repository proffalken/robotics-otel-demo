# fleet-sim

Simulates a central fleet controller sending commands to a small fleet of
robots. Nothing moves: it exists to produce realistic OpenTelemetry traces,
metrics and logs, all exported over OTLP/HTTP to an OpenTelemetry Collector.

## Run

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 uv run fleet-sim
```

or with Docker Compose, which also runs an internal OpenTelemetry Collector:

```bash
cp .env.example .env   # set the upstream endpoint and auth header
docker compose up -d
```

Stop with Ctrl+C / `docker compose down`; buffered telemetry is flushed on exit.

Compose pulls the published image `ghcr.io/proffalken/robotics-otel-demo`
(tag from `FLEET_SIM_TAG`, default `latest`); add `--build` to build locally.

## Collector and backend auth

```
fleet-sim ──OTLP/HTTP──▶ otel-collector (internal) ──OTLP/HTTP + auth──▶ backend
```

The collector (`otel/opentelemetry-collector-contrib`, config in
`collector/config.yaml`) publishes no ports, so only fleet-sim can reach it.
It forwards traces, metrics and logs with a single `otlphttp` exporter that
sends an `Authorization` header and a `Dash0-Dataset` header. It also prints
a one-line summary of each batch (`docker compose logs otel-collector`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `UPSTREAM_OTLP_ENDPOINT` | *(required)* | Backend OTLP/HTTP base URL |
| `UPSTREAM_OTLP_AUTH_HEADER` | empty | Full `Authorization` header value |
| `DASH0_DATASET` | `default` | Value of the `Dash0-Dataset` header; other backends ignore it |

**Dash0** uses a bearer token
([docs](https://www.dash0.com/documentation/dash0/get-started/sending-data-to-dash0)):

```bash
UPSTREAM_OTLP_ENDPOINT=https://ingress.eu-west-1.aws.dash0.com
UPSTREAM_OTLP_AUTH_HEADER=Bearer auth_xxxxxxxx
DASH0_DATASET=default
```

**Grafana Cloud** uses HTTP Basic auth with your stack's instance ID and an
access-policy token
([docs](https://grafana.com/docs/grafana-cloud/send-data/otlp/send-data-otlp/)):

```bash
UPSTREAM_OTLP_ENDPOINT=https://otlp-gateway-prod-eu-west-2.grafana.net/otlp
UPSTREAM_OTLP_AUTH_HEADER="Basic $(printf '%s' '<instance-id>:<token>' | base64 -w0)"
```

Put the computed value in `.env`; Compose does not run shell commands there.
Use the endpoint region your provider shows for your account.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(required)* | Collector OTLP/HTTP endpoint |
| `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_RESOURCE_ATTRIBUTES`, other `OTEL_*` | | Standard [OTel SDK env vars](https://opentelemetry.io/docs/specs/otel/configuration/sdk-environment-variables/) |
| `OTEL_METRIC_EXPORT_INTERVAL` | `10000` (ms) | Metric export interval |
| `FLEET_ROBOTS` | `6` | Number of robots |
| `FLEET_INTERVAL_MIN` / `FLEET_INTERVAL_MAX` | `1.0` / `3.0` | Seconds between tasks |
| `FLEET_FAULT_SCALE` | `1.0` | Multiplies robot fault probabilities (`0` = no faults) |
| `FLEET_SEED` | random | Seed for repeatable runs |

`service.name` is set by the simulator, so `OTEL_SERVICE_NAME` is ignored.

## What it emits

**Services** (namespace `robotics`):

- `fleet-controller`, instance `fleet-controller-1`
- `robot-amr-01` … `robot-amr-06`, one service per robot (instance ID = robot ID)

**Tasks**, picked at random by weight: `move_to_waypoint`, `pick_item`,
`place_item`, `status_ping`, `return_to_base`, `dock_and_charge`,
`firmware_update`, `emergency_stop`. A robot below 25% battery is always sent
to charge, and an e-stopped robot always gets `clear_estop` next.

**Traces**: each task is one trace from the controller into one robot:

```
schedule <task>        fleet-controller  INTERNAL
└─ <task>              fleet-controller  CLIENT   (repeated once on comms timeout)
   └─ <task>           robot-<id>        SERVER   (W3C trace context passed in command headers)
      └─ <step> …      robot-<id>        INTERNAL (plan_path, navigate, grasp, …)
```

Commands are modelled as JSON-RPC over MQTT using the RPC semantic
conventions (`rpc.system.name=jsonrpc`, `rpc.method=<task>`,
`jsonrpc.request.id`, and `rpc.response.status_code=-32000` on failure), so
each task type shows up as its own operation on the robot services.

Faults (`obstacle_blocked`, `gripper_fault`, `grasp_slip`, `localisation_lost`,
`dock_alignment_failed`, `firmware_checksum_mismatch`, `comms_timeout`) set
span status `ERROR`, `error.type`, and an exception event on the robot spans.

**Metrics**

| Name | Type | Service |
| --- | --- | --- |
| `fleet.commands.sent` | counter | controller |
| `fleet.commands.failed` | counter (`error.type`) | controller |
| `fleet.task.duration` | histogram (s) | controller |
| `fleet.command.latency` | histogram (s) | controller |
| `fleet.robots` | gauge by `robot.status` | controller |
| `fleet.task.queue.depth` | gauge | controller |
| `robot.battery.level` | gauge (%) | robot |
| `robot.cpu.temperature` | gauge (Cel) | robot |
| `robot.distance.travelled` | counter (m) | robot |
| `robot.commands.handled` | counter (`outcome`) | robot |
| `robot.step.duration` | histogram (s) | robot |

**Logs**: Python `logging` sent over OTLP through
`opentelemetry-instrumentation-logging`. Each record carries the active trace
and span ID. Logs are also printed to stdout.

## Optional: real ROS 2 statistics with ros2ebpf

[ros2ebpf](https://github.com/fujitatomoya/ros2ebpf) attaches eBPF uprobes
to ROS 2's `librmw_implementation.so` and counts publish, take and service
calls per topic and process, without changing or restarting the nodes. It
serves them as Prometheus metrics; the collector scrapes them and forwards
them upstream as OTLP, through the same endpoint and auth header as
everything else.

```
ros2-robot (ROS 2 nodes) ──uprobes──▶ ros2ebpfd :9477 ◀──scrape── otel-collector ──▶ backend
```

```bash
docker compose -f docker-compose.yml -f docker-compose.ros2.yml up -d --build
```

This adds:

- `ros2ebpfd`: built from upstream at a pinned commit (no image is
  published). It runs **privileged in the host pid namespace** and mounts
  `/sys/fs/bpf` and `/sys/kernel/btf`, which loading uprobes requires.
- `ros2-robot`: ROS 2 Rolling (`ros:rolling-ros-base`) running
  `ros2/robot.launch.xml` in namespace `/amr_01`: `talker` and `listener` on
  `/amr_01/chatter`, and `add_two_ints_server` with a client calling
  `/amr_01/add_two_ints` once a second.
- `collector/ros2ebpf.yaml`, loaded as a second collector `--config`: a
  `prometheus` receiver for `ros2ebpfd:9477` and its own `metrics/ros2ebpf`
  pipeline. The resource is `service.name=ros2ebpf`, with
  `service.namespace=robotics` and `robot.id` added (from `ROS2EBPF_ROBOT`,
  default `amr-01`) to match fleet-sim's robots.

Metrics include `ros2_rmw_publish_total`, `ros2_rmw_take_total`,
`ros2_rmw_service_request_total` and `ros2_process_info`; see
[ros2ebpf's metrics reference](https://github.com/fujitatomoya/ros2ebpf/blob/main/docs/metrics.md).

Requirements: Linux with kernel ≥ 5.8 and BTF (`ls /sys/kernel/btf/vmlinux`),
and Docker allowed to run privileged containers. ros2ebpf supports ROS 2
Rolling only.

All ROS 2 nodes run in one container on purpose. At the pinned commit,
ros2ebpf attaches its publish probes once per container that maps the same
`librmw_implementation.so`; with the nodes in N containers built from one
image, `ros2_rmw_publish_total` read N times the real count (take counts
were unaffected). The smoke test checks that publish and take counts agree.

## Dash0 dashboard

`dashboards/fleet-sim.yaml` is generated by `dashboards/generate.py` (edit the
generator, not the YAML) and applied with the [Dash0 CLI](https://www.dash0.com/docs/dash0/get-started/dash0-cli):

```bash
uv run python dashboards/generate.py
dash0 apply --dataset <dataset> -f dashboards/fleet-sim.yaml -f checks/robot-alerts.yaml
```

The dashboard has Robot and Task type filters and six sections: Overview,
Commands, Fleet state, Robot health, ROS 2 middleware (topics, publish and
take rates, services, timer jitter, processes) and ros2ebpf health (probes,
dropped events, unresolved endpoints). The last two stay empty unless the
ros2ebpf stack is running.

Both files carry a stable ID (`metadata.dash0Extensions.id` on the
dashboard, the `dash0.com/id` label on the rules), so re-applying updates
the existing assets instead of creating duplicates. Without `--dataset`,
`dash0 apply` uses the profile's default dataset.

`checks/robot-alerts.yaml` has two groups:

| Group | Rule | Fires when |
| --- | --- | --- |
| Robot Fleet | `RobotOperationErrorRate` | over 75% of a robot's operation spans failed (≥ 4 failures in 10 minutes) |
| Robot Fleet | `RobotTaskFailureRate` | over 75% of a robot's tasks of one type failed (≥ 4 in 10 minutes) |
| ROS 2 | `ROS2TopicDeliveryGap` | subscriptions take under 80% of what is published on a topic |
| ROS 2 | `ROS2TopicSilent` | a topic that published in the last 30 minutes has stopped |
| ROS 2 | `ROS2ServiceUnanswered` | a service server answers under half of its requests |
| ROS 2 | `ROS2EbpfProbesDetached` | ros2ebpf has fewer probes attached than configured |

ros2ebpf removes a series when its endpoint disappears, so the ROS 2 rules
treat a missing series as zero.

## Breaking the demo on purpose

`demo/break.sh` injects faults so the rules fire:

```bash
demo/break.sh ros2-subscriber-down  # kill the listener  -> ROS2TopicDeliveryGap
demo/break.sh ros2-publisher-down   # kill the talker    -> ROS2TopicSilent
demo/break.sh fleet-faults          # FLEET_FAULT_SCALE=5 -> RobotOperationErrorRate / RobotTaskFailureRate
demo/break.sh restore               # put everything back
```

It runs against the ros2ebpf stack by default; set
`COMPOSE_FILES="-f docker-compose.yml"` for fleet-sim alone.

## Agent0 automations

`agent0/automations/` holds three [Agent0](https://www.dash0.com/docs/dash0/agent0)
automations. Automations have no `dash0` CLI or API support yet, so each
file lists the settings and the prompt to enter in **Agent0 → Automations**
by hand:

- `robot-incident-triage.md`: on a failed Robot Fleet or ROS 2 check, works
  out whether the cause is the application, the ROS 2 middleware or the
  observability pipeline.
- `daily-fleet-briefing.md`: a weekday 08:00 UTC summary of the last 24 hours.
- `telemetry-pr-review.md`: on a pull request, checks that the metrics and
  attributes used by changed dashboards, rules and instrumentation exist in
  Dash0. It never posts to GitHub.

All three use no network access, no notifications (results stay in the run
history) and a 250 credit/week budget.

## Development

```bash
uv run pytest                 # unit tests, including collector config checks
SMOKE_BUILD=1 ci/smoke.sh     # end-to-end: fleet-sim -> collector -> fake backend
SMOKE_BUILD=1 ci/smoke-ros2.sh  # end-to-end: ROS 2 -> ros2ebpfd -> collector -> fake backend
```

The smoke test points the collector at a second collector
(`ci/fake-upstream.yaml`) that rejects requests without the expected bearer
token, and passes once that backend has received traces, metrics and logs.

## CI

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`:
unit tests, `otelcol-contrib validate` on the collector configs (including
the merged ros2ebpf overlay), both smoke tests, and an image build. The
ros2ebpf smoke test does not gate the image build. Pushes to `main` also publish the image to
`ghcr.io/proffalken/robotics-otel-demo` tagged `latest` and `sha-<short sha>`.

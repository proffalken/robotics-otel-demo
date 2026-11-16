"""Static checks on the bundled OpenTelemetry Collector configuration.

The collector forwards everything to an external OTLP backend (Dash0,
Grafana Cloud, ...). These tests pin down the contract: every signal is
forwarded, and the endpoint and credentials only ever come from env vars.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "collector" / "config.yaml"
COMPOSE = ROOT / "docker-compose.yml"
UPSTREAM = "otlphttp/upstream"


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


@pytest.mark.parametrize("signal", ["traces", "metrics", "logs"])
def test_every_signal_is_received_over_otlp_and_forwarded_upstream(config, signal):
    pipeline = config["service"]["pipelines"][signal]
    assert pipeline["receivers"] == ["otlp"]
    assert UPSTREAM in pipeline["exporters"]


@pytest.mark.parametrize("signal", ["traces", "metrics", "logs"])
def test_memory_limiter_runs_first_and_batch_last(config, signal):
    processors = config["service"]["pipelines"][signal]["processors"]
    assert processors[0] == "memory_limiter"
    assert processors[-1] == "batch"


def test_upstream_endpoint_comes_from_env(config):
    assert config["exporters"][UPSTREAM]["endpoint"] == "${env:UPSTREAM_OTLP_ENDPOINT}"


def test_upstream_auth_header_comes_from_env(config):
    headers = config["exporters"][UPSTREAM]["headers"]
    assert headers["Authorization"] == "${env:UPSTREAM_OTLP_AUTH_HEADER}"


def test_dash0_dataset_header_defaults_to_default_and_is_overridable(config):
    headers = config["exporters"][UPSTREAM]["headers"]
    assert headers["Dash0-Dataset"] == "${env:DASH0_DATASET:-default}"


def test_no_header_value_is_hard_coded(config):
    for name, value in config["exporters"][UPSTREAM]["headers"].items():
        assert value.startswith("${env:"), f"header {name} is hard-coded"


def test_otlp_receiver_accepts_http_and_grpc(config):
    protocols = config["receivers"]["otlp"]["protocols"]
    assert protocols["http"]["endpoint"] == "0.0.0.0:4318"
    assert protocols["grpc"]["endpoint"] == "0.0.0.0:4317"


def test_collector_is_internal_only(compose):
    collector = compose["services"]["otel-collector"]
    assert "ports" not in collector, "collector must not publish ports to the host"


def test_collector_image_is_pinned_contrib(compose):
    image = compose["services"]["otel-collector"]["image"]
    name, _, tag = image.partition(":")
    assert name == "otel/opentelemetry-collector-contrib"
    assert tag and tag != "latest"


def test_simulator_sends_to_internal_collector(compose):
    sim = compose["services"]["fleet-sim"]
    assert sim["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel-collector:4318"
    assert "otel-collector" in sim["depends_on"]


def test_collector_credentials_are_passed_through_from_env(compose):
    env = compose["services"]["otel-collector"]["environment"]
    for var in ("UPSTREAM_OTLP_ENDPOINT", "UPSTREAM_OTLP_AUTH_HEADER", "DASH0_DATASET"):
        assert var in env
        assert env[var].startswith("${" + var), f"{var} must come from the environment"

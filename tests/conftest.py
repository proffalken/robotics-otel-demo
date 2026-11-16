import random

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fleet_sim.telemetry import ServiceTelemetry, make_service_telemetry


class Capture:
    """In-memory exporters for one simulated service."""

    def __init__(self, service_name: str, instance_id: str):
        self.spans = InMemorySpanExporter()
        self.metrics = InMemoryMetricReader()
        self.logs = InMemoryLogRecordExporter()
        self.telemetry: ServiceTelemetry = make_service_telemetry(
            service_name=service_name,
            instance_id=instance_id,
            span_exporter=self.spans,
            metric_reader=self.metrics,
            log_exporter=self.logs,
            synchronous=True,
            console=False,
        )

    def metric_points(self, name: str) -> list:
        self.telemetry.meter_provider.force_flush()
        data = self.metrics.get_metrics_data()
        points = []
        if data is None:
            return points
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == name:
                        points.extend(metric.data.data_points)
        return points


@pytest.fixture
def rng():
    return random.Random(42)


@pytest.fixture
def captures():
    created: list[Capture] = []

    def _make(service_name: str, instance_id: str) -> Capture:
        cap = Capture(service_name, instance_id)
        created.append(cap)
        return cap

    yield _make
    for cap in created:
        cap.telemetry.shutdown()


def no_sleep(_seconds: float) -> None:
    return None

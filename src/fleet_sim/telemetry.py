"""OpenTelemetry setup for one simulated service.

Each simulated service (the controller and every robot agent) gets its own
tracer, meter and logger providers, so each shows up in the backend as its
own service instance with its own resource.

The OTLP exporters are configured only from the standard OTEL_* environment
variables (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS,
OTEL_RESOURCE_ATTRIBUTES, ...).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field

from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.metrics import Meter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Tracer

from fleet_sim import __version__

SCOPE = "fleet_sim"
DEFAULT_METRIC_INTERVAL_MS = 10_000


@dataclass
class ServiceTelemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    tracer: Tracer
    meter: Meter
    logger: logging.Logger
    _handlers: list[logging.Handler] = field(default_factory=list)

    def shutdown(self) -> None:
        for handler in self._handlers:
            self.logger.removeHandler(handler)
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
        self.logger_provider.shutdown()


def make_service_telemetry(
    *,
    service_name: str,
    instance_id: str,
    extra_resource: dict[str, str] | None = None,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
    log_exporter: LogRecordExporter | None = None,
    synchronous: bool = False,
    console: bool = True,
) -> ServiceTelemetry:
    """Build providers for one service. Exporters default to OTLP/HTTP."""
    if span_exporter is None or metric_reader is None or log_exporter is None:
        if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            raise RuntimeError(
                "OTEL_EXPORTER_OTLP_ENDPOINT is not set; point it at your OpenTelemetry Collector "
                "(e.g. http://collector:4318)"
            )
        # Imported here so tests never need a network exporter.
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporter = span_exporter or OTLPSpanExporter()
        log_exporter = log_exporter or OTLPLogExporter()
        if metric_reader is None:
            interval = None if os.environ.get("OTEL_METRIC_EXPORT_INTERVAL") else DEFAULT_METRIC_INTERVAL_MS
            metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=interval)

    # Explicit attributes win over OTEL_RESOURCE_ATTRIBUTES / OTEL_SERVICE_NAME,
    # so both simulated services keep their own names.
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "robotics",
            "service.version": __version__,
            "service.instance.id": instance_id,
            **(extra_resource or {}),
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        SimpleSpanProcessor(span_exporter) if synchronous else BatchSpanProcessor(span_exporter)
    )

    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        SimpleLogRecordProcessor(log_exporter) if synchronous else BatchLogRecordProcessor(log_exporter)
    )

    logger = logging.getLogger(f"fleet_sim.{service_name}.{instance_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handlers: list[logging.Handler] = [LoggingHandler(level=logging.INFO, logger_provider=logger_provider)]
    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
        handlers.append(stream)
    for handler in handlers:
        logger.addHandler(handler)

    return ServiceTelemetry(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        tracer=tracer_provider.get_tracer(SCOPE, __version__),
        meter=meter_provider.get_meter(SCOPE, __version__),
        logger=logger,
        _handlers=handlers,
    )

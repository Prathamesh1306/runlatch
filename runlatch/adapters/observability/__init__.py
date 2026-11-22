"""Observability adapters for runlatch."""

from runlatch.adapters.observability.otel import (
    OpenTelemetrySubscriber,
    RecordedSpan,
)

__all__ = ["OpenTelemetrySubscriber", "RecordedSpan"]

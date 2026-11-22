"""Optional OpenTelemetry EventBus subscriber (`runlatch[observability]`).

Lazy-imports `opentelemetry.trace` so `runlatch` works with zero extra dependencies
when `opentelemetry` is not installed.
Imports ONLY Python stdlib and runlatch.core.*.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

from runlatch.core.events import EventBus
from runlatch.core.types import LifecycleEvent


@dataclass
class RecordedSpan:
    """Structured span record emitted by OpenTelemetrySubscriber."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)


def _try_get_otel_tracer(service_name: str) -> Any | None:
    try:
        trace_mod = importlib.import_module("opentelemetry.trace")
        return trace_mod.get_tracer(service_name)
    except ImportError:
        return None


_EVENT_TO_SPAN_NAME: dict[str, str] = {
    "workflow.started": "workflow.run",
    "workflow.completed": "workflow.run",
    "workflow.paused": "workflow.run",
    "run.started": "agent.run",
    "run.completed": "agent.run",
    "step.started": "workflow.step",
    "step.completed": "workflow.step",
    "step.skipped": "workflow.step",
    "tool.completed": "tool.execute",
    "tool.denied": "tool.execute",
    "approval.requested": "approval.gate",
    "approval.resumed": "approval.gate",
}


class OpenTelemetrySubscriber:
    """Subscribes to EventBus and records structured OTel spans (with graceful no-op fallback)."""

    def __init__(
        self,
        *,
        service_name: str = "runlatch",
        tracer: Any | None = None,
    ) -> None:
        self.service_name = service_name
        self._tracer = tracer if tracer is not None else _try_get_otel_tracer(service_name)
        self.recorded_spans: list[RecordedSpan] = []

    @property
    def otel_available(self) -> bool:
        return self._tracer is not None

    def attach(self, event_bus: EventBus) -> OpenTelemetrySubscriber:
        """Subscribe this observer to all lifecycle events on `event_bus`."""
        event_bus.subscribe(self.on_event, event_type="*")
        return self

    def on_event(self, event: LifecycleEvent) -> None:
        """Handle a LifecycleEvent and emit a span."""
        span_name = _EVENT_TO_SPAN_NAME.get(event.event_type, event.event_type)
        attrs: dict[str, Any] = {
            "runlatch.event_type": event.event_type,
            "runlatch.run_id": str(event.run_id),
            "runlatch.workflow": event.workflow_name,
        }
        if event.step_name:
            attrs["runlatch.step"] = event.step_name
        for k, v in event.payload.items():
            if isinstance(v, (str, int, float, bool)):
                attrs[f"runlatch.{k}"] = v

        self.recorded_spans.append(RecordedSpan(name=span_name, attributes=attrs))

        if self._tracer is not None and hasattr(self._tracer, "start_as_current_span"):
            with self._tracer.start_as_current_span(span_name) as span:
                if hasattr(span, "set_attribute"):
                    for k, v in attrs.items():
                        span.set_attribute(k, v)

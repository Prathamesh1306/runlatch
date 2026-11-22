"""In-process EventBus for lifecycle notifications and telemetry hooks.

Pure domain module: imports ONLY Python stdlib and runlatch.core.types.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from runlatch.core.types import LifecycleEvent

EventListener = Callable[[LifecycleEvent], Any]


class EventBus:
    """Publishes structured LifecycleEvents to sync and async subscribers."""

    def __init__(self) -> None:
        self._wildcard_listeners: list[EventListener] = []
        self._topic_listeners: dict[str, list[EventListener]] = {}
        self.history: list[LifecycleEvent] = []

    def subscribe(self, listener: EventListener, *, event_type: str = "*") -> None:
        """Register a listener for all events ('*') or a specific event_type."""
        if event_type == "*":
            self._wildcard_listeners.append(listener)
        else:
            self._topic_listeners.setdefault(event_type, []).append(listener)

    def emit_sync(self, event: LifecycleEvent) -> None:
        """Synchronously notify all matching synchronous listeners and record history."""
        self.history.append(event)
        listeners = [
            *self._wildcard_listeners,
            *self._topic_listeners.get(event.event_type, ()),
        ]
        for fn in listeners:
            res = fn(event)
            if inspect.isawaitable(res):
                # Close unawaited coroutine if called from sync context
                if hasattr(res, "close"):
                    res.close()

    async def emit(self, event: LifecycleEvent) -> None:
        """Notify all matching sync and async subscribers."""
        self.history.append(event)
        listeners = [
            *self._wildcard_listeners,
            *self._topic_listeners.get(event.event_type, ()),
        ]
        for fn in listeners:
            res = fn(event)
            if inspect.isawaitable(res):
                await res

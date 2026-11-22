"""Server runtime context, Redis idempotency/PubSub adapters, and TaskDispatcher implementations."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol
from uuid import UUID

from fastapi import Request

from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings
from runlatch.core.protocols import TaskDispatcher


class IdempotencyBackend(Protocol):
    """Redis `SET NX EX 86400` idempotency store protocol."""

    async def claim_delivery(self, key: str, ttl_seconds: int = 86400) -> bool: ...

    async def ping(self) -> bool: ...


class InMemoryIdempotencyBackend:
    """In-process `SET NX EX` equivalent for zero-Redis unit/integration tests."""

    def __init__(self) -> None:
        self._keys: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def claim_delivery(self, key: str, ttl_seconds: int = 86400) -> bool:
        now = time.monotonic()
        async with self._lock:
            exp = self._keys.get(key)
            if exp is not None and exp > now:
                return False
            self._keys[key] = now + ttl_seconds
            return True

    async def ping(self) -> bool:
        return True


class RedisIdempotencyBackend:
    """Production Redis `SET key 1 NX EX 86400` idempotency store."""

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def claim_delivery(self, key: str, ttl_seconds: int = 86400) -> bool:
        namespaced = f"runlatch:idem:webhook:{key}"
        acquired = await self._redis.set(namespaced, "1", nx=True, ex=ttl_seconds)
        return bool(acquired)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False


class EventStreamBroker:
    """Streams live run events over Redis Pub/Sub (or in-process queues)."""

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client
        self._subscribers: dict[UUID, list[asyncio.Queue[dict[str, Any]]]] = {}

    async def publish(self, run_id: UUID, event: dict[str, Any]) -> None:
        for q in self._subscribers.get(run_id, ()):
            await q.put(event)
        if self._redis is not None:
            channel = f"runlatch:run:{run_id}:events"
            await self._redis.publish(channel, json.dumps(event, default=str))

    async def subscribe(
        self, run_id: UUID, *, max_events: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(queue)
        count = 0
        try:
            while True:
                item = await queue.get()
                yield item
                count += 1
                if item.get("terminal") is True or (
                    max_events is not None and count >= max_events
                ):
                    break
        finally:
            subs = self._subscribers.get(run_id, [])
            if queue in subs:
                subs.remove(queue)


class InProcessTaskDispatcher:
    """In-memory TaskDispatcher recording queued tasks (used when Celery is not active)."""

    def __init__(self) -> None:
        self.dispatched_runs: list[tuple[UUID, str, dict[str, Any]]] = []
        self.dispatched_resumes: list[tuple[UUID, UUID]] = []

    async def dispatch_run(
        self, run_id: UUID, workflow_name: str, payload: dict[str, Any]
    ) -> str:
        self.dispatched_runs.append((run_id, workflow_name, payload))
        return f"inproc-run-{run_id}"

    async def dispatch_resume(self, run_id: UUID, approval_id: UUID) -> str:
        self.dispatched_resumes.append((run_id, approval_id))
        return f"inproc-resume-{run_id}-{approval_id}"


class ServerContext:
    """Shared dependencies mounted on `app.state.ctx`."""

    def __init__(
        self,
        *,
        settings: RunLatchSettings,
        store: SQLAlchemyCheckpointStore,
        dispatcher: TaskDispatcher,
        idempotency: IdempotencyBackend,
        streamer: EventStreamBroker,
    ) -> None:
        self.settings = settings
        self.store = store
        self.dispatcher = dispatcher
        self.idempotency = idempotency
        self.streamer = streamer
        self.approval_locks: dict[UUID, asyncio.Lock] = {}

    def get_approval_lock(self, approval_id: UUID) -> asyncio.Lock:
        lock = self.approval_locks.get(approval_id)
        if lock is None:
            lock = asyncio.Lock()
            self.approval_locks[approval_id] = lock
        return lock


def get_ctx(request: Request) -> ServerContext:
    """FastAPI dependency returning the active ServerContext."""
    ctx: ServerContext = request.app.state.ctx
    return ctx

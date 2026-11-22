"""FastAPI application factory for runlatch Control Plane."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings
from runlatch.core.protocols import TaskDispatcher
from runlatch.runtime.server.deps import (
    EventStreamBroker,
    IdempotencyBackend,
    InMemoryIdempotencyBackend,
    InProcessTaskDispatcher,
    ServerContext,
)
from runlatch.runtime.server.routes.approvals import router as approvals_router
from runlatch.runtime.server.routes.health import router as health_router
from runlatch.runtime.server.routes.metrics import router as metrics_router
from runlatch.runtime.server.routes.runs import router as runs_router
from runlatch.runtime.server.routes.ui import router as ui_router
from runlatch.runtime.server.routes.webhooks import router as webhooks_router


def create_app(
    *,
    settings: RunLatchSettings | None = None,
    store: SQLAlchemyCheckpointStore | None = None,
    dispatcher: TaskDispatcher | None = None,
    idempotency: IdempotencyBackend | None = None,
    streamer: EventStreamBroker | None = None,
) -> FastAPI:
    """Create and configure the FastAPI control plane application."""
    active_settings = settings or RunLatchSettings.load()
    active_store = store or SQLAlchemyCheckpointStore(active_settings.database_url)
    active_dispatcher = dispatcher or InProcessTaskDispatcher()
    active_idempotency = idempotency or InMemoryIdempotencyBackend()
    active_streamer = streamer or EventStreamBroker()
    owns_store = store is None

    ctx = ServerContext(
        settings=active_settings,
        store=active_store,
        dispatcher=active_dispatcher,
        idempotency=active_idempotency,
        streamer=active_streamer,
    )

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        del app_instance
        await active_store.initialize()
        try:
            yield
        finally:
            if owns_store:
                await active_store.dispose()

    app = FastAPI(
        title="runlatch Control Plane",
        description="Durable, HITL-First Execution Runtime for Autonomous Developer Agents",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.ctx = ctx

    app.include_router(health_router)
    app.include_router(runs_router)
    app.include_router(approvals_router)
    app.include_router(webhooks_router)
    app.include_router(metrics_router)
    app.include_router(ui_router)
    return app

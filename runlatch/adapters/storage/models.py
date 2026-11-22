"""Unified SQLAlchemy 2.0 async ORM models supporting SQLite and PostgreSQL.

Imports ONLY Python stdlib, sqlalchemy, and runlatch.core.types.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import CHAR, TypeDecorator

from runlatch.core.types import utc_now


class UUIDType(TypeDecorator[UUID]):
    """Dialect-aware UUID column (native UUID on PostgreSQL, CHAR(36) on SQLite)."""

    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: UUID | str | None, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, UUID) else UUID(str(value))
        return str(value)

    def process_result_value(self, value: Any, dialect: Any) -> UUID | None:
        del dialect
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        return UUID(str(value))


class JSONType(TypeDecorator[Any]):
    """Dialect-aware JSON column (JSONB on PostgreSQL, JSON text on SQLite)."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return json.dumps(value, default=str)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        del dialect
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            return json.loads(value) if value else None
        return value


class Base(DeclarativeBase):
    """Declarative base for all runlatch persistence models."""


class WorkflowRunModel(Base):
    """Durable workflow execution and checkpoint record (workflow_runs)."""

    __tablename__ = "workflow_runs"

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    workflow_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(64), nullable=False, default="cli")
    repository: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)
    context_state: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)
    messages_checkpoint: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType(), nullable=False, default=list
    )
    pending_tool_call: Mapped[dict[str, Any] | None] = mapped_column(JSONType(), nullable=True)
    remaining_tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType(), nullable=False, default=list
    )
    pending_approval_id: Mapped[UUID | None] = mapped_column(UUIDType(), nullable=True)
    workspace_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONType(), nullable=True)
    workspace_patch: Mapped[str | None] = mapped_column(Text(), nullable=True)
    current_step_index: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    steps_taken: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    total_input_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    max_cost_usd: Mapped[float] = mapped_column(Float(), nullable=False, default=0.50)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        Index("idx_workflow_runs_status_created", "status", "created_at"),
        Index("idx_workflow_runs_name_created", "workflow_name", "created_at"),
    )


class RunStepModel(Base):
    """Individual step executions within a workflow run (run_steps)."""

    __tablename__ = "run_steps"

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    __table_args__ = (Index("idx_run_steps_run_id", "run_id", "step_index"),)


class ToolCallModel(Base):
    """Immutable audit log of every tool invocation (tool_calls)."""

    __tablename__ = "tool_calls"

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    step_id: Mapped[UUID | None] = mapped_column(UUIDType(), nullable=True)
    run_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    redacted_args: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)
    redacted_output: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    __table_args__ = (Index("idx_tool_calls_run_id", "run_id", "created_at"),)


class ApprovalRequestModel(Base):
    """Human-in-the-Loop approval gate records (approval_requests)."""

    __tablename__ = "approval_requests"

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    tool_call_id: Mapped[UUID | None] = mapped_column(UUIDType(), nullable=True)
    call_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    sanitized_args: Mapped[dict[str, Any]] = mapped_column(
        JSONType(), nullable=False, default=dict
    )
    diff_preview: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="PENDING")
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    __table_args__ = (Index("idx_approvals_status_expires", "status", "expires_at"),)


class NotificationDeliveryModel(Base):
    """Async notification delivery audit trail (notification_deliveries)."""

    __tablename__ = "notification_deliveries"

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    run_id: Mapped[UUID | None] = mapped_column(
        UUIDType(), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    __table_args__ = (Index("idx_notifications_run_id", "run_id", "created_at"),)


class WebhookEventModel(Base):
    """Webhook ingestion log & idempotency ledger (webhook_events)."""

    __tablename__ = "webhook_events"

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    repository: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType(), nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    triggered_run_id: Mapped[UUID | None] = mapped_column(
        UUIDType(), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

"""Initial 6-table schema for runlatch.

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-11-22 19:50:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from runlatch.adapters.storage.models import JSONType, UUIDType

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. workflow_runs
    op.create_table(
        "workflow_runs",
        sa.Column("id", UUIDType(), primary_key=True, nullable=False),
        sa.Column("workflow_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("trigger_source", sa.String(length=64), nullable=False),
        sa.Column("repository", sa.String(length=255), nullable=True),
        sa.Column("input_payload", JSONType(), nullable=False),
        sa.Column("context_state", JSONType(), nullable=False),
        sa.Column("messages_checkpoint", JSONType(), nullable=False),
        sa.Column("pending_tool_call", JSONType(), nullable=True),
        sa.Column("remaining_tool_calls", JSONType(), nullable=False),
        sa.Column("pending_approval_id", UUIDType(), nullable=True),
        sa.Column("workspace_meta", JSONType(), nullable=True),
        sa.Column("workspace_patch", sa.Text(), nullable=True),
        sa.Column("current_step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("steps_taken", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cost_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("max_cost_usd", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_workflow_runs_status_created",
        "workflow_runs",
        ["status", "created_at"],
    )
    op.create_index(
        "idx_workflow_runs_name_created",
        "workflow_runs",
        ["workflow_name", "created_at"],
    )

    # 2. run_steps
    op.create_table(
        "run_steps",
        sa.Column("id", UUIDType(), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            UUIDType(),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(length=255), nullable=False),
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("model_used", sa.String(length=128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_payload", JSONType(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_run_steps_run_id", "run_steps", ["run_id", "step_index"])

    # 3. tool_calls
    op.create_table(
        "tool_calls",
        sa.Column("id", UUIDType(), primary_key=True, nullable=False),
        sa.Column("step_id", UUIDType(), nullable=True),
        sa.Column(
            "run_id",
            UUIDType(),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("call_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("risk_tier", sa.String(length=32), nullable=False),
        sa.Column("redacted_args", JSONType(), nullable=False),
        sa.Column("redacted_output", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_tool_calls_run_id", "tool_calls", ["run_id", "created_at"])

    # 4. approval_requests
    op.create_table(
        "approval_requests",
        sa.Column("id", UUIDType(), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            UUIDType(),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_call_id", UUIDType(), nullable=True),
        sa.Column("call_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("risk_tier", sa.String(length=32), nullable=False),
        sa.Column("sanitized_args", JSONType(), nullable=False),
        sa.Column("diff_preview", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="PENDING"),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_approvals_status_expires", "approval_requests", ["status", "expires_at"]
    )

    # 5. notification_deliveries
    op.create_table(
        "notification_deliveries",
        sa.Column("id", UUIDType(), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            UUIDType(),
            sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_notifications_run_id", "notification_deliveries", ["run_id", "created_at"]
    )

    # 6. webhook_events
    op.create_table(
        "webhook_events",
        sa.Column("id", UUIDType(), primary_key=True, nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("delivery_id", sa.String(length=255), nullable=False, unique=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("repository", sa.String(length=255), nullable=True),
        sa.Column("payload", JSONType(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column(
            "triggered_run_id",
            UUIDType(),
            sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_index("idx_notifications_run_id", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_index("idx_approvals_status_expires", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_index("idx_tool_calls_run_id", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("idx_run_steps_run_id", table_name="run_steps")
    op.drop_table("run_steps")
    op.drop_index("idx_workflow_runs_name_created", table_name="workflow_runs")
    op.drop_index("idx_workflow_runs_status_created", table_name="workflow_runs")
    op.drop_table("workflow_runs")

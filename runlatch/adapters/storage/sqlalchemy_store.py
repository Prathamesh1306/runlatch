"""Unified async SQLAlchemy 2.0 CheckpointStore for SQLite and PostgreSQL.

Imports ONLY Python stdlib, sqlalchemy, runlatch.core.*, and runlatch.adapters.storage.models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from runlatch.adapters.storage.models import (
    ApprovalRequestModel,
    Base,
    NotificationDeliveryModel,
    RunStepModel,
    ToolCallModel,
    WebhookEventModel,
    WorkflowRunModel,
)
from runlatch.core.exceptions import (
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    RunLatchError,
)
from runlatch.core.types import (
    ApprovalRequestRecord,
    ApprovalStatus,
    Message,
    RiskTier,
    RunCheckpoint,
    RunStatus,
    StepRecord,
    StepStatus,
    ToolCall,
    ToolCallRecord,
    ToolCallStatus,
    WorkspaceMeta,
    utc_now,
)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SQLAlchemyCheckpointStore:
    """Unified async SQLAlchemy 2.0 CheckpointStore (`sqlite+aiosqlite` & `postgresql+asyncpg`)."""

    def __init__(
        self,
        database_url: str = "sqlite+aiosqlite:///./.runlatch.db",
        *,
        engine: AsyncEngine | None = None,
        use_null_pool: bool = False,
    ) -> None:
        self.database_url = database_url
        if engine is not None:
            self._engine = engine
        elif use_null_pool:
            self._engine = create_async_engine(database_url, poolclass=NullPool)
        else:
            self._engine = create_async_engine(database_url)

        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )
        self._initialized = False

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def initialize(self) -> None:
        """Create tables if they do not exist."""
        if self._initialized:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._initialized = True

    async def dispose(self) -> None:
        """Dispose the underlying async engine pool."""
        await self._engine.dispose()

    async def save_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        """Insert or update a WorkflowRunModel checkpoint row."""
        await self.initialize()
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.get(WorkflowRunModel, checkpoint.run_id)
                msgs_dump = [m.model_dump(mode="json") for m in checkpoint.messages_checkpoint]
                pending_tc = (
                    checkpoint.pending_tool_call.model_dump(mode="json")
                    if checkpoint.pending_tool_call
                    else None
                )
                remaining_tcs = [
                    tc.model_dump(mode="json") for tc in checkpoint.remaining_tool_calls
                ]
                ws_meta = (
                    checkpoint.workspace_meta.model_dump(mode="json")
                    if checkpoint.workspace_meta
                    else None
                )

                if existing is None:
                    row = WorkflowRunModel(
                        id=checkpoint.run_id,
                        workflow_name=checkpoint.workflow_name,
                        status=checkpoint.status.value,
                        trigger_source=checkpoint.trigger_source,
                        repository=checkpoint.repository,
                        input_payload=checkpoint.input_payload,
                        context_state=checkpoint.context_state,
                        messages_checkpoint=msgs_dump,
                        pending_tool_call=pending_tc,
                        remaining_tool_calls=remaining_tcs,
                        pending_approval_id=checkpoint.pending_approval_id,
                        workspace_meta=ws_meta,
                        workspace_patch=checkpoint.workspace_patch,
                        current_step_index=checkpoint.current_step_index,
                        steps_taken=checkpoint.steps_taken,
                        total_input_tokens=checkpoint.total_input_tokens,
                        total_output_tokens=checkpoint.total_output_tokens,
                        total_cost_usd=checkpoint.total_cost_usd,
                        max_cost_usd=checkpoint.max_cost_usd,
                        error_message=checkpoint.error_message,
                        started_at=checkpoint.started_at,
                        completed_at=checkpoint.completed_at,
                        created_at=checkpoint.created_at,
                        updated_at=utc_now(),
                    )
                    session.add(row)
                else:
                    existing.workflow_name = checkpoint.workflow_name
                    existing.status = checkpoint.status.value
                    existing.trigger_source = checkpoint.trigger_source
                    existing.repository = checkpoint.repository
                    existing.input_payload = checkpoint.input_payload
                    existing.context_state = checkpoint.context_state
                    existing.messages_checkpoint = msgs_dump
                    existing.pending_tool_call = pending_tc
                    existing.remaining_tool_calls = remaining_tcs
                    existing.pending_approval_id = checkpoint.pending_approval_id
                    existing.workspace_meta = ws_meta
                    existing.workspace_patch = checkpoint.workspace_patch
                    existing.current_step_index = checkpoint.current_step_index
                    existing.steps_taken = checkpoint.steps_taken
                    existing.total_input_tokens = checkpoint.total_input_tokens
                    existing.total_output_tokens = checkpoint.total_output_tokens
                    existing.total_cost_usd = checkpoint.total_cost_usd
                    existing.max_cost_usd = checkpoint.max_cost_usd
                    existing.error_message = checkpoint.error_message
                    existing.completed_at = checkpoint.completed_at
                    existing.updated_at = utc_now()

    @staticmethod
    def _row_to_checkpoint(row: WorkflowRunModel) -> RunCheckpoint:
        trigger = cast(
            Literal["cli", "api", "github_webhook", "schedule"],
            row.trigger_source
            if row.trigger_source in ("cli", "api", "github_webhook", "schedule")
            else "cli",
        )
        return RunCheckpoint(
            run_id=row.id,
            workflow_name=row.workflow_name,
            status=RunStatus(row.status),
            trigger_source=trigger,
            repository=row.repository,
            input_payload=row.input_payload or {},
            context_state=row.context_state or {},
            messages_checkpoint=[
                Message.model_validate(m) for m in (row.messages_checkpoint or [])
            ],
            pending_tool_call=(
                ToolCall.model_validate(row.pending_tool_call)
                if row.pending_tool_call
                else None
            ),
            remaining_tool_calls=[
                ToolCall.model_validate(tc) for tc in (row.remaining_tool_calls or [])
            ],
            pending_approval_id=row.pending_approval_id,
            workspace_meta=(
                WorkspaceMeta.model_validate(row.workspace_meta)
                if row.workspace_meta
                else None
            ),
            workspace_patch=row.workspace_patch,
            current_step_index=row.current_step_index,
            steps_taken=row.steps_taken,
            total_input_tokens=row.total_input_tokens,
            total_output_tokens=row.total_output_tokens,
            total_cost_usd=float(row.total_cost_usd),
            max_cost_usd=float(row.max_cost_usd),
            error_message=row.error_message,
            started_at=row.started_at,
            completed_at=row.completed_at,
            created_at=row.created_at,
        )

    async def get_checkpoint(self, run_id: UUID) -> RunCheckpoint | None:
        await self.initialize()
        async with self._session_factory() as session:
            row = await session.get(WorkflowRunModel, run_id)
            if row is None:
                return None
            return self._row_to_checkpoint(row)

    async def list_checkpoints(
        self,
        *,
        status: str | None = None,
        workflow_name: str | None = None,
        limit: int = 50,
    ) -> list[RunCheckpoint]:
        await self.initialize()
        async with self._session_factory() as session:
            stmt = select(WorkflowRunModel).order_by(WorkflowRunModel.created_at.desc())
            if status:
                stmt = stmt.where(WorkflowRunModel.status == status)
            if workflow_name:
                stmt = stmt.where(WorkflowRunModel.workflow_name == workflow_name)
            stmt = stmt.limit(limit)
            rows = (await session.scalars(stmt)).all()
            return [self._row_to_checkpoint(r) for r in rows]

    async def save_step(self, step: StepRecord) -> None:
        await self.initialize()
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.get(RunStepModel, step.id)
                if existing is None:
                    session.add(
                        RunStepModel(
                            id=step.id,
                            run_id=step.run_id,
                            step_index=step.step_index,
                            step_name=step.step_name,
                            agent_name=step.agent_name,
                            status=step.status.value,
                            model_used=step.model_used,
                            input_tokens=step.input_tokens,
                            output_tokens=step.output_tokens,
                            cost_usd=step.cost_usd,
                            latency_ms=step.latency_ms,
                            output_payload=step.output_payload,
                            error_message=step.error_message,
                            created_at=step.created_at,
                        )
                    )
                else:
                    existing.status = step.status.value
                    existing.input_tokens = step.input_tokens
                    existing.output_tokens = step.output_tokens
                    existing.cost_usd = step.cost_usd
                    existing.latency_ms = step.latency_ms
                    existing.output_payload = step.output_payload
                    existing.error_message = step.error_message

    async def list_steps(self, run_id: UUID) -> list[StepRecord]:
        await self.initialize()
        async with self._session_factory() as session:
            stmt = (
                select(RunStepModel)
                .where(RunStepModel.run_id == run_id)
                .order_by(RunStepModel.step_index.asc(), RunStepModel.created_at.asc())
            )
            rows = (await session.scalars(stmt)).all()
            return [
                StepRecord(
                    id=r.id,
                    run_id=r.run_id,
                    step_index=r.step_index,
                    step_name=r.step_name,
                    agent_name=r.agent_name,
                    status=StepStatus(r.status),
                    model_used=r.model_used,
                    input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens,
                    cost_usd=float(r.cost_usd),
                    latency_ms=r.latency_ms,
                    output_payload=r.output_payload,
                    error_message=r.error_message,
                    created_at=r.created_at,
                )
                for r in rows
            ]

    async def save_tool_call(self, record: ToolCallRecord) -> None:
        await self.initialize()
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    ToolCallModel(
                        id=record.id,
                        step_id=record.step_id,
                        run_id=record.run_id,
                        call_id=record.call_id,
                        tool_name=record.tool_name,
                        risk_tier=record.risk_tier.value,
                        redacted_args=record.redacted_args,
                        redacted_output=record.redacted_output,
                        status=record.status.value,
                        latency_ms=record.latency_ms,
                        error_message=record.error_message,
                        created_at=record.created_at,
                    )
                )

    async def list_tool_calls(self, run_id: UUID) -> list[ToolCallRecord]:
        await self.initialize()
        async with self._session_factory() as session:
            stmt = (
                select(ToolCallModel)
                .where(ToolCallModel.run_id == run_id)
                .order_by(ToolCallModel.created_at.asc())
            )
            rows = (await session.scalars(stmt)).all()
            return [
                ToolCallRecord(
                    id=r.id,
                    step_id=r.step_id,
                    run_id=r.run_id,
                    call_id=r.call_id,
                    tool_name=r.tool_name,
                    risk_tier=RiskTier(r.risk_tier),
                    redacted_args=r.redacted_args or {},
                    redacted_output=r.redacted_output,
                    status=ToolCallStatus(r.status),
                    latency_ms=r.latency_ms,
                    error_message=r.error_message,
                    created_at=r.created_at,
                )
                for r in rows
            ]

    async def create_approval(self, approval: ApprovalRequestRecord) -> None:
        await self.initialize()
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    ApprovalRequestModel(
                        id=approval.id,
                        run_id=approval.run_id,
                        tool_call_id=approval.tool_call_id,
                        call_id=approval.call_id,
                        tool_name=approval.tool_name,
                        risk_tier=approval.risk_tier.value,
                        sanitized_args=approval.sanitized_args,
                        diff_preview=approval.diff_preview,
                        status=approval.status.value,
                        decided_by=approval.decided_by,
                        decision_reason=approval.decision_reason,
                        expires_at=approval.expires_at,
                        decided_at=approval.decided_at,
                        created_at=approval.created_at,
                    )
                )

    @staticmethod
    def _row_to_approval(row: ApprovalRequestModel) -> ApprovalRequestRecord:
        return ApprovalRequestRecord(
            id=row.id,
            run_id=row.run_id,
            tool_call_id=row.tool_call_id,
            call_id=row.call_id,
            tool_name=row.tool_name,
            risk_tier=RiskTier(row.risk_tier),
            sanitized_args=row.sanitized_args or {},
            diff_preview=row.diff_preview,
            status=ApprovalStatus(row.status),
            decided_by=row.decided_by,
            decision_reason=row.decision_reason,
            expires_at=row.expires_at,
            decided_at=row.decided_at,
            created_at=row.created_at,
        )

    async def get_approval(self, approval_id: UUID) -> ApprovalRequestRecord | None:
        await self.initialize()
        async with self._session_factory() as session:
            row = await session.get(ApprovalRequestModel, approval_id)
            return self._row_to_approval(row) if row else None

    async def list_approvals(
        self, *, status: ApprovalStatus | None = None, run_id: UUID | None = None
    ) -> list[ApprovalRequestRecord]:
        await self.initialize()
        async with self._session_factory() as session:
            stmt = select(ApprovalRequestModel).order_by(ApprovalRequestModel.created_at.desc())
            if status is not None:
                stmt = stmt.where(ApprovalRequestModel.status == status.value)
            if run_id is not None:
                stmt = stmt.where(ApprovalRequestModel.run_id == run_id)
            rows = (await session.scalars(stmt)).all()
            return [self._row_to_approval(r) for r in rows]

    async def decide_approval(
        self,
        approval_id: UUID,
        *,
        approved: bool,
        decided_by: str,
        reason: str = "",
    ) -> ApprovalRequestRecord:
        """Atomically transition a PENDING approval via SELECT FOR UPDATE + conditional UPDATE."""
        await self.initialize()
        now = utc_now()
        new_status = (
            ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value
        )

        async with self._session_factory() as session:
            async with session.begin():
                stmt = (
                    select(ApprovalRequestModel)
                    .where(ApprovalRequestModel.id == approval_id)
                    .with_for_update()
                )
                row = (await session.scalars(stmt)).first()
                if row is None:
                    raise RunLatchError(f"Approval request {approval_id} not found.")
                if row.status != ApprovalStatus.PENDING.value:
                    raise ApprovalAlreadyDecidedError(
                        f"Approval {approval_id} already decided with status '{row.status}'."
                    )
                if _ensure_utc(row.expires_at) < now:
                    row.status = ApprovalStatus.EXPIRED.value
                    raise ApprovalExpiredError(
                        f"Approval {approval_id} expired at {row.expires_at.isoformat()}."
                    )

                # Atomic conditional UPDATE guarantees single-winner even on SQLite
                upd = (
                    update(ApprovalRequestModel)
                    .where(
                        ApprovalRequestModel.id == approval_id,
                        ApprovalRequestModel.status == ApprovalStatus.PENDING.value,
                    )
                    .values(
                        status=new_status,
                        decided_by=decided_by,
                        decision_reason=reason,
                        decided_at=now,
                    )
                )
                result = await session.execute(upd)
                rowcount = getattr(result, "rowcount", 1)
                if rowcount == 0:
                    raise ApprovalAlreadyDecidedError(
                        f"Approval {approval_id} was concurrently decided by another request."
                    )

                await session.refresh(row)
                return self._row_to_approval(row)

    async def expire_stale_approvals(self) -> list[ApprovalRequestRecord]:
        """Mark PENDING approvals past their expires_at as EXPIRED and cancel their runs."""
        await self.initialize()
        now = utc_now()
        expired_records: list[ApprovalRequestRecord] = []
        async with self._session_factory() as session:
            async with session.begin():
                stmt = select(ApprovalRequestModel).where(
                    ApprovalRequestModel.status == ApprovalStatus.PENDING.value
                )
                rows = (await session.scalars(stmt)).all()
                for row in rows:
                    if _ensure_utc(row.expires_at) < now:
                        row.status = ApprovalStatus.EXPIRED.value
                        row.decided_at = now
                        row.decision_reason = "Approval timed out"
                        expired_records.append(self._row_to_approval(row))
                        run_row = await session.get(WorkflowRunModel, row.run_id)
                        if run_row and run_row.status == RunStatus.WAITING_APPROVAL.value:
                            run_row.status = RunStatus.CANCELLED.value
                            run_row.error_message = "Cancelled due to expired HITL approval."
                            run_row.completed_at = now
                            run_row.updated_at = now
        return expired_records

    async def record_webhook_event(
        self,
        *,
        provider: str,
        delivery_id: str,
        event_type: str,
        repository: str | None,
        payload: dict[str, Any],
        status: str,
        triggered_run_id: UUID | None = None,
    ) -> bool:
        """Persist a webhook event. Returns False if delivery_id already exists (duplicate)."""
        await self.initialize()
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    existing = (
                        await session.scalars(
                            select(WebhookEventModel).where(
                                WebhookEventModel.delivery_id == delivery_id
                            )
                        )
                    ).first()
                    if existing is not None:
                        return False
                    session.add(
                        WebhookEventModel(
                            id=uuid4(),
                            provider=provider,
                            delivery_id=delivery_id,
                            event_type=event_type,
                            repository=repository,
                            payload=payload,
                            status=status,
                            triggered_run_id=triggered_run_id,
                            received_at=utc_now(),
                        )
                    )
            return True
        except IntegrityError:
            return False

    async def create_notification_delivery(
        self,
        *,
        run_id: UUID | None,
        event_type: str,
        channel: str,
        target: str,
        status: str = "PENDING",
    ) -> UUID:
        """Create an initial notification_deliveries audit row."""
        await self.initialize()
        delivery_id = uuid4()
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    NotificationDeliveryModel(
                        id=delivery_id,
                        run_id=run_id,
                        event_type=event_type,
                        channel=channel,
                        target=target,
                        status=status,
                        attempt_count=0,
                        created_at=utc_now(),
                    )
                )
        return delivery_id

    async def update_notification_delivery(
        self,
        delivery_id: UUID,
        *,
        status: str,
        attempt_count: int,
        provider_message_id: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """Update a notification_deliveries row after a delivery attempt."""
        await self.initialize()
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(NotificationDeliveryModel, delivery_id)
                if row is not None:
                    row.status = status
                    row.attempt_count = attempt_count
                    row.provider_message_id = provider_message_id
                    row.last_error = last_error
                    if status == "SENT":
                        row.sent_at = utc_now()

    async def list_notification_deliveries(
        self, *, run_id: UUID | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return notification delivery audit records."""
        await self.initialize()
        async with self._session_factory() as session:
            stmt = select(NotificationDeliveryModel).order_by(
                NotificationDeliveryModel.created_at.desc()
            )
            if run_id is not None:
                stmt = stmt.where(NotificationDeliveryModel.run_id == run_id)
            stmt = stmt.limit(limit)
            rows = (await session.scalars(stmt)).all()
            return [
                {
                    "id": str(r.id),
                    "run_id": str(r.run_id) if r.run_id else None,
                    "event_type": r.event_type,
                    "channel": r.channel,
                    "target": r.target,
                    "status": r.status,
                    "attempt_count": r.attempt_count,
                    "provider_message_id": r.provider_message_id,
                    "last_error": r.last_error,
                    "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]

    async def get_metrics_summary(self) -> dict[str, Any]:
        """Compute aggregate execution, token, and USD cost metrics across all runs."""
        await self.initialize()
        async with self._session_factory() as session:
            rows = (await session.scalars(select(WorkflowRunModel))).all()
            total_runs = len(rows)
            by_status: dict[str, int] = {}
            by_workflow: dict[str, dict[str, Any]] = {}
            total_tokens = 0
            total_cost = 0.0
            completed_count = 0

            for r in rows:
                by_status[r.status] = by_status.get(r.status, 0) + 1
                if r.status == RunStatus.COMPLETED.value:
                    completed_count += 1
                tok = r.total_input_tokens + r.total_output_tokens
                cost = float(r.total_cost_usd)
                total_tokens += tok
                total_cost = round(total_cost + cost, 6)

                wf_bucket = by_workflow.setdefault(
                    r.workflow_name,
                    {"runs": 0, "completed": 0, "tokens": 0, "cost_usd": 0.0},
                )
                wf_bucket["runs"] += 1
                if r.status == RunStatus.COMPLETED.value:
                    wf_bucket["completed"] += 1
                wf_bucket["tokens"] += tok
                wf_bucket["cost_usd"] = round(wf_bucket["cost_usd"] + cost, 6)

            success_rate = round((completed_count / total_runs) * 100.0, 2) if total_runs else 0.0
            return {
                "total_runs": total_runs,
                "completed_runs": completed_count,
                "success_rate_pct": success_rate,
                "total_tokens": total_tokens,
                "total_cost_usd": total_cost,
                "by_status": by_status,
                "by_workflow": by_workflow,
            }

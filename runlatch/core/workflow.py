"""Multi-step Workflow & Step orchestration engine with cross-step budget pooling and HITL resume.

Pure domain module: imports ONLY Python stdlib and runlatch.core.*.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from runlatch.core.agent import Agent
from runlatch.core.events import EventBus
from runlatch.core.exceptions import BudgetExceededError, RunLatchError
from runlatch.core.protocols import CheckpointStore, StepExecutor
from runlatch.core.types import (
    BudgetConfig,
    LifecycleEvent,
    RunCheckpoint,
    RunResult,
    RunStatus,
    StepRecord,
    StepStatus,
    ToolCallRecord,
    utc_now,
)


class Step:
    """Single step in a multi-step Workflow."""

    def __init__(
        self,
        name: str,
        agent: StepExecutor,
        *,
        prompt: str | Callable[[dict[str, Any]], str] | None = None,
        output_key: str | None = None,
        condition: Callable[[dict[str, Any]], bool] | None = None,
        retries: int = 0,
    ) -> None:
        self.name = name
        self.agent = agent
        self.prompt = prompt
        self.output_key = output_key or f"{name}_output"
        self.condition = condition
        self.retries = max(retries, 0)

    def build_prompt(self, context: dict[str, Any]) -> str:
        """Render step prompt from shared WorkflowContext."""
        if callable(self.prompt):
            return self.prompt(context)
        if isinstance(self.prompt, str) and self.prompt:
            try:
                return self.prompt.format(**context)
            except KeyError:
                return self.prompt
        if "prompt" in context and isinstance(context["prompt"], str):
            return str(context["prompt"])
        serialized = json.dumps(context, default=str)
        return f"Execute workflow step '{self.name}' with context: {serialized}"


class Workflow:
    """Composable multi-step task execution engine with pooled budgets and durable checkpoints."""

    def __init__(
        self,
        name: str,
        *,
        max_cost_usd: float = 0.50,
        max_tokens: int = 80000,
        max_steps_per_agent: int = 15,
        store: CheckpointStore | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.name = name
        self.max_cost_usd = max_cost_usd
        self.max_tokens = max_tokens
        self.max_steps_per_agent = max_steps_per_agent
        self._steps: list[Step] = []
        self._store = store
        self._event_bus = event_bus or EventBus()

    @property
    def steps(self) -> list[Step]:
        return list(self._steps)

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    def add_step(self, step: Step) -> Workflow:
        """Append a step to the workflow pipeline (fluent builder)."""
        self._steps.append(step)
        return self

    async def _emit(
        self, event_type: str, run_id: UUID, step_name: str | None = None, **payload: Any
    ) -> None:
        await self._event_bus.emit(
            LifecycleEvent(
                event_type=event_type,
                run_id=run_id,
                workflow_name=self.name,
                step_name=step_name,
                payload=payload,
            )
        )

    async def run(
        self,
        initial_input: dict[str, Any] | str | None = None,
        *,
        run_id: UUID | None = None,
        store: CheckpointStore | None = None,
    ) -> RunResult:
        """Execute workflow from Step 0."""
        active_run_id = run_id or uuid4()
        active_store = store or self._store
        if active_store is not None:
            await active_store.initialize()

        if isinstance(initial_input, str):
            context: dict[str, Any] = {"prompt": initial_input}
            input_payload: dict[str, Any] = {"prompt": initial_input}
        elif isinstance(initial_input, dict):
            context = dict(initial_input)
            input_payload = dict(initial_input)
        else:
            context = {}
            input_payload = {}

        await self._emit("workflow.started", active_run_id)
        return await self._execute_from_index(
            run_id=active_run_id,
            start_index=0,
            input_payload=input_payload,
            context=context,
            store=active_store,
            prior_in_tokens=0,
            prior_out_tokens=0,
            prior_cost_usd=0.0,
            prior_steps=[],
            prior_tool_calls=[],
            resumed_step_result=None,
        )

    async def resume(
        self,
        run_id: UUID,
        *,
        approved: bool,
        decided_by: str = "human",
        reason: str = "",
        store: CheckpointStore | None = None,
    ) -> RunResult:
        """Resume a paused WAITING_APPROVAL workflow at its checkpointed step."""
        active_store = store or self._store
        if active_store is None:
            raise RunLatchError("Cannot resume a Workflow without a CheckpointStore.")
        await active_store.initialize()

        checkpoint = await active_store.get_checkpoint(run_id)
        if checkpoint is None:
            raise RunLatchError(f"Workflow checkpoint {run_id} not found.")
        if checkpoint.status != RunStatus.WAITING_APPROVAL:
            raise RunLatchError(
                f"Workflow {run_id} is in status '{checkpoint.status}', not WAITING_APPROVAL."
            )

        idx = checkpoint.current_step_index
        if idx < 0 or idx >= len(self._steps):
            raise RunLatchError(f"Invalid current_step_index {idx} for workflow '{self.name}'.")

        paused_step = self._steps[idx]
        if not isinstance(paused_step.agent, Agent):
            raise RunLatchError(
                f"Step '{paused_step.name}' executor does not support HITL agent resume."
            )

        step_start = time.perf_counter()
        step_result = await paused_step.agent.resume(
            run_id,
            approved=approved,
            decided_by=decided_by,
            reason=reason,
            store=active_store,
        )
        step_latency = int((time.perf_counter() - step_start) * 1000)

        prior_steps = await active_store.list_steps(run_id)
        context = dict(checkpoint.context_state)

        if step_result.status == RunStatus.WAITING_APPROVAL:
            return step_result

        context[paused_step.output_key] = step_result.output
        context["last_output"] = step_result.output
        step_rec = StepRecord(
            run_id=run_id,
            step_index=idx,
            step_name=paused_step.name,
            agent_name=paused_step.agent.name,
            status=StepStatus.COMPLETED,
            input_tokens=step_result.total_input_tokens,
            output_tokens=step_result.total_output_tokens,
            cost_usd=step_result.total_cost_usd,
            latency_ms=step_latency,
            output_payload={"output": step_result.output},
        )
        await active_store.save_step(step_rec)
        prior_steps.append(step_rec)

        return await self._execute_from_index(
            run_id=run_id,
            start_index=idx + 1,
            input_payload=checkpoint.input_payload,
            context=context,
            store=active_store,
            prior_in_tokens=step_result.total_input_tokens,
            prior_out_tokens=step_result.total_output_tokens,
            prior_cost_usd=step_result.total_cost_usd,
            prior_steps=prior_steps,
            prior_tool_calls=list(step_result.tool_calls),
            resumed_step_result=step_result,
        )

    async def _execute_from_index(
        self,
        *,
        run_id: UUID,
        start_index: int,
        input_payload: dict[str, Any],
        context: dict[str, Any],
        store: CheckpointStore | None,
        prior_in_tokens: int,
        prior_out_tokens: int,
        prior_cost_usd: float,
        prior_steps: list[StepRecord],
        prior_tool_calls: list[ToolCallRecord],
        resumed_step_result: RunResult | None,
    ) -> RunResult:
        total_in = prior_in_tokens
        total_out = prior_out_tokens
        total_cost = prior_cost_usd
        step_records: list[StepRecord] = list(prior_steps)
        all_tool_calls: list[ToolCallRecord] = list(prior_tool_calls)
        last_output = resumed_step_result.output if resumed_step_result else ""
        total_agent_steps = resumed_step_result.steps_taken if resumed_step_result else 0

        for idx in range(start_index, len(self._steps)):
            step = self._steps[idx]

            # Evaluate conditional predicate
            if step.condition is not None and not step.condition(context):
                skip_rec = StepRecord(
                    run_id=run_id,
                    step_index=idx,
                    step_name=step.name,
                    agent_name=step.agent.name,
                    status=StepStatus.SKIPPED,
                )
                step_records.append(skip_rec)
                if store is not None:
                    await store.save_step(skip_rec)
                await self._emit("step.skipped", run_id, step_name=step.name)
                continue

            # Compute pooled remaining budget for this step
            rem_tokens = self.max_tokens - (total_in + total_out)
            rem_cost = round(self.max_cost_usd - total_cost, 8)
            if rem_tokens <= 0:
                raise BudgetExceededError(
                    f"Workflow '{self.name}' exhausted max_tokens ({self.max_tokens}).",
                    metric="max_tokens",
                    current=total_in + total_out,
                    limit=self.max_tokens,
                )
            if rem_cost <= 0:
                raise BudgetExceededError(
                    f"Workflow '{self.name}' exhausted max_cost_usd (${self.max_cost_usd:.4f}).",
                    metric="max_cost_usd",
                    current=total_cost,
                    limit=self.max_cost_usd,
                )

            step_budget = BudgetConfig(
                max_steps=self.max_steps_per_agent,
                max_tokens=rem_tokens,
                max_cost_usd=rem_cost,
            )
            prompt = step.build_prompt(context)
            await self._emit("step.started", run_id, step_name=step.name, step_index=idx)

            attempt = 0
            step_res: RunResult | None = None
            start_ts = time.perf_counter()
            while True:
                try:
                    step_res = await step.agent.execute_step(
                        prompt,
                        context=context,
                        run_id=run_id,
                        workflow_name=self.name,
                        step_index=idx,
                        budget=step_budget,
                        store=store,
                    )
                    break
                except (BudgetExceededError, RunLatchError):
                    if attempt < step.retries:
                        attempt += 1
                        continue
                    raise

            latency_ms = int((time.perf_counter() - start_ts) * 1000)
            total_in += step_res.total_input_tokens
            total_out += step_res.total_output_tokens
            total_cost = round(total_cost + step_res.total_cost_usd, 8)
            total_agent_steps += step_res.steps_taken
            all_tool_calls.extend(step_res.tool_calls)

            if step_res.status == RunStatus.WAITING_APPROVAL:
                paused_rec = StepRecord(
                    run_id=run_id,
                    step_index=idx,
                    step_name=step.name,
                    agent_name=step.agent.name,
                    status=StepStatus.PAUSED,
                    input_tokens=step_res.total_input_tokens,
                    output_tokens=step_res.total_output_tokens,
                    cost_usd=step_res.total_cost_usd,
                    latency_ms=latency_ms,
                )
                step_records.append(paused_rec)
                if store is not None:
                    await store.save_step(paused_rec)
                    # Ensure checkpoint stores cumulative workflow context & token/cost totals
                    cp = await store.get_checkpoint(run_id)
                    if cp is not None:
                        cp.workflow_name = self.name
                        cp.input_payload = input_payload
                        cp.context_state = context
                        cp.current_step_index = idx
                        cp.total_input_tokens = total_in
                        cp.total_output_tokens = total_out
                        cp.total_cost_usd = total_cost
                        cp.max_cost_usd = self.max_cost_usd
                        await store.save_checkpoint(cp)

                await self._emit(
                    "workflow.paused",
                    run_id,
                    step_name=step.name,
                    approval_id=str(step_res.approval_id),
                )
                return RunResult(
                    run_id=run_id,
                    workflow_name=self.name,
                    status=RunStatus.WAITING_APPROVAL,
                    output=step_res.output,
                    context_state=context,
                    messages=step_res.messages,
                    tool_calls=all_tool_calls,
                    steps=step_records,
                    approval_id=step_res.approval_id,
                    pending_tool_call=step_res.pending_tool_call,
                    workspace_patch=step_res.workspace_patch,
                    workspace_meta=step_res.workspace_meta,
                    total_input_tokens=total_in,
                    total_output_tokens=total_out,
                    total_tokens=total_in + total_out,
                    total_cost_usd=total_cost,
                    steps_taken=total_agent_steps,
                )

            # Step completed
            last_output = step_res.output
            context[step.output_key] = step_res.output
            context["last_output"] = step_res.output

            comp_rec = StepRecord(
                run_id=run_id,
                step_index=idx,
                step_name=step.name,
                agent_name=step.agent.name,
                status=StepStatus.COMPLETED,
                input_tokens=step_res.total_input_tokens,
                output_tokens=step_res.total_output_tokens,
                cost_usd=step_res.total_cost_usd,
                latency_ms=latency_ms,
                output_payload={"output": step_res.output},
            )
            step_records.append(comp_rec)
            if store is not None:
                await store.save_step(comp_rec)
                await store.save_checkpoint(
                    RunCheckpoint(
                        run_id=run_id,
                        workflow_name=self.name,
                        status=RunStatus.RUNNING,
                        input_payload=input_payload,
                        context_state=context,
                        current_step_index=idx + 1,
                        total_input_tokens=total_in,
                        total_output_tokens=total_out,
                        total_cost_usd=total_cost,
                        max_cost_usd=self.max_cost_usd,
                    )
                )
            await self._emit("step.completed", run_id, step_name=step.name, step_index=idx)

        if store is not None:
            await store.save_checkpoint(
                RunCheckpoint(
                    run_id=run_id,
                    workflow_name=self.name,
                    status=RunStatus.COMPLETED,
                    input_payload=input_payload,
                    context_state=context,
                    current_step_index=len(self._steps),
                    total_input_tokens=total_in,
                    total_output_tokens=total_out,
                    total_cost_usd=total_cost,
                    max_cost_usd=self.max_cost_usd,
                    completed_at=utc_now(),
                )
            )
        await self._emit(
            "workflow.completed",
            run_id,
            total_tokens=total_in + total_out,
            total_cost_usd=total_cost,
        )
        return RunResult(
            run_id=run_id,
            workflow_name=self.name,
            status=RunStatus.COMPLETED,
            output=last_output,
            context_state=context,
            tool_calls=all_tool_calls,
            steps=step_records,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_tokens=total_in + total_out,
            total_cost_usd=total_cost,
            steps_taken=total_agent_steps,
        )

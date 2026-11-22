"""Bounded ReAct tool-calling orchestration loop for runlatch.

Pure domain module: imports ONLY Python stdlib, pydantic, and runlatch.core.*.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from runlatch.core.exceptions import (
    BudgetExceededError,
    LoopDetectedError,
    RunLatchError,
)
from runlatch.core.protocols import (
    CheckpointStore,
    LLMProvider,
    PolicyEvaluator,
    RegisteredTool,
    SecretRedactorProtocol,
    WorkspacePatcher,
)
from runlatch.core.types import (
    ApprovalRequestRecord,
    ApprovalStatus,
    BudgetConfig,
    LifecycleEvent,
    Message,
    PolicyAction,
    PolicyDecision,
    RiskTier,
    RunCheckpoint,
    RunResult,
    RunStatus,
    ToolCall,
    ToolCallRecord,
    ToolCallStatus,
    ToolSpec,
    WorkspaceMeta,
    utc_now,
)


def _tool_fingerprint(call: ToolCall) -> str:
    canonical = json.dumps(call.arguments, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{call.name}:{digest}"


class Agent:
    """Bounded tool-calling ReAct agent implementing StepExecutor."""

    def __init__(
        self,
        name: str,
        provider: LLMProvider,
        tools: Sequence[RegisteredTool] = (),
        *,
        system_prompt: str = "You are an autonomous software engineering agent.",
        budget: BudgetConfig | None = None,
        max_steps: int | None = None,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
        max_duplicate_tool_calls: int | None = None,
        policy: PolicyEvaluator | None = None,
        redactor: SecretRedactorProtocol | None = None,
        store: CheckpointStore | None = None,
        patcher: WorkspacePatcher | None = None,
        event_listener: Callable[[LifecycleEvent], Any] | None = None,
    ) -> None:
        self._name = name
        self._provider = provider
        self._tools: dict[str, RegisteredTool] = {t.spec.name: t for t in tools}
        self._system_prompt = system_prompt

        base_budget = budget.model_copy() if budget else BudgetConfig()
        if max_steps is not None:
            base_budget.max_steps = max_steps
        if max_tokens is not None:
            base_budget.max_tokens = max_tokens
        if max_cost_usd is not None:
            base_budget.max_cost_usd = max_cost_usd
        if max_duplicate_tool_calls is not None:
            base_budget.max_duplicate_tool_calls = max_duplicate_tool_calls
        self._budget = base_budget

        self._policy = policy
        self._redactor = redactor
        self._store = store
        self._patcher = patcher
        self._event_listener = event_listener

    @property
    def name(self) -> str:
        return self._name

    @property
    def budget(self) -> BudgetConfig:
        return self._budget

    def _emit(self, event_type: str, run_id: UUID, workflow_name: str, **payload: Any) -> None:
        if self._event_listener is not None:
            self._event_listener(
                LifecycleEvent(
                    event_type=event_type,
                    run_id=run_id,
                    workflow_name=workflow_name,
                    step_name=self._name,
                    payload=payload,
                )
            )

    def _redact_text(self, text: str) -> str:
        if self._redactor is not None:
            return self._redactor.redact_text(text)
        return text

    def _redact_args(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._redactor is not None:
            redacted = self._redactor.redact_obj(args)
            return redacted if isinstance(redacted, dict) else {"value": redacted}
        return dict(args)

    def _evaluate_policy(self, spec: ToolSpec, args: dict[str, Any]) -> PolicyDecision:
        if self._policy is not None:
            return self._policy.evaluate(spec, args)
        if spec.requires_approval or spec.risk_tier == RiskTier.EXTERNAL:
            return PolicyDecision(
                action=PolicyAction.REQUIRE_APPROVAL,
                reason=f"Tool '{spec.name}' (tier={spec.risk_tier}) requires human approval.",
            )
        return PolicyDecision(action=PolicyAction.ALLOW)

    async def execute_step(
        self,
        prompt: str,
        *,
        context: dict[str, Any],
        run_id: UUID | None = None,
        workflow_name: str = "workflow",
        step_index: int = 0,
        budget: BudgetConfig | None = None,
        store: CheckpointStore | None = None,
    ) -> RunResult:
        """StepExecutor protocol entry point used by Workflow."""
        return await self.run(
            prompt,
            run_id=run_id,
            workflow_name=workflow_name,
            step_index=step_index,
            context=context,
            budget=budget,
            store=store,
        )

    async def run(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        workflow_name: str | None = None,
        step_index: int = 0,
        context: dict[str, Any] | None = None,
        budget: BudgetConfig | None = None,
        store: CheckpointStore | None = None,
    ) -> RunResult:
        """Execute a fresh agent task from a user prompt."""
        active_run_id = run_id or uuid4()
        wf_name = workflow_name or self._name
        active_store = store or self._store
        active_budget = budget or self._budget
        ctx = dict(context) if context else {}

        messages: list[Message] = [
            Message(role="system", content=self._system_prompt),
            Message(role="user", content=prompt),
        ]
        if active_store is not None:
            await active_store.initialize()

        return await self._loop(
            run_id=active_run_id,
            workflow_name=wf_name,
            step_index=step_index,
            messages=messages,
            context=ctx,
            budget=active_budget,
            store=active_store,
            initial_in_tokens=0,
            initial_out_tokens=0,
            initial_cost_usd=0.0,
            initial_steps_taken=0,
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
        """Resume a WAITING_APPROVAL agent run from its durable checkpoint."""
        active_store = store or self._store
        if active_store is None:
            raise RunLatchError("Cannot resume a run without a CheckpointStore.")
        await active_store.initialize()

        checkpoint = await active_store.get_checkpoint(run_id)
        if checkpoint is None:
            raise RunLatchError(f"RunCheckpoint {run_id} not found.")
        if checkpoint.status != RunStatus.WAITING_APPROVAL:
            raise RunLatchError(
                f"Run {run_id} is in status '{checkpoint.status}', not WAITING_APPROVAL."
            )
        if checkpoint.pending_tool_call is None:
            raise RunLatchError(f"Run {run_id} has no pending_tool_call to resume.")

        if checkpoint.pending_approval_id is not None:
            existing_appr = await active_store.get_approval(checkpoint.pending_approval_id)
            if existing_appr is not None and existing_appr.status == ApprovalStatus.PENDING:
                await active_store.decide_approval(
                    checkpoint.pending_approval_id,
                    approved=approved,
                    decided_by=decided_by,
                    reason=reason,
                )

        if (
            approved
            and self._patcher is not None
            and checkpoint.workspace_patch
            and checkpoint.workspace_meta
        ):
            await self._patcher.apply_patch_in_worktree(
                checkpoint.workspace_patch, checkpoint.workspace_meta
            )

        messages = list(checkpoint.messages_checkpoint)
        pending_tc = checkpoint.pending_tool_call
        tool_obj = self._tools.get(pending_tc.name)
        records: list[ToolCallRecord] = []

        if approved:
            if tool_obj is None:
                output_text = f"ToolError: Tool '{pending_tc.name}' is not registered."
                tc_status = ToolCallStatus.FAILED
            else:
                record, output_text = await self._invoke_single_tool(
                    run_id=run_id, tool_obj=tool_obj, tc=pending_tc
                )
                records.append(record)
                tc_status = record.status
                await active_store.save_tool_call(record)
            messages.append(
                Message(
                    role="tool",
                    tool_call_id=pending_tc.id,
                    name=pending_tc.name,
                    content=output_text,
                )
            )
            self._emit(
                "approval.resumed",
                run_id,
                checkpoint.workflow_name,
                tool=pending_tc.name,
                approved=True,
                status=str(tc_status),
            )
        else:
            rejection_msg = (
                f"HumanApprovalRejected: Tool '{pending_tc.name}' was rejected by {decided_by}."
                + (f" Reason: {reason}" if reason else "")
            )
            denied_record = ToolCallRecord(
                run_id=run_id,
                call_id=pending_tc.id,
                tool_name=pending_tc.name,
                risk_tier=tool_obj.spec.risk_tier if tool_obj else RiskTier.EXTERNAL,
                redacted_args=self._redact_args(pending_tc.arguments),
                redacted_output=self._redact_text(rejection_msg),
                status=ToolCallStatus.DENIED,
                error_message=reason or "Rejected by human reviewer",
            )
            records.append(denied_record)
            await active_store.save_tool_call(denied_record)
            messages.append(
                Message(
                    role="tool",
                    tool_call_id=pending_tc.id,
                    name=pending_tc.name,
                    content=rejection_msg,
                )
            )
            self._emit(
                "approval.resumed",
                run_id,
                checkpoint.workflow_name,
                tool=pending_tc.name,
                approved=False,
                reason=reason,
            )

        return await self._loop(
            run_id=run_id,
            workflow_name=checkpoint.workflow_name,
            step_index=checkpoint.current_step_index,
            messages=messages,
            context=dict(checkpoint.context_state),
            budget=self._budget,
            store=active_store,
            initial_in_tokens=checkpoint.total_input_tokens,
            initial_out_tokens=checkpoint.total_output_tokens,
            initial_cost_usd=checkpoint.total_cost_usd,
            initial_steps_taken=checkpoint.steps_taken,
            prior_tool_records=records,
            pending_sibling_calls=list(checkpoint.remaining_tool_calls),
        )

    async def _invoke_single_tool(
        self,
        *,
        run_id: UUID,
        tool_obj: RegisteredTool,
        tc: ToolCall,
    ) -> tuple[ToolCallRecord, str]:
        start = time.perf_counter()
        status = ToolCallStatus.SUCCEEDED
        err_msg: str | None = None
        try:
            raw_out = await tool_obj.invoke(tc.arguments)
            if raw_out.startswith("ToolValidationError:"):
                status = ToolCallStatus.FAILED
                err_msg = raw_out
        except TimeoutError:
            status = ToolCallStatus.TIMED_OUT
            err_msg = f"Tool '{tc.name}' timed out after {tool_obj.spec.timeout_s}s."
            raw_out = f"ToolTimeoutError: {err_msg}"
        except Exception as exc:
            status = ToolCallStatus.FAILED
            err_msg = str(exc)
            raw_out = f"ToolExecutionError: {self._redact_text(str(exc))}"

        latency_ms = int((time.perf_counter() - start) * 1000)
        redacted_out = self._redact_text(raw_out)
        record = ToolCallRecord(
            run_id=run_id,
            call_id=tc.id,
            tool_name=tc.name,
            risk_tier=tool_obj.spec.risk_tier,
            redacted_args=self._redact_args(tc.arguments),
            redacted_output=redacted_out,
            status=status,
            latency_ms=latency_ms,
            error_message=self._redact_text(err_msg) if err_msg else None,
        )
        return record, redacted_out

    def _check_budget(
        self,
        *,
        budget: BudgetConfig,
        steps_taken: int,
        total_tokens: int,
        total_cost_usd: float,
        before_call: bool,
    ) -> None:
        if before_call and steps_taken >= budget.max_steps:
            raise BudgetExceededError(
                f"Exceeded max_steps limit ({steps_taken} >= {budget.max_steps}).",
                metric="max_steps",
                current=steps_taken,
                limit=budget.max_steps,
            )
        if (before_call and total_tokens >= budget.max_tokens) or (
            not before_call and total_tokens > budget.max_tokens
        ):
            raise BudgetExceededError(
                f"Exceeded max_tokens limit ({total_tokens} > {budget.max_tokens}).",
                metric="max_tokens",
                current=total_tokens,
                limit=budget.max_tokens,
            )
        if (before_call and total_cost_usd >= budget.max_cost_usd) or (
            not before_call and total_cost_usd > budget.max_cost_usd
        ):
            raise BudgetExceededError(
                (
                    f"Exceeded max_cost_usd limit "
                    f"(${total_cost_usd:.4f} > ${budget.max_cost_usd:.4f})."
                ),
                metric="max_cost_usd",
                current=total_cost_usd,
                limit=budget.max_cost_usd,
            )

    async def _process_tool_calls(
        self,
        *,
        tool_calls: list[ToolCall],
        run_id: UUID,
        workflow_name: str,
        step_index: int,
        messages: list[Message],
        context: dict[str, Any],
        budget: BudgetConfig,
        store: CheckpointStore | None,
        in_tokens: int,
        out_tokens: int,
        cost_usd: float,
        steps_taken: int,
        tool_records: list[ToolCallRecord],
        fingerprints: dict[str, int],
    ) -> RunResult | None:
        """Process a batch of tool calls from an assistant turn.

        Returns a `RunResult(status=WAITING_APPROVAL)` if a tool pauses for HITL approval
        (saving any remaining sibling tool calls on the checkpoint), or `None` when all
        tool calls in the batch have completed and appended their `role="tool"` messages.
        """
        call_warnings: dict[str, str] = {}
        for tc in tool_calls:
            fp = _tool_fingerprint(tc)
            fingerprints[fp] = fingerprints.get(fp, 0) + 1
            count = fingerprints[fp]
            if count > budget.max_duplicate_tool_calls:
                raise LoopDetectedError(tc.name, count)
            if count == budget.max_duplicate_tool_calls:
                call_warnings[tc.id] = (
                    "\n[System Warning: You already ran this exact tool with identical "
                    "arguments. Try a different approach or summarize what is blocked.]"
                )

        can_parallelize = len(tool_calls) > 1
        decisions: list[tuple[ToolCall, RegisteredTool | None, PolicyDecision]] = []
        for tc in tool_calls:
            t_obj = self._tools.get(tc.name)
            if t_obj is None:
                can_parallelize = False
                decisions.append(
                    (tc, None, PolicyDecision(action=PolicyAction.DENY, reason="Unknown tool"))
                )
            else:
                dec = self._evaluate_policy(t_obj.spec, tc.arguments)
                if dec.action != PolicyAction.ALLOW or t_obj.spec.risk_tier != RiskTier.READ:
                    can_parallelize = False
                decisions.append((tc, t_obj, dec))

        if can_parallelize:
            tasks = [
                self._invoke_single_tool(
                    run_id=run_id,
                    tool_obj=t_obj,  # type: ignore[arg-type]
                    tc=tc,
                )
                for tc, t_obj, _ in decisions
            ]
            results = await asyncio.gather(*tasks)
            for (tc, _, _), (record, out_str) in zip(decisions, results, strict=True):
                tool_records.append(record)
                if store is not None:
                    await store.save_tool_call(record)
                warn = call_warnings.get(tc.id, "")
                messages.append(
                    Message(
                        role="tool",
                        tool_call_id=tc.id,
                        name=tc.name,
                        content=f"{out_str}{warn}",
                    )
                )
                self._emit(
                    "tool.completed",
                    run_id,
                    workflow_name,
                    tool=tc.name,
                    status=str(record.status),
                )
            return None

        for idx, (tc, t_obj, dec) in enumerate(decisions):
            if t_obj is None:
                err_text = f"ToolError: Unknown tool '{tc.name}'."
                messages.append(
                    Message(role="tool", tool_call_id=tc.id, name=tc.name, content=err_text)
                )
                continue

            if dec.action == PolicyAction.DENY:
                denial_text = f"PolicyDeniedError: Tool '{tc.name}' blocked: {dec.reason}"
                rec = ToolCallRecord(
                    run_id=run_id,
                    call_id=tc.id,
                    tool_name=tc.name,
                    risk_tier=t_obj.spec.risk_tier,
                    redacted_args=self._redact_args(tc.arguments),
                    redacted_output=self._redact_text(denial_text),
                    status=ToolCallStatus.DENIED,
                    error_message=dec.reason,
                )
                tool_records.append(rec)
                if store is not None:
                    await store.save_tool_call(rec)
                messages.append(
                    Message(role="tool", tool_call_id=tc.id, name=tc.name, content=denial_text)
                )
                self._emit(
                    "tool.denied", run_id, workflow_name, tool=tc.name, reason=dec.reason
                )
                continue

            if dec.action == PolicyAction.REQUIRE_APPROVAL:
                patch_str: str | None = None
                ws_meta: WorkspaceMeta | None = None
                if self._patcher is not None:
                    patch_str, ws_meta = await self._patcher.capture_patch()

                remaining_siblings = [rem_tc for rem_tc, _, _ in decisions[idx + 1 :]]

                approval = ApprovalRequestRecord(
                    run_id=run_id,
                    call_id=tc.id,
                    tool_name=tc.name,
                    risk_tier=t_obj.spec.risk_tier,
                    sanitized_args=self._redact_args(tc.arguments),
                    diff_preview=patch_str[:4000] if patch_str else None,
                    status=ApprovalStatus.PENDING,
                    expires_at=utc_now() + timedelta(minutes=60),
                )
                wait_rec = ToolCallRecord(
                    run_id=run_id,
                    call_id=tc.id,
                    tool_name=tc.name,
                    risk_tier=t_obj.spec.risk_tier,
                    redacted_args=self._redact_args(tc.arguments),
                    status=ToolCallStatus.WAITING_APPROVAL,
                )
                tool_records.append(wait_rec)

                checkpoint = RunCheckpoint(
                    run_id=run_id,
                    workflow_name=workflow_name,
                    status=RunStatus.WAITING_APPROVAL,
                    context_state=context,
                    messages_checkpoint=messages,
                    pending_tool_call=tc,
                    remaining_tool_calls=remaining_siblings,
                    pending_approval_id=approval.id,
                    workspace_meta=ws_meta,
                    workspace_patch=patch_str,
                    current_step_index=step_index,
                    steps_taken=steps_taken,
                    total_input_tokens=in_tokens,
                    total_output_tokens=out_tokens,
                    total_cost_usd=cost_usd,
                    max_cost_usd=budget.max_cost_usd,
                )
                if store is not None:
                    await store.save_checkpoint(checkpoint)
                    await store.save_tool_call(wait_rec)
                    await store.create_approval(approval)

                self._emit(
                    "approval.requested",
                    run_id,
                    workflow_name,
                    approval_id=str(approval.id),
                    tool=tc.name,
                    risk_tier=str(t_obj.spec.risk_tier),
                )
                return RunResult(
                    run_id=run_id,
                    workflow_name=workflow_name,
                    status=RunStatus.WAITING_APPROVAL,
                    output=f"Paused waiting for human approval on tool '{tc.name}'.",
                    context_state=context,
                    messages=messages,
                    tool_calls=tool_records,
                    approval_id=approval.id,
                    pending_tool_call=tc,
                    workspace_patch=patch_str,
                    workspace_meta=ws_meta,
                    total_input_tokens=in_tokens,
                    total_output_tokens=out_tokens,
                    total_tokens=in_tokens + out_tokens,
                    total_cost_usd=cost_usd,
                    steps_taken=steps_taken,
                )

            # PolicyAction.ALLOW
            record, out_str = await self._invoke_single_tool(
                run_id=run_id, tool_obj=t_obj, tc=tc
            )
            tool_records.append(record)
            if store is not None:
                await store.save_tool_call(record)
            warn = call_warnings.get(tc.id, "")
            messages.append(
                Message(
                    role="tool",
                    tool_call_id=tc.id,
                    name=tc.name,
                    content=f"{out_str}{warn}",
                )
            )
            self._emit(
                "tool.completed",
                run_id,
                workflow_name,
                tool=tc.name,
                status=str(record.status),
            )

        return None

    async def _loop(
        self,
        *,
        run_id: UUID,
        workflow_name: str,
        step_index: int,
        messages: list[Message],
        context: dict[str, Any],
        budget: BudgetConfig,
        store: CheckpointStore | None,
        initial_in_tokens: int,
        initial_out_tokens: int,
        initial_cost_usd: float,
        initial_steps_taken: int,
        prior_tool_records: list[ToolCallRecord] | None = None,
        pending_sibling_calls: list[ToolCall] | None = None,
    ) -> RunResult:
        in_tokens = initial_in_tokens
        out_tokens = initial_out_tokens
        cost_usd = initial_cost_usd
        steps_taken = initial_steps_taken
        tool_records: list[ToolCallRecord] = list(prior_tool_records or ())
        fingerprints: dict[str, int] = {}
        tool_specs = [t.spec for t in self._tools.values()] or None

        self._emit("run.started", run_id, workflow_name, agent=self._name)

        # Complete any remaining sibling tool calls from the paused assistant message first
        if pending_sibling_calls:
            paused_on_sibling = await self._process_tool_calls(
                tool_calls=pending_sibling_calls,
                run_id=run_id,
                workflow_name=workflow_name,
                step_index=step_index,
                messages=messages,
                context=context,
                budget=budget,
                store=store,
                in_tokens=in_tokens,
                out_tokens=out_tokens,
                cost_usd=cost_usd,
                steps_taken=steps_taken,
                tool_records=tool_records,
                fingerprints=fingerprints,
            )
            if paused_on_sibling is not None:
                return paused_on_sibling

        while True:
            self._check_budget(
                budget=budget,
                steps_taken=steps_taken,
                total_tokens=in_tokens + out_tokens,
                total_cost_usd=cost_usd,
                before_call=True,
            )

            response = await self._provider.complete(messages, tools=tool_specs)
            steps_taken += 1
            in_tokens += response.input_tokens
            out_tokens += response.output_tokens
            cost_usd = round(cost_usd + response.cost_usd, 8)

            self._check_budget(
                budget=budget,
                steps_taken=steps_taken,
                total_tokens=in_tokens + out_tokens,
                total_cost_usd=cost_usd,
                before_call=False,
            )

            assistant_content = (
                self._redact_text(response.content) if response.content is not None else None
            )
            messages.append(
                Message(
                    role="assistant",
                    content=assistant_content,
                    tool_calls=response.tool_calls,
                )
            )

            if not response.tool_calls:
                final_output = assistant_content or ""
                if store is not None:
                    await store.save_checkpoint(
                        RunCheckpoint(
                            run_id=run_id,
                            workflow_name=workflow_name,
                            status=RunStatus.COMPLETED,
                            context_state=context,
                            messages_checkpoint=messages,
                            current_step_index=step_index,
                            steps_taken=steps_taken,
                            total_input_tokens=in_tokens,
                            total_output_tokens=out_tokens,
                            total_cost_usd=cost_usd,
                            max_cost_usd=budget.max_cost_usd,
                            completed_at=utc_now(),
                        )
                    )
                self._emit(
                    "run.completed",
                    run_id,
                    workflow_name,
                    tokens=in_tokens + out_tokens,
                    cost_usd=cost_usd,
                )
                return RunResult(
                    run_id=run_id,
                    workflow_name=workflow_name,
                    status=RunStatus.COMPLETED,
                    output=final_output,
                    context_state=context,
                    messages=messages,
                    tool_calls=tool_records,
                    total_input_tokens=in_tokens,
                    total_output_tokens=out_tokens,
                    total_tokens=in_tokens + out_tokens,
                    total_cost_usd=cost_usd,
                    steps_taken=steps_taken,
                )

            paused_result = await self._process_tool_calls(
                tool_calls=response.tool_calls,
                run_id=run_id,
                workflow_name=workflow_name,
                step_index=step_index,
                messages=messages,
                context=context,
                budget=budget,
                store=store,
                in_tokens=in_tokens,
                out_tokens=out_tokens,
                cost_usd=cost_usd,
                steps_taken=steps_taken,
                tool_records=tool_records,
                fingerprints=fingerprints,
            )
            if paused_result is not None:
                return paused_result

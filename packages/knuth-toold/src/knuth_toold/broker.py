from __future__ import annotations

from typing import Any, Protocol

import anyio
from jsonschema import ValidationError, validate

from knuth.core.interrupts import InterruptSignal
from knuth.core.invocations import (
    EXTERNAL_EFFECTS,
    ToolCallDecision,
    ToolEffect,
    ToolInvocation,
    ToolRisk,
)
from knuth.core.tools import ToolExecutionOutcome, ToolExecutionResult, ToolResult
from knuth.core.types import ErrorInfo, KnuthModel

from knuth_toold.base import ToolManifest, ToolRuntimeContext
from knuth_toold.registry import ToolRegistry


class PolicyDecision(KnuthModel):
    decision: ToolCallDecision
    error: ErrorInfo | None = None
    approval_title: str | None = None
    approval_reason: str | None = None


class PolicyEngine(Protocol):
    async def evaluate_tool_call(
        self,
        run_id: str,
        manifest: ToolManifest,
        args: dict[str, Any],
    ) -> PolicyDecision:
        ...


class AllowAllPolicy:
    async def evaluate_tool_call(
        self,
        run_id: str,
        manifest: ToolManifest,
        args: dict[str, Any],
    ) -> PolicyDecision:
        return PolicyDecision(decision=ToolCallDecision.ALLOWED)


class ToolProposal(KnuthModel):
    """Outcome of proposing one tool call: policy decision plus manifest facts."""

    tool_name: str
    decision: ToolCallDecision
    effect: ToolEffect = ToolEffect.READ
    risk: ToolRisk = ToolRisk.LOW
    error: ErrorInfo | None = None
    approval_title: str | None = None
    approval_reason: str | None = None


class ToolBroker:
    """Runtime-facing gateway for tool workflow.

    ``propose`` is a pure decision (registry + schema + policy); approval state
    lives in the ledger, never here, so proposing is safely repeatable.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self.registry = registry
        self.policy_engine = policy_engine or AllowAllPolicy()

    async def list_visible_tools(self, run_id: str) -> list[dict[str, Any]]:
        await self.registry.refresh()
        return [
            manifest.to_func_spec()
            for manifest in self.registry.list_visible_manifests()
        ]

    async def propose(
        self,
        run_id: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> ToolProposal:
        await self.registry.refresh()
        try:
            manifest = self.registry.get_manifest(tool_name)
        except KeyError:
            return ToolProposal(
                tool_name=tool_name,
                decision=ToolCallDecision.DENIED,
                error=ErrorInfo(
                    code="tool_not_found",
                    message=f"Tool not found: {tool_name}",
                    retryable=False,
                ),
            )
        try:
            validate(instance=args, schema=manifest.parameters)
        except ValidationError as exc:
            return ToolProposal(
                tool_name=tool_name,
                decision=ToolCallDecision.DENIED,
                effect=manifest.effect,
                risk=manifest.risk,
                error=ErrorInfo(
                    code="invalid_tool_arguments",
                    message=exc.message,
                    retryable=True,
                ),
            )
        decision = await self.policy_engine.evaluate_tool_call(
            run_id=run_id,
            manifest=manifest,
            args=args,
        )
        return ToolProposal(
            tool_name=tool_name,
            decision=decision.decision,
            effect=manifest.effect,
            risk=manifest.risk,
            error=decision.error,
            approval_title=decision.approval_title,
            approval_reason=decision.approval_reason,
        )

    async def awaits_external_result(self, invocation: ToolInvocation) -> bool:
        await self.registry.refresh()
        provider = self.registry.get_provider_for_tool(invocation.tool_name)
        method = getattr(provider, "awaits_external_result", None)
        if method is None:
            return False
        return bool(await method(invocation))

    async def execute(
        self,
        invocation: ToolInvocation,
        signal: InterruptSignal | None = None,
    ) -> ToolExecutionResult:
        """Execute one approved tool call and report a cooperative outcome.

        The signal is handed to the tool so it can stop at its own safe point.
        A tool may return a plain ``ToolResult`` (normalized to succeeded/failed)
        or a richer ``ToolExecutionResult``. Routing of a raised exception
        depends on the signal: a user-stop cancellation is not the tool's own
        failure, so non-external tools fall back to ``interrupted`` while
        external-write/dangerous tools fall back to ``unknown``.
        """
        await self.registry.refresh()
        try:
            manifest = self.registry.get_manifest(invocation.tool_name)
        except KeyError:
            return ToolExecutionResult.failed(
                ToolResult.from_error(
                    "tool_not_found", f"Tool not found: {invocation.tool_name}"
                )
            )
        provider = self.registry.get_provider_for_tool(invocation.tool_name)
        ctx = ToolRuntimeContext(
            run_id=invocation.run_id,
            tool_call_id=invocation.tool_call_id,
            interrupt_signal=signal,
        )
        try:
            if manifest.timeout_s is not None:
                with anyio.fail_after(manifest.timeout_s):
                    raw = await provider.call_tool(invocation, ctx)
            else:
                raw = await provider.call_tool(invocation, ctx)
        except TimeoutError:
            return ToolExecutionResult.failed(
                ToolResult.from_error(
                    "tool_timeout",
                    f"Tool {invocation.tool_name} timed out after"
                    f" {manifest.timeout_s}s",
                    retryable=True,
                )
            )
        except Exception as exc:
            if signal is not None and signal.interrupted:
                return self._interrupt_fallback(manifest, exc)
            return ToolExecutionResult.failed(
                ToolResult.from_error(exc.__class__.__name__, str(exc))
            )
        return self._normalize(raw)

    @staticmethod
    def _normalize(raw: ToolResult | ToolExecutionResult) -> ToolExecutionResult:
        if isinstance(raw, ToolExecutionResult):
            return raw
        return (
            ToolExecutionResult.succeeded(raw)
            if raw.ok
            else ToolExecutionResult.failed(raw)
        )

    @staticmethod
    def _interrupt_fallback(
        manifest: ToolManifest, exc: BaseException
    ) -> ToolExecutionResult:
        if manifest.effect in EXTERNAL_EFFECTS:
            return ToolExecutionResult.unknown(
                reason=(
                    f"{manifest.name} was interrupted by user stop and could not"
                    " confirm whether its external side effect happened"
                    f" ({exc.__class__.__name__})"
                )
            )
        # A user stop is not the tool's own failure; default to interrupted.
        return ToolExecutionResult.interrupted(
            f"Tool {manifest.name} was interrupted by the user before it could"
            " report a result.",
            tool_status="interrupted",
        )

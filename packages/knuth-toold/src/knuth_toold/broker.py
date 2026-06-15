from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import anyio
from jsonschema import ValidationError, validate

from knuth.core.invocations import (
    ToolCallDecision,
    ToolEffect,
    ToolExecutionMode,
    ToolInvocation,
    ToolRisk,
)
from knuth.core.tools import ToolResult
from knuth.core.types import ErrorInfo, KnuthModel

from knuth_toold.base import ToolManifest, ToolRuntimeContext
from knuth_toold.providers import ToolProvider
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
    execution_mode: ToolExecutionMode = ToolExecutionMode.RUNTIME
    error: ErrorInfo | None = None
    approval_title: str | None = None
    approval_reason: str | None = None


@dataclass(frozen=True)
class _ManifestOwner:
    manifest: ToolManifest
    provider: ToolProvider


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

    async def list_visible_tools(
        self,
        run_id: str,
        overlay_providers: Iterable[ToolProvider] = (),
    ) -> list[dict[str, Any]]:
        await self.registry.refresh()
        owners = await self._overlay_index(overlay_providers)
        base = {manifest.name for manifest in self.registry.list_visible_manifests()}
        conflicts = sorted(base & set(owners))
        if conflicts:
            raise ValueError(
                "Tool name conflict in invocation overlay: " + ", ".join(conflicts)
            )
        manifests = self.registry.list_visible_manifests() + [
            owner.manifest for owner in owners.values()
        ]
        return [manifest.to_func_spec() for manifest in manifests]

    async def propose(
        self,
        run_id: str,
        tool_name: str,
        args: dict[str, Any],
        overlay_providers: Iterable[ToolProvider] = (),
    ) -> ToolProposal:
        await self.registry.refresh()
        overlay = await self._overlay_index(overlay_providers)
        try:
            manifest = overlay[tool_name].manifest
        except KeyError:
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
                execution_mode=manifest.execution_mode,
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
            execution_mode=manifest.execution_mode,
            error=decision.error,
            approval_title=decision.approval_title,
            approval_reason=decision.approval_reason,
        )

    async def execute(
        self,
        invocation: ToolInvocation,
        overlay_providers: Iterable[ToolProvider] = (),
    ) -> ToolResult:
        overlay = await self._overlay_index(overlay_providers)
        try:
            owner = overlay[invocation.tool_name]
        except KeyError:
            try:
                manifest = self.registry.get_manifest(invocation.tool_name)
            except KeyError:
                return ToolResult.from_error(
                    "tool_not_found", f"Tool not found: {invocation.tool_name}"
                )
            provider = self.registry.get_provider_for_tool(invocation.tool_name)
        else:
            manifest = owner.manifest
            provider = owner.provider
        ctx = ToolRuntimeContext(
            run_id=invocation.run_id,
            tool_call_id=invocation.tool_call_id,
        )
        try:
            if manifest.timeout_s is not None:
                with anyio.fail_after(manifest.timeout_s):
                    return await provider.call_tool(invocation, ctx)
            return await provider.call_tool(invocation, ctx)
        except TimeoutError:
            return ToolResult.from_error(
                "tool_timeout",
                f"Tool {invocation.tool_name} timed out after {manifest.timeout_s}s",
                retryable=True,
            )
        except Exception as exc:
            return ToolResult.from_error(exc.__class__.__name__, str(exc))

    async def _overlay_index(
        self, overlay_providers: Iterable[ToolProvider]
    ) -> dict[str, _ManifestOwner]:
        owners: dict[str, _ManifestOwner] = {}
        for provider in overlay_providers:
            for manifest in await provider.list_tools():
                if manifest.name in owners:
                    raise ValueError(
                        "Tool name conflict in invocation overlay: " + manifest.name
                    )
                owners[manifest.name] = _ManifestOwner(
                    manifest=manifest.model_copy(update={"provider": provider.name}),
                    provider=provider,
                )
        return owners

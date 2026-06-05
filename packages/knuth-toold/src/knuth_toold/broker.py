from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field
from jsonschema import ValidationError, validate

from knuth.core.messages import InferenceMessage, InferenceRole, ToolCall
from knuth.core.types import ErrorInfo, KnuthModel
from knuth_toold.base import ToolContext, ToolResult, ToolResultStatus
from knuth_toold.registry import ToolRegistry


class ToolProposalStatus(StrEnum):
    ALLOWED = "allowed"
    REQUIRES_APPROVAL = "requires_approval"
    DENIED = "denied"


class ApprovalRequest(KnuthModel):
    id: str
    run_id: str
    title: str
    reason: str
    risk: str
    payload: dict[str, Any]


class PolicyDecision(KnuthModel):
    kind: ToolProposalStatus
    approval: ApprovalRequest | None = None
    error: ErrorInfo | None = None


class PolicyEngine(Protocol):
    async def evaluate_tool_call(
        self,
        run_id: str,
        intent: "ToolIntent",
        manifest: Any,
        args: dict[str, Any],
    ) -> PolicyDecision:
        ...


class AllowAllPolicy:
    async def evaluate_tool_call(
        self,
        run_id: str,
        intent: "ToolIntent",
        manifest: Any,
        args: dict[str, Any],
    ) -> PolicyDecision:
        return PolicyDecision(kind=ToolProposalStatus.ALLOWED)


class ToolIntent(KnuthModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    index: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_tool_call(cls, call: ToolCall) -> "ToolIntent":
        return cls(
            id=call.id or f"call_{call.index}",
            name=call.name,
            arguments=call.arguments,
            index=call.index,
            raw=call.raw,
        )


class ToolProposal(KnuthModel):
    status: ToolProposalStatus
    intent: ToolIntent
    normalized_args: dict[str, Any] = Field(default_factory=dict)
    approval: ApprovalRequest | None = None
    error: ErrorInfo | None = None


class ToolExecutionRecord(KnuthModel):
    intent: ToolIntent
    result: ToolResult

    def to_tool_result_message(self) -> InferenceMessage:
        return InferenceMessage(
            role=InferenceRole.TOOL_RESULT,
            tool_call_id=self.intent.id,
            tool_name=self.intent.name,
            content=self.result.to_observation_text(),
            metadata={
                "tool_status": self.result.status.value,
                "artifacts": self.result.artifacts,
            },
        )


class ToolBroker:
    def __init__(
        self,
        registry: ToolRegistry,
        policy_engine: PolicyEngine | None = None,
        workspace_uri: str | None = None,
    ) -> None:
        self.registry = registry
        self.policy_engine = policy_engine or AllowAllPolicy()
        self.workspace_uri = workspace_uri

    async def list_visible_tools(self, run_id: str) -> list[dict[str, Any]]:
        await self.registry.refresh()
        return [manifest.to_func_spec() for manifest in self.registry.list_visible_manifests()]

    async def propose(self, run_id: str, intent: ToolIntent) -> ToolProposal:
        await self.registry.refresh()
        try:
            manifest = self.registry.get_manifest(intent.name)
        except KeyError:
            return ToolProposal(
                status=ToolProposalStatus.DENIED,
                intent=intent,
                error=ErrorInfo(
                    code="tool_not_found",
                    message=f"Tool not found: {intent.name}",
                    retryable=False,
                ),
            )
        try:
            validate(instance=intent.arguments, schema=manifest.parameters)
        except ValidationError as exc:
            return ToolProposal(
                status=ToolProposalStatus.DENIED,
                intent=intent,
                normalized_args=intent.arguments,
                error=ErrorInfo(
                    code="invalid_tool_arguments",
                    message=exc.message,
                    retryable=True,
                ),
            )
        decision = await self.policy_engine.evaluate_tool_call(
            run_id=run_id,
            intent=intent,
            manifest=manifest,
            args=intent.arguments,
        )
        if decision.kind == ToolProposalStatus.DENIED:
            return ToolProposal(
                status=ToolProposalStatus.DENIED,
                intent=intent,
                normalized_args=intent.arguments,
                error=decision.error,
            )
        if decision.kind == ToolProposalStatus.REQUIRES_APPROVAL:
            return ToolProposal(
                status=ToolProposalStatus.REQUIRES_APPROVAL,
                intent=intent,
                normalized_args=intent.arguments,
                approval=decision.approval,
            )
        return ToolProposal(
            status=ToolProposalStatus.ALLOWED,
            intent=intent,
            normalized_args=intent.arguments,
        )

    async def execute(self, run_id: str, proposal: ToolProposal) -> ToolExecutionRecord:
        if proposal.status != ToolProposalStatus.ALLOWED:
            return ToolExecutionRecord(
                intent=proposal.intent,
                result=ToolResult(
                    status=ToolResultStatus.ERROR,
                    error=proposal.error
                    or ErrorInfo(
                        code="tool_not_allowed",
                        message=f"Tool proposal is {proposal.status.value}",
                    ),
                ),
            )
        provider = self.registry.get_provider_for_tool(proposal.intent.name)
        try:
            result = await provider.call_tool(
                proposal.intent.name,
                proposal.normalized_args,
                ToolContext(
                    run_id=run_id,
                    tool_call_id=proposal.intent.id,
                    workspace_uri=self.workspace_uri,
                ),
            )
        except Exception as exc:
            result = ToolResult.from_error(exc.__class__.__name__, str(exc))
        return ToolExecutionRecord(intent=proposal.intent, result=result)

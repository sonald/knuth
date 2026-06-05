from __future__ import annotations

from knuth.core.types import ErrorInfo
from knuth_toold.base import ToolEffect, ToolManifest, ToolRisk
from knuth_toold.broker import ApprovalRequest, PolicyDecision, ToolIntent, ToolProposalStatus


class PolicyEngine:
    def __init__(self, approval_lookup: "ApprovalLookup | None" = None) -> None:
        self.approval_lookup = approval_lookup

    async def evaluate_tool_call(
        self,
        run_id: str,
        intent: ToolIntent,
        manifest: ToolManifest,
        args: dict,
    ) -> PolicyDecision:
        approval_id = approval_id_for(run_id, intent.id)
        if self.approval_lookup and await self.approval_lookup.is_approved(approval_id):
            return PolicyDecision(kind=ToolProposalStatus.ALLOWED)
        if manifest.effect in {ToolEffect.EXTERNAL_WRITE, ToolEffect.DANGEROUS}:
            return self._approval(run_id, intent, manifest, args)
        if manifest.effect == ToolEffect.LOCAL_WRITE or manifest.risk == ToolRisk.HIGH:
            return self._approval(run_id, intent, manifest, args)
        return PolicyDecision(kind=ToolProposalStatus.ALLOWED)

    def _approval(
        self,
        run_id: str,
        intent: ToolIntent,
        manifest: ToolManifest,
        args: dict,
    ) -> PolicyDecision:
        return PolicyDecision(
            kind=ToolProposalStatus.REQUIRES_APPROVAL,
            approval=ApprovalRequest(
                id=approval_id_for(run_id, intent.id),
                run_id=run_id,
                title=f"Approve tool call: {intent.name}",
                reason=f"Tool has effect={manifest.effect.value}, risk={manifest.risk.value}",
                risk=manifest.risk.value,
                payload={"tool": intent.name, "tool_call_id": intent.id, "args_preview": args},
            ),
        )


class ApprovalLookup:
    async def is_approved(self, approval_id: str) -> bool:
        ...


def approval_id_for(run_id: str, tool_call_id: str) -> str:
    return f"appr_{run_id}_{tool_call_id}"

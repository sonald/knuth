from __future__ import annotations

from typing import Any

from knuth.core.invocations import ToolCallDecision, ToolEffect, ToolRisk
from knuth_toold.base import ToolManifest
from knuth_toold.broker import PolicyDecision


class PolicyEngine:
    """Pure policy: decides from manifest facts and arguments only.

    Approval state lives in the ledger, never here, so evaluation is safely
    repeatable — proposing twice yields the same decision.
    """

    async def evaluate_tool_call(
        self,
        run_id: str,
        manifest: ToolManifest,
        args: dict[str, Any],
    ) -> PolicyDecision:
        if (
            manifest.effect
            in {ToolEffect.EXTERNAL_WRITE, ToolEffect.DANGEROUS, ToolEffect.LOCAL_WRITE}
            or manifest.risk == ToolRisk.HIGH
        ):
            return PolicyDecision(
                decision=ToolCallDecision.REQUIRES_APPROVAL,
                approval_title=f"Approve tool call: {manifest.name}",
                approval_reason=(
                    f"Tool has effect={manifest.effect.value}, risk={manifest.risk.value}"
                ),
            )
        return PolicyDecision(decision=ToolCallDecision.ALLOWED)

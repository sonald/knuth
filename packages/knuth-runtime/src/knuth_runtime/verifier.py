from __future__ import annotations

from knuth.core.messages import InferenceMessage
from knuth.core.types import KnuthModel


class VerificationResult(KnuthModel):
    ok: bool
    reason: str | None = None


class Verifier:
    async def verify_final_answer(
        self, run_id: str, message: InferenceMessage
    ) -> VerificationResult:
        if message.content and message.content.strip():
            return VerificationResult(ok=True)
        return VerificationResult(ok=False, reason="empty_final_answer")

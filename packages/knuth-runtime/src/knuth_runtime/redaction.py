from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel

from knuth.core.events import DurableRuntimeEventDraft, ToolBatchPlannedDraft
from knuth.core.invocations import args_hash_for

from knuth_runtime.context import ContextView, RunContext

_ModelT = TypeVar("_ModelT", bound=BaseModel)


@dataclass(frozen=True)
class SecretPattern:
    """One secret shape: text matching ``regex`` is masked as
    ``[REDACTED:<name>]``. With ``group`` set, only that capture group is
    masked and the surrounding match is kept."""

    name: str
    regex: re.Pattern[str]
    group: int = 0


DEFAULT_SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?s:.*?)-----END [A-Z ]*PRIVATE KEY-----"
        ),
    ),
    SecretPattern("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    SecretPattern("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    SecretPattern("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    SecretPattern("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    SecretPattern(
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}\b"),
    ),
    SecretPattern(
        "credential",
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token"
            r"|client[_-]?secret|secret|password|passwd)\b\s*[:=]\s*['\"]?"
            r"([^'\"\s,;]{6,})"
        ),
        group=2,
    ),
)

# Dict keys whose string values are masked outright, regardless of the value's
# shape: structured payloads (tool args and approval previews) carry the key
# context that value-only pattern matching cannot see.
_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "accesstoken",
        "authtoken",
        "refreshtoken",
        "clientsecret",
        "secret",
        "password",
        "passwd",
        "authorization",
        "privatekey",
        "credentials",
        "bearertoken",
    }
)


def _normalize_key(key: str) -> str:
    return key.replace("-", "").replace("_", "").lower()


class RegexSecretRedactor:
    """Pattern-based secret masking, applied before anything durable or
    model-visible is produced.

    One instance serves both seams (design §8): as an ``EventRedactor`` it
    runs inside ``RunLedger.apply()`` before append — the log is append-only,
    so this is the only chance — and as a ``ContextRedactor`` it runs as the
    redact stage of the context pipeline, covering preamble sections that
    never pass through the ledger.
    """

    def __init__(self, patterns: tuple[SecretPattern, ...] | None = None) -> None:
        self._patterns = patterns if patterns is not None else DEFAULT_SECRET_PATTERNS

    def redact_text(self, text: str) -> str:
        for pattern in self._patterns:
            text = self._mask(pattern, text)
        return text

    def redact_event(self, draft: DurableRuntimeEventDraft) -> DurableRuntimeEventDraft:
        data = draft.model_dump()
        redacted = self._redact_value(data)
        if redacted == data:
            return draft
        # args_hash binds approvals to the args as frozen in the ledger; once
        # redaction rewrites the args, the redacted form is the fact, so the
        # hash is recomputed over it.
        if isinstance(draft, ToolBatchPlannedDraft):
            for call in redacted["calls"]:
                call["args_hash"] = args_hash_for(call["args"])
        return type(draft).model_validate(redacted)

    async def redact(self, ctx: RunContext, view: ContextView) -> ContextView:
        messages = [self._redact_model(message) for message in view.messages]
        return view.model_copy(update={"messages": messages})

    def _redact_model(self, model: _ModelT) -> _ModelT:
        data = model.model_dump()
        redacted = self._redact_value(data)
        if redacted == data:
            return model
        return type(model).model_validate(redacted)

    def _redact_value(self, value: object) -> object:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {
                key: self._redact_entry(key, item) for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value

    def _redact_entry(self, key: object, value: object) -> object:
        if (
            isinstance(key, str)
            and isinstance(value, str)
            and value
            and _normalize_key(key) in _SENSITIVE_KEYS
        ):
            return "[REDACTED:sensitive_key]"
        return self._redact_value(value)

    @staticmethod
    def _mask(pattern: SecretPattern, text: str) -> str:
        marker = f"[REDACTED:{pattern.name}]"

        def replace(match: re.Match[str]) -> str:
            if pattern.group == 0:
                return marker
            full = match.group(0)
            offset = match.start(0)
            start, end = match.span(pattern.group)
            return full[: start - offset] + marker + full[end - offset :]

        return pattern.regex.sub(replace, text)


__all__ = [
    "DEFAULT_SECRET_PATTERNS",
    "RegexSecretRedactor",
    "SecretPattern",
]

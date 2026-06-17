from __future__ import annotations

from dataclasses import dataclass

from knuth_llmd import InferenceClient
from knuth_toold import ToolBroker

from knuth_runtime.context import ContextBuilder
from knuth_runtime.ledger import RunLedger
from knuth_runtime.middleware import MessageMiddlewareRunner


@dataclass
class RuntimeServices:
    inference_client: InferenceClient
    tool_broker: ToolBroker
    ledger: RunLedger
    context_builder: ContextBuilder
    message_middleware_runner: MessageMiddlewareRunner | None = None

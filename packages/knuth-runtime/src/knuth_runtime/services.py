from __future__ import annotations

from dataclasses import dataclass

from knuth_llmd import InferenceClient
from knuth_toold import ToolBroker

from knuth_runtime.context import ContextBuilder
from knuth_runtime.ledger import RunLedger


@dataclass
class RuntimeServices:
    inference_client: InferenceClient
    tool_broker: ToolBroker
    ledger: RunLedger
    context_builder: ContextBuilder

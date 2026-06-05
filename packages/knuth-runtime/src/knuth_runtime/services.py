from __future__ import annotations

from dataclasses import dataclass

from knuth_llmd import InferenceClient
from knuth_toold import ToolBroker
from knuth_runtime.approval import MemoryApprovalService
from knuth_runtime.context import ContextBuilder
from knuth_runtime.stores import EventStore, RunStore


@dataclass
class RuntimeServices:
    inference_client: InferenceClient
    tool_broker: ToolBroker
    run_store: RunStore
    event_store: EventStore
    approvals: MemoryApprovalService
    context_builder: ContextBuilder

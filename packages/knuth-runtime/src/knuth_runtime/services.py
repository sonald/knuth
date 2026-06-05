from __future__ import annotations

from dataclasses import dataclass

from knuth_llmd import InferenceClient
from knuth_toold import ToolBroker
from knuth_runtime.approval import MemoryApprovalService
from knuth_runtime.artifact_store import MemoryArtifactStore
from knuth_runtime.context import ContextBuilder
from knuth_runtime.hooks import HookManager
from knuth_runtime.stores import EventStore, RunStore
from knuth_runtime.verifier import Verifier


class RealtimeBus:
    def __init__(self) -> None:
        self.events: dict[str, list[object]] = {}

    async def publish(self, run_id: str, event: object) -> None:
        self.events.setdefault(run_id, []).append(event)


@dataclass
class RuntimeServices:
    inference_client: InferenceClient
    tool_broker: ToolBroker
    run_store: RunStore
    event_store: EventStore
    artifact_store: MemoryArtifactStore
    approvals: MemoryApprovalService
    context_builder: ContextBuilder
    hooks: HookManager
    realtime_bus: RealtimeBus
    verifier: Verifier

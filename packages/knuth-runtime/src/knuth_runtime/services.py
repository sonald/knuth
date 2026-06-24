from __future__ import annotations

from dataclasses import dataclass

from knuth_llmd import InferenceClient
from knuth_toold import SkillHotReloadService, SkillManager, ToolBroker

from knuth_runtime.context import ContextBuilder
from knuth_runtime.artifacts import FilesystemArtifactStore
from knuth_runtime.ledger import RunLedger
from knuth_runtime.middleware import MessageMiddlewareRunner
from knuth_runtime.projection_checkpoint import ProjectionCheckpointWriter


@dataclass
class RuntimeServices:
    inference_client: InferenceClient
    tool_broker: ToolBroker
    ledger: RunLedger
    artifact_store: FilesystemArtifactStore
    context_builder: ContextBuilder
    message_middleware_runner: MessageMiddlewareRunner | None = None
    projection_checkpoint_writer: ProjectionCheckpointWriter | None = None
    skill_manager: SkillManager | None = None
    skill_hot_reload_service: SkillHotReloadService | None = None

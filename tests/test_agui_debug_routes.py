"""Raw event debug channel: full-fidelity history and a replay stream.

The translated /agent stream is lossy by design; these routes must instead
expose every stored ``RuntimeEvent`` so a debug viewer can see what the runtime
actually emitted, plus a raw SSE that replays history and marks completion.
"""

from __future__ import annotations

import json
import tempfile
import unittest

from starlette.testclient import TestClient
from knuth_llmd import InferenceConfig
from knuth_runtime import MemoryRunLedger, build_memory_runtime
from knuth_runtime.policy import PolicyEngine
from knuth_toold import ToolBroker, create_default_registry

from knuth_agui import create_app

from tests.test_agui_spike import _ScriptedSpikeClient, _collect_events


def _runtime(workspace: str):
    from pathlib import Path

    fact_path = Path(workspace, "fact.txt")
    fact_path.write_text("hello", encoding="utf-8")
    registry = create_default_registry()
    return build_memory_runtime(
        inference_client=_ScriptedSpikeClient(str(fact_path)),
        inference_config=InferenceConfig(),
        ledger=MemoryRunLedger(),
        tool_broker=ToolBroker(registry, PolicyEngine()),
    )


def _drive_one_run(client: TestClient, thread_id: str) -> None:
    response = client.post(
        "/agent",
        json={
            "threadId": thread_id,
            "messages": [{"role": "user", "content": "what does the file say?"}],
        },
    )
    assert response.status_code == 200, response.text


class DebugEventRoutesTests(unittest.TestCase):
    def test_events_endpoint_returns_full_fidelity_durable_events(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            app = create_app(_runtime(workspace))
            with TestClient(app) as client:
                _drive_one_run(client, "run_dbg_1")
                response = client.get("/threads/run_dbg_1/events")
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()

        self.assertEqual(body["runId"], "run_dbg_1")
        events = body["events"]
        types = [event["type"] for event in events]
        # Untranslated: types the AG-UI translator drops must be present here.
        self.assertIn("run.created", types)
        self.assertIn("step.started", types)
        self.assertIn("tool.batch_planned", types)
        self.assertIn("tool.invocation_completed", types)
        self.assertIn("run.succeeded", types)
        # Durable events carry a monotonic seq and an id; lastSeq tracks the tail.
        seqs = [event["seq"] for event in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(body["lastSeq"], seqs[-1])

    def test_events_after_seq_returns_only_newer(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            app = create_app(_runtime(workspace))
            with TestClient(app) as client:
                _drive_one_run(client, "run_dbg_2")
                full = client.get("/threads/run_dbg_2/events").json()["events"]
                midpoint = full[len(full) // 2]["seq"]
                tail = client.get(
                    f"/threads/run_dbg_2/events?after_seq={midpoint}"
                ).json()["events"]

        self.assertTrue(tail)
        self.assertTrue(all(event["seq"] > midpoint for event in tail))

    def test_unknown_thread_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            app = create_app(_runtime(workspace))
            with TestClient(app) as client:
                self.assertEqual(
                    client.get("/threads/run_missing/events").status_code, 404
                )
                self.assertEqual(
                    client.get("/threads/run_missing/events/stream").status_code,
                    404,
                )

    def test_stream_replays_history_then_marks_complete_when_not_live(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            app = create_app(_runtime(workspace))
            with TestClient(app) as client:
                _drive_one_run(client, "run_dbg_3")
                response = client.get("/threads/run_dbg_3/events/stream")
                self.assertEqual(response.status_code, 200, response.text)
                frames = _collect_events(response.text)

        phases = [frame["phase"] for frame in frames]
        self.assertIn("replay", phases)
        # A finished run has no live session: the stream replays and closes
        # after a single replay_complete control frame.
        control = [frame for frame in frames if frame["phase"] == "control"]
        self.assertEqual(len(control), 1)
        self.assertEqual(control[0]["control"], "replay_complete")
        self.assertFalse(control[0]["live"])
        replay_types = [
            frame["event"]["type"] for frame in frames if frame["phase"] == "replay"
        ]
        self.assertIn("run.created", replay_types)
        self.assertIn("run.succeeded", replay_types)


if __name__ == "__main__":
    unittest.main()

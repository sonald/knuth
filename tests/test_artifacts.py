import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import anyio

from knuth_runtime import FilesystemArtifactStore, RegexSecretRedactor


class FilesystemArtifactStoreTests(unittest.TestCase):
    def test_put_redacts_writes_pending_manifest_and_survives_restart(self) -> None:
        async def scenario(root: Path):
            store = FilesystemArtifactStore(root, redactor=RegexSecretRedactor())
            artifact = await store.put(
                "run-1",
                "token sk-abcdefghijklmnopqrstuvwxyz123456",
                kind="shell_stdout",
                ext=".txt",
            )
            manifest = json.loads(
                (root / "run-1" / "manifest.json").read_text(encoding="utf-8")
            )
            restarted = FilesystemArtifactStore(root, redactor=RegexSecretRedactor())
            text = await restarted.read_text("run-1", artifact.id)
            return artifact, manifest, text, restarted.path_for("run-1", artifact.id)

        with tempfile.TemporaryDirectory() as temp_dir:
            artifact, manifest, text, path = anyio.run(scenario, Path(temp_dir))

        redacted = "token [REDACTED:openai_key]"
        self.assertEqual(text, redacted)
        self.assertEqual(path, Path(artifact.path))
        self.assertEqual(artifact.bytes, len(redacted.encode("utf-8")))
        self.assertEqual(
            artifact.sha256,
            hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
        )
        entry = manifest["artifacts"][artifact.id]
        self.assertEqual(entry["state"], "pending")
        self.assertEqual(entry["rel_path"], f"{artifact.id}.txt")

    def test_mark_committed_gc_and_reclaim_run(self) -> None:
        async def scenario(root: Path):
            store = FilesystemArtifactStore(
                root,
                redactor=RegexSecretRedactor(),
                ttl_days=0,
            )
            pending = await store.put("run-1", "pending", kind="raw", ext=".txt")
            committed = await store.put("run-1", "committed", kind="raw", ext=".txt")
            await store.mark_committed("run-1", [committed.id])
            await store.gc()
            manifest_after_gc = json.loads(
                (root / "run-1" / "manifest.json").read_text(encoding="utf-8")
            )
            committed_text = await store.read_text("run-1", committed.id)
            await store.reclaim_run("run-1")
            return pending, committed, manifest_after_gc, committed_text

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending, committed, manifest_after_gc, committed_text = anyio.run(
                scenario, root
            )

            self.assertFalse(Path(pending.path).exists())
            self.assertEqual(committed_text, "committed")
            self.assertIn(committed.id, manifest_after_gc["artifacts"])
            self.assertNotIn(pending.id, manifest_after_gc["artifacts"])
            self.assertEqual(
                manifest_after_gc["artifacts"][committed.id]["state"],
                "committed",
            )
            self.assertFalse((root / "run-1").exists())

    def test_rejects_unsafe_extension(self) -> None:
        async def scenario(root: Path):
            store = FilesystemArtifactStore(root, redactor=RegexSecretRedactor())
            await store.put("run-1", "x", kind="raw", ext="../escape")

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "unsafe artifact extension"):
                anyio.run(scenario, Path(temp_dir))

    def test_rejects_dotdot_extension(self) -> None:
        # `.foo..bar` matched the ext regex but the read/GC path rejects "..",
        # so put must reject it too rather than write an unreadable artifact.
        async def scenario(root: Path):
            store = FilesystemArtifactStore(root, redactor=RegexSecretRedactor())
            await store.put("run-1", "x", kind="raw", ext=".foo..bar")

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "unsafe artifact extension"):
                anyio.run(scenario, Path(temp_dir))

    def test_rejects_dot_run_id_component(self) -> None:
        # run_id="." resolves the run dir to the store root; reclaim_run(".")
        # would then delete everything. The component guard must reject it.
        with tempfile.TemporaryDirectory() as temp_dir:
            store = FilesystemArtifactStore(
                Path(temp_dir), redactor=RegexSecretRedactor()
            )
            with self.assertRaisesRegex(ValueError, "unsafe artifact run_id"):
                store.sink_for(".", "call-1")

    def test_mark_committed_skips_unknown_ids(self) -> None:
        # A tool may report an id it never stored; marking must be a no-op for
        # the phantom (not raise after the referencing event is durable).
        async def scenario(root: Path):
            store = FilesystemArtifactStore(root, redactor=RegexSecretRedactor())
            kept = await store.put("run-1", "x", kind="raw", ext=".txt")
            await store.mark_committed("run-1", [kept.id, "art_phantom"])
            manifest = json.loads(
                (root / "run-1" / "manifest.json").read_text(encoding="utf-8")
            )
            return kept, manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            kept, manifest = anyio.run(scenario, Path(temp_dir))
        self.assertEqual(manifest["artifacts"][kept.id]["state"], "committed")
        self.assertNotIn("art_phantom", manifest["artifacts"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sqlite3
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_catalog as catalog  # noqa: E402
import artifact_ingestion as ingestion  # noqa: E402


class ArtifactIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.derived = self.root / "derived"
        self.workspace.mkdir()
        (self.workspace / "plans").mkdir()
        self.policy = self.root / "policy.json"
        self.policy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": {
                        "canonical_roots": ["plans"],
                        "top_level_globs": [],
                        "include_path_globs": ["plans/**"],
                        "exclude_roots": [],
                        "prune_directory_names": [".git"],
                        "prune_path_globs": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.source = self.workspace / "plans/demo-roadmap.md"
        self.source.write_text(
            "# Demo\n\n"
            + "Architecture and temporal knowledge. " * 40
            + "\n\n## Decisions\n\nUse Qdrant for revision search.\n",
            encoding="utf-8",
        )
        catalog.run_catalog(
            self.workspace,
            self.derived,
            self.policy,
            dry_run=False,
        )
        self.catalog = self.derived / "artifact-catalog.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def prepare(self, output_name: str = "outbox") -> dict:
        return ingestion.prepare_outbox(
            workspace=self.workspace,
            catalog=self.catalog,
            output_root=self.root / output_name,
            max_chars=320,
            overlap_chars=40,
            group_id="personal_artifacts",
        )

    def test_chunking_is_deterministic_and_bounded(self) -> None:
        text = self.source.read_text(encoding="utf-8")
        first = ingestion.split_text(text, max_chars=320, overlap_chars=40)
        second = ingestion.split_text(text, max_chars=320, overlap_chars=40)

        self.assertEqual(first, second)
        self.assertGreater(len(first), 1)
        self.assertTrue(all(len(chunk.content) <= 320 for chunk in first))
        self.assertEqual(first[0].heading, "Demo")
        self.assertTrue(any(chunk.overlap_prefix_chars > 0 for chunk in first[1:]))

    def test_prepare_verifies_sources_and_creates_immutable_outbox(self) -> None:
        result = self.prepare()
        outbox = Path(result["outbox"])
        manifest = ingestion.load_outbox_manifest(outbox)
        units = list(ingestion.iter_outbox_units(outbox))

        self.assertTrue(manifest["complete"])
        self.assertEqual(stat.S_IMODE(outbox.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((outbox / "manifest.json").stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(
                (outbox / "ingest-units.jsonl.gz").stat().st_mode
            ),
            0o600,
        )
        self.assertEqual(manifest["catalog_run_id"], 1)
        self.assertEqual(manifest["counts"]["artifacts"], 1)
        self.assertEqual(len(units), manifest["counts"]["units"])
        self.assertEqual(len({unit["unit_id"] for unit in units}), len(units))
        self.assertEqual(manifest["outbox_schema_version"], 2)
        self.assertEqual(manifest["graphiti_group_prefix"], "personal_artifacts")
        self.assertTrue(
            all(
                unit["graphiti_group_id"].startswith("personal_artifacts_")
                for unit in units
            )
        )

        replay = self.prepare()
        self.assertEqual(replay["publication"], "idempotent")
        self.assertEqual(replay["units_sha256"], manifest["units_sha256"])

    def test_outbox_crashes_leave_only_hidden_unpublished_state(self) -> None:
        root = self.root / "atomic-outbox"

        def before_publish(stage: str) -> None:
            if stage == "after_manifest_fsync":
                raise RuntimeError("simulated crash")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            ingestion.prepare_outbox(
                workspace=self.workspace,
                catalog=self.catalog,
                output_root=root,
                max_chars=320,
                overlap_chars=40,
                group_id="personal_artifacts",
                _fault=before_publish,
            )
        final = root / "catalog-run-1-chunks-v2"
        self.assertFalse(final.exists())
        self.assertTrue(
            all(path.name.startswith(".tmp-") for path in root.iterdir())
        )
        result = ingestion.prepare_outbox(
            workspace=self.workspace,
            catalog=self.catalog,
            output_root=root,
            max_chars=320,
            overlap_chars=40,
            group_id="personal_artifacts",
        )
        self.assertEqual(result["publication"], "created")
        self.assertTrue(final.exists())

    def test_graphiti_namespace_is_stable_and_separates_repository_scopes(self) -> None:
        artifact = ingestion.CatalogArtifact(
            artifact_id="artifact-1",
            revision_id="revision-1",
            relative_path="plans/demo.md",
            content_sha256="abc",
            byte_size=3,
            mtime_ns=1,
            artifact_type="plan",
            authority_class="working",
            lifecycle_hints=("active",),
            source_scope="repository",
            repository="platform",
            project="platform",
        )

        first = ingestion._graphiti_namespace(artifact, "personal_artifacts")
        repeated = ingestion._graphiti_namespace(artifact, "personal_artifacts")
        other = ingestion._graphiti_namespace(
            replace(artifact, repository="mosaic", project="mosaic"),
            "personal_artifacts",
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other)
        self.assertRegex(first, r"^personal_artifacts_[a-z0-9_-]+_[0-9a-f]{10}$")

    def test_graphiti_result_namespace_validation_fails_closed(self) -> None:
        expected = "personal_artifacts_platform_0123456789"
        valid = SimpleNamespace(
            episode=SimpleNamespace(group_id=expected),
            nodes=[SimpleNamespace(group_id=expected)],
            edges=[SimpleNamespace(group_id=expected)],
            episodic_edges=[SimpleNamespace(group_id=expected)],
        )
        self.assertEqual(
            ingestion._validate_graphiti_result_namespace(valid, expected),
            {"nodes": 1, "edges": 1, "episodic_edges": 1},
        )

        escaped = SimpleNamespace(
            episode=SimpleNamespace(group_id=expected),
            nodes=[SimpleNamespace(group_id="personal_artifacts")],
            edges=[],
            episodic_edges=[],
        )
        with self.assertRaisesRegex(ingestion.IngestionError, "escaped namespace"):
            ingestion._validate_graphiti_result_namespace(escaped, expected)

    def test_stale_source_and_workspace_output_fail_closed(self) -> None:
        self.source.write_text("changed after catalog\n", encoding="utf-8")
        with self.assertRaisesRegex(ingestion.SourceChangedError, "differs"):
            self.prepare("stale-outbox")

        with self.assertRaisesRegex(ingestion.IngestionError, "outside"):
            ingestion.prepare_outbox(
                workspace=self.workspace,
                catalog=self.catalog,
                output_root=self.workspace / "outbox",
                max_chars=320,
                overlap_chars=40,
                group_id="personal_artifacts",
            )

    def test_qdrant_and_graphiti_plans_need_no_optional_dependencies(self) -> None:
        result = self.prepare()
        outbox = Path(result["outbox"])
        state = self.root / "state.sqlite3"
        qdrant = ingestion.qdrant_ingest(
            workspace=self.workspace,
            catalog=self.catalog,
            outbox=outbox,
            state_path=state,
            collection="test",
            embedding_model="test-model",
            local_path=self.root / "qdrant",
            url=None,
            api_key_env="QDRANT_API_KEY",
            batch_size=8,
            limit_units=2,
            apply=False,
        )
        graphiti = ingestion.graphiti_ingest(
            workspace=self.workspace,
            outbox=outbox,
            state_path=state,
            host="127.0.0.1",
            port=6379,
            password_env="FALKORDB_PASSWORD",
            database="personal_artifacts",
            group_id="personal_artifacts",
            llm_base_url="http://127.0.0.1:11434/v1",
            llm_model=None,
            embedding_base_url="http://127.0.0.1:11434/v1",
            embedding_model=None,
            embedding_dim=0,
            api_key_env="GRAPHITI_LLM_API_KEY",
            structured_output_mode="json_schema",
            instructions_file=None,
            limit_units=2,
            retry_ambiguous=False,
            apply=False,
        )

        self.assertEqual(qdrant["would_ingest"], 2)
        self.assertEqual(graphiti["would_ingest"], 2)
        self.assertEqual(graphiti["backend"], "falkordb")
        with sqlite3.connect(state) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                ingestion.STATE_SCHEMA_VERSION,
            )

    def test_embedder_shortfall_fails_loudly_instead_of_partial_success(self) -> None:
        # V-M1 regression: a short (or empty) embedder yield used to be skipped
        # (`continue`) or silently truncated by `zip`, while the WHOLE batch was
        # still marked completed and counted in `ingested` — a success return
        # hiding never-upserted units, and false checkpoint rows that the
        # catalog-vs-checkpoint anti-join cannot see. Callers (the consumer)
        # clear their durable publication-failure row on any non-raising return,
        # so this stranded content behind a green health signal. Must raise.
        result = self.prepare()
        outbox = Path(result["outbox"])

        class ShortEmbedder:
            def embed(self, texts):
                items = list(texts)
                return [[0.1, 0.2, 0.3] for _ in items[:-1]]

        class UnusedClient:
            """The length check must fire before any client call."""

        with self.assertRaises(ingestion.IngestionError) as caught:
            ingestion.qdrant_ingest(
                workspace=self.workspace,
                catalog=self.catalog,
                outbox=outbox,
                state_path=self.root / "shortfall-state.sqlite3",
                collection="test",
                embedding_model="test-model",
                local_path=self.root / "qdrant",
                url=None,
                api_key_env="QDRANT_API_KEY",
                batch_size=8,
                limit_units=0,
                apply=True,
                client=UnusedClient(),
                embedder=ShortEmbedder(),
            )
        self.assertIn("refusing to report partial success", str(caught.exception))

    def test_state_tracks_completed_and_ambiguous_units(self) -> None:
        state = ingestion.IngestionState(
            self.root / "state.sqlite3",
            self.workspace.resolve(),
        )
        try:
            state.set_status(
                sink="graphiti",
                target="target",
                unit_id="u1",
                revision_id="r1",
                status="in_progress",
            )
            state.set_status(
                sink="graphiti",
                target="target",
                unit_id="u1",
                revision_id="r1",
                status="ambiguous",
            )
            self.assertEqual(
                state.statuses("graphiti", "target"),
                {"u1": "ambiguous"},
            )
        finally:
            state.close()

    def test_limit_units_is_a_stable_replay_prefix(self) -> None:
        result = self.prepare()
        outbox = Path(result["outbox"])
        units = list(ingestion.iter_outbox_units(outbox))
        state_path = self.root / "state.sqlite3"
        target = ingestion._qdrant_target(
            f"local:{(self.root / 'qdrant').resolve()}",
            "test",
            "test-model",
        )
        state = ingestion.IngestionState(state_path, self.workspace.resolve())
        try:
            for unit in units[:2]:
                state.set_status(
                    sink="qdrant",
                    target=target,
                    unit_id=str(unit["unit_id"]),
                    revision_id=str(unit["revision_id"]),
                    status="completed",
                )
        finally:
            state.close()

        plan = ingestion.qdrant_ingest(
            workspace=self.workspace,
            catalog=self.catalog,
            outbox=outbox,
            state_path=state_path,
            collection="test",
            embedding_model="test-model",
            local_path=self.root / "qdrant",
            url=None,
            api_key_env="QDRANT_API_KEY",
            batch_size=8,
            limit_units=2,
            apply=False,
        )

        self.assertEqual(plan["already_completed"], 2)
        self.assertEqual(plan["would_ingest"], 0)

    def test_graphiti_apply_fails_cleanly_when_falkordb_is_absent(self) -> None:
        result = self.prepare()
        with self.assertRaisesRegex(ingestion.IngestionError, "not reachable"):
            ingestion.graphiti_ingest(
                workspace=self.workspace,
                outbox=Path(result["outbox"]),
                state_path=self.root / "state.sqlite3",
                host="127.0.0.1",
                port=9,
                password_env="FALKORDB_PASSWORD",
                database="personal_artifacts",
                group_id="personal_artifacts",
                llm_base_url="http://127.0.0.1:11434/v1",
                llm_model="validated-model",
                embedding_base_url="http://127.0.0.1:11434/v1",
                embedding_model="validated-embedding",
                embedding_dim=384,
                api_key_env="GRAPHITI_LLM_API_KEY",
                structured_output_mode="json_schema",
                instructions_file=None,
                limit_units=1,
                retry_ambiguous=False,
                apply=True,
            )

    def test_graphiti_apply_is_hard_capped_to_one_quality_pilot(self) -> None:
        result = self.prepare()
        with self.assertRaisesRegex(
            ingestion.IngestionError,
            "quality-pilot-only",
        ):
            ingestion.graphiti_ingest(
                workspace=self.workspace,
                outbox=Path(result["outbox"]),
                state_path=self.root / "state.sqlite3",
                host="127.0.0.1",
                port=1,
                password_env="FALKORDB_PASSWORD",
                database="personal_artifacts",
                group_id="personal_artifacts",
                llm_base_url="http://127.0.0.1:11434/v1",
                llm_model="qwen3:8b",
                embedding_base_url="http://127.0.0.1:11434/v1",
                embedding_model="nomic-embed-text",
                embedding_dim=768,
                api_key_env="GRAPHITI_LLM_API_KEY",
                structured_output_mode="json_schema",
                instructions_file=None,
                limit_units=2,
                retry_ambiguous=False,
                apply=True,
            )

    def test_qdrant_payload_and_filter_include_lifecycle_fields(self) -> None:
        result = self.prepare()
        unit = next(ingestion.iter_outbox_units(Path(result["outbox"])))
        payload = ingestion._unit_payload(
            unit,
            "test-model",
            catalog_current=True,
        )

        class MatchValue:
            def __init__(self, *, value):
                self.value = value

        class FieldCondition:
            def __init__(self, *, key, match):
                self.key = key
                self.match = match

        class Filter:
            def __init__(self, *, must):
                self.must = must

        class Models:
            pass

        Models.MatchValue = MatchValue
        Models.FieldCondition = FieldCondition
        Models.Filter = Filter
        query_filter = ingestion._qdrant_query_filter(
            Models,
            current_only=True,
            project="plans",
            artifact_type="roadmap",
            authority_class=None,
            repository=None,
            lifecycle_hint="active",
        )

        self.assertTrue(payload["catalog_current"])
        self.assertEqual(
            [(condition.key, condition.match.value) for condition in query_filter.must],
            [
                ("project", "plans"),
                ("artifact_type", "roadmap"),
                ("lifecycle_hints", "active"),
                ("catalog_current", True),
            ],
        )

    def test_graphiti_readiness_reports_closed_gates_without_mutation(self) -> None:
        result = ingestion.graphiti_readiness(
            host="127.0.0.1",
            port=9,
            password_env="FALKORDB_PASSWORD",
            llm_base_url="http://127.0.0.1:11434/v1",
            llm_model=None,
            embedding_base_url="http://127.0.0.1:11434/v1",
            embedding_model=None,
            embedding_dim=0,
            api_key_env="GRAPHITI_LLM_API_KEY",
            probe_models=False,
            timeout_seconds=0.1,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["graph_mutation"], "disabled")
        self.assertEqual(result["gates"]["configuration"]["status"], "fail")
        self.assertEqual(result["gates"]["structured_output"]["status"], "not_probed")

    def test_graphiti_group_must_match_outbox(self) -> None:
        result = self.prepare()
        with self.assertRaisesRegex(ingestion.IngestionError, "outbox Graphiti group"):
            ingestion.graphiti_ingest(
                workspace=self.workspace,
                outbox=Path(result["outbox"]),
                state_path=self.root / "state.sqlite3",
                host="127.0.0.1",
                port=6379,
                password_env="FALKORDB_PASSWORD",
                database="other_group",
                group_id="other_group",
                llm_base_url="http://127.0.0.1:11434/v1",
                llm_model=None,
                embedding_base_url="http://127.0.0.1:11434/v1",
                embedding_model=None,
                embedding_dim=0,
                api_key_env="GRAPHITI_LLM_API_KEY",
                structured_output_mode="json_schema",
                instructions_file=None,
                limit_units=1,
                retry_ambiguous=False,
                apply=False,
            )


if __name__ == "__main__":
    unittest.main()

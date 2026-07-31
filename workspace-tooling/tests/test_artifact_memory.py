from __future__ import annotations

import json
import sqlite3
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(TEST_DIR))

import artifact_memory as memory  # noqa: E402
import artifact_service_client as service_client  # noqa: E402
import artifact_runtime  # noqa: E402
from test_artifact_runtime import RuntimeFixture  # noqa: E402


class ArtifactMemoryControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.catalog = self.root / "catalog.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_catalog_status_marks_a_complete_generation_available(self) -> None:
        with sqlite3.connect(self.catalog) as connection:
            connection.executescript(
                """
                CREATE TABLE scan_runs (
                  run_id INTEGER PRIMARY KEY,
                  finished_at TEXT,
                  status TEXT NOT NULL
                );
                CREATE TABLE artifacts (
                  artifact_id TEXT PRIMARY KEY
                );
                CREATE VIEW current_artifact_revisions AS
                  SELECT artifact_id FROM artifacts;
                INSERT INTO scan_runs(run_id, finished_at, status)
                VALUES (1, '2026-07-18T00:00:00Z', 'complete');
                INSERT INTO artifacts(artifact_id) VALUES ('artifact:one');
                """
            )
        self.catalog.chmod(0o600)

        status = memory._catalog_status(self.catalog)

        self.assertTrue(status["available"])
        self.assertEqual(status["run_id"], 1)
        self.assertEqual(status["latest_attempt_status"], "complete")
        self.assertEqual(status["current_artifacts"], 1)

    def test_historical_search_is_rejected_before_qdrant_access(self) -> None:
        with mock.patch.object(memory.ingestion, "qdrant_search") as search:
            with mock.patch.object(service_client, "post_json") as post:
                with self.assertRaisesRegex(
                    memory.MemoryReadError,
                    "historical artifact snippets are disabled",
                ):
                    memory.search_artifacts(
                        workspace=self.workspace,
                        catalog=self.catalog,
                        query="old decision",
                        limit=8,
                        include_history=True,
                        project=None,
                        artifact_type=None,
                        authority_class=None,
                        repository=None,
                        lifecycle_hint=None,
                    )
        # Rejected ahead of every backend, so neither store is consulted and no
        # runtime backend selection is needed to refuse the request.
        search.assert_not_called()
        post.assert_not_called()

    def test_current_search_is_hard_clamped_to_current_only(self) -> None:
        # Pinned to a STUBBED embedded runtime so this stays a test of the
        # embedded read path rather than of whichever backend the machine
        # running the suite happens to declare. It used to pin to an unreadable
        # config, which silently degraded to the embedded branch; that degrade
        # is now a hard error, so the backend is stubbed explicitly instead.
        # `retrieval=None`, not a bare Mock. A Mock AUTO-CREATES attributes, so
        # `runtime.retrieval.embedding_model` silently produced a Mock object
        # and the `embedding_model=` set here was never read by anything. The
        # search argument was then never asserted, so this fixture proved
        # nothing whatsoever about model selection.
        embedded = mock.Mock(
            active_backend="embedded",
            qdrant_collection="personal_artifact_chunks_v1",
            retrieval=None,
        )
        with mock.patch.object(
            memory, "_runtime_selector", return_value=(embedded, {})
        ), mock.patch.object(
            memory.ingestion,
            "qdrant_search",
            return_value={"results": []},
        ) as search:
            result = memory.search_artifacts(
                workspace=self.workspace,
                catalog=self.catalog,
                query="current decision",
                limit=8,
                include_history=False,
                project=None,
                artifact_type=None,
                authority_class=None,
                repository=None,
                lifecycle_hint=None,
            )
        self.assertEqual(result["results"], [])
        self.assertTrue(search.call_args.kwargs["current_only"])
        # A config with no retrieval block declares no model, so search must be
        # asked to resolve it from the collection -- never handed a guess.
        self.assertIsNone(
            search.call_args.kwargs["embedding_model"],
            "a model was invented for a config that declares none",
        )

    def test_a_declared_embedding_model_is_passed_through(self) -> None:
        """When the config DOES declare a model, that exact string is used."""
        declared = mock.Mock(
            active_backend="embedded",
            qdrant_collection="personal_artifact_chunks_v1",
            retrieval=mock.Mock(embedding_model="BAAI/bge-small-en-v1.5"),
        )
        with mock.patch.object(
            memory, "_runtime_selector", return_value=(declared, {})
        ), mock.patch.object(
            memory.ingestion, "qdrant_search", return_value={"results": []}
        ) as search:
            memory.search_artifacts(
                workspace=self.workspace,
                catalog=self.catalog,
                query="q",
                limit=8,
                include_history=False,
                project=None,
                artifact_type=None,
                authority_class=None,
                repository=None,
                lifecycle_hint=None,
            )
        self.assertEqual(
            search.call_args.kwargs["embedding_model"], "BAAI/bge-small-en-v1.5"
        )

    def test_pilot_facts_are_rejected_before_falkordb_access(self) -> None:
        with self.assertRaisesRegex(
            memory.MemoryReadError,
            "unapproved Graphiti pilot access is disabled",
        ):
            memory.temporal_facts(
                query="ownership",
                limit=8,
                group_id=None,
                include_pilot=True,
                approvals=self.root / "missing-approvals.json",
                host="127.0.0.1",
                port=6379,
                password_env="FALKORDB_PASSWORD",
            )

    def test_status_hides_unapproved_group_names_and_content(self) -> None:
        approvals = self.root / "approvals.json"
        approvals.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "approved_groups": ["approved_v1"],
                }
            ),
            encoding="utf-8",
        )
        approvals.chmod(0o600)

        class QueryResult:
            result_set = [[2]]

        class Graph:
            def query(self, _query: str) -> QueryResult:
                return QueryResult()

        class Client:
            def __init__(self, **_kwargs: object):
                pass

            def list_graphs(self) -> list[str]:
                return ["approved_v1", "secret_pilot_v3"]

            def select_graph(self, name: str) -> Graph:
                if name != "approved_v1":
                    raise AssertionError("unapproved group was accessed")
                return Graph()

            def close(self) -> None:
                pass

        module = types.SimpleNamespace(FalkorDB=Client)
        with mock.patch.dict(sys.modules, {"falkordb": module}):
            status = memory._graphiti_status(
                "127.0.0.1",
                6379,
                "FALKORDB_PASSWORD",
                approvals,
            )

        serialized = json.dumps(status)
        self.assertNotIn("secret_pilot_v3", serialized)
        self.assertEqual(status["pilot_access"], "disabled")
        self.assertNotIn("unapproved_groups_hidden", status)
        self.assertEqual(
            status["groups"],
            [
                {
                    "group_id": "approved_v1",
                    "episodes": 2,
                    "quality_status": "approved",
                }
            ],
        )
        self.assertEqual(stat.S_IMODE(approvals.stat().st_mode), 0o600)

    def test_unapproved_and_unknown_group_ids_share_one_pre_db_denial(
        self,
    ) -> None:
        approvals = self.root / "approvals.json"
        approvals.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "approved_groups": ["approved_v1"],
                }
            ),
            encoding="utf-8",
        )
        approvals.chmod(0o600)
        messages = []
        for group_id in ("secret_pilot_v3", "does_not_exist"):
            with self.assertRaises(memory.MemoryReadError) as raised:
                memory.temporal_facts(
                    query="ownership",
                    limit=8,
                    group_id=group_id,
                    include_pilot=False,
                    approvals=approvals,
                    host="127.0.0.1",
                    port=6379,
                    password_env="FALKORDB_PASSWORD",
                )
            messages.append(str(raised.exception))
        self.assertEqual(
            messages,
            ["Graphiti group is not approved or unavailable"] * 2,
        )

    def test_unscoped_fact_query_never_selects_unapproved_graphs(self) -> None:
        approvals = self.root / "approvals.json"
        approvals.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "approved_groups": ["approved_v1"],
                }
            ),
            encoding="utf-8",
        )
        approvals.chmod(0o600)
        selected: list[str] = []

        class QueryResult:
            result_set: list[list[object]] = []

        class Graph:
            def query(
                self,
                _query: str,
                *,
                params: dict[str, str],
            ) -> QueryResult:
                self.params = params
                return QueryResult()

        class Client:
            def __init__(self, **_kwargs: object):
                pass

            def list_graphs(self) -> list[str]:
                return ["approved_v1", "secret_pilot_v3"]

            def select_graph(self, name: str) -> Graph:
                selected.append(name)
                return Graph()

            def close(self) -> None:
                pass

        module = types.SimpleNamespace(FalkorDB=Client)
        with mock.patch.dict(sys.modules, {"falkordb": module}):
            result = memory.temporal_facts(
                query="ownership",
                limit=8,
                group_id=None,
                include_pilot=False,
                approvals=approvals,
                host="127.0.0.1",
                port=6379,
                password_env="FALKORDB_PASSWORD",
            )
        self.assertEqual(selected, ["approved_v1"])
        self.assertNotIn("skipped_unapproved_groups", result)
        self.assertNotIn("secret_pilot_v3", json.dumps(result))


SERVICE_STATUS = {
    "service": {
        "status": "healthy",
        "active_backend": "server",
        "active_retrieval": "legacy-vector-v1",
        "degraded_reasons": [],
    },
    "qdrant": {
        "available": True,
        "collection": "legacy_chunks_g1",
        "generation": "legacy-g1",
        "points": 37_527,
        "current_points": 34_882,
        "historical_points": 2_645,
        "snapshot_age_seconds": 120.0,
        "error": None,
    },
}


class StatusBackendSelectionTests(unittest.TestCase):
    """`status` must describe the store clients ride, not the rollback copy."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = RuntimeFixture(Path(self.temp.name))
        self.opened: list[Path] = []
        patches = [
            mock.patch.object(
                memory,
                "_catalog_status",
                return_value={"available": True},
            ),
            mock.patch.object(
                memory,
                "_graphiti_status",
                return_value={"available": None},
            ),
            mock.patch.object(memory, "_receipt_count", return_value=0),
            mock.patch.object(memory, "_consumer_status", return_value={}),
            mock.patch.object(
                memory,
                "_open_embedded_store",
                side_effect=self._record_open,
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _record_open(self, path: Path, collection: str) -> dict[str, object]:
        self.opened.append(path)
        return {
            "available": True,
            "opened": True,
            "collection": collection,
            "points": 37_527,
        }

    def _config(self, **overrides: object) -> Path:
        payload = self.fixture.payload(active="legacy-vector-v1")
        payload.pop("retrieval")
        payload.update(overrides)
        return self.fixture.write(payload)

    def _status(self, config: Path, **kwargs: object) -> dict[str, object]:
        return memory.memory_status(
            catalog=self.fixture.derived / "catalog.sqlite3",
            approvals=self.fixture.derived / "approvals.json",
            receipt_root=self.fixture.receipts,
            consumer_state=self.fixture.derived / "consumer.sqlite3",
            host="127.0.0.1",
            port=6379,
            password_env="FALKORDB_PASSWORD",
            config_path=config,
            **kwargs,
        )

    def test_server_backend_reports_the_serving_generation_from_the_socket(
        self,
    ) -> None:
        config = self._config()
        with mock.patch.object(
            service_client,
            "post_json",
            return_value=SERVICE_STATUS,
        ) as post:
            result = self._status(config)

        self.assertEqual(post.call_args.kwargs["route"], "/v1/status")
        self.assertEqual(result["runtime"]["active_backend"], "server")
        self.assertEqual(result["runtime"]["generation"], "legacy-g1")
        self.assertEqual(result["qdrant"]["role"], "active")
        self.assertEqual(result["qdrant"]["backend"], "server")
        self.assertEqual(result["qdrant"]["source"], "resident-service-socket")
        self.assertEqual(result["qdrant"]["generation"], "legacy-g1")
        self.assertEqual(result["qdrant"]["collection"], "legacy_chunks_g1")
        self.assertEqual(result["qdrant"]["points"], 37_527)
        self.assertEqual(self.opened, [])

    def test_server_backend_labels_the_embedded_store_as_retained_rollback(
        self,
    ) -> None:
        config = self._config()
        with mock.patch.object(
            service_client,
            "post_json",
            return_value=SERVICE_STATUS,
        ):
            result = self._status(config)

        embedded = result["embedded_store"]
        self.assertEqual(embedded["role"], "rollback")
        self.assertEqual(embedded["mode"], "read-only")
        self.assertEqual(embedded["retained_until"], "2026-08-18T00:00:00Z")
        self.assertIs(embedded["retention_expired"], False)
        self.assertEqual(
            embedded["label"],
            "rollback (read-only, retained until 2026-08-18T00:00:00Z)",
        )
        self.assertIs(embedded["opened"], False)
        self.assertIsNone(embedded["available"])
        self.assertEqual(self.opened, [])

    def test_rollback_store_is_opened_only_when_explicitly_requested(
        self,
    ) -> None:
        config = self._config()
        with mock.patch.object(
            service_client,
            "post_json",
            return_value=SERVICE_STATUS,
        ):
            result = self._status(config, rollback_store=True)

        embedded = result["embedded_store"]
        self.assertIs(embedded["opened"], True)
        self.assertEqual(embedded["points"], 37_527)
        self.assertEqual(embedded["role"], "rollback")
        self.assertEqual(self.opened, [self.fixture.embedded])
        self.assertEqual(result["qdrant"]["generation"], "legacy-g1")

    def test_unreachable_service_never_falls_back_to_the_rollback_store(
        self,
    ) -> None:
        config = self._config()
        with mock.patch.object(
            service_client,
            "post_json",
            side_effect=service_client.ArtifactServiceError("socket is missing"),
        ):
            result = self._status(config)

        self.assertIs(result["qdrant"]["available"], False)
        self.assertEqual(result["qdrant"]["role"], "active")
        self.assertEqual(result["qdrant"]["generation"], "legacy-g1")
        self.assertIn("socket is missing", result["qdrant"]["error"])
        self.assertEqual(self.opened, [])

    def test_embedded_backend_reads_the_embedded_store_without_the_socket(
        self,
    ) -> None:
        config = self._config(active_backend="embedded")
        with mock.patch.object(service_client, "post_json") as post:
            result = self._status(config)

        post.assert_not_called()
        self.assertEqual(result["runtime"]["active_backend"], "embedded")
        self.assertEqual(result["qdrant"]["role"], "active")
        self.assertEqual(result["qdrant"]["backend"], "embedded")
        self.assertEqual(result["qdrant"]["label"], "active")
        self.assertEqual(self.opened, [self.fixture.embedded])

    def test_unreadable_runtime_config_degrades_to_the_embedded_store(
        self,
    ) -> None:
        missing = self.fixture.derived / "absent-runtime.json"
        with mock.patch.object(service_client, "post_json") as post:
            result = self._status(missing)

        post.assert_not_called()
        self.assertIs(result["runtime"]["available"], False)
        self.assertEqual(result["runtime"]["active_backend"], "unknown")
        self.assertEqual(result["qdrant"]["backend"], "embedded")
        self.assertEqual(self.opened, [memory.ingestion.DEFAULT_QDRANT_PATH])

    def test_status_names_paths_that_diverge_from_the_runtime_declaration(
        self,
    ) -> None:
        config = self._config()
        stray = self.fixture.derived / "stray-catalog.sqlite3"
        stray.write_bytes(b"stray\n")
        stray.chmod(0o600)
        with mock.patch.object(
            service_client,
            "post_json",
            return_value=SERVICE_STATUS,
        ):
            result = memory.memory_status(
                catalog=stray,
                approvals=self.fixture.derived / "approvals.json",
                receipt_root=self.fixture.receipts,
                consumer_state=self.fixture.derived / "consumer.sqlite3",
                host="127.0.0.1",
                port=6379,
                password_env="FALKORDB_PASSWORD",
                config_path=config,
            )

        self.assertEqual(
            result["path_divergence"],
            [
                {
                    "field": "catalog",
                    "inspected": str(stray),
                    "runtime_declares": str(
                        self.fixture.derived / "catalog.sqlite3"
                    ),
                }
            ],
        )


SERVICE_SEARCH = {
    "results": [{"relative_path": "plans/example.md", "score": 0.91}],
    "schema_version": memory.SCHEMA_VERSION,
    "retrieval_path": "resident-exact-hybrid-v2",
    "generation": "exact-g2",
    "active_backend": "server",
    "active_retrieval": "exact-hybrid-v2",
    "authority": "current-canonical-span",
    "verification": "source-and-span-hash-verified",
    "source_mutation": "disabled",
}


class SearchBackendSelectionTests(unittest.TestCase):
    """`search` must query the store clients ride, not the rollback copy."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = RuntimeFixture(Path(self.temp.name))
        patch = mock.patch.object(
            memory.ingestion,
            "qdrant_search",
            side_effect=AssertionError(
                "the embedded rollback store must not be opened"
            ),
        )
        self.embedded_search = patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _config(self, **overrides: object) -> Path:
        payload = self.fixture.payload(active="legacy-vector-v1")
        payload.pop("retrieval")
        payload.update(overrides)
        return self.fixture.write(payload)

    def _search(self, config: Path, **overrides: object) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "workspace": self.fixture.workspace,
            "catalog": self.fixture.derived / "catalog.sqlite3",
            "query": "promotion ownership",
            "limit": 8,
            "include_history": False,
            "project": None,
            "artifact_type": None,
            "authority_class": None,
            "repository": None,
            "lifecycle_hint": None,
            "config_path": config,
        }
        kwargs.update(overrides)
        return memory.search_artifacts(**kwargs)

    def test_server_backend_queries_the_service_socket_not_the_rollback_store(
        self,
    ) -> None:
        config = self._config()
        with mock.patch.object(
            service_client,
            "post_json",
            return_value=dict(SERVICE_SEARCH),
        ) as post:
            result = self._search(config)

        self.assertEqual(post.call_args.kwargs["route"], "/v1/search")
        self.assertEqual(
            post.call_args.kwargs["socket_path"],
            self.fixture.derived / "artifact-memory.sock",
        )
        self.assertEqual(result["results"], SERVICE_SEARCH["results"])
        self.assertEqual(result["active_backend"], "server")
        self.embedded_search.assert_not_called()

    def test_server_search_forwards_the_query_limit_and_filters(self) -> None:
        config = self._config()
        with mock.patch.object(
            service_client,
            "post_json",
            return_value=dict(SERVICE_SEARCH),
        ) as post:
            self._search(
                config,
                limit=3,
                project="kargo-cutover",
                artifact_type="roadmap",
                authority_class="canonical",
                repository="platform",
                lifecycle_hint="active",
            )

        self.assertEqual(
            post.call_args.kwargs["payload"],
            {
                "query": "promotion ownership",
                "limit": 3,
                "project": "kargo-cutover",
                "artifact_type": "roadmap",
                "authority_class": "canonical",
                "repository": "platform",
                "lifecycle_hint": "active",
            },
        )

    def test_server_search_preserves_the_authority_the_service_reported(
        self,
    ) -> None:
        config = self._config()
        with mock.patch.object(
            service_client,
            "post_json",
            return_value=dict(SERVICE_SEARCH),
        ):
            result = self._search(config)

        # The exact-hybrid path returns verified spans; stamping the weaker
        # discovery-only envelope over it would understate what was returned.
        self.assertEqual(result["authority"], "current-canonical-span")
        self.assertEqual(
            result["verification"],
            "source-and-span-hash-verified",
        )
        self.assertEqual(result["retrieval_path"], "resident-exact-hybrid-v2")

    def test_server_search_fills_an_envelope_a_legacy_service_omitted(
        self,
    ) -> None:
        config = self._config()
        with mock.patch.object(
            service_client,
            "post_json",
            return_value={"results": []},
        ):
            result = self._search(config)

        self.assertEqual(result["schema_version"], memory.SCHEMA_VERSION)
        self.assertEqual(result["authority"], "discovery-only")
        self.assertEqual(
            result["verification"],
            "read returned relative_path with get_artifact",
        )
        self.assertEqual(result["source_mutation"], "disabled")

    def test_unreachable_service_fails_and_never_reads_the_rollback_store(
        self,
    ) -> None:
        config = self._config()
        with mock.patch.object(
            service_client,
            "post_json",
            side_effect=service_client.ArtifactServiceError("socket is missing"),
        ):
            with self.assertRaisesRegex(
                memory.MemoryReadError,
                "socket is missing",
            ) as raised:
                self._search(config)

        # Answering from the retained rollback copy would return a different
        # generation's data under the serving store's name.
        self.assertIn("rollback", str(raised.exception))
        self.embedded_search.assert_not_called()

    def test_embedded_backend_reads_the_embedded_store_without_the_socket(
        self,
    ) -> None:
        config = self._config(active_backend="embedded")
        self.embedded_search.side_effect = None
        self.embedded_search.return_value = {"results": []}
        with mock.patch.object(service_client, "post_json") as post:
            result = self._search(config)

        post.assert_not_called()
        self.assertTrue(self.embedded_search.call_args.kwargs["current_only"])
        # The collection must come from the SAME runtime config the server
        # branch reads. Asserting ingestion.DEFAULT_COLLECTION here is what let
        # the two branches select different collections for one config.
        declared = artifact_runtime.load_runtime(config).qdrant_collection
        self.assertEqual(
            self.embedded_search.call_args.kwargs["collection"],
            declared,
        )
        self.assertEqual(result["authority"], "discovery-only")

    def test_unreadable_runtime_config_fails_loudly_instead_of_degrading(
        self,
    ) -> None:
        """CONTRACT CHANGE: an unreadable config is an error, not a fallback.

        This previously asserted that search degraded to the embedded store. It
        could only ever return zero hits: the fallback used
        ingestion.DEFAULT_COLLECTION, which the provisioner never creates, so
        "degraded" meant querying a collection that does not exist and
        answering emptily -- indistinguishable from "nothing matched". With no
        config there is no collection name to use, so the only honest answer is
        to say which file could not be read.
        """
        missing = self.fixture.derived / "absent-runtime.json"
        self.embedded_search.side_effect = None
        self.embedded_search.return_value = {"results": []}
        with mock.patch.object(service_client, "post_json") as post:
            with self.assertRaises(memory.MemoryReadError) as raised:
                self._search(missing)

        self.assertIn(str(missing), str(raised.exception))
        post.assert_not_called()
        self.embedded_search.assert_not_called()


if __name__ == "__main__":
    unittest.main()

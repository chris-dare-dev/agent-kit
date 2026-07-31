from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_memory_provision as provision  # noqa: E402
import artifact_runtime  # noqa: E402


# Health files are rewritten by the resident service, the consumer, and the
# bootstrap agent on their own schedules; they are volatile by design and are
# excluded from the plan hash-invariance assertion.
VOLATILE = ("*-health.json",)


def tree_digest(root: Path, *, exclude: tuple[str, ...] = VOLATILE) -> str:
    """Digest a derived-state tree including identity, not just content.

    Inode and mtime are folded in deliberately: an atomic same-content
    republish (``os.replace`` of a temporary file) changes both while leaving
    the bytes equal, and that IS a write the plan mode must not perform.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in exclude):
            continue
        info = path.lstat()
        digest.update(
            "\0".join(
                (
                    path.relative_to(root).as_posix(),
                    str(stat.S_IFMT(info.st_mode)),
                    str(stat.S_IMODE(info.st_mode)),
                    str(info.st_ino),
                    str(info.st_mtime_ns),
                    str(info.st_size),
                )
            ).encode("utf-8")
        )
        if stat.S_ISREG(info.st_mode):
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class ArtifactMemoryProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.derived = self.directory("derived")
        self.workspace = self.directory("workspace")
        self.outbox = self.directory("derived/outbox")
        self.receipts = self.directory("derived/skill-events")
        self.embedded = self.directory("derived/qdrant")
        self.catalog = self.file("artifact-catalog.sqlite3")
        self.ingestion_state = self.file("ingestion-state.sqlite3")
        self.consumer_state = self.file("artifact-event-consumer.sqlite3")
        self.service_root = self.derived / "services" / "qdrant"
        self.config = self.derived / "artifact-memory-runtime.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def directory(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(mode=0o700, parents=True)
        path.chmod(0o700)
        return path

    def file(self, name: str) -> Path:
        path = self.derived / name
        path.write_bytes(b"fixture\n")
        path.chmod(0o600)
        return path

    def write_config(self, payload: dict[str, object]) -> str:
        body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self.config.write_text(body, encoding="utf-8")
        self.config.chmod(0o600)
        return body

    # ---- harness -------------------------------------------------------

    def run_provision(
        self,
        *,
        apply: bool = False,
        replace_runtime: bool = False,
        service_root: Path | None = None,
        config: Path | None = None,
        docker_returncode: int = 0,
    ) -> dict[str, object]:
        service_root = self.service_root if service_root is None else service_root
        config = self.config if config is None else config
        model_cache = self.derived / "model-cache"
        completed = subprocess.CompletedProcess(
            args=["docker"],
            returncode=docker_returncode,
            stdout="qdrant started" if docker_returncode == 0 else "",
            stderr="" if docker_returncode == 0 else "docker daemon unreachable",
        )
        with ExitStack() as stack:
            patch = stack.enter_context
            patch(mock.patch.object(provision, "SERVICE_ROOT", service_root))
            patch(
                mock.patch.object(
                    provision.artifact_runtime,
                    "DEFAULT_DERIVED_ROOT",
                    self.derived,
                )
            )
            patch(
                mock.patch.object(
                    provision.artifact_runtime, "DEFAULT_CONFIG", config
                )
            )
            patch(
                mock.patch.object(
                    provision.ingestion, "DEFAULT_MODEL_CACHE", model_cache
                )
            )
            patch(
                mock.patch.object(
                    provision.ingestion, "DEFAULT_QDRANT_PATH", self.embedded
                )
            )
            patch(
                mock.patch.object(
                    provision.ingestion, "DEFAULT_CATALOG", self.catalog
                )
            )
            patch(
                mock.patch.object(
                    provision.ingestion, "DEFAULT_STATE", self.ingestion_state
                )
            )
            self.docker = patch(
                mock.patch.object(
                    provision.subprocess, "run", return_value=completed
                )
            )
            return provision.provision(
                workspace=self.workspace,
                apply=apply,
                replace_runtime=replace_runtime,
            )

    def install(self) -> dict[str, object]:
        """A completed first install, so re-provision behaviour is testable."""
        return self.run_provision(apply=True)

    # ---- F-03 (1): plan mode is strictly read-only ----------------------

    def test_plan_on_a_virgin_tree_creates_nothing(self) -> None:
        before = tree_digest(self.derived)
        result = self.run_provision(apply=False)

        self.assertEqual(tree_digest(self.derived), before)
        self.assertFalse(self.service_root.exists())
        self.assertFalse(self.config.exists())
        self.assertFalse((self.derived / "model-cache").exists())
        self.assertEqual(result["mode"], "plan")
        self.assertIs(result["read_only"], True)
        self.assertEqual(result["docker"], "planned")
        self.assertEqual(
            set(result["secrets"].values()),
            {"absent"},
            "a plan run must not mint API keys",
        )
        self.assertEqual(set(result["directories"].values()), {"absent"})

    def test_plan_hash_invariance_against_a_provisioned_tree(self) -> None:
        """Falsification test #3 — the F-03 regression guard.

        Hash the derived tree, run a plan, re-hash: identical.  Before the
        fix a plan run rewrote compose.yaml, .env, and the live runtime
        configuration, so this assertion fails against the old provisioner.
        """
        self.install()
        before = tree_digest(self.derived)

        result = self.run_provision(apply=False)

        self.assertEqual(
            tree_digest(self.derived),
            before,
            "plan mode mutated the derived-state tree",
        )
        self.assertEqual(result["mode"], "plan")
        self.assertEqual(result["runtime"]["action"], "unchanged")
        self.assertEqual(result["runtime"]["changes"], [])
        self.assertEqual(set(result["secrets"].values()), {"present"})
        self.assertEqual(set(result["directories"].values()), {"present"})
        self.assertEqual(set(result["files"].values()), {"identical"})

    def test_plan_never_invokes_docker(self) -> None:
        self.run_provision(apply=False)
        self.docker.assert_not_called()

    def test_plan_reports_the_field_level_diff_it_would_write(self) -> None:
        self.install()
        stale = json.loads(self.config.read_text(encoding="utf-8"))
        stale["qdrant"]["url"] = "http://127.0.0.1:6399"
        self.write_config(stale)

        result = self.run_provision(apply=False, replace_runtime=True)

        self.assertEqual(result["runtime"]["action"], "replace")
        self.assertIn(
            {
                "path": "qdrant.url",
                "from": "http://127.0.0.1:6399",
                # Read from the module so a deployment's port choice cannot
                # silently red this test (it did, after the 6333 -> 6343 move).
                "to": provision.QDRANT_URL,
                "kind": "changed",
            },
            result["runtime"]["changes"],
        )
        self.assertEqual(
            {change["path"] for change in result["runtime"]["changes"]},
            # --replace-runtime also mints a fresh retention deadline.
            {"qdrant.url", "rollback.retain_embedded_until"},
        )
        self.assertEqual(
            json.loads(self.config.read_text(encoding="utf-8"))["qdrant"]["url"],
            "http://127.0.0.1:6399",
            "reporting a diff must not apply it",
        )

    # ---- F-03 (2): the rollback retention clock is never silently reset --

    def test_reprovision_preserves_the_rollback_retention_deadline(self) -> None:
        self.install()
        original = json.loads(self.config.read_text(encoding="utf-8"))
        deadline = original["rollback"]["retain_embedded_until"]
        # Move the recorded deadline back; a re-provision must honour it.
        original["rollback"]["retain_embedded_until"] = "2026-07-20T00:00:00+00:00"
        self.write_config(original)

        planned = self.run_provision(apply=False)
        self.assertEqual(
            planned["runtime"]["retain_embedded_until"],
            "2026-07-20T00:00:00+00:00",
        )
        self.assertEqual(planned["runtime"]["retention"], "preserved")
        self.assertEqual(planned["runtime"]["action"], "unchanged")

        applied = self.run_provision(apply=True)
        self.assertEqual(
            json.loads(self.config.read_text(encoding="utf-8"))["rollback"][
                "retain_embedded_until"
            ],
            "2026-07-20T00:00:00+00:00",
        )
        self.assertEqual(applied["runtime"]["retention"], "preserved")
        self.assertNotEqual(deadline, "2026-07-20T00:00:00+00:00")

    def test_first_install_mints_a_thirty_day_retention_window(self) -> None:
        result = self.install()
        self.assertEqual(result["runtime"]["retention"], "minted")
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["rollback"]["retain_embedded_until"],
            result["runtime"]["retain_embedded_until"],
        )
        self.assertTrue(payload["rollback"]["deletion_requires_separate_approval"])
        self.assertEqual(payload["rollback"]["embedded_mode"], "read-only")

    def test_replace_runtime_is_the_only_way_to_reset_the_clock(self) -> None:
        self.install()
        stale = json.loads(self.config.read_text(encoding="utf-8"))
        stale["rollback"]["retain_embedded_until"] = "2026-07-20T00:00:00+00:00"
        self.write_config(stale)

        result = self.run_provision(apply=False, replace_runtime=True)

        self.assertEqual(result["runtime"]["retention"], "minted")
        self.assertNotEqual(
            result["runtime"]["retain_embedded_until"],
            "2026-07-20T00:00:00+00:00",
        )

    # ---- F-03 (3): the replace guard covers every existing runtime -------

    def test_exact_runtime_is_not_replaced_without_explicit_opt_in(self) -> None:
        original = self.write_config(
            {
                "schema_version": artifact_runtime.SCHEMA_VERSION,
                "active_retrieval": "exact-hybrid-v2",
                "retrieval": {"release": "pinned"},
            }
        )

        with self.assertRaisesRegex(provision.ProvisionError, "--replace-runtime"):
            self.run_provision(apply=True)

        self.assertEqual(self.config.read_text(encoding="utf-8"), original)
        self.assertFalse(self.service_root.exists())

    def test_legacy_runtime_is_not_replaced_without_explicit_opt_in(self) -> None:
        """The old guard covered only exact-hybrid; a legacy runtime was
        replaced silently, including on a plan run."""
        self.install()
        stale = json.loads(self.config.read_text(encoding="utf-8"))
        stale["qdrant"]["collection"] = "personal_artifact_chunks_v1"
        original = self.write_config(stale)

        with self.assertRaisesRegex(provision.ProvisionError, "--replace-runtime"):
            self.run_provision(apply=True)

        self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    def test_plan_reports_a_blocked_replacement_instead_of_raising(self) -> None:
        """A plan writes nothing, so it must be able to show the diff without
        the operator first passing the flag that authorises the overwrite."""
        self.install()
        stale = json.loads(self.config.read_text(encoding="utf-8"))
        stale["qdrant"]["collection"] = "personal_artifact_chunks_v1"
        original = self.write_config(stale)
        before = tree_digest(self.derived)

        result = self.run_provision(apply=False)

        self.assertEqual(result["runtime"]["action"], "replace")
        self.assertIs(result["runtime"]["blocked"], True)
        self.assertEqual(result["runtime"]["requires"], "--replace-runtime")
        self.assertEqual(
            [change["path"] for change in result["runtime"]["changes"]],
            ["qdrant.collection"],
        )
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)
        self.assertEqual(tree_digest(self.derived), before)

    def test_guard_names_the_profile_and_the_changed_fields(self) -> None:
        self.install()
        stale = json.loads(self.config.read_text(encoding="utf-8"))
        stale["qdrant"]["collection"] = "personal_artifact_chunks_v1"
        self.write_config(stale)

        with self.assertRaises(provision.ProvisionError) as caught:
            self.run_provision(apply=True)

        message = str(caught.exception)
        self.assertIn("legacy-or-unknown", message)
        self.assertIn("qdrant.collection", message)

    def test_identical_reprovision_needs_no_opt_in_and_rewrites_nothing(
        self,
    ) -> None:
        self.install()
        before = tree_digest(self.derived)

        result = self.run_provision(apply=True)

        self.assertEqual(result["runtime"]["action"], "unchanged")
        self.assertEqual(result["runtime"]["config_write"], "unchanged")
        self.assertEqual(set(result["files"].values()), {"unchanged"})
        self.assertEqual(set(result["secrets"].values()), {"existing"})
        self.assertEqual(
            tree_digest(self.derived),
            before,
            "an idempotent re-apply must not republish identical state",
        )

    def test_replace_runtime_opt_in_can_replace_an_exact_runtime(self) -> None:
        self.install()
        exact = json.loads(self.config.read_text(encoding="utf-8"))
        exact["active_retrieval"] = "exact-hybrid-v2"
        exact["retrieval"] = {"release": "pinned"}
        self.write_config(exact)

        result = self.run_provision(apply=True, replace_runtime=True)

        configured = artifact_runtime.load_runtime(self.config)
        self.assertEqual(configured.active_retrieval, "legacy-vector-v1")
        self.assertIsNone(configured.retrieval)
        self.assertEqual(
            result["runtime"]["previous_profile"], "exact-hybrid-v2/retrieval"
        )
        self.assertIs(result["runtime"]["replace_runtime"], True)
        self.assertEqual(result["runtime"]["action"], "replace")

    # ---- F-13: no phantom lexical index ---------------------------------

    def test_lexical_index_is_null_when_the_generation_has_none(self) -> None:
        result = self.install()

        payload = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertIsNone(
            payload["paths"]["lexical_index"],
            "the legacy generation has no lexical index; naming one invents a "
            "file that never existed",
        )
        self.assertIsNone(result["runtime"]["changes"] or None)
        self.assertIsNone(artifact_runtime.load_runtime(self.config).lexical_index)

    def test_lexical_index_is_named_only_when_the_file_exists(self) -> None:
        index = self.file(f"artifact-retrieval-{provision.GENERATION}.sqlite3")

        self.install()

        payload = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(payload["paths"]["lexical_index"], str(index))
        self.assertEqual(
            artifact_runtime.load_runtime(self.config).lexical_index, index
        )

    def test_runtime_loader_accepts_an_omitted_lexical_index(self) -> None:
        self.install()
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["paths"].pop("lexical_index")
        self.write_config(payload)

        self.assertIsNone(artifact_runtime.load_runtime(self.config).lexical_index)

    # ---- apply path ------------------------------------------------------

    def test_apply_publishes_a_loadable_uds_runtime_contract(self) -> None:
        self.service_root.mkdir(mode=0o700, parents=True)
        self.service_root.chmod(0o700)
        legacy_token = self.service_root / "service-token"
        legacy_token.write_text("legacy-token\n", encoding="utf-8")
        legacy_token.chmod(0o600)

        result = self.install()

        configured = artifact_runtime.load_runtime(self.config)
        self.assertEqual(
            configured.service_socket_path,
            self.service_root / "artifact-memory.sock",
        )
        self.assertFalse(os.path.lexists(configured.service_socket_path))
        self.assertEqual(configured.active_backend, "server")
        self.assertEqual(configured.qdrant_collection, provision.COLLECTION)
        self.assertEqual(legacy_token.read_text(encoding="utf-8"), "legacy-token\n")
        self.assertEqual(result["mode"], "apply")
        self.assertIs(result["read_only"], False)
        self.assertIs(result["runtime"]["validated"], True)
        self.assertEqual(
            set(result["secrets"]),
            {"admin", "read_only", "restore_admin", "restore_read_only"},
        )
        self.assertEqual(set(result["secrets"].values()), {"created"})
        self.assertEqual(result["docker"], "qdrant started")
        self.assertEqual(
            result["warnings"],
            [
                {
                    "code": "legacy-service-token-inert",
                    "path": str(legacy_token),
                    "message": (
                        "Inert legacy TCP bearer token retained; manually remove "
                        "it only after a verified UDS cutover."
                    ),
                }
            ],
        )

    def test_apply_writes_every_key_file_privately(self) -> None:
        self.install()
        for filename in provision.SECRET_FILES.values():
            path = self.service_root / filename
            with self.subTest(secret=filename):
                self.assertTrue(path.exists())
                self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600)
        environment = (self.service_root / ".env").read_text(encoding="utf-8")
        self.assertIn("QDRANT_API_KEY=", environment)
        self.assertEqual(
            stat.S_IMODE((self.service_root / ".env").lstat().st_mode), 0o600
        )

    def test_docker_failure_leaves_the_runtime_configuration_untouched(
        self,
    ) -> None:
        """The partial-activation window: the configuration is published only
        after Docker is up."""
        with self.assertRaisesRegex(RuntimeError, "docker compose failed"):
            self.run_provision(apply=True, docker_returncode=1)

        self.assertFalse(
            self.config.exists(),
            "a failed Docker start must not leave a published configuration",
        )

    def test_docker_failure_does_not_replace_an_existing_configuration(
        self,
    ) -> None:
        self.install()
        stale = json.loads(self.config.read_text(encoding="utf-8"))
        stale["qdrant"]["collection"] = "personal_artifact_chunks_v1"
        original = self.write_config(stale)

        with self.assertRaisesRegex(RuntimeError, "docker compose failed"):
            self.run_provision(
                apply=True, replace_runtime=True, docker_returncode=1
            )

        self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    # ---- CLI -------------------------------------------------------------

    def test_cli_defaults_to_a_read_only_plan(self) -> None:
        parsed = provision._parser().parse_args([])
        self.assertFalse(parsed.apply)
        self.assertFalse(parsed.replace_runtime)

    def test_replace_runtime_flag_is_explicit_in_the_cli(self) -> None:
        self.assertTrue(
            provision._parser().parse_args(["--replace-runtime"]).replace_runtime
        )


class ProfileIsolationTests(unittest.TestCase):
    """A profile must isolate every path, however the profile was selected.

    `--profile` sets AGENT_KIT_PROFILE during argument parsing, which is
    necessarily AFTER this module's imports have already snapshotted
    DEFAULT_DERIVED_ROOT, DEFAULT_CONFIG, DEFAULT_CATALOG and the rest. Service
    root and collection followed the flag because they are computed by
    functions; the other nine fields did not, because they are globals. The two
    supposedly equivalent ways of selecting a profile therefore produced
    different runs, and a profiled deployment wrote its catalog, outbox,
    ingestion state, consumer state, receipts, embedded Qdrant and model cache
    into the UNPROFILED tree — sharing bytes with the default deployment, and
    pointing `--profile` at the default deployment's runtime config file.
    """

    PROFILE = "isolationtest"

    def test_the_layout_places_every_path_under_the_profiled_root(self) -> None:
        layout = artifact_runtime.ResolvedLayout.for_profile(self.PROFILE)
        self.assertEqual(layout.profile, self.PROFILE)
        for path in layout.paths():
            self.assertTrue(
                str(path).startswith(str(layout.root)),
                f"{path} escapes the profiled root {layout.root}",
            )
        # The suffix must actually be present, or "under the root" is vacuous.
        self.assertTrue(layout.root.name.endswith(f"-{self.PROFILE}"), layout.root)

    def test_an_unprofiled_layout_still_honours_the_patchable_globals(self) -> None:
        # The provision tests redirect a whole run into a temp tree by patching
        # these. Resolving them freshly would ignore the patch and send the run
        # at the real derived root.
        sentinel = Path("/tmp/sentinel-derived-root/artifact-memory-runtime.json")
        with mock.patch.object(provision.artifact_runtime, "DEFAULT_CONFIG", sentinel):
            self.assertEqual(provision.resolve_layout(None).config, sentinel)

    def test_flag_and_environment_select_the_same_layout(self) -> None:
        """The regression: these two disagreed in nine fields."""
        from_flag = provision.resolve_layout(self.PROFILE)
        with mock.patch.dict(
            os.environ, {artifact_runtime.PROFILE_ENV: self.PROFILE}
        ):
            from_env = provision.resolve_layout(None)
        self.assertEqual(from_flag, from_env)

    def test_the_published_payload_never_leaks_an_unprofiled_path(self) -> None:
        """The invariant, checked over the payload rather than a field list.

        Asserting on named fields only catches the leaks already known. This
        walks every string in the published configuration, so a path added
        later that forgets the layout fails here too.
        """
        layout = provision.resolve_layout(self.PROFILE)
        payload = provision._runtime_payload(
            workspace=Path.cwd(),
            root=layout.root / "services" / "qdrant",
            snapshots=layout.root / "snapshots",
            retain_embedded_until="2099-01-01T00:00:00+00:00",
            layout=layout,
        )
        unprofiled_root = artifact_runtime.derived_root(None)

        def under(path: Path, base: Path) -> bool:
            # Path-aware, NOT a string prefix: the profiled root is a SIBLING
            # whose name STARTS WITH the unprofiled one (`agent-kit-p` beside
            # `agent-kit`), so `startswith` calls every profiled path a leak.
            return path == base or base in path.parents

        leaked = []

        def walk(node: object, where: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{where}.{key}")
            elif isinstance(node, str) and node.startswith(("/", "\\")) or (
                isinstance(node, str) and len(node) > 2 and node[1] == ":"
            ):
                if under(Path(node), unprofiled_root):
                    leaked.append((where, node))

        walk(payload, "")
        # `workspace` is the source repository, shared by every profile by
        # design, so it is exempt — and only it.
        leaked = [item for item in leaked if item[0] != ".paths.workspace"]
        self.assertEqual(leaked, [], f"unprofiled paths in the payload: {leaked}")


if __name__ == "__main__":
    unittest.main()

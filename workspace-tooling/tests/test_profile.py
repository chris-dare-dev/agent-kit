#!/usr/bin/env python3
"""Two profiles on one machine must not share a byte.

GENERATION, COLLECTION and QDRANT_URL were module constants, so one machine got
exactly one catalog, one collection and one port. Two engineers on one box, or
one engineer wanting a separate index per client repo, had no supported path.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifact_ingestion as ingestion  # noqa: E402
import artifact_memory_provision as provision  # noqa: E402
import artifact_runtime  # noqa: E402


class ProfileValidationTests(unittest.TestCase):
    def test_rejects_names_that_cannot_be_a_path_or_container_segment(self) -> None:
        for bad in ("Bad Name", "UPPER", "has/separator", "", "x" * 33):
            with self.subTest(name=bad):
                with self.assertRaises(artifact_runtime.ProfileError) as caught:
                    artifact_runtime.validate_profile(bad)
                self.assertIn("[a-z0-9-]", str(caught.exception))

    def test_accepts_reasonable_names(self) -> None:
        for good in ("work", "personal", "client-a", "x", "a1-b2"):
            self.assertEqual(artifact_runtime.validate_profile(good), good)


class ProfileIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get(artifact_runtime.PROFILE_ENV)
        os.environ.pop(artifact_runtime.PROFILE_ENV, None)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(artifact_runtime.PROFILE_ENV, None)
        else:
            os.environ[artifact_runtime.PROFILE_ENV] = self._saved

    def test_two_profiles_share_no_root_collection_or_port(self) -> None:
        names = (None, "work", "personal")
        roots = [artifact_runtime.derived_root(n) for n in names]
        colls = [provision.collection(n) for n in names]
        ports = [artifact_runtime.allocated_ports(n)[0] for n in names]

        self.assertEqual(len(set(roots)), 3, f"roots collide: {roots}")
        self.assertEqual(len(set(colls)), 3, f"collections collide: {colls}")
        self.assertEqual(len(set(ports)), 3, f"ports collide: {ports}")

    def test_ports_are_deterministic_across_calls(self) -> None:
        """A stored runtime config must stay valid across restarts."""
        self.assertEqual(
            artifact_runtime.allocated_ports("work"), artifact_runtime.allocated_ports("work")
        )

    def test_the_env_var_and_the_argument_agree(self) -> None:
        os.environ[artifact_runtime.PROFILE_ENV] = "work"
        self.assertEqual(artifact_runtime.derived_root(), artifact_runtime.derived_root("work"))
        self.assertEqual(ingestion.collection_for("g1"), ingestion.collection_for("g1", "work"))

    def test_the_unprofiled_default_is_unchanged(self) -> None:
        """Adding profiles must not move anyone's existing store."""
        self.assertTrue(str(artifact_runtime.derived_root(None)).endswith("agent-kit"))
        self.assertEqual(artifact_runtime.allocated_ports(None)[0], 6343)
        self.assertEqual(provision.collection(None), "personal_artifact_chunks_p20260721v1")


class PortAllocationTests(unittest.TestCase):
    """Ports must be unique across profiles, not merely unique across three.

    The previous scheme hashed the name into 200 buckets:

        span = sum(ord(c) * (i + 1) for i, c in enumerate(name))
        return BASE + offset + 10 + (span % 200) * 2

    `a` and `ad` both produce span % 200 == 97, so both claim 6547; `aag`'s
    restore port and `acf`'s main port both claim 6355. Sweeping every one-,
    two- and three-letter name, 185 of the 187 reachable ports had more than
    one claimant. The test that was supposed to cover this checked three names
    -- and three names happening not to collide says nothing about the scheme.
    """

    def sweep_names(self) -> list[str]:
        import itertools
        import string

        return [
            "".join(c)
            for n in (1, 2)
            for c in itertools.product(string.ascii_lowercase, repeat=n)
        ]

    def test_allocation_is_collision_free_across_many_profiles(self) -> None:
        registry: dict[str, dict[str, int]] = {}
        claims: dict[int, str] = {}
        for name in self.sweep_names():
            main, restore = artifact_runtime.allocate_ports(name, registry=registry)
            for port, kind in ((main, "main"), (restore, "restore")):
                self.assertNotIn(
                    port, claims,
                    f"{name}/{kind} port {port} already claimed by {claims.get(port)}",
                )
                claims[port] = f"{name}/{kind}"
            registry[name] = {"main": main, "restore": restore}
        self.assertEqual(len(claims), 2 * len(self.sweep_names()))

    def test_allocation_is_stable_once_recorded(self) -> None:
        """A stored runtime config must stay valid across restarts."""
        registry: dict[str, dict[str, int]] = {}
        first = artifact_runtime.allocate_ports("work", registry=registry)
        registry["work"] = {"main": first[0], "restore": first[1]}
        # Other profiles arriving later must not move an existing allocation.
        for name in ("alpha", "beta", "gamma"):
            main, restore = artifact_runtime.allocate_ports(name, registry=registry)
            registry[name] = {"main": main, "restore": restore}
        self.assertEqual(artifact_runtime.allocate_ports("work", registry=registry), first)

    def test_no_profile_may_take_the_unprofiled_default_ports(self) -> None:
        registry: dict[str, dict[str, int]] = {}
        reserved = {
            artifact_runtime.QDRANT_BASE_PORT,
            artifact_runtime.QDRANT_BASE_PORT + provision.RESTORE_PORT_OFFSET,
        }
        for name in self.sweep_names()[:64]:
            main, restore = artifact_runtime.allocate_ports(name, registry=registry)
            self.assertNotIn(main, reserved, f"{name} took a default port")
            self.assertNotIn(restore, reserved, f"{name} took a default port")
            registry[name] = {"main": main, "restore": restore}

    def test_the_ledger_stays_shared_while_a_profile_is_active(self) -> None:
        """The ledger must not follow the profile, or it is not shared.

        `derived_root(None)` means "consult AGENT_KIT_PROFILE", not "no
        profile". Resolving the ledger that way put each profile's ledger inside
        its OWN root, so no profile could see what any other held and they would
        all allocate the same port. Same None-is-bimodal trap that made
        --profile and the environment variable disagree.
        """
        saved = os.environ.get(artifact_runtime.PROFILE_ENV)
        os.environ.pop(artifact_runtime.PROFILE_ENV, None)
        try:
            unprofiled = artifact_runtime.port_registry_path()
            os.environ[artifact_runtime.PROFILE_ENV] = "work"
            self.assertEqual(
                artifact_runtime.port_registry_path(), unprofiled,
                "the shared ledger moved into the active profile's root",
            )
        finally:
            if saved is None:
                os.environ.pop(artifact_runtime.PROFILE_ENV, None)
            else:
                os.environ[artifact_runtime.PROFILE_ENV] = saved

    def test_a_recorded_port_is_honoured_even_when_it_looks_unusual(self) -> None:
        """The registry is the authority: an operator may pin a port by hand."""
        registry = {"work": {"main": 7001, "restore": 7003}}
        self.assertEqual(artifact_runtime.allocate_ports("work", registry=registry), (7001, 7003))
        # And a later profile must route around it.
        main, restore = artifact_runtime.allocate_ports("other", registry=registry)
        self.assertNotIn(main, (7001, 7003))
        self.assertNotIn(restore, (7001, 7003))


class ComposeRenderTests(unittest.TestCase):
    """`docker compose up` for two profiles must not collide."""

    def setUp(self) -> None:
        self.text = provision.CANONICAL_COMPOSE.read_text(encoding="utf-8")

    def _names(self, out: str) -> list[str]:
        return [l.split(": ", 1)[1].strip() for l in out.splitlines() if "container_name" in l]

    def _ports(self, out: str) -> list[str]:
        return [l.strip().strip('- "') for l in out.splitlines() if "127.0.0.1:6" in l]

    def test_unprofiled_render_is_byte_identical(self) -> None:
        """No profile must mean no rewrite at all."""
        self.assertEqual(provision.render_compose(self.text, None), self.text)

    def test_each_profile_gets_distinct_names_and_ports(self) -> None:
        work = provision.render_compose(self.text, "work")
        personal = provision.render_compose(self.text, "personal")
        self.assertEqual(len(set(self._names(work)) & set(self._names(personal))), 0)
        self.assertEqual(len(set(self._ports(work)) & set(self._ports(personal))), 0)
        for out, name in ((work, "work"), (personal, "personal")):
            for container in self._names(out):
                self.assertTrue(container.endswith(name), container)

    def test_the_restore_container_is_not_double_suffixed(self) -> None:
        """Sequential str.replace produced '-qdrant-work-restore-work'.

        The shorter '<project>-qdrant' rule matched the prefix of the line the
        restore rule had already rewritten. The substitutions are anchored to
        end-of-line so each matches exactly one whole name.
        """
        names = self._names(provision.render_compose(self.text, "work"))
        self.assertIn("personal-artifact-memory-qdrant-work", names)
        self.assertIn("personal-artifact-memory-qdrant-restore-work", names)
        for container in names:
            self.assertEqual(container.count("work"), 1, f"double-suffixed: {container}")

    def test_the_compose_project_name_is_suffixed(self) -> None:
        """It sits on line 1, so a rule keyed on a preceding newline misses it."""
        out = provision.render_compose(self.text, "work")
        project = next(l for l in out.splitlines() if l.startswith("name:"))
        self.assertEqual(project, "name: personal-artifact-memory-work")

    def test_restore_port_keeps_its_offset_from_the_main_port(self) -> None:
        main, restore = artifact_runtime.allocated_ports("work")
        self.assertEqual(restore - main, provision.RESTORE_PORT_OFFSET)


# At the END of the file, deliberately. This block used to sit above
# ComposeRenderTests, so `python tests/test_profile.py` ran unittest.main()
# before those classes were defined and silently tested none of them.
if __name__ == "__main__":
    unittest.main()

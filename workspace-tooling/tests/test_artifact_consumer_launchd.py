from __future__ import annotations

import plistlib
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
PLIST = SCRIPT_DIR / "com.personal.artifact-event-consumer.plist"


class ArtifactConsumerLaunchdTests(unittest.TestCase):
    def test_job_enforces_private_umask_and_failure_surface(self) -> None:
        with PLIST.open("rb") as handle:
            value = plistlib.load(handle)
        arguments = value["ProgramArguments"]
        self.assertEqual(value["Umask"], 0o77)
        self.assertIn("--health-file", arguments)
        self.assertIn("--desktop-notify", arguments)
        self.assertIn("--reconcile-retry-seconds", arguments)
        self.assertNotEqual(
            value["StandardOutPath"],
            value["StandardErrorPath"],
        )
        self.assertNotIn("KeepAlive", value)

    def test_job_names_no_sink_and_cannot_target_the_retired_store(self) -> None:
        """The job must not carry sink flags at all.

        The installed job previously pinned --qdrant-path at the embedded
        store and --collection at the pre-migration collection, so loading it
        after the server cutover would have published into the retired
        rollback store while the serving generation went stale.  The sink is
        now resolved from the runtime configuration, and a job that names one
        is a regression this asserts against.
        """
        with PLIST.open("rb") as handle:
            arguments = plistlib.load(handle)["ProgramArguments"]
        self.assertNotIn("--qdrant-path", arguments)
        self.assertNotIn("--collection", arguments)
        self.assertIn("--runtime-config", arguments)
        joined = " ".join(arguments)
        self.assertNotIn("personal_artifact_chunks_v1", joined)
        self.assertNotIn("personal-artifacts/qdrant", joined)

    def test_installed_copy_matches_the_repository_copy(self) -> None:
        """A drifted installed copy is what launchd actually runs."""
        installed = (
            Path.home()
            / "Library/LaunchAgents"
            / "com.personal.artifact-event-consumer.plist"
        )
        if not installed.exists():
            self.skipTest("agent is not installed on this host")
        self.assertEqual(installed.read_bytes(), PLIST.read_bytes())


if __name__ == "__main__":
    unittest.main()

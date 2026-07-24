from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_qdrant_bootstrap as bootstrap  # noqa: E402


def _completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["docker"], returncode=returncode, stdout=stdout, stderr="boom"
    )


def _healthy_docker(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """A REACHABLE engine with a RUNNING container.

    Exit codes alone cannot express this: the WSL-integration `docker` shim
    exits 0 with empty stdout when Docker Desktop is stopped, so a healthy
    fixture must carry the payloads the real checks read — the server version
    from `info`, and a container id from `compose ps --status running`.
    """
    if arguments[:1] == ["info"]:
        return _completed(0, "29.1.3\n")
    if "ps" in arguments:
        return _completed(0, "b0bd0dc4f00d\n")
    return _completed(0)


class BootstrapWatchdogPhaseTests(unittest.TestCase):
    """The bootstrap job carries the watchdog; neither phase may mask the other."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.health = Path(self.temp.name) / "bootstrap-health.json"
        docker = mock.MagicMock()
        docker.is_file.return_value = True
        docker.__str__.return_value = "/usr/local/bin/docker"
        self.patches = [
            mock.patch.object(bootstrap, "HEALTH", self.health),
            mock.patch.object(bootstrap, "DOCKER", docker),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def test_watchdog_runs_on_a_healthy_bootstrap(self) -> None:
        with mock.patch.object(bootstrap, "_run", side_effect=_healthy_docker), \
            mock.patch.object(
                bootstrap.artifact_watchdog, "run", return_value={"status": "healthy"}
            ) as runner:
            code = bootstrap.main()
        self.assertEqual(code, 0)
        runner.assert_called_once()

    def test_watchdog_still_runs_when_docker_fails(self) -> None:
        """A Qdrant outage must not silence consumer alerting."""
        with mock.patch.object(bootstrap, "_run", return_value=_completed(1)), \
            mock.patch.object(
                bootstrap.artifact_watchdog, "run", return_value={"status": "unhealthy"}
            ) as runner:
            code = bootstrap.main()
        self.assertEqual(code, 2)  # docker failure is still reported
        runner.assert_called_once()

    def test_watchdog_fault_does_not_change_bootstrap_exit_status(self) -> None:
        """A watchdog fault must not mask a healthy — or a failing — Qdrant."""
        with mock.patch.object(bootstrap, "_run", side_effect=_healthy_docker), \
            mock.patch.object(
                bootstrap.artifact_watchdog, "run", side_effect=RuntimeError("nope")
            ):
            code = bootstrap.main()
        self.assertEqual(code, 0)

    def test_unhealthy_watchdog_verdict_does_not_fail_the_bootstrap_job(self) -> None:
        with mock.patch.object(bootstrap, "_run", side_effect=_healthy_docker), \
            mock.patch.object(
                bootstrap.artifact_watchdog,
                "run",
                return_value={"status": "unhealthy", "failures": ["consumer gone"]},
            ):
            code = bootstrap.main()
        self.assertEqual(code, 0)


class BootstrapHonestHealthTests(unittest.TestCase):
    """`status: healthy` must mean Qdrant is SERVING — not "commands exited 0".

    Both cases below were live false-healthies on the Windows/WSL port
    (2026-07-22): the health file asserted a healthy bootstrap while Qdrant
    was not running at all.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.health = Path(self.temp.name) / "bootstrap-health.json"
        docker = mock.MagicMock()
        docker.is_file.return_value = True
        docker.__str__.return_value = "/usr/bin/docker"
        self.patches = [
            mock.patch.object(bootstrap, "HEALTH", self.health),
            mock.patch.object(bootstrap, "DOCKER", docker),
            mock.patch.object(bootstrap.artifact_watchdog, "run", return_value={}),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def _status(self) -> str:
        return json.loads(self.health.read_text(encoding="utf-8"))["status"]

    def test_engine_down_shim_exiting_zero_is_not_healthy(self) -> None:
        # Docker Desktop stopped: the WSL-integration shim exits 0 and prints
        # NOTHING. A returncode-only check called this healthy.
        with mock.patch.object(bootstrap, "_run", return_value=_completed(0)):
            code = bootstrap.main()
        self.assertEqual(code, 2)
        self.assertEqual(self._status(), "failed")

    def test_container_not_running_after_compose_is_not_healthy(self) -> None:
        # Engine reachable and `compose up` exits 0, but no container is
        # actually running — health must not claim a serving Qdrant.
        def engine_up_container_down(arguments, timeout):
            if arguments[:1] == ["info"]:
                return _completed(0, "29.1.3\n")
            if "ps" in arguments:
                return _completed(0, "")  # no running container id
            return _completed(0)

        with mock.patch.object(bootstrap, "_run", side_effect=engine_up_container_down):
            code = bootstrap.main()
        self.assertEqual(code, 2)
        self.assertEqual(self._status(), "failed")

    def test_serving_engine_and_running_container_is_healthy(self) -> None:
        with mock.patch.object(bootstrap, "_run", side_effect=_healthy_docker):
            code = bootstrap.main()
        self.assertEqual(code, 0)
        self.assertEqual(self._status(), "healthy")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest

import platform_skips

import platform_compat
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_security as security  # noqa: E402


class ArtifactSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @platform_skips.requires_posix_modes
    def test_explicit_modes_hold_under_umask_022(self) -> None:
        prior = os.umask(0o022)
        try:
            directory = security.ensure_private_directory(self.root / "derived")
            target = directory / "event.json"
            security.atomic_write_bytes(target, b"complete\n")
        finally:
            os.umask(prior)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_unsafe_existing_mode_and_symlink_fail_closed(self) -> None:
        directory = self.root / "derived"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        with self.assertRaisesRegex(security.PrivateStateError, "0700"):
            security.ensure_private_directory(directory)
        external = self.root / "external"
        external.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(security.PrivateStateError, "symlink"):
            security.require_private_directory(linked)

    @platform_skips.requires_symlinks
    def test_user_owned_intermediate_symlink_fails_closed(self) -> None:
        external = self.root / "external"
        external.mkdir(mode=0o700)
        linked = self.root / "linked"
        linked.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(
            security.PrivateStateError,
            "user-controlled symlink",
        ):
            security.ensure_private_directory(linked / "derived")

    def test_foreign_owner_is_rejected(self) -> None:
        directory = self.root / "derived"
        directory.mkdir(mode=0o700)
        real = directory.lstat()
        foreign = os.stat_result(
            (
                real.st_mode,
                real.st_ino,
                real.st_dev,
                real.st_nlink,
                platform_compat.current_uid() + 1,
                real.st_gid,
                real.st_size,
                real.st_atime,
                real.st_mtime,
                real.st_ctime,
            )
        )
        with mock.patch.object(Path, "lstat", return_value=foreign):
            with self.assertRaisesRegex(security.PrivateStateError, "not owned"):
                security.require_private_directory(directory)

    def test_atomic_failure_before_publish_leaves_no_visible_file(self) -> None:
        directory = security.ensure_private_directory(self.root / "derived")
        target = directory / "receipt.json"

        def fail(stage: str) -> None:
            if stage == "after_file_fsync":
                raise RuntimeError("crash")

        with self.assertRaisesRegex(RuntimeError, "crash"):
            security.atomic_write_bytes(target, b"complete", fault=fail)
        self.assertFalse(target.exists())
        self.assertEqual(list(directory.iterdir()), [])

    def test_atomic_failure_after_publish_leaves_complete_visible_file(self) -> None:
        directory = security.ensure_private_directory(self.root / "derived")
        target = directory / "receipt.json"

        def fail(stage: str) -> None:
            if stage == "after_publish":
                raise RuntimeError("crash")

        with self.assertRaisesRegex(RuntimeError, "crash"):
            security.atomic_write_bytes(target, b"complete", fault=fail)
        self.assertEqual(target.read_bytes(), b"complete")
        self.assertEqual([path.name for path in directory.iterdir()], ["receipt.json"])

    def test_immutable_publication_never_overwrites(self) -> None:
        directory = security.ensure_private_directory(self.root / "derived")
        target = directory / "receipt.json"
        security.atomic_write_bytes(target, b"first")
        with self.assertRaises(FileExistsError):
            security.atomic_write_bytes(target, b"second")
        self.assertEqual(target.read_bytes(), b"first")

    @platform_skips.requires_posix_modes
    def test_explicit_repair_preserves_content_and_rejects_links(self) -> None:
        root = self.root / "derived"
        child = root / "outbox"
        child.mkdir(parents=True)
        target = child / "units.gz"
        target.write_bytes(b"payload")
        root.chmod(0o755)
        child.chmod(0o755)
        target.chmod(0o644)
        result = security.repair_private_tree(root)
        self.assertEqual(result, {"directories": 2, "files": 1})
        self.assertEqual(target.read_bytes(), b"payload")
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

        external = self.root / "external"
        external.mkdir()
        (root / "linked").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(security.PrivateStateError, "refuses"):
            security.repair_private_tree(root)


class PermissionSweepTests(unittest.TestCase):
    """Regression cover for F-15: a loose mode under the derived root is caught."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace-artifacts"
        self.root.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_clean_tree_conforms(self) -> None:
        child = self.root / "outbox"
        child.mkdir(mode=0o700)
        target = child / "units.json"
        target.write_bytes(b"{}")
        target.chmod(0o600)
        report = security.sweep_private_tree(self.root)
        self.assertTrue(report["conforming"])
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["counts"]["directories"], 2)
        self.assertEqual(report["counts"]["files"], 1)

    def test_group_readable_directory_is_a_violation(self) -> None:
        # The exact F-15 shape: a 0755 directory under an 0700 parent.
        loose = self.root / "evals"
        loose.mkdir(mode=0o700)
        loose.chmod(0o755)
        report = security.sweep_private_tree(self.root)
        self.assertFalse(report["conforming"])
        self.assertEqual(
            [(item["path"], item["reason"]) for item in report["violations"]],
            [("evals", "group or other accessible")],
        )
        self.assertEqual(report["violations"][0]["mode"], "0755")

    def test_group_readable_file_is_a_violation(self) -> None:
        target = self.root / "runtime.json"
        target.write_bytes(b"{}")
        target.chmod(0o644)
        report = security.sweep_private_tree(self.root)
        self.assertFalse(report["conforming"])
        self.assertEqual(report["violations"][0]["reason"], "group or other accessible")

    @platform_skips.requires_chmod_enforcement
    def test_sealed_read_only_file_is_not_a_violation(self) -> None:
        """0400 is a seal, not a defect — flagging it invites a loosening repair."""
        sealed = self.root / "retired"
        sealed.mkdir(mode=0o700)
        target = sealed / "gold-v1.SEALED.tar"
        target.write_bytes(b"sealed")
        target.chmod(0o400)
        report = security.sweep_private_tree(self.root)
        self.assertTrue(report["conforming"])
        self.assertEqual(
            [(item["path"], item["mode"]) for item in report["tighter_than_policy"]],
            [("retired/gold-v1.SEALED.tar", "0400")],
        )

    @platform_skips.requires_af_unix
    def test_socket_mode_is_swept(self) -> None:
        import socket as socket_module

        # AF_UNIX paths are capped near 104 bytes on macOS, so the default
        # /var/folders temporary root is too deep to bind under. Use the real
        # /private/tmp rather than the /tmp symlink, which the ancestor check
        # rejects. On Linux there is no /private/tmp and /tmp is a real,
        # short directory, so it satisfies both constraints directly.
        short_root = "/private/tmp" if Path("/private/tmp").is_dir() else "/tmp"
        short = tempfile.TemporaryDirectory(dir=short_root)
        self.addCleanup(short.cleanup)
        root = Path(short.name) / "d"
        root.mkdir(mode=0o700)
        sock_path = root / "m.sock"
        server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(sock_path))
        sock_path.chmod(0o600)
        self.assertTrue(security.sweep_private_tree(root)["conforming"])
        sock_path.chmod(0o666)
        report = security.sweep_private_tree(root)
        self.assertFalse(report["conforming"])
        self.assertEqual(report["violations"][0]["type"], "socket")
        self.assertEqual(report["counts"]["sockets"], 1)

    @platform_skips.requires_symlinks
    def test_symlink_is_a_violation(self) -> None:
        external = Path(self.temp.name) / "external"
        external.mkdir(mode=0o700)
        (self.root / "linked").symlink_to(external, target_is_directory=True)
        report = security.sweep_private_tree(self.root)
        self.assertFalse(report["conforming"])
        self.assertEqual(report["violations"][0]["type"], "symlink")

    def test_third_party_trees_are_excluded_visibly_not_silently(self) -> None:
        venv = self.root / "venv"
        venv.mkdir(mode=0o755)
        (venv / "bin").mkdir(mode=0o755)
        report = security.sweep_private_tree(self.root)
        self.assertTrue(report["conforming"])
        self.assertEqual(report["excluded_trees"], ["venv"])
        self.assertIn("third-party", report["excluded_reason"])

    def test_sweep_opens_no_file_content(self) -> None:
        """Holdout custody: the sweep must judge from lstat alone, never a read."""
        corpus = self.root / "evals" / "retrieval-gold-v1"
        corpus.mkdir(mode=0o700, parents=True)
        holdout = corpus / "retrieval-gold-v1.holdout.json"
        holdout.write_bytes(b'{"secret": "holdout"}')
        holdout.chmod(0o600)

        opened: list[str] = []
        real_open = os.open

        def tracking_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            opened.append(os.fspath(path))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(os, "open", side_effect=tracking_open):
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read")):
                with mock.patch.object(Path, "read_text", side_effect=AssertionError("read")):
                    report = security.sweep_private_tree(self.root)

        self.assertTrue(report["conforming"])
        self.assertEqual(report["content_read"], "none")
        self.assertNotIn(str(holdout), opened)


if __name__ == "__main__":
    unittest.main()

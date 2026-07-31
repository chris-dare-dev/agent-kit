"""Tests for platform_compat — the shim every other portability fix depends on.

M2 / gates-green-t-platform-compat. Built before any call site was touched: if
the shim is wrong, routing ten modules through it converts one import error into
ten silent correctness bugs.

Runs on win32, linux and darwin with no skips. The contention test is the load
bearing one — a lock that does not actually serialise is worse than no lock,
because the callers it replaces documented an atomicity guarantee.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import platform_compat  # noqa: E402


ITERATIONS = 500


def _append_under_lock(path: str, marker: str, iterations: int) -> None:
    """Write `marker` twice per iteration, holding the lock across both writes.

    Two writes with the lock held is what makes interleaving detectable: if the
    lock does not serialise, some other holder's marker lands between them and
    the file contains an "ab" pair instead of "aa"/"bb".
    """
    lock_path = path + ".lock"
    data_fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    # The lock file is never written to. On Windows the locked byte range is not
    # writable by the other holder, so a holder that writes its own lock file
    # gets EACCES the moment it loses the race — which is a bug in the caller,
    # not in the lock. Every real call site opens the lock file and locks it.
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        for _ in range(iterations):
            with platform_compat.exclusive_file_lock(lock_fd):
                os.write(data_fd, marker.encode())
                os.write(data_fd, marker.encode())
    finally:
        os.close(data_fd)
        os.close(lock_fd)


class TestIdentity(unittest.TestCase):
    def test_supports_posix_privacy_is_false_only_on_windows(self):
        self.assertEqual(
            platform_compat.supports_posix_privacy(),
            sys.platform != "win32",
        )

    def test_current_uid_is_stable_within_a_process(self):
        first = platform_compat.current_uid()
        for _ in range(10):
            self.assertEqual(platform_compat.current_uid(), first)
        self.assertIsInstance(first, int)

    def test_current_uid_matches_geteuid_on_posix(self):
        if sys.platform == "win32":
            self.assertEqual(platform_compat.current_uid(), platform_compat.NO_POSIX_UID)
        else:
            self.assertEqual(platform_compat.current_uid(), os.geteuid())

    def test_windows_sentinel_can_never_equal_a_real_st_uid(self):
        # The whole point of -1: an ownership check that forgets to branch on
        # supports_posix_privacy() must DENY on Windows, not pass. st_uid is 0
        # there, and 0 != -1.
        self.assertLess(platform_compat.NO_POSIX_UID, 0)
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe"
            probe.write_text("x", encoding="utf-8")
            if sys.platform == "win32":
                self.assertNotEqual(probe.stat().st_uid, platform_compat.current_uid())

    def test_identity_is_a_stable_non_empty_string(self):
        identity = platform_compat.current_user_identity()
        self.assertIsInstance(identity, str)
        self.assertTrue(identity)
        self.assertEqual(identity, platform_compat.current_user_identity())


class TestLocking(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.lock_path = self.tmp / "resource.lock"
        self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)

    def tearDown(self):
        os.close(self.fd)
        self._tmp.cleanup()

    def test_lock_round_trips(self):
        with platform_compat.exclusive_file_lock(self.fd) as held:
            self.assertEqual(held, self.fd)
        # Re-acquiring proves the release actually happened.
        with platform_compat.exclusive_file_lock(self.fd):
            pass

    def test_lock_is_released_on_exception(self):
        with self.assertRaises(RuntimeError):
            with platform_compat.exclusive_file_lock(self.fd):
                raise RuntimeError("boom")
        # If the finally had been dropped, this would hang or fail.
        with platform_compat.exclusive_file_lock(self.fd, timeout=5):
            pass

    def test_locks_a_zero_length_lock_file(self):
        """The call sites this shim replaces open a lock file and flock it —
        none of them writes a byte first. Windows locks a byte RANGE, and a
        range beyond EOF is lockable, so the shim must not require content."""
        self.assertEqual(os.fstat(self.fd).st_size, 0)
        other = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with platform_compat.exclusive_file_lock(self.fd):
                # A bounded wait must mean the same thing on every platform.
                # While POSIX blocked in flock and ignored `timeout`, this line
                # hung forever on Linux and returned in 0.5s on Windows.
                with self.assertRaises(platform_compat.LockTimeout):
                    platform_compat.lock_file_exclusive(other, timeout=0.5)
            # Released — the second holder can now take it.
            with platform_compat.exclusive_file_lock(other, timeout=5):
                pass
        finally:
            os.close(other)

    def test_accepts_a_file_object_not_just_an_int_fd(self):
        """`fcntl.flock` takes an int OR anything with fileno(), and several
        call sites pass an open file. A shim that took only ints would be a
        silent narrowing of the contract it replaced."""
        with open(self.lock_path, "r+", encoding="utf-8") as handle:
            with platform_compat.exclusive_file_lock(handle):
                pass
            self.assertTrue(platform_compat.try_lock_exclusive(handle))
            platform_compat.unlock_file(handle)

    def test_lock_does_not_disturb_the_file_offset(self):
        os.lseek(self.fd, 0, os.SEEK_END)
        before = os.lseek(self.fd, 0, os.SEEK_CUR)
        with platform_compat.exclusive_file_lock(self.fd):
            pass
        self.assertEqual(os.lseek(self.fd, 0, os.SEEK_CUR), before)

    def test_threads_serialise(self):
        data = self.tmp / "threads.dat"
        data.touch()
        threads = [
            threading.Thread(target=_append_under_lock, args=(str(data), m, ITERATIONS))
            for m in ("a", "b")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        for t in threads:
            self.assertFalse(t.is_alive(), "a locking thread did not finish")

        text = data.read_text(encoding="utf-8")
        self.assertEqual(len(text), ITERATIONS * 4)
        self._assert_no_interleaving(text)

    def test_processes_serialise(self):
        data = self.tmp / "procs.dat"
        data.touch()
        ctx = multiprocessing.get_context("spawn")
        procs = [
            ctx.Process(target=_append_under_lock, args=(str(data), m, ITERATIONS))
            for m in ("a", "b")
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=180)
        for p in procs:
            self.assertEqual(p.exitcode, 0, "a locking process failed or hung")

        text = data.read_text(encoding="utf-8")
        self.assertEqual(len(text), ITERATIONS * 4)
        self._assert_no_interleaving(text)

    def _assert_no_interleaving(self, text: str) -> None:
        """Every holder wrote its marker twice under the lock, so the file must
        decompose into same-character pairs. One "ab" pair is one lost mutual
        exclusion."""
        pairs = [text[i : i + 2] for i in range(0, len(text), 2)]
        bad = [(i, p) for i, p in enumerate(pairs) if p[0] != p[1]]
        self.assertEqual(
            bad[:5],
            [],
            f"{len(bad)} of {len(pairs)} writes interleaved — the lock did not "
            f"serialise on {sys.platform}",
        )


class TestNoDirectPlatformImports(unittest.TestCase):
    """The shim is only worth having if it is the single definition."""

    def test_platform_modules_are_imported_behind_a_branch(self):
        source = (Path(platform_compat.__file__)).read_text(encoding="utf-8")
        for module in ("fcntl", "msvcrt"):
            for line in source.splitlines():
                if line.strip() == f"import {module}":
                    self.assertTrue(
                        line.startswith("    "),
                        f"`import {module}` must sit inside a platform branch, "
                        "not at unconditional module scope",
                    )


if __name__ == "__main__":
    unittest.main()

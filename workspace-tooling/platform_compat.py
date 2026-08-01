#!/usr/bin/env python3
"""platform_compat — the one place this repository is allowed to know about POSIX.

M2 / gates-green-t-platform-compat. Two unconditional POSIX dependencies kept
56% of the Python suite from even collecting off macOS:

  * `import fcntl` at module scope in three substrate modules and seven
    data/scripts modules, so those files and everything importing them raised
    ModuleNotFoundError on Windows before a single test ran; and
  * `os.geteuid()` at fifteen call sites, which does not exist on Windows at all.

Both are now expressed here, once, behind an API that has a real implementation
on every supported platform. Callers import this module; nothing else in the
tree imports `fcntl`, `msvcrt` or calls `os.geteuid` directly.

Stdlib only, importable on win32/linux/darwin, and safe to import from both the
`workspace-tooling` and the `data/scripts` trees.

## Locking

`exclusive_file_lock(fd)` is a context manager with the semantics the existing
call sites already relied on: wait until the lock is held, release on exit
INCLUDING on exception. POSIX uses `fcntl.flock(LOCK_EX)`; Windows uses
`msvcrt.locking` over a one-byte range at offset 0. Passing a `timeout` bounds
the wait identically on both — see `lock_file_exclusive` for why that
symmetry is enforced rather than assumed.

The lock file is never written to. Windows denies writes inside a locked byte
range, so a holder that writes its own lock file gets EACCES the moment it
loses the race; every call site opens the lock file and locks it, nothing more.
A zero-length lock file is fine — Windows can lock a range beyond EOF.

Both platforms lock per *open file description*, so two handles on the same
path contend whether they are in one process or two. Do not share one fd
between threads and expect them to serialise — open the lock file per holder,
which is what every call site already does.

## Ownership

`current_uid()` returns `os.geteuid()` on POSIX and `NO_POSIX_UID` (-1) on
Windows, where `os.stat().st_uid` is hardcoded to 0 and carries no identity.
-1 is chosen deliberately: it can never equal a real `st_uid`, so an ownership
check that forgets to branch on `supports_posix_privacy()` fails CLOSED (denies)
rather than silently passing. `current_user_identity()` gives a human-readable
principal for the log line that must accompany any such degradation.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from typing import IO, Iterator, Union

#: What `fcntl.flock` accepts, and therefore what this shim must accept.
FileDescriptorLike = Union[int, IO]

__all__ = [
    "IS_WINDOWS",
    "NO_POSIX_UID",
    "LockTimeout",
    "current_uid",
    "current_user_identity",
    "degraded_owner_checks",
    "owner_check_degraded",
    "reset_degraded_owner_checks",
    "create_directory_link",
    "exclusive_file_lock",
    "file_identity",
    "fsync_directory",
    "lock_file_exclusive",
    "peak_rss_bytes",
    "supports_posix_privacy",
    "supports_symlinks",
    "try_lock_exclusive",
    "unlock_file",
]

IS_WINDOWS = sys.platform == "win32"

#: Sentinel `current_uid()` returns where the OS has no POSIX uid. Never equal
#: to a real `st_uid` (which is >= 0), so `st_uid == current_uid()` is False and
#: an unguarded ownership check denies instead of passing.
NO_POSIX_UID = -1

#: Poll interval for a BOUNDED wait. An unbounded POSIX wait blocks in the
#: kernel and never polls; a bounded wait polls on both platforms so the
#: `timeout` argument means the same thing everywhere.
DEFAULT_LOCK_POLL = 0.01

#: A sane bounded wait for callers that would rather fail than hang. Not the
#: default — the call sites this shim replaces all waited indefinitely, and
#: silently converting those to a timeout would change their failure mode.
SUGGESTED_LOCK_TIMEOUT = 30.0

#: Windows locks a byte RANGE, not a file. One byte at offset 0 is the whole
#: protocol: every holder locks the same range, so every holder contends.
_LOCK_RANGE_BYTES = 1

# Platform modules are imported behind a branch, never at unconditional module
# scope — importing this file must not raise on either platform.
if IS_WINDOWS:  # pragma: no cover - platform-selected
    import msvcrt
else:  # pragma: no cover - platform-selected
    import fcntl


class LockTimeout(TimeoutError):
    """Raised when a Windows lock stays contended past its timeout."""


# ---------------------------------------------------------------------------
# Ownership / privacy


def supports_posix_privacy() -> bool:
    """True where mode bits and `st_uid` are meaningful enforcement.

    On Windows both exist in `os.stat()` output and mean nothing: `st_uid` is 0
    for every file and the mode bits do not reflect the ACL that actually
    governs access. A caller that wants to *enforce* privacy must branch on
    this and either apply a Windows-appropriate check or log that the check is
    degraded — the one thing it must not do is treat a meaningless 0 as a pass.
    """
    return not IS_WINDOWS


def current_uid() -> int:
    """The effective uid on POSIX; `NO_POSIX_UID` where there is no such thing."""
    if IS_WINDOWS:
        return NO_POSIX_UID
    return os.geteuid()


def current_user_identity() -> str:
    """A stable, human-readable principal for diagnostics and degradation logs.

    Not an authorization input — `supports_posix_privacy()` and `current_uid()`
    are. This exists so a degraded-check warning can name *who* it declined to
    verify.
    """
    if IS_WINDOWS:
        for var in ("USERNAME", "USER"):
            value = os.environ.get(var)
            if value:
                domain = os.environ.get("USERDOMAIN")
                return f"{domain}\\{value}" if domain else value
        return "unknown-windows-principal"
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError):
        return f"uid:{os.geteuid()}"


#: Ownership checks that could not be enforced on this host, keyed by check
#: name. Read by tests (and by anything that wants to report posture) so a
#: degradation is an ASSERTABLE fact, not a log line nobody greps for.
_degraded_owner_checks: dict[str, str] = {}


def owner_check_degraded(check: str, detail: str = "") -> None:
    """Record that an ownership check cannot be enforced on this platform.

    Called from the `else` branch of `supports_posix_privacy()`. Emits ONE
    structured warning per check name — the checks run in hot loops, and a
    warning per file would be noise nobody reads, which is the same outcome as
    silence.

    The rule these sites follow: on a platform where `st_uid` carries no
    identity, an ownership check must not silently PASS. It either applies a
    platform-appropriate check or comes through here, where the degradation is
    named, counted and testable.
    """
    if check in _degraded_owner_checks:
        return
    _degraded_owner_checks[check] = detail
    sys.stderr.write(
        f"[platform_compat] DEGRADED ownership check {check!r} on {sys.platform}: "
        f"st_uid carries no identity here, so owner verification is not enforced"
        + (f" ({detail})" if detail else "")
        + "\n"
    )


def degraded_owner_checks() -> dict[str, str]:
    """The ownership checks degraded so far in this process."""
    return dict(_degraded_owner_checks)


def reset_degraded_owner_checks() -> None:
    """Clear the record (tests only)."""
    _degraded_owner_checks.clear()


# ---------------------------------------------------------------------------
# Locking


def _as_fd(fd: FileDescriptorLike) -> int:
    """Accept what `fcntl.flock` accepts: an int, or anything with `fileno()`.

    Several call sites pass an open file object rather than a raw descriptor.
    `fcntl.flock` takes either, so a shim that took only ints would have been a
    silent narrowing of the contract it replaced — and it was: the first pass
    raised `TypeError: '_io.TextIOWrapper' object cannot be interpreted as an
    integer` from six self-tests.
    """
    return fd if isinstance(fd, int) else fd.fileno()


def try_lock_exclusive(fd: FileDescriptorLike) -> bool:
    """One non-blocking attempt. True if the lock is now held, False if contended.

    This is the `flock(LOCK_EX | LOCK_NB)` form — the single-instance guard the
    resident service uses to refuse a second copy of itself. It returns a bool
    rather than raising, because "someone else holds it" is the expected answer
    there, not an error.
    """
    return _try_lock_exclusive(fd)


def _try_lock_exclusive(raw: FileDescriptorLike) -> bool:
    """One non-blocking attempt. True if the lock is now held."""
    fd = _as_fd(raw)
    if not IS_WINDOWS:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    saved = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_RANGE_BYTES)
            return True
        except OSError:
            return False
    finally:
        os.lseek(fd, saved, os.SEEK_SET)


def lock_file_exclusive(
    fd: FileDescriptorLike,
    timeout: float | None = None,
    poll: float = DEFAULT_LOCK_POLL,
) -> None:
    """Take an exclusive lock on `fd`.

    `timeout=None` (the default) waits indefinitely — the semantics every call
    site this replaces already had. A numeric timeout raises `LockTimeout` when
    it expires, and means the SAME thing on both platforms: an earlier version
    of this shim let POSIX block in `flock` and ignore the argument entirely,
    so `timeout=0.5` returned in half a second on Windows and hung forever on
    Linux. A shim whose contract changes per platform is the defect it exists
    to remove, so the bounded wait is polled on both.

    The unbounded POSIX path still blocks in the kernel rather than spinning;
    only a caller that asks for a deadline pays for polling.
    """
    if timeout is None and not IS_WINDOWS:
        fcntl.flock(_as_fd(fd), fcntl.LOCK_EX)
        return

    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if _try_lock_exclusive(fd):
            return
        if deadline is not None and time.monotonic() >= deadline:
            raise LockTimeout(
                f"could not acquire an exclusive lock on fd {fd} within {timeout}s"
            )
        time.sleep(poll)


def unlock_file(raw: FileDescriptorLike) -> None:
    """Release a lock taken by `lock_file_exclusive`."""
    fd = _as_fd(raw)
    if not IS_WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    saved = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_RANGE_BYTES)
    finally:
        os.lseek(fd, saved, os.SEEK_SET)


@contextmanager
def exclusive_file_lock(
    fd: FileDescriptorLike,
    timeout: float | None = None,
    poll: float = DEFAULT_LOCK_POLL,
) -> Iterator[FileDescriptorLike]:
    """Hold an exclusive lock on `fd` for the duration of the block.

    Released on normal exit and on exception — the `try/finally` is the point,
    since every call site this replaces had one and a partial port would have
    quietly dropped it.
    """
    lock_file_exclusive(fd, timeout=timeout, poll=poll)
    try:
        yield fd
    finally:
        unlock_file(fd)


def file_identity(info: "os.stat_result") -> tuple[int, ...]:
    """A stable identity for one file, comparable across `fstat` and `lstat`.

    Used to pin a file open and then prove the path still refers to the same
    bytes -- the "changed while its identity was pinned" guard.

    `st_ctime_ns` is deliberately EXCLUDED on Windows. On POSIX it is the
    inode-change time, which is exactly the signal this guard wants. Windows has
    no such concept: `st_ctime` there is the file CREATION time, and `os.fstat`
    on an open handle and `os.lstat` on the path fill it from different
    information classes, so the two disagree by a millisecond or two for a file
    that was just written. Comparing it did not detect tampering, it just failed
    -- nine substrate errors, all of them the guard firing on itself.

    The remaining four are real identity on both platforms: on Windows `st_ino`
    is the 64-bit file ID and `st_dev` the volume serial number.
    """
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if IS_WINDOWS:
        return identity
    return identity + (info.st_ctime_ns,)


# ---------------------------------------------------------------------------
# Durability


def fsync_directory(path: "os.PathLike[str] | str") -> bool:
    """fsync a DIRECTORY so a temp+rename is durable. True if it actually synced.

    The POSIX idiom is `os.open(dir, O_RDONLY)` then `os.fsync`, and it is what
    makes atomic-rename writes survive a crash. Windows cannot open a directory
    as a file at all — the call raises `PermissionError`, which is how
    milestone-pipeline-checkpoint's self-test died on Windows long after its
    fcntl problem was fixed.

    On Windows this is a documented no-op returning False: `MoveFileEx` already
    orders the rename against the file data, and there is no supported way to
    flush a directory entry from Python. Callers that need to KNOW whether the
    directory was synced get the bool; callers that just want best-effort
    durability can ignore it.
    """
    if IS_WINDOWS:
        return False
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
        return True
    finally:
        os.close(descriptor)


def create_directory_link(target: "os.PathLike[str] | str", link: "os.PathLike[str] | str") -> str:
    """Make `link` resolve to the directory `target`. Returns the mechanism used.

    A directory symlink on POSIX, and on Windows a symlink if the account holds
    the privilege, else an NTFS **junction** — which needs no privilege and is
    transparent to every path operation that just wants to read through it.

    Use this for *fixture and layout* links. It is deliberately NOT a
    substitute for `os.symlink` where the symlink-ness itself is under test:
    `os.path.islink()` is False for a junction, so a check that refuses symlink
    aliases will not see one. Guard those with `supports_symlinks()`.
    """
    if not IS_WINDOWS:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        import _winapi

        _winapi.CreateJunction(os.fspath(target), os.fspath(link))
        return "junction"


_symlink_support: bool | None = None


def supports_symlinks() -> bool:
    """Whether this process may create symlinks.

    Windows needs Developer Mode or SeCreateSymbolicLinkPrivilege; without it
    `os.symlink` raises `OSError: [WinError 1314] A required privilege is not
    held by the client`. That is a property of the ACCOUNT, not the OS version,
    so it is probed once rather than assumed from `sys.platform`.
    """
    global _symlink_support
    if _symlink_support is None:
        import tempfile

        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = os.path.join(tmp, "target")
                os.mkdir(target)
                os.symlink(target, os.path.join(tmp, "link"), target_is_directory=True)
            _symlink_support = True
        except (OSError, NotImplementedError, AttributeError):
            _symlink_support = False
    return _symlink_support


# ---------------------------------------------------------------------------
# Process metrics


def peak_rss_bytes() -> int | None:
    """Peak resident set size of this process, in BYTES, or None if unavailable.

    `resource` is POSIX-only, which is why the resident service could not be
    imported on Windows at all. It is also a unit trap: `ru_maxrss` is
    KILOBYTES on Linux and BYTES on macOS, and the service reported
    `int(ru_maxrss)` as `rss_peak_bytes` on both — so the metric was correct on
    macOS and 1024x too small on Linux. Normalised here, once.

    Windows has no `resource`; `GetProcessMemoryInfo().PeakWorkingSetSize` is
    the direct equivalent and is already in bytes.
    """
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if not ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return None
            return int(counters.PeakWorkingSetSize)
        except Exception:  # noqa: BLE001 - a metric must never take the process down
            return None

    import resource

    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes; macOS reports bytes.
    return int(maxrss) if sys.platform == "darwin" else int(maxrss) * 1024


# ---------------------------------------------------------------------------
# Self-test (the substrate suite covers this far more thoroughly; this is the
# gate-runner entrypoint so a broken shim is caught by `npm run gates` too).


def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(label: str, cond: bool) -> None:
        if not cond:
            failures.append(label)

    ok("supports_posix_privacy matches platform", supports_posix_privacy() is (not IS_WINDOWS))
    ok("current_uid is an int", isinstance(current_uid(), int))
    ok("current_uid is stable", current_uid() == current_uid())
    ok("identity is a non-empty str", isinstance(current_user_identity(), str) and current_user_identity())
    if IS_WINDOWS:
        ok("windows uid is the sentinel", current_uid() == NO_POSIX_UID)
        ok("sentinel can never equal a real st_uid", NO_POSIX_UID < 0)
    else:
        ok("posix uid is geteuid", current_uid() == os.geteuid())

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "lock")
        # Zero-length on purpose: every call site locks a lock file it never
        # writes to, and Windows can lock a range beyond EOF.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with exclusive_file_lock(fd):
                pass
            ok("lock/unlock round-trips", True)

            # Released on exception, not just on normal exit.
            try:
                with exclusive_file_lock(fd):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            with exclusive_file_lock(fd):
                pass
            ok("lock released on exception", True)
        except Exception as exc:  # noqa: BLE001 - the self-test reports, never raises
            failures.append(f"lock round-trip raised {exc!r}")
        finally:
            os.close(fd)

    for f in failures:
        print(f"FAIL: platform_compat self-test: {f}", file=sys.stderr)
    if failures:
        print(f"{len(failures)} self-test failure(s)", file=sys.stderr)
        return 1
    print(f"OK: platform_compat self-test passed (platform={sys.platform})")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv[1:] else _self_test())

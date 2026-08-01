#!/usr/bin/env python3
"""Private-filesystem primitives for local artifact-memory derived state.

Canonical workspace sources are deliberately outside this module's scope.
Derived artifact content is owner-only: directories are 0700, regular files
are 0600, symlinks are rejected, and every object must be owned by the
effective user. Immutable publication uses a same-directory private temporary
file followed by an atomic no-replace hard link.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

import platform_compat


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
PRIVATE_UMASK = 0o077


class PrivateStateError(PermissionError):
    """Derived state is not private, owner-controlled, and symlink-free."""


FaultHook = Callable[[str], None]


def activate_private_umask() -> int:
    """Install the process-wide owner-only umask and return the prior value."""
    return os.umask(PRIVATE_UMASK)


def _mode(info: os.stat_result) -> int:
    return stat.S_IMODE(info.st_mode)


def _owner_and_mode_ok(info: os.stat_result, path: Path) -> bool:
    """Whether `info` shows an owner-private regular file.

    Both halves are POSIX-only facts: `st_uid` is 0 for every file on Windows,
    and the mode bits do not reflect the ACL that actually governs access. So
    on Windows this reports the degradation once and returns True — the caller
    keeps its other checks (regular file, digest, size) and this one is
    explicitly not enforced rather than silently satisfied.
    """
    if not platform_compat.supports_posix_privacy():
        platform_compat.owner_check_degraded(
            "artifact_security._owner_and_mode_ok", str(path)
        )
        return True
    return info.st_uid == platform_compat.current_uid() and _mode(info) == PRIVATE_FILE_MODE


def _owned(info: os.stat_result, path: Path) -> None:
    if not platform_compat.supports_posix_privacy():
        # st_uid is hardcoded to 0 for every file on Windows, so comparing it
        # would either pass everything (if we compared 0 to 0) or fail
        # everything. Neither is an ownership check. The degradation is
        # recorded and warned once rather than silently treated as a pass;
        # a real ACL-backed check is F003, a later milestone.
        platform_compat.owner_check_degraded("artifact_security._owned", str(path))
        return
    if info.st_uid != platform_compat.current_uid():
        raise PrivateStateError(
            f"derived state is not owned by uid {platform_compat.current_uid()}: {path}"
        )


def _reject_user_owned_symlink_ancestors(path: Path) -> None:
    """Reject symlinks in the caller-controlled portion of an absolute path.

    macOS legitimately exposes system roots such as ``/var`` through a
    root-owned symlink. Starting at the target and stopping at the first
    foreign-owned component lets those platform aliases exist while ensuring
    that every path component controlled by the effective user is real.
    """
    current = path.expanduser().absolute()
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                return
            current = parent
            continue
        if stat.S_ISLNK(info.st_mode):
            raise PrivateStateError(
                f"derived-state path contains a user-controlled symlink: {current}"
            )
        if not platform_compat.supports_posix_privacy():
            platform_compat.owner_check_degraded(
                "artifact_security._walk_owned_ancestors", str(current)
            )
            return
        if info.st_uid != platform_compat.current_uid():
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _mode_is_private(info: os.stat_result, path: Path, expected: int, kind: str) -> bool:
    """Whether `info`'s permission bits match `expected`.

    POSIX mode bits are not an access control on Windows. `os.chmod` there only
    toggles the read-only attribute; a directory created with mode 0o700 reports
    0o777 and a file 0o666, regardless of the ACL that actually governs access.
    Comparing them does not enforce privacy — it just fails, which is what 223
    of the substrate's Windows errors were: `private directory mode must be
    0700, found 0777`.

    Same rule as the ownership checks: where the check cannot mean anything,
    record the degradation and do not pretend. A real ACL-backed check is F003.
    """
    if not platform_compat.supports_posix_privacy():
        platform_compat.owner_check_degraded(
            f"artifact_security.private_{kind}_mode", str(path)
        )
        return True
    return _mode(info) == expected


def require_private_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    _reject_user_owned_symlink_ancestors(path)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise PrivateStateError(f"private directory is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PrivateStateError(f"private directory must be a real directory: {path}")
    _owned(info, path)
    if not _mode_is_private(info, path, PRIVATE_DIR_MODE, "directory"):
        raise PrivateStateError(
            f"private directory mode must be 0700, found {_mode(info):04o}: {path}"
        )
    return path


def ensure_private_directory(path: Path) -> Path:
    """Create a private directory tree, then fail closed on an unsafe target."""
    activate_private_umask()
    path = path.expanduser().absolute()
    _reject_user_owned_symlink_ancestors(path)
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    _reject_user_owned_symlink_ancestors(path)
    return require_private_directory(path)


def require_private_file(path: Path) -> Path:
    path = path.expanduser().absolute()
    _reject_user_owned_symlink_ancestors(path)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise PrivateStateError(f"private file is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PrivateStateError(f"private file must be a real regular file: {path}")
    _owned(info, path)
    if not _mode_is_private(info, path, PRIVATE_FILE_MODE, "file"):
        raise PrivateStateError(
            f"private file mode must be 0600, found {_mode(info):04o}: {path}"
        )
    return path


def private_tree_manifest(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Hash one owner-only, symlink-free tree using same-descriptor reads."""
    root = require_private_directory(path)
    files: list[Path] = []
    for directory, names, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        base = Path(directory)
        require_private_directory(base)
        for name in names:
            require_private_directory(base / name)
        for name in filenames:
            candidate = base / name
            require_private_file(candidate)
            files.append(candidate)
    entries: list[dict[str, Any]] = []
    aggregate = hashlib.sha256()
    flags = (
        os.O_RDONLY | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for candidate in sorted(files):
        descriptor = os.open(candidate, flags)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or not _owner_and_mode_ok(before, candidate):
                raise PrivateStateError(
                    f"private manifest entry changed before read: {candidate}"
                )
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
            after = os.fstat(handle.fileno())
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or size != after.st_size
        ):
            raise PrivateStateError(
                f"private manifest entry changed during read: {candidate}"
            )
        require_private_file(candidate)
        current = candidate.lstat()
        if (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PrivateStateError(
                f"private manifest entry changed after read: {candidate}"
            )
        relative = candidate.relative_to(root).as_posix()
        file_digest = digest.hexdigest()
        entries.append(
            {
                "path": relative,
                "bytes": size,
                "sha256": file_digest,
            }
        )
        aggregate.update(
            f"{relative}\0{size}\0{file_digest}\n".encode("utf-8")
        )
    if not entries:
        raise PrivateStateError(f"private manifest tree is empty: {root}")
    return aggregate.hexdigest(), entries


def secure_created_file(path: Path) -> Path:
    """Set and verify 0600 on a file this process has just created."""
    path = path.expanduser().absolute()
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PrivateStateError(f"created file must be regular: {path}")
    _owned(info, path)
    path.chmod(PRIVATE_FILE_MODE)
    return require_private_file(path)


def fsync_directory(path: Path) -> None:
    """fsync a directory so a temp+rename is durable, where the OS allows it.

    Windows cannot open a directory as a file: `os.open` on one raises
    PermissionError, so this was an error rather than a durability measure
    there. platform_compat.fsync_directory is the single implementation and
    reports whether the sync actually happened.
    """
    directory = require_private_directory(path)
    platform_compat.fsync_directory(directory)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    replace: bool = False,
    fault: FaultHook | None = None,
) -> str:
    """Publish complete private bytes atomically.

    With ``replace=False`` the destination is immutable and publication never
    overwrites it. With ``replace=True`` the destination is a mutable status
    projection and ``os.replace`` is used after the temporary file is durable.
    """
    activate_private_umask()
    path = path.expanduser().absolute()
    directory = ensure_private_directory(path.parent)
    temporary = directory / f".tmp-{path.name}-{os.getpid()}-{uuid.uuid4().hex}"
    flags = (
        os.O_WRONLY | getattr(os, "O_BINARY", 0)
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, PRIVATE_FILE_MODE)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        require_private_file(temporary)
        if fault is not None:
            fault("after_file_fsync")
        if replace:
            if path.exists() and path.is_symlink():
                raise PrivateStateError(
                    f"refusing to replace a symlinked status file: {path}"
                )
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
        published = True
        require_private_file(path)
        if fault is not None:
            fault("after_publish")
        fsync_directory(directory)
        return "replaced" if replace else "created"
    finally:
        # After no-replace publication, path and temporary are two names for
        # the same complete inode. Removing the hidden name cannot damage the
        # published object.
        temporary.unlink(missing_ok=True)
        if published:
            fsync_directory(directory)


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    replace: bool = False,
    fault: FaultHook | None = None,
) -> str:
    data = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return atomic_write_bytes(path, data, replace=replace, fault=fault)


#: Trees under the derived-state root that a third-party package manager owns
#: and re-creates with its own modes (pip for ``venv``, huggingface_hub for the
#: model cache). Tightening them is both futile — the next install re-loosens
#: them — and harmful: 0600 on ``venv/bin/python`` strips the execute bit and
#: breaks the interpreter. They are excluded by name and the exclusion is
#: reported, never silent.
SWEEP_EXCLUDED_NAMES = ("venv", "model-cache")


def _sweep_finding(path: Path, root: Path, kind: str, mode: int, reason: str) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix() if path != root else ".",
        "type": kind,
        "mode": f"{mode:04o}",
        "reason": reason,
    }


def sweep_private_tree(
    root: Path,
    *,
    exclude: Sequence[str] = SWEEP_EXCLUDED_NAMES,
) -> dict[str, Any]:
    """Audit one derived-state tree for group/other-accessible objects.

    The policy this enforces is *no group or other bits, anywhere, and no
    symlinks*. That is deliberately weaker than "every directory is exactly
    0700 and every file is exactly 0600": a sealed corpus is legitimately
    published 0400, and flagging stricter-than-policy modes would invite a
    "repair" that loosens a seal. Modes that differ from the 0700/0600 norm
    without being group/other-accessible are reported as ``tighter`` for
    visibility and do not fail the sweep.

    The sweep reads no file content — every judgement comes from ``lstat`` —
    so it can audit a sealed holdout corpus without touching its custody.
    """
    root = root.expanduser().absolute()
    require_private_directory(root)
    excluded_names = set(exclude)
    violations: list[dict[str, Any]] = []
    tighter: list[dict[str, Any]] = []
    excluded: list[str] = []
    counts = {"directories": 0, "files": 0, "sockets": 0}

    def inspect(path: Path, info: os.stat_result) -> None:
        mode = _mode(info)
        if stat.S_ISLNK(info.st_mode):
            violations.append(_sweep_finding(path, root, "symlink", mode, "symlink in derived state"))
            return
        if stat.S_ISDIR(info.st_mode):
            kind, expected, counter = "directory", PRIVATE_DIR_MODE, "directories"
        elif stat.S_ISREG(info.st_mode):
            kind, expected, counter = "file", PRIVATE_FILE_MODE, "files"
        elif stat.S_ISSOCK(info.st_mode):
            kind, expected, counter = "socket", PRIVATE_FILE_MODE, "sockets"
        else:
            violations.append(
                _sweep_finding(path, root, "other", mode, "unexpected object type")
            )
            return
        counts[counter] += 1
        if not platform_compat.supports_posix_privacy():
            platform_compat.owner_check_degraded("artifact_security._sweep", str(path))
        elif info.st_uid != platform_compat.current_uid():
            violations.append(
                _sweep_finding(
                    path, root, kind, mode,
                    f"not owned by uid {platform_compat.current_uid()}",
                )
            )
            return
        if mode & 0o077:
            violations.append(
                _sweep_finding(path, root, kind, mode, "group or other accessible")
            )
        elif mode != expected:
            tighter.append(
                _sweep_finding(path, root, kind, mode, f"tighter than {expected:04o}")
            )

    inspect(root, root.lstat())
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        retained: list[str] = []
        for name in sorted(names):
            child = base / name
            if base == root and name in excluded_names:
                excluded.append(name)
                continue
            retained.append(name)
            inspect(child, child.lstat())
        names[:] = retained
        for name in sorted(filenames):
            child = base / name
            inspect(child, child.lstat())

    return {
        "root": str(root),
        "policy": "no group or other bits; no symlinks",
        "conforming": not violations,
        "counts": counts,
        "violations": violations,
        "tighter_than_policy": tighter,
        "excluded_trees": sorted(excluded),
        "excluded_reason": "third-party package manager owns these modes",
        "content_read": "none",
    }


def repair_private_tree(root: Path) -> dict[str, int]:
    """Explicitly tighten one sensitive derived-data tree without deleting it."""
    root = root.expanduser().absolute()
    if not root.exists():
        return {"directories": 0, "files": 0}
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise PrivateStateError(f"repair root must be a real directory: {root}")
    _owned(root_info, root)
    directories: list[Path] = [root]
    files: list[Path] = []
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        for name in names:
            child = base / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise PrivateStateError(
                    f"repair refuses linked or non-directory entry: {child}"
                )
            _owned(info, child)
            directories.append(child)
        for name in filenames:
            child = base / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise PrivateStateError(
                    f"repair refuses linked or non-regular entry: {child}"
                )
            _owned(info, child)
            files.append(child)
    for path in files:
        path.chmod(PRIVATE_FILE_MODE)
    for path in reversed(directories):
        path.chmod(PRIVATE_DIR_MODE)
    for path in files:
        require_private_file(path)
    for path in directories:
        require_private_directory(path)
    return {"directories": len(directories), "files": len(files)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repair",
        action="store_true",
        help="explicitly chmod the selected sensitive trees before auditing",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help=(
            "recursively audit each tree for group/other-accessible objects and "
            "symlinks; reads no file content and changes nothing"
        ),
    )
    parser.add_argument("paths", nargs="+", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.repair and args.sweep:
        print(json.dumps({"status": "error", "error": "--repair and --sweep are exclusive"}))
        return 2
    results: dict[str, Any] = {}
    conforming = True
    try:
        for path in args.paths:
            resolved = path.expanduser().absolute()
            if args.repair:
                results[str(resolved)] = repair_private_tree(resolved)
            elif args.sweep:
                report = sweep_private_tree(resolved)
                conforming = conforming and report["conforming"]
                results[str(resolved)] = report
            else:
                require_private_directory(resolved)
                results[str(resolved)] = {"status": "private"}
    except (OSError, PrivateStateError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2
    print(json.dumps({"status": "ok", "paths": results}, indent=2, sort_keys=True))
    return 0 if conforming else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create one vault symlink without deleting, replacing, or following parents.

This is the narrow mutation boundary used by ``build-agent-vault.sh``.  The
source must still be a regular file inside the workspace.  The vault must be an
existing real directory.  Destination parents are opened with ``O_NOFOLLOW``;
an existing leaf is always preserved.
"""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path
from typing import Sequence


class UnsafeLinkError(ValueError):
    """A requested link cannot be created within the no-delete contract."""


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _regular_source(target: Path, workspace: Path) -> Path:
    target = target.expanduser().absolute()
    workspace = workspace.expanduser().resolve(strict=True)
    if not _inside(target, workspace):
        raise UnsafeLinkError(f"source is outside workspace: {target}")
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeLinkError(f"source is not a regular non-symlink file: {target}")
    if not _inside(target.resolve(strict=True), workspace):
        raise UnsafeLinkError(f"resolved source escapes workspace: {target}")
    return target


def create_missing_link(
    workspace: Path,
    vault: Path,
    target: Path,
    destination: Path,
) -> str:
    """Return ``created``, ``preserved``, or ``collision``."""
    workspace = workspace.expanduser().resolve(strict=True)
    vault = vault.expanduser().absolute()
    destination = destination.expanduser().absolute()
    target = _regular_source(target, workspace)

    vault_info = vault.lstat()
    if not stat.S_ISDIR(vault_info.st_mode):
        raise UnsafeLinkError(f"vault must be an existing real directory: {vault}")
    # Normalize symlinked system path prefixes such as macOS /var ->
    # /private/var without allowing the vault leaf itself to be a symlink.
    vault = vault.resolve(strict=True)
    if not _inside(destination, vault) or destination == vault:
        raise UnsafeLinkError(f"destination is outside vault: {destination}")

    relative = destination.relative_to(vault)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise UnsafeLinkError(f"unsafe destination: {destination}")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open(vault, directory_flags)
        descriptors.append(current)
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, mode=0o755, dir_fd=current)
            except FileExistsError:
                pass
            try:
                next_descriptor = os.open(part, directory_flags, dir_fd=current)
            except OSError as exc:
                raise UnsafeLinkError(
                    f"destination parent is not a real directory: "
                    f"{destination.parent}"
                ) from exc
            descriptors.append(next_descriptor)
            current = next_descriptor

        leaf = relative.parts[-1]
        try:
            existing = os.stat(leaf, dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISLNK(existing.st_mode):
                return "collision"
            raw = os.readlink(leaf, dir_fd=current)
            existing_target = (destination.parent / raw).resolve(strict=False)
            return "preserved" if existing_target == target.resolve(strict=True) else "collision"

        # Revalidate immediately before creation so a vanished source is not
        # knowingly turned into a broken alias.
        _regular_source(target, workspace)
        stored_target = os.path.relpath(target, destination.parent)
        os.symlink(stored_target, leaf, dir_fd=current)
        return "created"
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--destination", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(
        create_missing_link(
            Path(args.workspace),
            Path(args.vault),
            Path(args.target),
            Path(args.destination),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnsafeLinkError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

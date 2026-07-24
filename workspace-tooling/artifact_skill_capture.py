#!/usr/bin/env python3
"""Record finalized skill artifacts without mutating sources or ingestion sinks.

The receipt is an immutable intent record for a later incremental ingester.  A
capture never writes to Qdrant or Graphiti, and Graphiti bulk ingestion remains
explicitly disabled in every receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import artifact_catalog as catalog
import artifact_security as security


SCHEMA_VERSION = 1
DEFAULT_RECEIPT_ROOT = Path("~/.local/share/personal-artifacts/skill-events").expanduser()
GRAPHITI_CANDIDATE_TYPES = {"decision", "handoff", "plan", "roadmap"}
PRODUCER_TYPES = {
    "handoff": {"handoff"},
    "roadmap": {"roadmap", "milestone_evidence"},
    "spike": {"research", "milestone_evidence"},
    "milestone-pipeline": {"milestone_evidence"},
}
RUN_ID_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
SAFETY = {
    "source_mutation": "none",
    "sink_writes": "none",
    "qdrant_write": "disabled",
    "graphiti_write": "disabled",
    "graphiti_bulk": "disabled",
    "receipt_mode": "append-only",
}


class CaptureError(ValueError):
    """The requested skill-artifact capture is unsafe or invalid."""


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _absolute_lexical(path: Path, base: Path | None = None) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = (base or Path.cwd()) / expanded
    return Path(os.path.abspath(os.fspath(expanded)))


def validate_workspace(raw: Path) -> Path:
    lexical = _absolute_lexical(raw)
    if lexical.is_symlink():
        raise CaptureError(f"workspace must not be a symlink: {lexical}")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise CaptureError(f"workspace is not accessible: {lexical}: {exc}") from exc
    if not resolved.is_dir():
        raise CaptureError(f"workspace is not a directory: {resolved}")
    # Preserve the caller's lexical root. On macOS, /var resolves through
    # /private/var; paths created by tempfile still use the lexical /var form.
    return lexical


def validate_receipt_root(raw: Path, workspace: Path) -> Path:
    lexical = _absolute_lexical(raw)
    resolved = lexical.resolve(strict=False)
    if _is_within(lexical, workspace) or _is_within(resolved, workspace.resolve()):
        raise CaptureError("receipt root must stay outside the source workspace")
    if lexical.exists() and lexical.is_symlink():
        raise CaptureError(f"receipt root must not be a symlink: {lexical}")
    return resolved


def _reject_symlink_components(lexical: Path, workspace: Path) -> None:
    relative = lexical.relative_to(workspace)
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CaptureError(f"artifact path contains a symlink: {current}")


def validate_input(raw: Path, workspace: Path, expected: str) -> Path:
    lexical = _absolute_lexical(raw, workspace)
    if not _is_within(lexical, workspace):
        raise CaptureError(f"artifact {expected} must be lexically inside the workspace: {raw}")
    _reject_symlink_components(lexical, workspace)
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise CaptureError(f"artifact {expected} is not accessible: {lexical}: {exc}") from exc
    if not _is_within(resolved, workspace.resolve()):
        raise CaptureError(f"artifact {expected} resolves outside the workspace: {raw}")
    if expected == "file" and not resolved.is_file():
        raise CaptureError(f"artifact path is not a file: {resolved}")
    if expected == "root" and not resolved.is_dir():
        raise CaptureError(f"artifact root is not a directory: {resolved}")
    return lexical


def _policy_relative(path: Path, workspace: Path) -> str:
    return path.relative_to(workspace).as_posix()


def _validate_candidate(path: Path, workspace: Path, policy: dict[str, Any]) -> str:
    relative = _policy_relative(path, workspace)
    if not catalog._candidate_file(path):
        raise CaptureError(f"artifact is not a supported document: {relative}")
    if catalog._is_excluded(relative, policy):
        raise CaptureError(f"artifact is excluded by catalog policy: {relative}")
    if not catalog._is_included(relative, policy):
        raise CaptureError(f"artifact is outside the default-deny catalog scope: {relative}")
    return relative


def _walk_root(root: Path, workspace: Path, policy: dict[str, Any]) -> Iterable[Path]:
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        retained: list[str] = []
        for name in sorted(directory_names):
            child = directory_path / name
            relative = _policy_relative(child, workspace)
            if child.is_symlink() or catalog._should_prune_dir(relative, name, policy):
                continue
            retained.append(name)
        directory_names[:] = retained
        for name in sorted(file_names):
            path = directory_path / name
            if path.is_symlink() or not catalog._candidate_file(path):
                continue
            relative = _policy_relative(path, workspace)
            if catalog._is_excluded(relative, policy) or not catalog._is_included(relative, policy):
                continue
            yield path


def collect_paths(
    workspace: Path,
    policy: dict[str, Any],
    files: Sequence[Path],
    roots: Sequence[Path],
) -> list[Path]:
    collected: dict[str, Path] = {}
    for raw in files:
        path = validate_input(raw, workspace, "file")
        relative = _validate_candidate(path, workspace, policy)
        collected[relative] = path
    for raw in roots:
        root = validate_input(raw, workspace, "root")
        for path in _walk_root(root, workspace, policy):
            collected[_policy_relative(path, workspace)] = path
    if not collected:
        raise CaptureError("capture requires at least one policy-eligible document")
    return [collected[key] for key in sorted(collected)]


def build_artifacts(
    paths: Sequence[Path],
    workspace: Path,
    policy: dict[str, Any],
    producer: str,
) -> list[dict[str, Any]]:
    allowed_types = PRODUCER_TYPES[producer]
    repository_cache: dict[Path, Path | None] = {}
    artifacts: list[dict[str, Any]] = []
    for path in paths:
        relative = _validate_candidate(path, workspace, policy)
        content_sha256, text, stat_result = catalog._read_file_snapshot(path)
        artifact_type, authority, hints = catalog.classify_artifact(relative, text)
        if artifact_type not in allowed_types:
            expected = ", ".join(sorted(allowed_types))
            raise CaptureError(
                f"{producer} cannot capture {artifact_type} artifact {relative}; "
                f"expected one of: {expected}"
            )
        source_scope, repository, project = catalog.source_metadata(
            relative, path, workspace, repository_cache
        )
        artifact_id, revision_id = catalog.make_ids(relative, content_sha256)
        graphiti_route = (
            "candidate" if artifact_type in GRAPHITI_CANDIDATE_TYPES else "ineligible"
        )
        artifacts.append(
            {
                "relative_path": relative,
                "content_sha256": content_sha256,
                "byte_size": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
                "artifact_type": artifact_type,
                "authority_class": authority,
                "lifecycle_hints": list(hints),
                "source_scope": source_scope,
                "repository": repository,
                "project": project,
                "artifact_id": artifact_id,
                "revision_id": revision_id,
                "routing": {
                    "qdrant": "eligible",
                    "graphiti": graphiti_route,
                    "graphiti_bulk": "disabled",
                },
            }
        )
    return artifacts


def validate_run_id(run_id: str) -> str:
    if run_id != run_id.strip() or not RUN_ID_PATTERN.fullmatch(run_id):
        raise CaptureError(
            "run id must be 1-200 printable characters with no surrounding whitespace"
        )
    return run_id


def make_event_id(producer: str, run_id: str, artifacts: Sequence[dict[str, Any]]) -> str:
    identity = {
        "schema_version": SCHEMA_VERSION,
        "producer": producer,
        "run_id": run_id,
        "artifacts": [
            {
                "relative_path": artifact["relative_path"],
                "revision_id": artifact["revision_id"],
            }
            for artifact in artifacts
        ],
    }
    return hashlib.sha256(_json_bytes(identity)).hexdigest()


def make_receipt(
    producer: str,
    run_id: str,
    event_hex: str,
    artifacts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"event:{event_hex}",
        "producer": producer,
        "run_id": run_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": list(artifacts),
        "routing_summary": {
            "qdrant": "eligible-for-incremental-ingestion",
            "graphiti": "candidate-filter-only",
            "graphiti_bulk": "disabled",
        },
        "safety": dict(SAFETY),
    }


def _validate_existing(
    path: Path,
    producer: str,
    run_id: str,
    event_hex: str,
    artifacts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError(f"existing receipt is unreadable; refusing overwrite: {path}") from exc
    expected = {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"event:{event_hex}",
        "producer": producer,
        "run_id": run_id,
        "artifacts": list(artifacts),
        "routing_summary": {
            "qdrant": "eligible-for-incremental-ingestion",
            "graphiti": "candidate-filter-only",
            "graphiti_bulk": "disabled",
        },
        "safety": dict(SAFETY),
    }
    comparable = {key: existing.get(key) for key in expected}
    if comparable != expected or not isinstance(existing.get("captured_at"), str):
        raise CaptureError(f"existing receipt identity mismatch; refusing overwrite: {path}")
    return existing


def write_receipt(
    receipt_root: Path,
    producer: str,
    run_id: str,
    event_hex: str,
    artifacts: Sequence[dict[str, Any]],
    *,
    fault: security.FaultHook | None = None,
) -> tuple[str, Path, dict[str, Any]]:
    try:
        security.ensure_private_directory(receipt_root)
        directory = receipt_root / event_hex[:2]
        security.ensure_private_directory(directory)
    except security.PrivateStateError as exc:
        raise CaptureError(str(exc)) from exc
    path = directory / f"{event_hex}.json"
    if path.exists():
        security.require_private_file(path)
        return "idempotent", path, _validate_existing(
            path, producer, run_id, event_hex, artifacts
        )

    receipt = make_receipt(producer, run_id, event_hex, artifacts)
    try:
        security.atomic_write_json(path, receipt, fault=fault)
    except FileExistsError:
        security.require_private_file(path)
        return "idempotent", path, _validate_existing(
            path, producer, run_id, event_hex, artifacts
        )
    return "created", path, receipt


def emit(args: argparse.Namespace) -> dict[str, Any]:
    workspace = validate_workspace(args.workspace)
    receipt_root = validate_receipt_root(args.receipt_root, workspace)
    if receipt_root.exists():
        try:
            security.require_private_directory(receipt_root)
        except security.PrivateStateError as exc:
            raise CaptureError(str(exc)) from exc
    run_id = validate_run_id(args.run_id)
    policy_path = _absolute_lexical(args.policy, workspace)
    if not _is_within(policy_path, workspace):
        raise CaptureError("artifact policy must stay inside the source workspace")
    _reject_symlink_components(policy_path, workspace)
    policy = catalog.load_policy(policy_path)
    paths = collect_paths(workspace, policy, args.paths, args.roots)
    artifacts = build_artifacts(paths, workspace, policy, args.producer)
    event_hex = make_event_id(args.producer, run_id, artifacts)
    receipt_path = receipt_root / event_hex[:2] / f"{event_hex}.json"
    if not args.apply:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "plan",
            "status": "planned",
            "event_id": f"event:{event_hex}",
            "producer": args.producer,
            "run_id": run_id,
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
            "would_write": str(receipt_path),
            "safety": dict(SAFETY),
        }
    status, written_path, receipt = write_receipt(
        receipt_root, args.producer, run_id, event_hex, artifacts
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply",
        "status": status,
        "event_id": receipt["event_id"],
        "artifact_count": len(artifacts),
        "receipt_path": str(written_path),
        "safety": receipt["safety"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create immutable skill-artifact receipts for later ingestion"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    emit_parser = subparsers.add_parser("emit", help="plan or record a finalized artifact set")
    emit_parser.add_argument("--workspace", type=Path, required=True)
    emit_parser.add_argument(
        "--policy", type=Path, default=Path("scripts/artifact-policy.json")
    )
    emit_parser.add_argument("--producer", choices=sorted(PRODUCER_TYPES), required=True)
    emit_parser.add_argument("--run-id", required=True)
    emit_parser.add_argument("--path", dest="paths", type=Path, action="append", default=[])
    emit_parser.add_argument("--root", dest="roots", type=Path, action="append", default=[])
    emit_parser.add_argument("--receipt-root", type=Path, default=DEFAULT_RECEIPT_ROOT)
    emit_parser.add_argument(
        "--apply",
        action="store_true",
        help="create the append-only receipt; otherwise only print the plan",
    )
    emit_parser.set_defaults(handler=emit)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except (CaptureError, catalog.PolicyError, catalog.ChangedDuringScan, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

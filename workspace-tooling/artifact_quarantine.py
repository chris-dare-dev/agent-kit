#!/usr/bin/env python3
"""Stage, quarantine, and restore terminal agent artifacts without deletion.

Phase 6 is deliberately two-step:

1. ``stage`` creates a verified immutable copy outside the workspace.
2. ``quarantine`` may relocate the original only after a second explicit
   acknowledgement and a fresh reference/tracking preflight.

There is intentionally no cleanup or delete command.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import artifact_catalog as catalog
import artifact_security as security
import artifact_runtime


SCHEMA_VERSION = 1
DEFAULT_WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = artifact_runtime.derived_root() / "artifact-catalog.sqlite3"
DEFAULT_QUARANTINE_ROOT = artifact_runtime.derived_root() / "quarantine"
DEFAULT_RULES: dict[str, Any] = {
    "candidate_path_globs": ["plans/**", "GitLab/**/plans/**"],
    "eligible_artifact_types": ["decision", "handoff", "plan", "research", "roadmap"],
    "terminal_states": ["archived", "cancelled", "obsolete", "superseded"],
    "review_only_states": ["closed"],
    "minimum_age_days": 14,
}
ACTIVE_STATES = {
    "active",
    "current",
    "in-progress",
    "in_progress",
    "open",
    "pending",
    "requested",
}
STATUS_KEYS = {"lifecycle", "review-status", "review_status", "status"}
PATH_STATE_RE = re.compile(
    r"(?:^|[-_.+ ])(archived|cancelled|obsolete|superseded)(?:$|[-_.+ ])",
    re.IGNORECASE,
)
FIELD_RE = re.compile(
    r"^\s*(status|review[_-]status|lifecycle)\s*:\s*"
    r"[\"']?([a-z][a-z0-9_-]*)[\"']?\s*$",
    re.IGNORECASE,
)
BANNER_STATE_RE = re.compile(
    r"^(?:(?:status|lifecycle|review status)\s*[:—-]\s*)?"
    r"(archived|cancelled|obsolete|superseded)\b",
    re.IGNORECASE,
)
BANNER_SENTENCE_RE = re.compile(
    r"^this\s+(?:artifact|decision|document|file|handoff|plan|roadmap)\s+"
    r"is\s+(?:now\s+)?(archived|cancelled|obsolete|superseded)\b",
    re.IGNORECASE,
)
CANONICAL_BANNER_RE = re.compile(
    r"^(?:this\s+(?:artifact|document|file|plan|roadmap)\s+is\s+)?"
    r"(?:the\s+)?(?:active\s+)?canonical\b",
    re.IGNORECASE,
)


class QuarantineError(ValueError):
    """A quarantine operation is unsafe or its immutable state is invalid."""


@dataclass(frozen=True)
class CatalogSnapshot:
    run_id: int
    finished_at: str
    workspace_path: str
    rows: tuple[dict[str, Any], ...]


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
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


def _validate_workspace(raw: Path) -> Path:
    lexical = _absolute_lexical(raw)
    if lexical.is_symlink():
        raise QuarantineError(f"workspace must not be a symlink: {lexical}")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise QuarantineError(f"workspace is not accessible: {lexical}: {exc}") from exc
    if not resolved.is_dir():
        raise QuarantineError(f"workspace is not a directory: {resolved}")
    return lexical


def _validate_external_root(raw: Path, workspace: Path) -> Path:
    lexical = _absolute_lexical(raw)
    resolved = lexical.resolve(strict=False)
    if _is_within(lexical, workspace) or _is_within(resolved, workspace.resolve()):
        raise QuarantineError("quarantine root must stay outside the source workspace")
    if lexical.exists() and lexical.is_symlink():
        raise QuarantineError(f"quarantine root must not be a symlink: {lexical}")
    if lexical.exists():
        try:
            security.require_private_directory(lexical)
        except security.PrivateStateError as exc:
            raise QuarantineError(str(exc)) from exc
    return resolved


def _validate_relative(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise QuarantineError(f"unsafe workspace-relative artifact path: {value!r}")
    return path.as_posix()


def _reject_symlink_components(path: Path, root: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise QuarantineError(f"{label} escapes its root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise QuarantineError(f"{label} contains a symlink component: {current}")


def _safe_source_path(workspace: Path, relative: str, *, must_exist: bool) -> Path:
    relative = _validate_relative(relative)
    lexical = _absolute_lexical(Path(relative), workspace)
    if not _is_within(lexical, workspace):
        raise QuarantineError(f"artifact escapes workspace lexically: {relative}")
    _reject_symlink_components(lexical, workspace, "artifact path")
    if must_exist:
        try:
            resolved = lexical.resolve(strict=True)
        except OSError as exc:
            raise QuarantineError(f"artifact is not accessible: {relative}: {exc}") from exc
        if not _is_within(resolved, workspace.resolve()):
            raise QuarantineError(f"artifact resolves outside workspace: {relative}")
    return lexical


def _safe_mkdirs(root: Path, relative_parent: Path) -> Path:
    current = root
    if current.exists() and (current.is_symlink() or not current.is_dir()):
        raise QuarantineError(f"unsafe directory root: {current}")
    current.mkdir(mode=0o700, parents=True, exist_ok=True)
    for part in relative_parent.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise QuarantineError(f"unsafe destination parent: {relative_parent}")
        current = current / part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise QuarantineError(f"unsafe destination component: {current}")
        else:
            current.mkdir(mode=0o700)
    return current


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _load_rules(policy_path: Path, minimum_age_days: int | None) -> dict[str, Any]:
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    configured = raw.get("quarantine", {})
    if not isinstance(configured, dict):
        raise QuarantineError("artifact policy quarantine section must be an object")
    rules: dict[str, Any] = {
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_RULES.items()
    }
    rules.update(configured)
    if minimum_age_days is not None:
        rules["minimum_age_days"] = minimum_age_days
    for key in (
        "candidate_path_globs",
        "eligible_artifact_types",
        "terminal_states",
        "review_only_states",
    ):
        values = rules.get(key)
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value for value in values
        ):
            raise QuarantineError(f"quarantine.{key} must be a non-empty string list")
        rules[key] = sorted(set(value.lower() for value in values))
    age = rules.get("minimum_age_days")
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise QuarantineError("quarantine.minimum_age_days must be a non-negative integer")
    return rules


def _load_catalog(database: Path, workspace: Path) -> CatalogSnapshot:
    database = database.expanduser().resolve(strict=True)
    security.require_private_file(database)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != catalog.SCHEMA_VERSION:
            raise QuarantineError(
                f"unsupported artifact catalog schema {version}; "
                f"expected {catalog.SCHEMA_VERSION}"
            )
        run = connection.execute(
            "SELECT run_id, finished_at, workspace_path "
            "FROM scan_runs WHERE finished_at IS NOT NULL AND status='complete' "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise QuarantineError("artifact catalog has no completed scan")
        catalog_workspace = Path(str(run["workspace_path"])).resolve()
        if catalog_workspace != workspace.resolve():
            raise QuarantineError(
                f"catalog workspace mismatch: {catalog_workspace} != {workspace.resolve()}"
            )
        rows = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT revision_id, artifact_id, content_sha256, relative_path, "
                "byte_size, mtime_ns, artifact_type, authority_class, "
                "lifecycle_hints_json, source_scope, repository, project "
                "FROM current_artifact_revisions ORDER BY relative_path"
            )
        )
        return CatalogSnapshot(
            int(run["run_id"]),
            str(run["finished_at"]),
            str(catalog_workspace),
            rows,
        )
    finally:
        connection.close()


def _path_is_scoped(relative: str, rules: dict[str, Any]) -> bool:
    lowered = relative.lower()
    return any(
        fnmatch.fnmatchcase(lowered, pattern.lower())
        for pattern in rules["candidate_path_globs"]
    )


def _frontmatter_fields(lines: list[str]) -> dict[str, str]:
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:101]:
        if line.strip() == "---":
            break
        match = FIELD_RE.match(line)
        if match:
            fields[match.group(1).lower()] = match.group(2).lower()
    return fields


def _strip_banner_markup(line: str) -> str:
    value = line.strip()
    value = re.sub(r"^[>#*\s_-]+", "", value)
    value = re.sub(r"^\[[^\]]+\]\s*", "", value)
    return value.strip("*_` []")


def _terminal_evidence(relative: str, text: str) -> tuple[str | None, list[str], dict[str, str]]:
    lines = text.splitlines()
    fields = _frontmatter_fields(lines)
    evidence: list[str] = []
    states: list[str] = []

    for part in Path(relative).parts:
        match = PATH_STATE_RE.search(part)
        if match:
            states.append(match.group(1).lower())
            evidence.append(f"path-marker:{match.group(1).lower()}")

    for key, value in fields.items():
        if value in DEFAULT_RULES["terminal_states"]:
            states.append(value)
            evidence.append(f"frontmatter:{key}={value}")

    body_start = 0
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:101], start=1):
            if line.strip() == "---":
                body_start = index + 1
                break
    for line in lines[body_start : body_start + 40]:
        banner = _strip_banner_markup(line)
        match = BANNER_STATE_RE.match(banner) or BANNER_SENTENCE_RE.match(banner)
        if match:
            state = match.group(1).lower()
            states.append(state)
            evidence.append(f"opening-banner:{state}")

    unique_states = sorted(set(states))
    if len(unique_states) > 1:
        evidence.append("conflicting-terminal-states:" + ",".join(unique_states))
        return None, sorted(set(evidence)), fields
    return (unique_states[0] if unique_states else None), sorted(set(evidence)), fields


def _active_blockers(text: str, fields: dict[str, str], lifecycle_hints: list[str]) -> list[str]:
    blockers: list[str] = []
    for key, value in fields.items():
        if value in ACTIVE_STATES:
            blockers.append(f"active-frontmatter:{key}={value}")
    if "review_open" in lifecycle_hints:
        blockers.append("catalog-review-open")
    lines = text.splitlines()
    body_start = 0
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:101], start=1):
            if line.strip() == "---":
                body_start = index + 1
                break
    for line in lines[body_start : body_start + 20]:
        if CANONICAL_BANNER_RE.match(_strip_banner_markup(line)):
            blockers.append("opening-banner:canonical")
            break
    return sorted(set(blockers))


def _find_git_root(path: Path, workspace: Path) -> Path | None:
    current = path.parent
    workspace_parent = workspace.parent
    while current != workspace_parent:
        if (current / ".git").exists():
            return current
        if current == workspace:
            break
        current = current.parent
    return None


def _git_tracking(path: Path, workspace: Path) -> tuple[str, str | None]:
    root = _find_git_root(path, workspace)
    if root is None:
        return "no-repository", None
    relative = path.relative_to(root).as_posix()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "error", str(exc)
    return ("tracked" if result.returncode == 0 else "untracked"), str(root)


def _candidate_record(
    row: dict[str, Any],
    workspace: Path,
    rules: dict[str, Any],
    now_ns: int,
) -> tuple[str, dict[str, Any]]:
    relative = _validate_relative(str(row["relative_path"]))
    base: dict[str, Any] = {
        "artifact_id": str(row["artifact_id"]),
        "revision_id": str(row["revision_id"]),
        "relative_path": relative,
        "content_sha256": str(row["content_sha256"]),
        "byte_size": int(row["byte_size"]),
        "mtime_ns": int(row["mtime_ns"]),
        "artifact_type": str(row["artifact_type"]),
        "authority_class": str(row["authority_class"]),
        "source_scope": str(row["source_scope"]),
        "repository": row["repository"],
        "project": str(row["project"]),
    }
    try:
        lifecycle_hints = json.loads(str(row["lifecycle_hints_json"]))
    except json.JSONDecodeError as exc:
        raise QuarantineError(f"malformed lifecycle hints for {relative}") from exc
    if not isinstance(lifecycle_hints, list) or not all(
        isinstance(value, str) for value in lifecycle_hints
    ):
        raise QuarantineError(f"malformed lifecycle hints for {relative}")
    base["catalog_lifecycle_hints"] = lifecycle_hints

    path = _safe_source_path(workspace, relative, must_exist=True)
    if path.is_symlink() or not path.is_file():
        base["reason_codes"] = ["source-not-regular"]
        return "blocked", base
    try:
        content_sha256, text, info = catalog._read_file_snapshot(path)
    except OSError as exc:
        base["reason_codes"] = [f"source-read-failed:{exc}"]
        return "blocked", base
    if (
        content_sha256 != base["content_sha256"]
        or info.st_size != base["byte_size"]
        or info.st_mtime_ns != base["mtime_ns"]
    ):
        base["reason_codes"] = ["catalog-source-drift"]
        return "blocked", base
    base["source_mode"] = stat.S_IMODE(info.st_mode)

    terminal_state, terminal_evidence, fields = _terminal_evidence(relative, text)
    base["terminal_state"] = terminal_state
    base["terminal_evidence"] = terminal_evidence
    age_ns = now_ns - info.st_mtime_ns
    base["age_days"] = round(age_ns / (86_400 * 1_000_000_000), 3)
    review_reasons: list[str] = []
    if any(value in rules["review_only_states"] for value in fields.values()):
        review_reasons.append("closed-frontmatter-only")
    if any(
        value in set(rules["terminal_states"]) | set(rules["review_only_states"])
        for value in lifecycle_hints
    ):
        review_reasons.append("catalog-lifecycle-hint-only")

    if terminal_evidence and terminal_state is None:
        base["reason_codes"] = ["conflicting-terminal-evidence"]
        return "blocked", base
    if terminal_state is None:
        if review_reasons:
            base["reason_codes"] = sorted(set(review_reasons))
            return "review", base
        base["reason_codes"] = ["no-terminal-evidence"]
        return "skip", base

    reasons = _active_blockers(text, fields, lifecycle_hints)
    if age_ns < 0:
        reasons.append("future-mtime")
    if terminal_state not in rules["terminal_states"]:
        reasons.append(f"terminal-state-not-allowed:{terminal_state}")
    if base["age_days"] < rules["minimum_age_days"]:
        reasons.append("minimum-age-not-met")
    if base["authority_class"] == "canonical":
        reasons.append("canonical-authority")
    tracking, repository_root = _git_tracking(path, workspace)
    base["git_tracking"] = tracking
    base["git_repository_root"] = repository_root
    if tracking == "tracked":
        reasons.append("git-tracked")
    elif tracking == "error":
        reasons.append("git-tracking-unknown")
    if reasons:
        base["reason_codes"] = sorted(set(reasons))
        return "blocked", base

    base["reason_codes"] = ["strong-terminal-evidence"]
    return "eligible", base


def _make_set_hex(
    snapshot: CatalogSnapshot,
    rules: dict[str, Any],
    eligible: Sequence[dict[str, Any]],
    review: Sequence[dict[str, Any]] = (),
    blocked: Sequence[dict[str, Any]] = (),
) -> str:
    def identities(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        # age_days changes as the clock advances and is informational only.
        # Every operational or source-identity field remains authenticated by
        # the set id so a modified manifest fails closed when it is loaded.
        return [
            {key: value for key, value in item.items() if key != "age_days"}
            for item in items
        ]

    identity = {
        "schema_version": SCHEMA_VERSION,
        "catalog_run_id": snapshot.run_id,
        "catalog_finished_at": snapshot.finished_at,
        "workspace": str(Path(snapshot.workspace_path).resolve()),
        "rules": rules,
        "selections": {
            "eligible": identities(eligible),
            "review": identities(review),
            "blocked": identities(blocked),
        },
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def build_plan(
    workspace: Path,
    catalog_database: Path,
    policy_path: Path,
    minimum_age_days: int | None,
    limit: int | None,
) -> dict[str, Any]:
    rules = _load_rules(policy_path, minimum_age_days)
    snapshot = _load_catalog(catalog_database, workspace)
    now = datetime.now(timezone.utc)
    now_ns = int(now.timestamp() * 1_000_000_000)
    eligible: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    skipped = 0
    for row in snapshot.rows:
        relative = str(row["relative_path"])
        if (
            not _path_is_scoped(relative, rules)
            or str(row["artifact_type"]).lower() not in rules["eligible_artifact_types"]
            or "ai-studio" in Path(relative).parts
        ):
            skipped += 1
            continue
        category, record = _candidate_record(row, workspace, rules, now_ns)
        if category == "eligible":
            eligible.append(record)
        elif category == "review":
            review.append(record)
        elif category == "blocked":
            blocked.append(record)
        else:
            skipped += 1
    eligible.sort(key=lambda item: item["relative_path"])
    review.sort(key=lambda item: item["relative_path"])
    blocked.sort(key=lambda item: item["relative_path"])
    if limit is not None:
        if limit < 1:
            raise QuarantineError("--limit must be positive")
        eligible = eligible[:limit]
    set_hex = _make_set_hex(snapshot, rules, eligible, review, blocked)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "plan",
        "planned_at": now.isoformat(),
        "workspace": str(workspace),
        "catalog": {
            "database": str(catalog_database.expanduser().resolve()),
            "run_id": snapshot.run_id,
            "finished_at": snapshot.finished_at,
        },
        "rules": rules,
        "set_id": f"quarantine-set:{set_hex}",
        "counts": {
            "eligible": len(eligible),
            "review": len(review),
            "blocked": len(blocked),
            "skipped": skipped,
            "eligible_bytes": sum(item["byte_size"] for item in eligible),
        },
        "eligible": eligible,
        "review": review,
        "blocked": blocked,
        "safety": {
            "source_mutation": "disabled",
            "source_delete": "disabled",
            "sink_writes": "disabled",
            "cleanup_command": "absent",
        },
    }


def _compact_plan(plan: dict[str, Any], sample_limit: int = 20) -> dict[str, Any]:
    compact = dict(plan)
    compact["review_sample"] = [
        {
            "relative_path": item["relative_path"],
            "reason_codes": item["reason_codes"],
        }
        for item in plan["review"][:sample_limit]
    ]
    compact["blocked_sample"] = [
        {
            "relative_path": item["relative_path"],
            "reason_codes": item["reason_codes"],
        }
        for item in plan["blocked"][:sample_limit]
    ]
    compact.pop("review", None)
    compact.pop("blocked", None)
    return compact


def _set_hex(set_id: str) -> str:
    value = set_id.removeprefix("quarantine-set:")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise QuarantineError(f"invalid quarantine set id: {set_id!r}")
    return value


def _set_dir(root: Path, set_id: str) -> Path:
    return root / "sets" / _set_hex(set_id)


def _copy_exclusive(source: Path, destination: Path, expected: dict[str, Any]) -> None:
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise QuarantineError(f"unsafe existing staged artifact: {destination}")
        digest, _, info = catalog._read_file_snapshot(destination)
        if digest != expected["content_sha256"] or info.st_size != expected["byte_size"]:
            raise QuarantineError(f"staged artifact mismatch; refusing overwrite: {destination}")
        return
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_descriptor = os.open(source, source_flags)
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise QuarantineError(f"source is not a regular file: {source}")
        destination_descriptor = os.open(destination, destination_flags, 0o400)
        digest = hashlib.sha256()
        size = 0
        try:
            while True:
                block = os.read(source_descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                view = memoryview(block)
                while view:
                    written = os.write(destination_descriptor, view)
                    view = view[written:]
            after = os.fstat(source_descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise QuarantineError(f"source changed while staging: {source}")
            if digest.hexdigest() != expected["content_sha256"] or size != expected["byte_size"]:
                raise QuarantineError(f"source no longer matches sealed revision: {source}")
            os.fchmod(destination_descriptor, 0o400)
            os.fsync(destination_descriptor)
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)


def _write_json_exclusive(path: Path, payload: dict[str, Any], mode: int = 0o400) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, mode)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        encoded += b"\n"
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _manifest_core(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "set_id": plan["set_id"],
        "workspace": plan["workspace"],
        "catalog": plan["catalog"],
        "rules": plan["rules"],
        "artifacts": plan["eligible"],
        "review_queue": plan["review"],
        "blocked_queue": plan["blocked"],
        "selection_counts": plan["counts"],
        "safety": {
            "staged_copy_mode": "immutable-copy",
            "source_mutation": "none-during-stage",
            "source_delete": "disabled",
            "quarantine_relocation": "explicit-second-step",
            "cleanup_command": "absent",
        },
    }


def _load_manifest(root: Path, set_id: str) -> tuple[Path, dict[str, Any]]:
    directory = _set_dir(root, set_id)
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise QuarantineError(f"sealed manifest not found: {manifest_path}")
    _reject_symlink_components(manifest_path, root, "quarantine manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuarantineError(f"sealed manifest is unreadable: {manifest_path}") from exc
    if not isinstance(manifest, dict) or manifest.get("set_id") != set_id:
        raise QuarantineError(f"sealed manifest identity mismatch: {manifest_path}")
    try:
        manifest_catalog = manifest["catalog"]
        manifest_rules = manifest["rules"]
        manifest_artifacts = manifest["artifacts"]
        manifest_review = manifest["review_queue"]
        manifest_blocked = manifest["blocked_queue"]
        if (
            not isinstance(manifest_catalog, dict)
            or not isinstance(manifest_rules, dict)
            or not isinstance(manifest_artifacts, list)
            or not isinstance(manifest_review, list)
            or not isinstance(manifest_blocked, list)
            or not all(isinstance(item, dict) for item in manifest_artifacts)
            or not all(isinstance(item, dict) for item in manifest_review)
            or not all(isinstance(item, dict) for item in manifest_blocked)
        ):
            raise TypeError("manifest fields have the wrong type")
        expected_hex = _make_set_hex(
            CatalogSnapshot(
                int(manifest_catalog["run_id"]),
                str(manifest_catalog["finished_at"]),
                str(manifest["workspace"]),
                (),
            ),
            manifest_rules,
            manifest_artifacts,
            manifest_review,
            manifest_blocked,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise QuarantineError(f"sealed manifest shape is invalid: {manifest_path}") from exc
    if set_id != f"quarantine-set:{expected_hex}":
        raise QuarantineError(f"sealed manifest digest mismatch: {manifest_path}")
    return directory, manifest


def stage_plan(plan: dict[str, Any], root: Path, apply: bool) -> dict[str, Any]:
    directory = _set_dir(root, plan["set_id"])
    if not apply:
        result = dict(plan)
        result["mode"] = "stage-plan"
        result["would_write"] = str(directory)
        return result
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise QuarantineError(f"unsafe quarantine root: {root}")
    with _locked(root / ".stage.lock"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise QuarantineError(f"unsafe quarantine set directory: {directory}")
        files_root = directory / "files"
        files_root.mkdir(mode=0o700, exist_ok=True)
        if files_root.is_symlink() or not files_root.is_dir():
            raise QuarantineError(f"unsafe staged-files directory: {files_root}")
        workspace = Path(plan["workspace"])
        for item in plan["eligible"]:
            source = _safe_source_path(workspace, item["relative_path"], must_exist=True)
            destination = files_root / item["relative_path"]
            _safe_mkdirs(files_root, Path(item["relative_path"]).parent)
            _copy_exclusive(source, destination, item)
        manifest_path = directory / "manifest.json"
        expected_core = _manifest_core(plan)
        if manifest_path.exists():
            _, existing = _load_manifest(root, plan["set_id"])
            comparable = {key: existing.get(key) for key in expected_core}
            if comparable != expected_core or not isinstance(existing.get("sealed_at"), str):
                raise QuarantineError(
                    f"existing quarantine manifest mismatch; refusing overwrite: {manifest_path}"
                )
            status = "idempotent"
        else:
            manifest = dict(expected_core)
            manifest["sealed_at"] = datetime.now(timezone.utc).isoformat()
            _write_json_exclusive(manifest_path, manifest)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            status = "created"
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "stage-apply",
        "status": status,
        "set_id": plan["set_id"],
        "artifact_count": len(plan["eligible"]),
        "review_count": len(plan["review"]),
        "blocked_count": len(plan["blocked"]),
        "byte_count": plan["counts"]["eligible_bytes"],
        "manifest": str(directory / "manifest.json"),
        "source_mutation": "none",
        "source_delete": "disabled",
    }


def _verify_file(path: Path, item: dict[str, Any]) -> str:
    if path.is_symlink():
        return "symlink"
    if not path.exists():
        return "missing"
    if not path.is_file():
        return "not-regular"
    try:
        digest, _, info = catalog._read_file_snapshot(path)
    except OSError:
        return "unreadable"
    if digest != item["content_sha256"] or info.st_size != item["byte_size"]:
        return "drift"
    return "match"


def set_status(root: Path, set_id: str) -> dict[str, Any]:
    directory, manifest = _load_manifest(root, set_id)
    workspace = Path(manifest["workspace"])
    records: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    for item in manifest["artifacts"]:
        relative = item["relative_path"]
        staged = _verify_file(directory / "files" / relative, item)
        source = _verify_file(_safe_source_path(workspace, relative, must_exist=False), item)
        relocated = _verify_file(directory / "relocated" / relative, item)
        if staged != "match":
            state = f"staged-{staged}"
        elif source == "match" and relocated == "missing":
            state = "staged"
        elif source == "missing" and relocated == "match":
            state = "quarantined"
        elif source == "match" and relocated == "match":
            state = "source-and-relocated-both-present"
        elif source != "missing":
            state = f"source-{source}"
        else:
            state = f"relocated-{relocated}"
        counts[state] = counts.get(state, 0) + 1
        records.append(
            {
                "relative_path": relative,
                "state": state,
                "staged": staged,
                "source": source,
                "relocated": relocated,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "set_id": set_id,
        "manifest": str(directory / "manifest.json"),
        "counts": dict(sorted(counts.items())),
        "artifacts": records,
        "source_delete": "disabled",
    }


def _find_aliases(workspace: Path, sources: dict[Path, str]) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {relative: [] for relative in sources.values()}
    roots = (workspace / "Vault", workspace / "Notes" / "Projects")
    resolved_sources = {path.resolve(): relative for path, relative in sources.items()}
    for root in roots:
        if not root.exists() or root.is_symlink():
            continue
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            kept: list[str] = []
            for name in directory_names:
                path = directory_path / name
                if path.is_symlink():
                    target = path.resolve(strict=False)
                    if target in resolved_sources:
                        aliases[resolved_sources[target]].append(
                            path.relative_to(workspace).as_posix()
                        )
                else:
                    kept.append(name)
            directory_names[:] = kept
            for name in file_names:
                path = directory_path / name
                if not path.is_symlink():
                    continue
                target = path.resolve(strict=False)
                if target in resolved_sources:
                    aliases[resolved_sources[target]].append(
                        path.relative_to(workspace).as_posix()
                    )
    return {key: sorted(value) for key, value in aliases.items()}


def _find_backlinks(
    workspace: Path,
    catalog_database: Path,
    candidate_items: Sequence[dict[str, Any]],
) -> dict[str, list[str]]:
    snapshot = _load_catalog(catalog_database, workspace)
    candidate_paths = {item["relative_path"] for item in candidate_items}
    needles: dict[str, tuple[str, ...]] = {}
    for item in candidate_items:
        relative = item["relative_path"].lower()
        absolute = str(workspace / item["relative_path"]).lower()
        basename = Path(relative).name
        needles[item["relative_path"]] = (relative, absolute, f"]({basename}", f"[[{basename}")
    backlinks: dict[str, list[str]] = {item["relative_path"]: [] for item in candidate_items}
    for row in snapshot.rows:
        referrer = str(row["relative_path"])
        if referrer in candidate_paths:
            continue
        path = _safe_source_path(workspace, referrer, must_exist=True)
        if path.is_symlink() or not path.is_file() or not catalog._candidate_file(path):
            continue
        try:
            _, text, _ = catalog._read_file_snapshot(path, hint_limit=2 * 1024 * 1024)
        except OSError:
            continue
        for relative, candidate_needles in needles.items():
            if any(needle in text for needle in candidate_needles):
                backlinks[relative].append(referrer)
    return {key: sorted(set(value)) for key, value in backlinks.items()}


def _append_event(path: Path, event: dict[str, Any]) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise QuarantineError(f"unsafe event log: {path}")
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        encoded = _canonical_json(event) + b"\n"
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short append to quarantine event log")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _event_last_actions(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise QuarantineError(f"unsafe event log: {path}")
    actions: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise QuarantineError(f"invalid event log line {number}: {path}") from exc
        actions[str(event.get("relative_path"))] = str(event.get("action"))
    return actions


def quarantine_set(
    root: Path,
    set_id: str,
    catalog_database: Path,
    acknowledge: str | None,
    apply: bool,
) -> dict[str, Any]:
    directory, manifest = _load_manifest(root, set_id)
    if apply and not manifest["artifacts"]:
        raise QuarantineError(
            "sealed set has no move-eligible artifacts; review queue remains source-read-only"
        )
    if apply and acknowledge != set_id:
        raise QuarantineError("--acknowledge must exactly match the quarantine set id")
    workspace = Path(manifest["workspace"])
    status = set_status(root, set_id)
    sources = {
        _safe_source_path(workspace, item["relative_path"], must_exist=False): item["relative_path"]
        for item in manifest["artifacts"]
    }
    aliases = _find_aliases(workspace, sources)
    backlinks = _find_backlinks(workspace, catalog_database, manifest["artifacts"])
    blockers: list[dict[str, Any]] = []
    planned: list[dict[str, str]] = []
    if len(manifest["artifacts"]) != len(status["artifacts"]):
        raise QuarantineError("set status cardinality does not match sealed manifest")
    for item, state_record in zip(manifest["artifacts"], status["artifacts"]):
        relative = item["relative_path"]
        source = _safe_source_path(workspace, relative, must_exist=False)
        relocated = directory / "relocated" / relative
        reasons: list[str] = []
        if state_record["state"] == "quarantined":
            planned.append({"relative_path": relative, "action": "recover-or-noop"})
            continue
        if state_record["state"] != "staged":
            reasons.append(f"set-state:{state_record['state']}")
        tracking, _ = _git_tracking(source, workspace)
        if tracking == "tracked":
            reasons.append("git-tracked")
        elif tracking == "error":
            reasons.append("git-tracking-unknown")
        if aliases.get(relative):
            reasons.append("live-symlink-alias")
        if backlinks.get(relative):
            reasons.append("literal-backlink")
        if source.exists() and source.stat().st_dev != directory.stat().st_dev:
            reasons.append("cross-filesystem-rename")
        if relocated.exists() or relocated.is_symlink():
            reasons.append("relocated-destination-present")
        if reasons:
            blockers.append(
                {
                    "relative_path": relative,
                    "reason_codes": sorted(set(reasons)),
                    "aliases": aliases.get(relative, []),
                    "backlinks": backlinks.get(relative, []),
                }
            )
        else:
            planned.append({"relative_path": relative, "action": "rename-to-quarantine"})
    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": "quarantine-apply" if apply else "quarantine-plan",
        "set_id": set_id,
        "planned": planned,
        "blocked": blockers,
        "source_delete": "disabled",
    }
    if not apply:
        return result
    if blockers:
        raise QuarantineError(
            f"quarantine preflight has {len(blockers)} blocked artifact(s); no sources moved"
        )
    events_path = directory / "events.jsonl"
    with _locked(directory / ".operation.lock"):
        actions = _event_last_actions(events_path)
        for item in manifest["artifacts"]:
            relative = item["relative_path"]
            source = _safe_source_path(workspace, relative, must_exist=False)
            relocated = directory / "relocated" / relative
            source_state = _verify_file(source, item)
            relocated_state = _verify_file(relocated, item)
            recovered = False
            if source_state == "missing" and relocated_state == "match":
                recovered = True
            elif source_state == "match" and relocated_state == "missing":
                _safe_mkdirs(directory / "relocated", Path(relative).parent)
                try:
                    os.rename(source, relocated)
                except OSError as exc:
                    if exc.errno == errno.EXDEV:
                        raise QuarantineError(
                            f"cross-filesystem quarantine is forbidden: {relative}"
                        ) from exc
                    raise
            else:
                raise QuarantineError(
                    f"artifact changed after preflight: {relative} "
                    f"(source={source_state}, relocated={relocated_state})"
                )
            if actions.get(relative) != "quarantined":
                _append_event(
                    events_path,
                    {
                        "action": "quarantined",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "content_sha256": item["content_sha256"],
                        "recovered_after_interruption": recovered,
                        "relative_path": relative,
                        "revision_id": item["revision_id"],
                        "set_id": set_id,
                    },
                )
    result["status"] = "complete"
    result["moved_count"] = len(manifest["artifacts"])
    return result


def restore_set(
    root: Path,
    set_id: str,
    acknowledge: str | None,
    apply: bool,
) -> dict[str, Any]:
    directory, manifest = _load_manifest(root, set_id)
    if apply and not manifest["artifacts"]:
        raise QuarantineError("sealed set has no relocated artifacts to restore")
    if apply and acknowledge != set_id:
        raise QuarantineError("--acknowledge must exactly match the quarantine set id")
    workspace = Path(manifest["workspace"])
    planned: list[dict[str, str]] = []
    blockers: list[dict[str, Any]] = []
    for item in manifest["artifacts"]:
        relative = item["relative_path"]
        source = _safe_source_path(workspace, relative, must_exist=False)
        relocated = directory / "relocated" / relative
        source_state = _verify_file(source, item)
        relocated_state = _verify_file(relocated, item)
        if source_state == "match" and relocated_state == "missing":
            planned.append({"relative_path": relative, "action": "already-restored"})
        elif source_state == "missing" and relocated_state == "match":
            planned.append({"relative_path": relative, "action": "rename-to-workspace"})
        else:
            blockers.append(
                {
                    "relative_path": relative,
                    "reason_codes": [
                        f"source-state:{source_state}",
                        f"relocated-state:{relocated_state}",
                    ],
                }
            )
    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": "restore-apply" if apply else "restore-plan",
        "set_id": set_id,
        "planned": planned,
        "blocked": blockers,
        "source_delete": "disabled",
    }
    if not apply:
        return result
    if blockers:
        raise QuarantineError(
            f"restore preflight has {len(blockers)} blocked artifact(s); nothing restored"
        )
    events_path = directory / "events.jsonl"
    with _locked(directory / ".operation.lock"):
        actions = _event_last_actions(events_path)
        for item in manifest["artifacts"]:
            relative = item["relative_path"]
            source = _safe_source_path(workspace, relative, must_exist=False)
            relocated = directory / "relocated" / relative
            source_state = _verify_file(source, item)
            relocated_state = _verify_file(relocated, item)
            if source_state == "match" and relocated_state == "missing":
                recovered = True
            elif source_state == "missing" and relocated_state == "match":
                _safe_mkdirs(workspace, Path(relative).parent)
                if source.exists() or source.is_symlink():
                    raise QuarantineError(f"restore destination appeared: {relative}")
                try:
                    os.rename(relocated, source)
                except OSError as exc:
                    if exc.errno == errno.EXDEV:
                        raise QuarantineError(
                            f"cross-filesystem restore is forbidden: {relative}"
                        ) from exc
                    raise
                os.chmod(source, int(item["source_mode"]))
                recovered = False
            else:
                raise QuarantineError(
                    f"artifact changed after restore preflight: {relative}"
                )
            if actions.get(relative) != "restored":
                _append_event(
                    events_path,
                    {
                        "action": "restored",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "content_sha256": item["content_sha256"],
                        "recovered_after_interruption": recovered,
                        "relative_path": relative,
                        "revision_id": item["revision_id"],
                        "set_id": set_id,
                    },
                )
    result["status"] = "complete"
    result["restored_count"] = len(manifest["artifacts"])
    return result


def _common_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).with_name("artifact-policy.json"),
    )
    parser.add_argument("--minimum-age-days", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--details",
        action="store_true",
        help="include every review-only and blocked record instead of compact samples",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quarantine-root",
        type=Path,
        default=DEFAULT_QUARANTINE_ROOT,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="classify candidates without writes")
    _common_plan_arguments(plan_parser)

    stage_parser = subparsers.add_parser(
        "stage", help="plan or create immutable pre-cleanup copies"
    )
    _common_plan_arguments(stage_parser)
    stage_parser.add_argument("--apply", action="store_true")

    status_parser = subparsers.add_parser("status", help="verify a sealed set")
    status_parser.add_argument("--set", dest="set_id", required=True)

    quarantine_parser = subparsers.add_parser(
        "quarantine", help="plan or relocate sealed originals without deletion"
    )
    quarantine_parser.add_argument("--set", dest="set_id", required=True)
    quarantine_parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    quarantine_parser.add_argument("--acknowledge")
    quarantine_parser.add_argument("--apply", action="store_true")

    restore_parser = subparsers.add_parser(
        "restore", help="plan or restore relocated originals"
    )
    restore_parser.add_argument("--set", dest="set_id", required=True)
    restore_parser.add_argument("--acknowledge")
    restore_parser.add_argument("--apply", action="store_true")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    workspace = _validate_workspace(getattr(args, "workspace", DEFAULT_WORKSPACE))
    root = _validate_external_root(args.quarantine_root, workspace)
    if args.command in {"plan", "stage"}:
        policy_path = _absolute_lexical(args.policy, workspace)
        if not _is_within(policy_path, workspace):
            raise QuarantineError("artifact policy must stay inside the source workspace")
        _reject_symlink_components(policy_path, workspace, "artifact policy")
        plan = build_plan(
            workspace,
            args.catalog,
            policy_path,
            args.minimum_age_days,
            args.limit,
        )
        if args.command == "plan":
            return plan if args.details else _compact_plan(plan)
        result = stage_plan(plan, root, args.apply)
        if not args.apply and not args.details:
            return _compact_plan(result)
        return result
    if args.command == "status":
        return set_status(root, args.set_id)
    if args.command == "quarantine":
        return quarantine_set(
            root,
            args.set_id,
            args.catalog,
            args.acknowledge,
            args.apply,
        )
    if args.command == "restore":
        return restore_set(root, args.set_id, args.acknowledge, args.apply)
    raise QuarantineError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    security.activate_private_umask()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (
        QuarantineError,
        catalog.ChangedDuringScan,
        catalog.PolicyError,
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
    ) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

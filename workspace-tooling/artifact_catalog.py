#!/usr/bin/env python3
"""Build a source-read-only catalog of workspace artifacts.

The catalog is an inventory, not a new source of truth.  It never writes into
the workspace and deliberately skips vault projections and known dependency /
worktree trees.  Its SQLite file and JSON summary are derived outputs that can
be safely rebuilt from the original files.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import artifact_security as security

SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = (2, 3)
CATALOG_READ_ATTEMPTS = 3
DEFAULT_OUTPUT_DIR = Path("~/.local/share/personal-artifacts").expanduser()
DEFAULT_ROOTS = ("plans", ".claude", "docs", "GitLab")
DEFAULT_INCLUDE_GLOBS = (
    "plans/**",
    ".claude/notes/**",
    ".claude/agent-memory/**",
    "docs/**",
    "GitLab/**/plans/**",
    "GitLab/**/.claude/notes/**",
    "GitLab/**/.claude/agent-memory/**",
    "GitLab/**/docs/**",
)
DOCUMENT_SUFFIXES = {
    ".md", ".mdx", ".txt", ".rst", ".adoc", ".json", ".yaml", ".yml",
    ".toml", ".csv", ".log",
}
ALWAYS_PRUNE_NAMES = {
    ".git", ".venv", "__pycache__", "node_modules", "deps", "vendor",
    "cue.mod", ".worktrees",
}
ALWAYS_EXCLUDE_SEGMENTS = {"Notes", "ai-studio"}


class PolicyError(ValueError):
    """The policy cannot safely be applied."""


class ChangedDuringScan(OSError):
    """A source file changed while its read-only snapshot was being hashed."""


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    revision_id: str
    relative_path: str
    content_sha256: str
    byte_size: int
    mtime_ns: int
    artifact_type: str
    authority_class: str
    lifecycle_hints: tuple[str, ...]
    source_scope: str
    repository: str | None
    project: str


def _normalize_relative(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise PolicyError(f"policy path must stay inside the workspace: {value!r}")
    return path.as_posix().rstrip("/")


def load_policy(path: Path) -> dict[str, Any]:
    """Load and minimally validate the catalog portion of an artifact policy."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    catalog = raw.get("catalog")
    if not isinstance(catalog, dict):
        raise PolicyError("policy requires a catalog object")
    defaults = {
        "exclude_roots": [],
        "prune_directory_names": [],
        "prune_path_globs": [],
        "top_level_globs": ["*.md"],
        "include_path_globs": list(DEFAULT_INCLUDE_GLOBS),
    }
    for key, default in defaults.items():
        catalog.setdefault(key, default)
    for key in (
        "exclude_roots",
        "prune_directory_names",
        "prune_path_globs",
        "top_level_globs",
        "include_path_globs",
    ):
        values = catalog.get(key, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise PolicyError(f"catalog.{key} must be a string list")
    if not catalog["include_path_globs"]:
        raise PolicyError("catalog.include_path_globs must not be empty")
    roots = catalog.get("canonical_roots", list(DEFAULT_ROOTS))
    if not isinstance(roots, list) or not all(isinstance(value, str) for value in roots):
        raise PolicyError("catalog.canonical_roots must be a string list")
    catalog["canonical_roots"] = [_normalize_relative(value) for value in roots]
    if not catalog["canonical_roots"]:
        raise PolicyError("catalog.canonical_roots must not be empty")
    required_roots = catalog.get("required_roots", catalog["canonical_roots"])
    if not isinstance(required_roots, list) or not all(
        isinstance(value, str) for value in required_roots
    ):
        raise PolicyError("catalog.required_roots must be a string list")
    catalog["required_roots"] = [
        _normalize_relative(value) for value in required_roots
    ]
    unknown_required = sorted(
        set(catalog["required_roots"]) - set(catalog["canonical_roots"])
    )
    if unknown_required:
        raise PolicyError(
            "catalog.required_roots must be canonical roots: "
            + ", ".join(unknown_required)
        )
    catalog["exclude_roots"] = [_normalize_relative(value) for value in catalog["exclude_roots"]]
    root_parts = [(root, Path(root).parts) for root in catalog["canonical_roots"]]
    for index, (left, left_parts) in enumerate(root_parts):
        for right, right_parts in root_parts[index + 1 :]:
            common = min(len(left_parts), len(right_parts))
            if left_parts[:common] == right_parts[:common]:
                raise PolicyError(
                    f"catalog canonical roots overlap and would double count: {left!r}, {right!r}"
                )
    raw["catalog"] = catalog
    return raw


def _matches_glob(relative: str, pattern: str) -> bool:
    """Match policy globs predictably for both root and nested paths."""
    relative = relative.strip("/")
    pattern = pattern.strip("/")
    return fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(
        "/" + relative, "/" + pattern
    )


def _is_excluded(relative: str, policy: dict[str, Any]) -> bool:
    parts = Path(relative).parts
    catalog = policy["catalog"]
    if any(part in ALWAYS_EXCLUDE_SEGMENTS for part in parts):
        return True
    if any(relative == root or relative.startswith(root + "/") for root in catalog["exclude_roots"]):
        return True
    patterns = list(catalog["prune_path_globs"]) + [
        "**/.aggregate/**", "**/*run.failed*/**", "**/cue.mod/pkg/**",
        "**/vendor/**", "**/deps/**", "**/ai-studio/**",
    ]
    return any(_matches_glob(relative, pattern) for pattern in patterns)


def _is_included(relative: str, policy: dict[str, Any]) -> bool:
    """Apply the catalog's default-deny artifact scope."""
    lowered = relative.lower()
    return any(
        _matches_glob(lowered, pattern.lower())
        for pattern in policy["catalog"]["include_path_globs"]
    )


def _should_prune_dir(relative: str, name: str, policy: dict[str, Any]) -> bool:
    if name in ALWAYS_PRUNE_NAMES or name in policy["catalog"]["prune_directory_names"]:
        return True
    return _is_excluded(relative, policy)


def _candidate_file(path: Path) -> bool:
    return path.suffix.lower() in DOCUMENT_SUFFIXES or path.name in {
        "README", "README.md", "CHANGELOG", "CHANGELOG.md",
    }


def _read_file_snapshot(path: Path, hint_limit: int = 128 * 1024) -> tuple[str, str, os.stat_result]:
    """Hash and sample one stable, no-follow file descriptor."""
    digest = hashlib.sha256()
    hint = bytearray()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"not a regular file: {path}")
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            if len(hint) < hint_limit:
                hint.extend(block[: hint_limit - len(hint)])
        after = os.fstat(handle.fileno())
    identity_before = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
    )
    identity_after = (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    )
    if identity_before != identity_after:
        raise ChangedDuringScan(str(path))
    return digest.hexdigest(), hint.decode("utf-8", errors="replace").lower(), after


def classify_artifact(relative: str, text: str = "") -> tuple[str, str, tuple[str, ...]]:
    """Classify using stable pathname and small textual status hints only."""
    lowered = relative.lower()
    filename = Path(lowered).name
    if any(token in lowered for token in (".aggregate/", "run.failed", "/scratch/", "/tmp/")):
        artifact_type, authority = "scratch", "ephemeral"
    elif "handoff" in lowered or "continuation" in filename or "adversarial" in filename:
        artifact_type, authority = "handoff", "working"
    elif "roadmap" in lowered:
        artifact_type, authority = "roadmap", "working"
    elif "milestone" in lowered or ".claude/notes/" in lowered:
        artifact_type, authority = "milestone_evidence", "evidence"
    elif any(token in lowered for token in ("/adr/", "decision", "architecture-decision")) or filename.startswith("adr-"):
        artifact_type, authority = "decision", "canonical"
    elif any(token in lowered for token in ("research", "spike", "investigation")):
        artifact_type, authority = "research", "working"
    elif "/plans/" in "/" + lowered or lowered.startswith("plans/"):
        artifact_type, authority = "plan", "working"
    else:
        artifact_type, authority = "document", "canonical" if lowered.startswith("docs/") else "source"

    hints: list[str] = []
    if any(token in lowered for token in ("archive", "archived")):
        hints.append("archived")
    if "supersed" in lowered or "superseded" in text:
        hints.append("superseded")
    if any(token in text for token in ("status: closed", "status: complete", "status: completed")):
        hints.append("closed")
    if artifact_type == "handoff" and any(
        token in text for token in ("status: requested", "review: requested", "verdict: pending")
    ):
        hints.append("review_open")
    if not hints:
        hints.append("active")
    return artifact_type, authority, tuple(sorted(set(hints)))


def _find_repository(path: Path, workspace: Path, cache: dict[Path, Path | None]) -> Path | None:
    current = path.parent
    while current != workspace.parent:
        if current in cache:
            return cache[current]
        if (current / ".git").exists():
            cache[current] = current
            return current
        if current == workspace:
            break
        current = current.parent
    cache[path.parent] = None
    return None


def source_metadata(relative: str, path: Path, workspace: Path, cache: dict[Path, Path | None]) -> tuple[str, str | None, str]:
    """Derive scope identifiers without inspecting Git configuration or remote state."""
    repo_root = _find_repository(path, workspace, cache)
    if repo_root is not None:
        repo = repo_root.relative_to(workspace).as_posix()
        return "repository", repo, repo_root.name
    parts = Path(relative).parts
    if parts and parts[0] == ".claude":
        scope = "runtime_evidence"
    elif parts and parts[0] == "plans":
        scope = "workspace_plans"
    elif parts and parts[0] == "docs":
        scope = "workspace_docs"
    elif parts and parts[0] == "GitLab":
        scope = "gitlab_workspace"
    else:
        scope = "workspace"
    project = parts[1] if len(parts) > 1 and parts[0] in {"plans", "docs"} else (parts[0] if parts else "workspace")
    return scope, None, project


def make_ids(relative: str, content_sha256: str) -> tuple[str, str]:
    """Create stable IDs: path identity stays stable; content creates revisions."""
    # NOTE: this literal is an ID-STABILITY constant, not a label. It is hashed
    # into every artifact_id/revision_id, so changing it re-keys the entire
    # corpus and invalidates every ingestion checkpoint. Left at its original
    # value deliberately when the kit was genericized (2026-07-23).
    logical = hashlib.sha256(f"nalej-artifact-v1\\0{relative}".encode()).hexdigest()
    artifact_id = f"artifact:{logical}"
    revision = hashlib.sha256(f"{artifact_id}\\0{content_sha256}".encode()).hexdigest()
    return artifact_id, f"revision:{revision}"


def iter_artifacts(
    workspace: Path,
    policy: dict[str, Any],
    *,
    read_attempts: int = CATALOG_READ_ATTEMPTS,
) -> tuple[Iterator[Artifact], Counter[str], list[str]]:
    """Yield supported regular files beneath canonical roots, never following links."""
    if read_attempts < 1:
        raise ValueError("catalog read_attempts must be positive")
    stats: Counter[str] = Counter()
    diagnostics: list[str] = []
    repo_cache: dict[Path, Path | None] = {}

    def iterator() -> Iterator[Artifact]:
        seen: set[str] = set()

        for path in sorted(workspace.iterdir(), key=lambda item: item.name.casefold()):
            if (
                not path.is_symlink()
                and path.is_file()
                and any(
                    fnmatch.fnmatchcase(path.name.lower(), pattern.lower())
                    for pattern in policy["catalog"]["top_level_globs"]
                )
            ):
                yield from emit(path, seen)

        for configured_root in policy["catalog"]["canonical_roots"]:
            root = workspace / configured_root
            if not root.exists():
                if configured_root in policy["catalog"]["required_roots"]:
                    stats["missing_required_roots"] += 1
                    diagnostics.append(f"missing required root: {configured_root}")
                else:
                    stats["missing_optional_roots"] += 1
                continue
            if _is_excluded(configured_root, policy):
                continue
            if root.is_symlink():
                raise PolicyError(f"catalog canonical root must not be a symlink: {root}")
            try:
                root.resolve(strict=True).relative_to(workspace)
            except ValueError as exc:
                raise PolicyError(f"catalog canonical root escapes workspace: {root}") from exc
            if root.is_file():
                roots: list[tuple[str, list[str], list[str]]] = [(str(root.parent), [], [root.name])]
            else:
                def walk_error(exc: OSError) -> None:
                    stats["unreadable_directories"] += 1
                    diagnostics.append(
                        f"unreadable directory: {getattr(exc, 'filename', root)}"
                    )

                roots = os.walk(
                    root,
                    topdown=True,
                    followlinks=False,
                    onerror=walk_error,
                )
            for directory, directories, filenames in roots:
                directory_path = Path(directory)
                directories[:] = [
                    name for name in directories
                    if not _should_prune_dir((directory_path / name).relative_to(workspace).as_posix(), name, policy)
                ]
                for name in sorted(filenames):
                    path = directory_path / name
                    relative = path.relative_to(workspace).as_posix()
                    if _is_excluded(relative, policy):
                        stats["excluded_files"] += 1
                        continue
                    if path.is_symlink():
                        stats["skipped_symlinks"] += 1
                        continue
                    if not path.is_file() or not _candidate_file(path):
                        stats["skipped_non_documents"] += 1
                        continue
                    if not _is_included(relative, policy):
                        stats["outside_artifact_scope"] += 1
                        continue
                    yield from emit(path, seen)

    def emit(path: Path, seen: set[str]) -> Iterator[Artifact]:
        relative = path.relative_to(workspace).as_posix()
        if relative in seen:
            raise PolicyError(f"artifact was discovered more than once: {relative}")
        seen.add(relative)
        last_error: OSError | None = None
        for attempt in range(1, read_attempts + 1):
            try:
                content_sha256, text, info = _read_file_snapshot(path)
                break
            except (ChangedDuringScan, OSError) as exc:
                last_error = exc
                if attempt < read_attempts:
                    stats["read_retries"] += 1
                    continue
                if isinstance(exc, ChangedDuringScan):
                    stats["changed_during_scan"] += 1
                    diagnostics.append(f"changed during scan: {relative}")
                else:
                    stats["unreadable_files"] += 1
                    diagnostics.append(f"unreadable file: {relative}")
                return
        else:  # pragma: no cover - the bounded loop always breaks or returns
            raise last_error or OSError(f"unable to read {path}")
        artifact_type, authority, hints = classify_artifact(relative, text)
        scope, repository, project = source_metadata(relative, path, workspace, repo_cache)
        artifact_id, revision_id = make_ids(relative, content_sha256)
        stats["cataloged_files"] += 1
        stats["cataloged_bytes"] += info.st_size
        yield Artifact(
            artifact_id, revision_id, relative, content_sha256, info.st_size,
            info.st_mtime_ns, artifact_type, authority, hints, scope, repository, project,
        )

    return iterator(), stats, diagnostics


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA user_version = 3;
CREATE TABLE IF NOT EXISTS scan_runs (
  run_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  workspace_path TEXT NOT NULL, policy_path TEXT NOT NULL, dry_run INTEGER NOT NULL,
  artifact_count INTEGER NOT NULL DEFAULT 0, byte_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'complete'
    CHECK(status IN ('complete', 'degraded', 'failed')),
  diagnostics_json TEXT NOT NULL DEFAULT '[]',
  missing_required_roots INTEGER NOT NULL DEFAULT 0,
  unreadable_files INTEGER NOT NULL DEFAULT 0,
  changed_during_scan INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY, logical_path TEXT NOT NULL UNIQUE,
  first_seen_run_id INTEGER, last_seen_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS artifact_revisions (
  revision_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  content_sha256 TEXT NOT NULL, relative_path TEXT NOT NULL, byte_size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL, artifact_type TEXT NOT NULL, authority_class TEXT NOT NULL,
  lifecycle_hints_json TEXT NOT NULL, source_scope TEXT NOT NULL, repository TEXT,
  project TEXT NOT NULL, blob_uri TEXT, observed_run_id INTEGER NOT NULL REFERENCES scan_runs(run_id),
  observed_at TEXT NOT NULL, UNIQUE(artifact_id, content_sha256)
);
CREATE TABLE IF NOT EXISTS artifact_observations (
  run_id INTEGER NOT NULL REFERENCES scan_runs(run_id),
  revision_id TEXT NOT NULL REFERENCES artifact_revisions(revision_id),
  PRIMARY KEY(run_id, revision_id)
);
CREATE VIEW IF NOT EXISTS current_artifact_revisions AS
SELECT revisions.*
FROM artifact_revisions AS revisions
JOIN artifact_observations AS observations
  ON observations.revision_id = revisions.revision_id
WHERE observations.run_id = (
  SELECT MAX(run_id) FROM scan_runs
  WHERE finished_at IS NOT NULL AND status = 'complete'
);
CREATE VIEW IF NOT EXISTS duplicate_content AS
SELECT content_sha256, COUNT(DISTINCT artifact_id) AS artifact_count,
       SUM(byte_size) AS combined_bytes
FROM current_artifact_revisions
GROUP BY content_sha256 HAVING COUNT(DISTINCT artifact_id) > 1;
CREATE VIEW IF NOT EXISTS duplicate_content_history AS
SELECT content_sha256, COUNT(DISTINCT artifact_id) AS artifact_count,
       SUM(byte_size) AS combined_bytes
FROM artifact_revisions
GROUP BY content_sha256 HAVING COUNT(DISTINCT artifact_id) > 1;
CREATE INDEX IF NOT EXISTS artifact_revisions_content_idx ON artifact_revisions(content_sha256);
CREATE INDEX IF NOT EXISTS artifact_revisions_run_idx ON artifact_revisions(observed_run_id);
"""


def _ensure_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version == 0:
        connection.executescript(SCHEMA)
        return
    if version == 2:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(scan_runs)")
        }
        additions = {
            "status": (
                "ALTER TABLE scan_runs ADD COLUMN status TEXT NOT NULL "
                "DEFAULT 'complete'"
            ),
            "diagnostics_json": (
                "ALTER TABLE scan_runs ADD COLUMN diagnostics_json TEXT NOT NULL "
                "DEFAULT '[]'"
            ),
            "missing_required_roots": (
                "ALTER TABLE scan_runs ADD COLUMN missing_required_roots INTEGER "
                "NOT NULL DEFAULT 0"
            ),
            "unreadable_files": (
                "ALTER TABLE scan_runs ADD COLUMN unreadable_files INTEGER "
                "NOT NULL DEFAULT 0"
            ),
            "changed_during_scan": (
                "ALTER TABLE scan_runs ADD COLUMN changed_during_scan INTEGER "
                "NOT NULL DEFAULT 0"
            ),
            "error": "ALTER TABLE scan_runs ADD COLUMN error TEXT",
        }
        with connection:
            for name, statement in additions.items():
                if name not in columns:
                    connection.execute(statement)
            connection.execute(
                "UPDATE scan_runs SET status='complete' "
                "WHERE finished_at IS NOT NULL"
            )
            connection.execute("DROP VIEW IF EXISTS duplicate_content")
            connection.execute("DROP VIEW IF EXISTS current_artifact_revisions")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.executescript(SCHEMA)
        return
    if version == SCHEMA_VERSION:
        connection.executescript(SCHEMA)
        return
    raise PolicyError(
        f"catalog schema version {version} is not supported; "
        f"expected one of: {', '.join(str(value) for value in SUPPORTED_SCHEMA_VERSIONS)}"
    )


def _open_staging_database(destination: Path) -> tuple[sqlite3.Connection, Path]:
    security.ensure_private_directory(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    security.secure_created_file(temporary)
    try:
        if destination.exists():
            security.require_private_file(destination)
            with sqlite3.connect(destination) as old, sqlite3.connect(temporary) as new:
                version = int(old.execute("PRAGMA user_version").fetchone()[0])
                if version not in SUPPORTED_SCHEMA_VERSIONS:
                    raise PolicyError(
                        f"catalog schema version {version} is not supported; "
                        f"expected one of: "
                        f"{', '.join(str(value) for value in SUPPORTED_SCHEMA_VERSIONS)}"
                    )
                old.backup(new)
        connection = sqlite3.connect(temporary)
        _ensure_schema(connection)
        return connection, temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_catalog(
    destination: Path,
    artifacts: Sequence[Artifact],
    workspace: Path,
    policy_path: Path,
    *,
    generation_status: str,
    scan_stats: Counter[str],
    diagnostics: Sequence[str],
    error: str | None = None,
) -> dict[str, Any]:
    if generation_status not in {"complete", "degraded", "failed"}:
        raise ValueError(f"invalid catalog generation status: {generation_status}")
    connection, temporary = _open_staging_database(destination)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_runs(
                  started_at, workspace_path, policy_path, dry_run, status,
                  diagnostics_json, missing_required_roots, unreadable_files,
                  changed_during_scan, error
                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    str(workspace),
                    str(policy_path),
                    generation_status,
                    json.dumps(list(diagnostics), ensure_ascii=False),
                    int(scan_stats["missing_required_roots"]),
                    int(
                        scan_stats["unreadable_files"]
                        + scan_stats["unreadable_directories"]
                    ),
                    int(scan_stats["changed_during_scan"]),
                    error,
                ),
            )
            run_id = int(cursor.lastrowid)
            for item in artifacts:
                connection.execute(
                    "INSERT INTO artifacts(artifact_id, logical_path, first_seen_run_id, last_seen_run_id) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(artifact_id) DO UPDATE SET last_seen_run_id=excluded.last_seen_run_id",
                    (item.artifact_id, item.relative_path, run_id, run_id),
                )
                connection.execute(
                    "INSERT INTO artifact_revisions(revision_id, artifact_id, content_sha256, relative_path, byte_size, mtime_ns, artifact_type, authority_class, lifecycle_hints_json, source_scope, repository, project, blob_uri, observed_run_id, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?) "
                    "ON CONFLICT(artifact_id, content_sha256) DO UPDATE SET relative_path=excluded.relative_path, byte_size=excluded.byte_size, mtime_ns=excluded.mtime_ns, artifact_type=excluded.artifact_type, authority_class=excluded.authority_class, lifecycle_hints_json=excluded.lifecycle_hints_json, source_scope=excluded.source_scope, repository=excluded.repository, project=excluded.project, observed_run_id=excluded.observed_run_id, observed_at=excluded.observed_at",
                    (item.revision_id, item.artifact_id, item.content_sha256, item.relative_path, item.byte_size, item.mtime_ns, item.artifact_type, item.authority_class, json.dumps(item.lifecycle_hints), item.source_scope, item.repository, item.project, run_id, now),
                )
                connection.execute("INSERT OR IGNORE INTO artifact_observations(run_id, revision_id) VALUES (?, ?)", (run_id, item.revision_id))
            connection.execute(
                "UPDATE scan_runs SET finished_at=?, artifact_count=?, byte_count=? WHERE run_id=?",
                (datetime.now(timezone.utc).isoformat(), len(artifacts), sum(item.byte_size for item in artifacts), run_id),
            )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        authoritative = connection.execute(
            "SELECT MAX(run_id) FROM scan_runs "
            "WHERE finished_at IS NOT NULL AND status='complete'"
        ).fetchone()[0]
        connection.close()
        os.replace(temporary, destination)
        security.secure_created_file(destination)
        security.fsync_directory(destination.parent)
        return {
            "run_id": run_id,
            "generation_status": generation_status,
            "authoritative_run_id": (
                None if authoritative is None else int(authoritative)
            ),
        }
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    security.atomic_write_json(path, payload, replace=True)


def build_summary(
    artifacts: Sequence[Artifact],
    scan_stats: Counter[str],
    workspace: Path,
    policy_path: Path,
    policy: dict[str, Any],
    dry_run: bool,
    *,
    generation_status: str,
    diagnostics: Sequence[str],
    error: str | None = None,
) -> dict[str, Any]:
    by_hash: dict[str, list[Artifact]] = defaultdict(list)
    for item in artifacts:
        by_hash[item.content_sha256].append(item)
    duplicates = [
        {"content_sha256": digest, "artifact_count": len(items), "combined_bytes": sum(item.byte_size for item in items), "paths": sorted(item.relative_path for item in items)}
        for digest, items in by_hash.items() if len(items) > 1
    ]
    duplicates.sort(key=lambda item: (-item["artifact_count"], item["content_sha256"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run" if dry_run else "catalog-written",
        "source_mutation": "disabled",
        "blob_copies": "disabled",
        "workspace": str(workspace),
        "policy": str(policy_path),
        "canonical_roots": list(policy["catalog"]["canonical_roots"]),
        "required_roots": list(policy["catalog"]["required_roots"]),
        "generation": {
            "status": generation_status,
            "authoritative": generation_status == "complete",
            "diagnostics": list(diagnostics),
            "error": error,
        },
        "counts": {
            "artifacts": len(artifacts), "bytes": sum(item.byte_size for item in artifacts),
            "duplicate_content_groups": len(duplicates),
            "duplicate_content_artifacts": sum(item["artifact_count"] for item in duplicates),
            **dict(sorted(scan_stats.items())),
        },
        "by_artifact_type": dict(sorted(Counter(item.artifact_type for item in artifacts).items())),
        "by_authority_class": dict(sorted(Counter(item.authority_class for item in artifacts).items())),
        "duplicate_content": duplicates,
    }


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


@contextmanager
def _output_lock(output_dir: Path) -> Iterator[None]:
    lock = output_dir / ".artifact-catalog.lock"
    security.ensure_private_directory(output_dir)
    try:
        lock.mkdir(mode=security.PRIVATE_DIR_MODE)
    except FileExistsError as exc:
        raise OSError(f"catalog output is locked by another run: {lock}") from exc
    try:
        yield
    finally:
        lock.rmdir()


def run_catalog(workspace: Path, output_dir: Path, policy_path: Path, dry_run: bool) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    policy_path = policy_path.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")
    if _is_inside(output_dir, workspace):
        raise ValueError("output directory must be outside the source workspace")
    policy = load_policy(policy_path)
    iterator, stats, diagnostics = iter_artifacts(workspace, policy)
    artifacts: list[Artifact] = []
    scan_error: BaseException | None = None
    try:
        for item in iterator:
            artifacts.append(item)
    except BaseException as exc:
        scan_error = exc
        diagnostics.append(f"scan failed: {type(exc).__name__}: {exc}")
    if scan_error is not None or stats["missing_required_roots"]:
        generation_status = "failed"
    elif (
        stats["unreadable_files"]
        or stats["unreadable_directories"]
        or stats["changed_during_scan"]
    ):
        generation_status = "degraded"
    else:
        generation_status = "complete"
    error_text = (
        None
        if scan_error is None
        else f"{type(scan_error).__name__}: {scan_error}"[:2000]
    )
    summary = build_summary(
        artifacts,
        stats,
        workspace,
        policy_path,
        policy,
        dry_run,
        generation_status=generation_status,
        diagnostics=diagnostics,
        error=error_text,
    )
    if not dry_run:
        destination = output_dir / "artifact-catalog.sqlite3"
        summary_path = output_dir / "artifact-catalog-summary.json"
        with _output_lock(output_dir):
            summary["catalog"] = {
                "database": str(destination),
                "summary": str(summary_path),
                **_write_catalog(
                    destination,
                    artifacts,
                    workspace,
                    policy_path,
                    generation_status=generation_status,
                    scan_stats=stats,
                    diagnostics=diagnostics,
                    error=error_text,
                ),
            }
            _atomic_json_write(summary_path, summary)
    if scan_error is not None:
        raise scan_error
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]), help="workspace to scan")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="derived catalog output directory")
    parser.add_argument("--policy", default=str(Path(__file__).with_name("artifact-policy.json")), help="artifact policy JSON")
    parser.add_argument("--dry-run", action="store_true", help="scan and report without creating derived output")
    parser.add_argument("--json-summary", action="store_true", help="write the complete summary JSON to stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    summary = run_catalog(Path(args.workspace), Path(args.output_dir), Path(args.policy), args.dry_run)
    if args.json_summary:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        counts = summary["counts"]
        print(f"{summary['mode']}: {counts['artifacts']} artifacts, {counts['duplicate_content_groups']} duplicate-content groups")
    return 0 if summary["generation"]["status"] == "complete" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, PolicyError, json.JSONDecodeError, sqlite3.Error) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

#!/usr/bin/env python3
"""One-way, backup-first migration of milestone state v1 to delivery-state v2.

The default is a read-only preview. `--apply` writes `state.v1.json` once, then
atomically replaces state.json. A legacy `complete` claim is conservatively
downgraded to `critique-running`, where v2 adversarial review and closure can be
run honestly. Migration never synthesizes or imports a partial evidence bundle.
Operational status always migrates to `pending` and is never inferred from v1
prose or an external-write ledger.

Usage:
  milestone-pipeline-migrate.py <ID> [--repo-root PATH] [--apply]
  milestone-pipeline-migrate.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


POINTERS = {
    "review_manifest": "review-manifest.json",
    "implementation_evidence": "implementation-evidence.json",
    "publication_intent": "publication-intent.json",
    "release_manifest": "release-manifest.json",
    "operations_plan": "operations-plan.json",
    "operations_evidence": "operations-evidence.json",
    "waivers": "waivers.json",
}
AGENT_KIT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
STATE_SCHEMA_NAME = "milestone-pipeline-state-v2.schema.json"
MILESTONE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TEST_ALLOW_DIRTY_KIT = False
_OPTIONAL_STATE_VALIDATOR: Any = None
_OPTIONAL_STATE_VALIDATOR_RESOLVED = False


def _agent_kit_commit() -> str:
    if not TEST_ALLOW_DIRTY_KIT:
        status = subprocess.run(
            [
                "git", "-C", str(AGENT_KIT_ROOT), "status", "--porcelain",
                "--untracked-files=all", "--", "data/agents",
                "data/commands/milestone-pipeline.md",
                "data/provider-adapters/codex/entrypoints/milestone-pipeline",
                "data/references/milestone-pipeline-*", "data/references/pipeline-pattern-v2.md",
                "data/schemas", "data/scripts/milestone-pipeline-*",
                "data/scripts/milestone-render-provenance.py", "data/model-policy.json",
                "data/facts/catalog.json",
            ],
            capture_output=True, text=True,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise MigrationError(
                "canonical pipeline kit is dirty; commit/regenerate it before migration"
            )
    proc = subprocess.run(
        ["git", "-C", str(AGENT_KIT_ROOT), "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise MigrationError("cannot freeze canonical agent-kit HEAD")
    return proc.stdout.strip()

V1_PHASES = {
    "init", "research-running", "research-complete", "implement-running",
    "implement-complete", "critique-running", "critique-complete",
    "rectify-running", "complete",
}


class MigrationError(Exception):
    pass


def _optional_state_validator() -> Any:
    """Return a bundled Draft 2020-12 validator when available.

    `jsonschema` is intentionally not a runtime dependency of this stdlib migration
    tool. The strict checks below therefore remain authoritative, while an existing
    jsonschema installation gives us full candidate-vs-bundle validation for free.
    """
    global _OPTIONAL_STATE_VALIDATOR, _OPTIONAL_STATE_VALIDATOR_RESOLVED
    if _OPTIONAL_STATE_VALIDATOR_RESOLVED:
        return _OPTIONAL_STATE_VALIDATOR
    try:
        from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-not-found]
        from referencing import Registry, Resource  # type: ignore[import-not-found]
    except ImportError:
        _OPTIONAL_STATE_VALIDATOR_RESOLVED = True
        return None
    try:
        documents = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(SCHEMA_DIR.glob("milestone-*.schema.json"))
        }
        state_schema = documents[STATE_SCHEMA_NAME]
        registry = Registry().with_resources([
            (document["$id"], Resource.from_contents(document))
            for document in documents.values()
        ])
        Draft202012Validator.check_schema(state_schema)
        _OPTIONAL_STATE_VALIDATOR = Draft202012Validator(
            state_schema,
            registry=registry,
            format_checker=FormatChecker(),
        )
    except Exception as exc:  # third-party validators expose version-specific exception classes
        raise MigrationError(f"cannot load bundled v2 state schema: {exc}") from exc
    _OPTIONAL_STATE_VALIDATOR_RESOLVED = True
    return _OPTIONAL_STATE_VALIDATOR


def _validate_finding_counts(value: Any) -> None:
    """Mirror `$defs.findingCounts`, including additionalProperties=false."""
    if value is None:
        return
    keys = {"critical", "high", "medium", "low"}
    if not isinstance(value, dict) or set(value) != keys:
        raise MigrationError(
            "candidate.critique_finding_counts must be null or an object with exactly "
            "critical, high, medium, and low"
        )
    for key in sorted(keys):
        count = value[key]
        # JSON Schema treats mathematically integral JSON numbers (for example
        # `1.0`) as integers, but excludes booleans, fractions, and non-finite
        # values. Mirror that behavior when jsonschema is unavailable.
        is_integer = (
            isinstance(count, int) and not isinstance(count, bool)
        ) or (
            isinstance(count, float)
            and math.isfinite(count)
            and count.is_integer()
        )
        if not is_integer or count < 0:
            raise MigrationError(
                f"candidate.critique_finding_counts.{key} must be a non-negative integer"
            )


def _validate_unique_strings(value: list[str], label: str) -> None:
    if len(value) != len(set(value)):
        raise MigrationError(f"{label} must not contain duplicate items")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _string_list(value: Any, label: str, *, default_empty: bool = True) -> list[str]:
    if value is None and default_empty:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and bool(item.strip()) for item in value
    ):
        raise MigrationError(f"{label} must be an array of non-empty strings")
    return list(value)


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise MigrationError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise MigrationError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_candidate(value: dict[str, Any]) -> None:
    required_keys = {
        "schema_version", "id", "created_at", "updated_at", "phase", "phase_history",
        "agent_kit_commit", "kit_upgrade_history", "check_run_head", "check_run_hashes", "check_run_history",
        "check_run_attempts", "publication_required", "publication_not_required_reason",
        "operations_required", "operations_not_required_reason", "implementation_status",
        "operational_status", "review_status", "artifact_bindings", "migration", *POINTERS,
    }
    missing = sorted(required_keys - set(value))
    if missing:
        raise MigrationError(f"candidate v2 state is missing fields: {missing}")
    if value.get("schema_version") != 2:
        raise MigrationError("candidate schema_version must be 2")
    if not isinstance(value.get("id"), str) or not MILESTONE_ID_RE.fullmatch(value["id"]):
        raise MigrationError("candidate id is invalid")
    created = _parse_time(value.get("created_at"), "candidate.created_at")
    updated = _parse_time(value.get("updated_at"), "candidate.updated_at")
    if created > updated:
        raise MigrationError("candidate.created_at cannot follow migration time")
    if value.get("phase") not in V1_PHASES | {"code-complete"}:
        raise MigrationError("candidate phase is invalid")
    history = value.get("phase_history")
    if not isinstance(history, list) or len(history) != 1 or history[0].get("phase") != value["phase"]:
        raise MigrationError("candidate phase_history must contain the mapped phase exactly once")
    _parse_time(history[0].get("at"), "candidate.phase_history[0].at")
    list_fields = (
        "research_briefs", "implementation_commits", "critics_run", "critique_files",
        "fixed_findings", "deferred_findings", "invalidated_findings",
        "regression_tests_added", "check_run_attempts",
    )
    for field in list_fields:
        if not isinstance(value.get(field), list):
            raise MigrationError(f"candidate.{field} must be an array")
    for field in list_fields[:-1]:
        if not all(isinstance(item, str) and bool(item.strip()) for item in value[field]):
            raise MigrationError(f"candidate.{field} items must be non-empty strings")
    for field in ("critics_run", "critique_files"):
        _validate_unique_strings(value[field], f"candidate.{field}")
    _validate_finding_counts(value.get("critique_finding_counts"))
    for field in (
        "milestone_brief", "research_synthesis", "implementation_specialist",
        "implementation_base", "implementation_commit_range", "implementation_branch",
        "critique_path", "findings_register", "rectification_commit",
        "rectification_not_required_reason", "publication_not_required_reason",
        "operations_not_required_reason",
    ):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise MigrationError(f"candidate.{field} must be string or null")
    if value.get("research_mode") not in {None, "standard", "deep", "single"}:
        raise MigrationError("candidate.research_mode is invalid")
    if value.get("implementation_path") not in {None, "inline", "delegated", "specialist"}:
        raise MigrationError("candidate.implementation_path is invalid")
    if not isinstance(value.get("publication_required"), bool) or not isinstance(
        value.get("operations_required"), bool
    ):
        raise MigrationError("candidate delivery requirement flags must be booleans")
    if value.get("operational_status") != "pending":
        raise MigrationError("migration may not infer operational completion")
    if value.get("artifact_bindings") != {} or value.get("check_run_hashes") != {}:
        raise MigrationError("migration may not synthesize artifact/check bindings")
    migration = value.get("migration")
    if not isinstance(migration, dict):
        raise MigrationError("candidate migration receipt is missing")
    ledger = migration.get("legacy_external_writes")
    if not isinstance(ledger, dict):
        raise MigrationError("candidate legacy external-write ledger is malformed")
    for field in ("required", "completed"):
        _string_list(ledger.get(field), f"candidate.migration.legacy_external_writes.{field}")
    validator = _optional_state_validator()
    if validator is not None:
        errors = sorted(
            validator.iter_errors(value),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            error = errors[0]
            location = "/".join(str(part) for part in error.absolute_path) or "$"
            raise MigrationError(
                f"candidate violates bundled v2 state schema at {location}: {error.message}"
            )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MigrationError(f"state not found: {path}")
    except json.JSONDecodeError as exc:
        raise MigrationError(f"invalid state JSON: {exc}")
    if not isinstance(value, dict):
        raise MigrationError("state root must be an object")
    return value


def _repo_root(arg: str | None) -> Path:
    if arg:
        root = Path(arg).expanduser().resolve()
    elif os.environ.get("REPO_ROOT"):
        root = Path(os.environ["REPO_ROOT"]).expanduser().resolve()
    else:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise MigrationError("pass --repo-root or run inside the target git repo")
        root = Path(proc.stdout.strip()).resolve()
    if not root.is_dir():
        raise MigrationError(f"repo root is not a directory: {root}")
    return root


def _base_state(old: dict[str, Any], old_sha: str) -> dict[str, Any]:
    phase = old.get("phase")
    if phase not in V1_PHASES:
        raise MigrationError(f"unrecognized v1 phase {phase!r}")
    # Legacy post-critique states have no v2 assessment binding. Re-enter the
    # reviewable phase instead of stranding the migration in a phase whose v2
    # prerequisites can no longer be materialized.
    mapped = {
        "research-complete": "research-running",
        "implement-complete": "implement-running",
        "critique-complete": "critique-running",
        "rectify-running": "critique-running",
        "complete": "critique-running",
    }.get(phase, phase)
    now = _now()
    # Migration is one explicit, audited boundary. Legacy histories used a
    # different terminal edge (`rectify-running -> complete`) and cannot be
    # replayed as native v2 history without inventing gates that never ran.
    # The source phase and full source hash remain in `migration`.
    clean_history = [{"phase": mapped, "at": now}]

    result = {
        "schema_version": 2,
        "id": old.get("id"),
        "created_at": old.get("created_at") or now,
        "updated_at": now,
        "phase": mapped,
        "phase_history": clean_history,
        "agent_kit_commit": _agent_kit_commit(),
        "kit_upgrade_history": [],
        "check_run_head": None,
        "check_run_hashes": {},
        "check_run_history": {},
        "check_run_attempts": [],
        "milestone_brief": old.get("milestone_brief") or "",
        "research_mode": old.get("research_mode"),
        "research_briefs": _string_list(old.get("research_briefs"), "v1.research_briefs"),
        "research_synthesis": old.get("research_synthesis"),
        "implementation_path": old.get("implementation_path"),
        "implementation_specialist": old.get("implementation_specialist"),
        "implementation_base": old.get("implementation_base"),
        "implementation_commit_range": old.get("implementation_commit_range"),
        "implementation_commits": _string_list(old.get("implementation_commits"), "v1.implementation_commits"),
        "implementation_branch": old.get("implementation_branch"),
        "critique_path": old.get("critique_path"),
        "critics_run": _string_list(old.get("critics_run"), "v1.critics_run"),
        "critique_files": _string_list(old.get("critique_files"), "v1.critique_files"),
        "critique_finding_counts": old.get("critique_finding_counts"),
        "findings_register": old.get("findings_register"),
        "rectification_commit": old.get("rectification_commit"),
        "rectification_not_required_reason": None,
        "fixed_findings": _string_list(old.get("fixed_findings"), "v1.fixed_findings"),
        "deferred_findings": _string_list(old.get("deferred_findings"), "v1.deferred_findings"),
        "invalidated_findings": _string_list(old.get("invalidated_findings"), "v1.invalidated_findings"),
        "regression_tests_added": _string_list(old.get("regression_tests_added"), "v1.regression_tests_added"),
        "publication_required": True,
        "publication_not_required_reason": None,
        "operations_required": True,
        "operations_not_required_reason": None,
        "implementation_status": (
            "committed"
            if phase in {"critique-complete", "rectify-running", "complete"}
            else "in_progress"
        ),
        "operational_status": "pending",
        "review_status": "pending",
        **{key: f"artifacts/{name}" for key, name in POINTERS.items()},
        "artifact_bindings": {},
        "migration": {
            "source_schema_version": 1,
            "source_phase": phase,
            "source_sha256": old_sha,
            "migrated_at": now,
            "terminal_claim_downgraded": phase == "complete",
            "legacy_external_writes": {
                "required": _string_list(
                    old.get("external_writes_required"), "v1.external_writes_required"
                ),
                "completed": _string_list(
                    old.get("external_writes_completed"), "v1.external_writes_completed"
                ),
                "authorized": old.get("external_writes_authorized") is True,
            },
        },
    }
    if not isinstance(result["id"], str) or not result["id"]:
        raise MigrationError("v1 state has no milestone id")
    _validate_candidate(result)
    return result


def migrate(
    milestone_id: str,
    root: Path,
    *,
    apply: bool,
) -> dict[str, Any]:
    if not MILESTONE_ID_RE.fullmatch(milestone_id):
        raise MigrationError(
            "milestone id must match ^[A-Za-z0-9][A-Za-z0-9._-]*$"
        )
    state_dir = root / ".claude" / "notes" / "milestones" / milestone_id
    state_path = state_dir / "state.json"
    old = _load(state_path)
    if old.get("schema_version") == 2:
        raise MigrationError("state is already schema v2")
    if old.get("schema_version") not in {None, 1}:
        raise MigrationError(f"unsupported source schema {old.get('schema_version')!r}")
    if old.get("id") != milestone_id:
        raise MigrationError("state.id does not match requested milestone id")
    old_sha = _sha(state_path)
    new = _base_state(old, old_sha)
    result = {
        "milestone_id": milestone_id,
        "source_phase": old.get("phase"),
        "mapped_phase": new["phase"],
        "terminal_claim_downgraded": old.get("phase") == "complete",
        "operational_status": "pending",
        "apply": apply,
    }
    if not apply:
        result["next"] = (
            "Re-run with --apply, then resume at the mapped phase and create fresh v2 review evidence."
        )
        return result

    backup = state_dir / "state.v1.json"
    if backup.exists():
        if backup.is_symlink() or not backup.is_file():
            raise MigrationError(f"backup exists but is not a safe regular file: {backup}")
        if _sha(backup) != old_sha:
            raise MigrationError(f"backup already exists with different content: {backup}")
    else:
        _write_atomic(backup, state_path.read_bytes())
    _write_atomic(state_path, (json.dumps(new, indent=2) + "\n").encode("utf-8"))

    return result


def self_test() -> int:
    global TEST_ALLOW_DIRTY_KIT
    TEST_ALLOW_DIRTY_KIT = True
    failures = 0

    def expect(name: str, condition: bool) -> None:
        nonlocal failures
        print(f"  {name}: {'ok' if condition else 'FAIL'}")
        failures += 0 if condition else 1

    def sparse_source(now: str, **updates: Any) -> dict[str, Any]:
        source: dict[str, Any] = {"id": "m1", "created_at": now, "phase": "init"}
        source.update(updates)
        return source

    def migration_refused(source: dict[str, Any]) -> bool:
        try:
            _base_state(source, "a" * 64)
        except MigrationError:
            return True
        return False

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state_dir = root / ".claude" / "notes" / "milestones" / "m1"
        state_dir.mkdir(parents=True)
        now = "2026-07-12T00:00:00Z"
        old = {
            "id": "m1", "created_at": now, "updated_at": now, "phase": "complete",
            "phase_history": [{"phase": "init", "at": now}, {"phase": "complete", "at": now}],
            "rectification_commit": "abcdef1", "external_writes_required": ["git push origin HEAD:main"],
            "external_writes_completed": ["git push origin HEAD:main"], "external_writes_authorized": True,
        }
        state_path = state_dir / "state.json"
        state_path.write_text(json.dumps(old), encoding="utf-8")
        preview = migrate("m1", root, apply=False)
        expect("preview is read-only", json.loads(state_path.read_text(encoding="utf-8")).get("schema_version") is None)
        expect("legacy complete preview returns to review", preview["mapped_phase"] == "critique-running")
        applied = migrate("m1", root, apply=True)
        new = json.loads(state_path.read_text(encoding="utf-8"))
        expect("backup written", (state_dir / "state.v1.json").is_file())
        expect("schema bumped", new["schema_version"] == 2)
        expect("operations never inferred", new["operational_status"] == "pending")
        expect("terminal claim recorded", applied["terminal_claim_downgraded"] is True)
        expect("legacy ledger quarantined", "external_writes_required" not in new and new["migration"]["legacy_external_writes"]["authorized"] is True)
        expected_mapping = {
            "init": "init",
            "research-running": "research-running",
            "research-complete": "research-running",
            "implement-running": "implement-running",
            "implement-complete": "implement-running",
            "critique-running": "critique-running",
            "critique-complete": "critique-running",
            "rectify-running": "critique-running",
            "complete": "critique-running",
        }
        sparse_ok = True
        for source_phase, mapped_phase in expected_mapping.items():
            sparse = sparse_source(now, phase=source_phase)
            try:
                candidate = _base_state(sparse, "a" * 64)
            except MigrationError:
                sparse_ok = False
                break
            expected_implementation = (
                "committed"
                if source_phase in {"critique-complete", "rectify-running", "complete"}
                else "in_progress"
            )
            if (
                candidate["phase"] != mapped_phase
                or candidate["phase_history"] != [
                    {"phase": mapped_phase, "at": candidate["updated_at"]}
                ]
                or candidate["migration"]["source_phase"] != source_phase
                or candidate["implementation_status"] != expected_implementation
                or candidate["migration"]["terminal_claim_downgraded"]
                != (source_phase == "complete")
            ):
                sparse_ok = False
                break
        expect("every sparse v1 phase preserves mapped-state parity", sparse_ok)
        schema_validator = _optional_state_validator()
        if schema_validator is not None:
            schema_only_invalid = _base_state(sparse_source(now), "a" * 64)
            schema_only_invalid["review_status"] = "not-in-the-schema"
            try:
                _validate_candidate(schema_only_invalid)
            except MigrationError:
                schema_only_refused = True
            else:
                schema_only_refused = False
            expect(
                "bundled Draft 2020-12 validator rejects schema-only mutation",
                schema_only_refused,
            )
        else:
            print("  bundled Draft 2020-12 validator: unavailable (strict mirror active)")
        valid_counts = {"critical": 0, "high": 1.0, "medium": 2, "low": 3}
        try:
            valid_counts_candidate = _base_state(
                sparse_source(now, critique_finding_counts=valid_counts), "a" * 64
            )
        except MigrationError:
            valid_counts_ok = False
        else:
            valid_counts_ok = valid_counts_candidate["critique_finding_counts"] == valid_counts
        expect("schema-shaped critique finding counts accepted", valid_counts_ok)
        malformed_counts = {
            "missing severity": {"critical": 0, "high": 0, "medium": 0},
            "extra severity": {
                "critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0,
            },
            "negative count": {"critical": -1, "high": 0, "medium": 0, "low": 0},
            "fractional count": {"critical": 0.5, "high": 0, "medium": 0, "low": 0},
            "boolean count": {"critical": False, "high": 0, "medium": 0, "low": 0},
            "non-finite count": {
                "critical": float("nan"), "high": 0, "medium": 0, "low": 0,
            },
            "non-object": [0, 0, 0, 0],
        }
        for label, counts in malformed_counts.items():
            expect(
                f"malformed critique finding counts refused ({label})",
                migration_refused(sparse_source(now, critique_finding_counts=counts)),
            )
        expect(
            "duplicate critics_run refused",
            migration_refused(
                sparse_source(
                    now,
                    critics_run=["milestone-adversary", "milestone-adversary"],
                )
            ),
        )
        expect(
            "duplicate critique_files refused",
            migration_refused(
                sparse_source(now, critique_files=["docs/a.md", "docs/a.md"])
            ),
        )
        try:
            _base_state({
                "id": "m1", "created_at": now, "phase": "complete",
                "external_writes_required": "not-a-list",
            }, "a" * 64)
        except MigrationError:
            malformed_ledger_refused = True
        else:
            malformed_ledger_refused = False
        expect("malformed legacy external-write ledger refused", malformed_ledger_refused)
        try:
            _base_state({
                "id": "m1", "created_at": "2099-01-01T00:00:00Z", "phase": "init",
            }, "a" * 64)
        except MigrationError:
            future_created_refused = True
        else:
            future_created_refused = False
        expect("future legacy creation time refused", future_created_refused)
        try:
            migrate("../escape", root, apply=False)
        except MigrationError:
            invalid_refused = True
        else:
            invalid_refused = False
        expect("milestone id traversal refused", invalid_refused)
    print(f"milestone-pipeline-migrate self-test: {'OK' if failures == 0 else f'{failures} failure(s)'}")
    return 0 if failures == 0 else 1


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("milestone_id")
    parser.add_argument("--repo-root")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = migrate(
            args.milestone_id,
            _repo_root(args.repo_root),
            apply=args.apply,
        )
    except MigrationError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

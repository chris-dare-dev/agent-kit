#!/usr/bin/env python3
"""Validate and reconcile milestone-pipeline delivery-state v2 artifacts.

The pipeline's agents author evidence, reviews, and explanations.  This script
owns the deterministic claims: artifact shape, hash binding, legal delivery
evidence, freshness, append-only review/attempt history, and phase gates.

No third-party packages are used; engineer workstations and the data-lint image
both provide only the Python standard library for this surface.

Usage:
  milestone-pipeline-artifacts.py validate <kind> <path> --state STATE [--at ISO]
  milestone-pipeline-artifacts.py gate --state STATE --phase PHASE [--at ISO]
  milestone-pipeline-artifacts.py reconcile --state STATE [--at ISO]
  milestone-pipeline-artifacts.py kit-upgrade-preview --state STATE
  milestone-pipeline-artifacts.py kit-upgrade --state STATE --approved-by HUMAN --scope-hash SHA256
  milestone-pipeline-artifacts.py plan-hash <operations-plan.json>
  milestone-pipeline-artifacts.py scope-hash <operations-plan.json> <target-id>
  milestone-pipeline-artifacts.py check-run --state STATE --name NAME -- COMMAND [ARG ...]
  milestone-pipeline-artifacts.py review-append --state STATE --stage closure|operations --receipt FILE
  milestone-pipeline-artifacts.py attempt-preview --state STATE --target ID [--attempt-id ID]
  milestone-pipeline-artifacts.py attempt-start --state STATE --target ID --approved-by HUMAN --scope-hash SHA256
  milestone-pipeline-artifacts.py attempt-apply --state STATE --target ID --attempt-id ID ...
  milestone-pipeline-artifacts.py attempt-adopt-auto-sync --state STATE --target ID [--collector NAME]
  milestone-pipeline-artifacts.py attempt-verify --state STATE --target ID --attempt-id ID [--collector NAME] [--approved-by HUMAN --scope-hash SHA256]
  milestone-pipeline-artifacts.py attempt-verify-recover --state STATE --target ID --refresh-id ID
  milestone-pipeline-artifacts.py waiver-append --state STATE --target ID ...
  milestone-pipeline-artifacts.py --self-test

`gate` emits a JSON object containing hash-bound artifact receipts.  The
checkpoint writer persists those receipts in state.json in the same locked
transition that advances the phase.  A later edit therefore invalidates every
downstream gate instead of silently replacing already-approved evidence.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

# The file-locking primitives live in the sibling workspace-tooling tree: one
# definition, shared by both trees, so data/scripts and the substrate cannot
# drift apart (M2, gates-green-t-fcntl-datascripts). The path is derived from
# __file__ rather than guessed from the CWD -- these scripts are invoked from
# runbooks, from the gate runner and (from M3) from CI, none of which promise a
# working directory.
_WORKSPACE_TOOLING = Path(__file__).resolve().parents[2] / "workspace-tooling"
if str(_WORKSPACE_TOOLING) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_TOOLING))
import platform_compat  # noqa: E402


SCRIPT_ROOT = Path(__file__).resolve().parents[2]
AGENT_KIT_ROOT = SCRIPT_ROOT
PIPELINE_KIT_PATHS = (
    "data/agents",
    "data/commands/milestone-pipeline.md",
    "data/provider-adapters/codex/entrypoints/milestone-pipeline",
    "data/references/milestone-pipeline-agent-contract.md",
    "data/references/milestone-pipeline-artifacts-v2.md",
    "data/references/milestone-pipeline-critic-modes.md",
    "data/references/milestone-pipeline-critique-format.md",
    "data/references/milestone-pipeline-findings-schema.md",
    "data/references/milestone-pipeline-phase-critique.md",
    "data/references/milestone-pipeline-phase-implement.md",
    "data/references/milestone-pipeline-phase-rectify.md",
    "data/references/milestone-pipeline-state-schema.md",
    "data/references/pipeline-pattern-v2.md",
    "data/schemas",
    "data/scripts/milestone-pipeline-artifacts.py",
    "data/scripts/milestone-pipeline-checkpoint.py",
    "data/scripts/milestone-pipeline-init-state.sh",
    "data/scripts/milestone-pipeline-migrate.py",
    "data/scripts/milestone-pipeline-schema-check.py",
    "data/scripts/milestone-pipeline-status.sh",
    "data/scripts/milestone-render-provenance.py",
    "data/model-policy.json",
    "data/facts/catalog.json",
)
ALLOW_LOCAL_DELIVERY_ENDPOINTS = False
TEST_ARTIFACT_RESOLVER: tuple[str, str] | None = None
TEST_FAIL_AFTER_ARTIFACT_WRITE = False
TEST_FAIL_AFTER_CHECK_EVIDENCE_WRITE = False
TEST_FAIL_AFTER_APPLY_INTENT = False
TEST_FAIL_AFTER_REFRESH_INTENT = False
ALLOW_TEST_OPERATION_EXECUTABLES = False
_WRITER_VERSION_CACHE: dict[tuple[str, ...], set[str]] = {}
MAX_CAPTURE_BYTES = 262_144
STATE_SCHEMA_VERSION = 2
ARTIFACT_SCHEMA_VERSION = 2
HEX_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MILESTONE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

POINTERS = {
    "review_manifest": "review-manifest.json",
    "implementation_evidence": "implementation-evidence.json",
    "publication_intent": "publication-intent.json",
    "release_manifest": "release-manifest.json",
    "operations_plan": "operations-plan.json",
    "operations_evidence": "operations-evidence.json",
    "waivers": "waivers.json",
}

ALWAYS_REVIEWERS = {
    "milestone-adversary",
    "milestone-delivery-integrity-adversary",
}

ARTIFACTS_FOR_PHASE = {
    "critique-complete": {"review_manifest"},
    "code-complete": {"review_manifest", "implementation_evidence"},
    "publish-running": {"review_manifest", "implementation_evidence"},
    "published": {"publication_intent", "release_manifest"},
    "plan-reviewed": {
        "review_manifest", "publication_intent", "release_manifest", "operations_plan"
    },
    "apply-running": {"operations_plan"},
    "applied": {"operations_evidence"},
    "operationally-verified": {"operations_evidence", "waivers"},
    "complete": set(POINTERS),
}

PHASE_EDGES: dict[str, set[str]] = {
    "init": {"research-running"},
    "research-running": {"research-complete"},
    "research-complete": {"implement-running"},
    "implement-running": {"implement-complete"},
    "implement-complete": {"critique-running"},
    "critique-running": {"critique-complete"},
    "critique-complete": {"rectify-running"},
    "rectify-running": {"code-complete"},
    "code-complete": {"publish-running", "complete"},
    "publish-running": {"published"},
    "published": {"plan-review-running", "complete"},
    "plan-review-running": {"plan-reviewed"},
    "plan-reviewed": {"apply-running"},
    "apply-running": {"applied"},
    "applied": {"verify-running", "apply-running"},
    "verify-running": {"operationally-verified", "apply-running"},
    "operationally-verified": {"complete", "verify-running"},
    "complete": {"verify-running"},
}
STATE_PHASES = set(PHASE_EDGES)
MIGRATION_SOURCE_PHASES = {
    "init", "research-running", "research-complete", "implement-running",
    "implement-complete", "critique-running", "critique-complete",
    "rectify-running", "complete",
}
MIGRATION_PHASE_MAP = {
    "research-complete": "research-running",
    "implement-complete": "implement-running",
    "critique-complete": "critique-running",
    "rectify-running": "critique-running",
    "complete": "critique-running",
}
STATE_ALLOWED_FIELDS = {
    "_state_path", "_loaded_state_sha256", "schema_version", "id", "created_at", "updated_at", "phase", "phase_history",
    "agent_kit_commit", "kit_upgrade_history", "check_run_head", "check_run_hashes", "check_run_history",
    "check_run_attempts",
    "milestone_brief", "research_mode", "research_briefs", "research_synthesis",
    "implementation_path", "implementation_specialist", "implementation_base",
    "implementation_commit_range", "implementation_commits", "implementation_branch",
    "critique_path", "critics_run", "critique_files", "critique_finding_counts",
    "findings_register", "rectification_commit", "rectification_not_required_reason",
    "fixed_findings", "deferred_findings", "invalidated_findings", "regression_tests_added",
    "publication_required", "publication_not_required_reason", "operations_required",
    "operations_not_required_reason", "implementation_status", "operational_status",
    "review_status", "review_manifest", "implementation_evidence", "publication_intent",
    "release_manifest",
    "operations_plan", "operations_evidence", "waivers", "artifact_bindings", "migration",
}


class ValidationError(Exception):
    """A fail-closed artifact or reconciliation error."""


def _fail(message: str) -> None:
    raise ValidationError(message)


def _expect(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


def _redact_output(value: bytes) -> str:
    text = value.decode("utf-8", errors="replace")
    patterns = (
        (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"), r"\1[REDACTED]"),
        (re.compile(
            r"(?i)((?:secret|token|password|passwd|api[_-]?key|private[_-]?key|"
            r"client[_-]?secret|access[_-]?key|cookie)\s*[:=]\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
        ), r"\1[REDACTED]"),
        (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_ACCESS_KEY]"),
        (re.compile(
            r"-----BEGIN [^-]+-----.*?-----END [^-]+-----",
            re.DOTALL,
        ), "[REDACTED_PEM]"),
        (re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ), "[REDACTED_JWT]"),
    )
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text


def _persisted_text_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_bounded_process(
    argv: list[str], *, cwd: Path, timeout: int, env: dict[str, str]
) -> dict[str, Any]:
    """Run with bounded in-memory capture and redact before persistence.

    Reader threads continue draining after the capture limit, preventing a
    noisy child from deadlocking while ensuring the agent never holds or writes
    unbounded output.
    """
    proc = subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        start_new_session=True,
    )
    streams: dict[str, dict[str, Any]] = {
        "stdout": {"buffer": bytearray(), "sha": hashlib.sha256(), "truncated": False},
        "stderr": {"buffer": bytearray(), "sha": hashlib.sha256(), "truncated": False},
    }

    def drain(name: str, stream: Any) -> None:
        slot = streams[name]
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                break
            slot["sha"].update(chunk)
            remaining = MAX_CAPTURE_BYTES - len(slot["buffer"])
            if remaining > 0:
                slot["buffer"].extend(chunk[:remaining])
            if len(chunk) > remaining:
                slot["truncated"] = True
        stream.close()

    threads = [
        threading.Thread(target=drain, args=(name, stream), daemon=True)
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        return_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGKILL)
        return_code = proc.wait()
    for thread in threads:
        thread.join(timeout=0.25)
    background_processes_terminated = any(thread.is_alive() for thread in threads)
    if background_processes_terminated:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for thread in threads:
            thread.join(timeout=2)
        _expect(not any(thread.is_alive() for thread in threads),
                "bounded process: descendant retained output pipes after group termination")
    exit_code = return_code if return_code >= 0 else 128 + abs(return_code)
    overflow = streams["stdout"]["truncated"] or streams["stderr"]["truncated"]
    if timed_out:
        exit_code = 124
    elif overflow and exit_code == 0:
        exit_code = 125
    elif background_processes_terminated and exit_code == 0:
        exit_code = 125
    return {
        "exit_code": exit_code,
        "stdout_bytes": bytes(streams["stdout"]["buffer"]),
        "stderr_bytes": bytes(streams["stderr"]["buffer"]),
        "stdout_sha256": streams["stdout"]["sha"].hexdigest(),
        "stderr_sha256": streams["stderr"]["sha"].hexdigest(),
        "stdout_truncated": streams["stdout"]["truncated"],
        "stderr_truncated": streams["stderr"]["truncated"],
        "timed_out": timed_out,
        "background_processes_terminated": background_processes_terminated,
    }


def _strict_keys(obj: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(obj) - allowed)
    _expect(not unknown, f"{label}: unknown field(s): {', '.join(unknown)}")


def _require_keys(obj: dict[str, Any], required: set[str], label: str) -> None:
    missing = sorted(required - set(obj))
    _expect(not missing, f"{label}: missing field(s): {', '.join(missing)}")


def _nonempty_string(value: Any, label: str) -> str:
    _expect(isinstance(value, str) and bool(value.strip()), f"{label}: expected non-empty string")
    return value.strip()


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    _expect(isinstance(value, int) and not isinstance(value, bool), f"{label}: expected integer")
    _expect(value >= minimum, f"{label}: expected integer >= {minimum}")
    return value


def _command_argv(value: Any, label: str, *, operational: bool = True) -> list[str]:
    _expect(isinstance(value, list) and bool(value), f"{label}: expected non-empty argv array")
    argv = [_nonempty_string(part, f"{label}[{i}]") for i, part in enumerate(value)]
    _expect(not argv[0].startswith("-"), f"{label}[0]: option-like executable forbidden")
    executable = Path(argv[0]).name.casefold()
    trivial_or_self_reporting = {
        "true", "false", "printf", "echo", "test", "[", "bash", "sh", "zsh",
        "fish", "python", "python3", "node", "ruby", "perl", "env",
    }
    if operational:
        _expect(executable not in trivial_or_self_reporting,
                f"{label}[0]: trivial, shell, or self-reporting executable {executable!r} forbidden")
    return argv


def _check_command_argv(value: Any, label: str) -> list[str]:
    """Validate a project check without forbidding normal language runners."""
    argv = _command_argv(value, label, operational=False)
    executable = Path(argv[0]).name.casefold()
    _expect(
        executable not in {"true", "false", "printf", "echo", "test", "[", "bash", "sh", "zsh", "fish"},
        f"{label}[0]: trivial or shell-wrapper check executable {executable!r} forbidden",
    )
    if executable in {"python", "python3", "node", "ruby", "perl"} and len(argv) > 1:
        _expect(
            argv[1] not in {"-c", "-e", "--eval", "--print"},
            f"{label}: inline self-reporting program forbidden; execute a reviewed file/module",
        )
    _reject_secret_argv(argv, label)
    return argv


def _reject_secret_argv(argv: list[str], label: str) -> None:
    secret_flags = re.compile(
        r"^--?(?:token|password|passwd|secret|api[-_]?key|private[-_]?key|"
        r"client[-_]?secret|access[-_]?key|credential|cookie|authorization)(?:=|$)",
        re.IGNORECASE,
    )
    secret_value = re.compile(
        r"(?i)(?:^|\s)bearer\s+\S+|AKIA[0-9A-Z]{16}|"
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|"
        r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]+:[^/@\s]+@",
    )
    for index, part in enumerate(argv):
        _expect(not secret_flags.search(part),
                f"{label}[{index}]: inline secret-bearing flags are forbidden; use a "
                "non-persisted workload identity or config-file reference")
        _expect(not secret_value.search(part),
                f"{label}[{index}]: value appears to contain credential material")
        if part.casefold().startswith(("http://", "https://")):
            _validated_remote_url(part, f"{label}[{index}]")


def _resolved_executable(
    argv: list[str], label: str, *, require_absolute: bool = False,
    expected_sha256: str | None = None,
) -> tuple[str, str]:
    raw = argv[0]
    if require_absolute:
        _expect(os.path.isabs(raw), f"{label}[0]: operational executable must be absolute")
        resolved = Path(raw).resolve()
    else:
        found = shutil.which(raw)
        _expect(found is not None, f"{label}[0]: executable not found: {raw!r}")
        resolved = Path(found).resolve()
    _expect(resolved.is_file() and os.access(resolved, os.X_OK),
            f"{label}[0]: executable is missing or not executable: {resolved}")
    digest = _file_sha(resolved)
    if expected_sha256 is not None:
        expected = _sha256_value(expected_sha256, f"{label}.executable_sha256")
        _expect(digest == expected,
                f"{label}: executable bytes changed for {resolved}")
    return str(resolved), digest


def _contract_by_kind(target: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["kind"]: item for item in target["verification_contract"]}


def _contract_kinds(target: dict[str, Any]) -> set[str]:
    return set(_contract_by_kind(target))


def _allowed_state_kit_commits(state: dict[str, Any]) -> set[str]:
    allowed = {_commit(state.get("agent_kit_commit"), "state.agent_kit_commit")}
    for i, upgrade in enumerate(state.get("kit_upgrade_history") or []):
        allowed.add(_commit(
            upgrade.get("from_commit"), f"state.kit_upgrade_history[{i}].from_commit"
        ))
        allowed.add(_commit(
            upgrade.get("to_commit"), f"state.kit_upgrade_history[{i}].to_commit"
        ))
    return allowed


def _iso(value: Any, label: str) -> datetime:
    raw = _nonempty_string(value, label)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        _fail(f"{label}: invalid ISO-8601 timestamp: {exc}")
    _expect(parsed.tzinfo is not None, f"{label}: timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _now(value: str | None) -> datetime:
    return _iso(value, "--at") if value else datetime.now(timezone.utc)


def _commit(value: Any, label: str) -> str:
    raw = _nonempty_string(value, label)
    _expect(bool(HEX_COMMIT_RE.fullmatch(raw)), f"{label}: expected 7-64 hexadecimal characters")
    return raw.lower()


def _sha256_value(value: Any, label: str) -> str:
    raw = _nonempty_string(value, label).lower()
    _expect(bool(SHA256_RE.fullmatch(raw)), f"{label}: expected lowercase sha256 hex")
    return raw


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _review_projection(data: dict[str, Any], stage: str) -> dict[str, Any]:
    _expect(stage in {"assessment", "code"}, f"review projection: unknown stage {stage!r}")
    excluded = {"operations_reviews"}
    if stage == "assessment":
        excluded.add("closure_reviews")
    return {key: value for key, value in data.items() if key not in excluded}


def _delivery_requirements(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "publication_required": state.get("publication_required"),
        "publication_not_required_reason": state.get("publication_not_required_reason"),
        "operations_required": state.get("operations_required"),
        "operations_not_required_reason": state.get("operations_not_required_reason"),
    }


def _value_sha(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _tree_sha256(root: Path) -> str:
    entries: list[str] = []
    for current_raw, dirs, files in os.walk(root, followlinks=False):
        current = Path(current_raw)
        for name in sorted([*dirs, *files]):
            path = current / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append(f"L {rel}\0{os.readlink(path)}")
            elif path.is_dir():
                entries.append(f"D {rel}")
            elif path.is_file():
                entries.append(f"F {rel}\0{_file_sha(path)}")
            else:
                entries.append(f"O {rel}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(f"{label}: file not found: {path}")
    except json.JSONDecodeError as exc:
        _fail(f"{label}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
    except OSError as exc:
        _fail(f"{label}: cannot read {path}: {exc}")
    _expect(isinstance(value, dict), f"{label}: root must be an object")
    return value


def _load_state(state_path: Path, label: str = "state") -> dict[str, Any]:
    state = _load_json(state_path, label)
    state["_loaded_state_sha256"] = _file_sha(state_path)
    return state


def _persisted_state(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if not key.startswith("_")}


def _state_dir(state_path: Path) -> Path:
    return state_path.resolve().parent


def _repo_root(state_path: Path) -> Path:
    # <repo>/.claude/notes/milestones/<id>/state.json
    resolved = state_path.resolve()
    _expect(len(resolved.parents) >= 5, f"state path is not under <repo>/.claude/notes/milestones/<id>: {resolved}")
    root = resolved.parents[4]
    expected = root / ".claude" / "notes" / "milestones" / resolved.parent.name / "state.json"
    _expect(expected == resolved, f"state path is not canonical: {resolved}")
    return root


def _canonical_workspace_root() -> Path:
    """Derive the workspace from the trusted installed kit, never the target.

    A repository under review can add arbitrary marker directories.  Starting
    this search from ``repo`` therefore lets reviewed code replace reviewer
    configuration.  The canonical workspace installation instead links
    ``<workspace>/.claude/{scripts,agents}`` to this versioned kit.  Requiring
    both backlinks makes the workspace identity a property of the installed
    kit/root contract rather than of the target diff.
    """
    kit = AGENT_KIT_ROOT.resolve()
    scripts = (kit / "data" / "scripts").resolve()
    agents = (kit / "data" / "agents").resolve()
    matches: list[Path] = []
    for candidate in kit.parents:
        try:
            linked_scripts = (candidate / ".claude" / "scripts").resolve(strict=True)
            linked_agents = (candidate / ".claude" / "agents").resolve(strict=True)
        except FileNotFoundError:
            continue
        if (
            linked_scripts == scripts
            and linked_agents == agents
            and (candidate / ".codex" / "agents").is_dir()
            and (candidate / "AGENTS.md").is_file()
            and (candidate / "CLAUDE.md").is_file()
        ):
            matches.append(candidate.resolve())
    _expect(
        len(matches) == 1,
        "cannot derive exactly one canonical workspace whose .claude scripts/agents "
        "resolve to the frozen agent kit",
    )
    return matches[0]


def _safe_artifact_path(state_path: Path, state: dict[str, Any], kind: str) -> Path:
    pointer = _nonempty_string(state.get(kind), f"state.{kind}")
    _expect(not os.path.isabs(pointer), f"state.{kind}: absolute paths are forbidden")
    base = _state_dir(state_path)
    artifact_root = (base / "artifacts").resolve()
    candidate = (base / pointer).resolve()
    try:
        candidate.relative_to(artifact_root)
    except ValueError:
        _fail(f"state.{kind}: pointer escapes the artifacts directory: {pointer}")
    _expect(candidate.is_file(), f"state.{kind}: artifact is missing: {candidate}")
    return candidate


def _artifact_envelope(
    data: dict[str, Any], label: str, milestone_id: str, now: datetime | None = None
) -> None:
    _expect(data.get("schema_version") == ARTIFACT_SCHEMA_VERSION,
            f"{label}.schema_version: expected {ARTIFACT_SCHEMA_VERSION}")
    _expect(data.get("milestone_id") == milestone_id,
            f"{label}.milestone_id: expected {milestone_id!r}, got {data.get('milestone_id')!r}")
    _integer(data.get("generation"), f"{label}.generation", 1)
    created = _iso(data.get("created_at"), f"{label}.created_at")
    if now is not None:
        _expect(created <= now, f"{label}.created_at: future-dated artifact forbidden")
    producer = data.get("producer")
    _expect(isinstance(producer, dict), f"{label}.producer: expected object")
    _strict_keys(producer, {"kind", "name", "provider", "version"}, f"{label}.producer")
    _require_keys(producer, {"kind", "name", "provider"}, f"{label}.producer")
    _expect(producer.get("kind") in {"deterministic-tool", "agent", "human"},
            f"{label}.producer.kind: invalid value")
    for field in ("name", "provider"):
        _nonempty_string(producer.get(field), f"{label}.producer.{field}")
    if producer.get("version") is not None:
        _expect(isinstance(producer.get("version"), str),
                f"{label}.producer.version: expected string or null")


def _validate_evidence_ref(value: Any, label: str, evidence_root: Path | None = None) -> None:
    _expect(isinstance(value, dict), f"{label}: expected object")
    _strict_keys(value, {"path", "sha256", "media_type", "size_bytes", "collector", "command"}, label)
    _require_keys(value, {"path", "sha256", "media_type", "size_bytes", "collector"}, label)
    rel = _nonempty_string(value.get("path"), f"{label}.path")
    digest = _sha256_value(value.get("sha256"), f"{label}.sha256")
    _nonempty_string(value.get("media_type"), f"{label}.media_type")
    size = _integer(value.get("size_bytes"), f"{label}.size_bytes", 0)
    _nonempty_string(value.get("collector"), f"{label}.collector")
    if value.get("command") is not None:
        _nonempty_string(value.get("command"), f"{label}.command")
    if evidence_root is not None:
        _expect(not os.path.isabs(rel), f"{label}.path: absolute evidence paths are forbidden")
        root = (evidence_root / "artifacts").resolve()
        path = (evidence_root / rel).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            _fail(f"{label}.path: evidence escapes the milestone artifacts directory")
        _expect(path.is_file(), f"{label}.path: evidence file is missing: {path}")
        _expect(path.stat().st_size == size,
                f"{label}.size_bytes: recorded {size}, actual {path.stat().st_size}")
        _expect(_file_sha(path) == digest, f"{label}.sha256: evidence file content changed")


def _validate_frozen_command_record(
    ref: Any, expected_argv: list[str], expected_environment: dict[str, str],
    label: str, evidence_root: Path | None, *,
    expected_output_policy: str | None = None,
    expected_observed: dict[str, Any] | None = None,
    expected_probe_kind: str | None = None,
    expected_target: dict[str, Any] | None = None,
) -> int | None:
    _validate_evidence_ref(ref, label, evidence_root)
    _expect(ref.get("media_type") == "application/json",
            f"{label}.media_type: deterministic JSON required")
    _expect(ref.get("command") == shlex.join(expected_argv),
            f"{label}.command: frozen command mismatch")
    if evidence_root is None:
        return None
    record = _load_json(evidence_root / ref["path"], f"{label} record")
    keys = {
        "argv", "environment", "exit_code", "stderr", "stdout", "stdout_sha256",
        "stderr_sha256", "stdout_truncated", "stderr_truncated", "output_limit_bytes",
        "background_processes_terminated", "timed_out", "output_capture_policy",
    }
    _strict_keys(record, keys, f"{label} record")
    _require_keys(record, keys, f"{label} record")
    _expect(record.get("argv") == expected_argv, f"{label} record.argv: mismatch")
    _expect(record.get("environment") == expected_environment,
            f"{label} record.environment: frozen environment mismatch")
    exit_code = _integer(record.get("exit_code"), f"{label} record.exit_code")
    _expect(isinstance(record.get("stdout"), str) and isinstance(record.get("stderr"), str),
            f"{label} record: stdout/stderr must be strings")
    policy = record.get("output_capture_policy")
    _expect(policy in {
        "omitted", "projected-observed-identity", "projected-verification-fact",
        "projected-auto-sync-adoption",
    },
            f"{label} record.output_capture_policy: invalid")
    if expected_output_policy is not None:
        _expect(policy == expected_output_policy,
                f"{label} record.output_capture_policy: expected {expected_output_policy}")
    if policy == "omitted":
        _expect(record["stdout"] == "" and record["stderr"] == "",
                f"{label} record: omitted output must not persist command bytes")
    elif policy == "projected-observed-identity":
        _expect(record["stderr"] == "",
                f"{label} record: projected identity must omit stderr")
        try:
            projected = json.loads(record["stdout"])
        except json.JSONDecodeError as exc:
            _fail(f"{label} record: projected identity is invalid JSON: {exc}")
        _expect(isinstance(projected, dict)
                and set(projected) <= {
                    "source_commit", "render_commit", "image_digest", "generation"
                }, f"{label} record: projected identity contains undeclared fields")
        if expected_observed is not None:
            _expect(projected == expected_observed,
                    f"{label} record: projected identity differs from claimed observation")
    elif policy == "projected-verification-fact":
        _expect(record["stderr"] == "",
                f"{label} record: projected verification fact must omit stderr")
        try:
            fact = json.loads(record["stdout"])
        except json.JSONDecodeError as exc:
            _fail(f"{label} record: projected verification fact is invalid JSON: {exc}")
        _expect(isinstance(fact, dict) and fact.get("kind") == expected_probe_kind,
                f"{label} record: projected verification fact kind mismatch")
        if exit_code == 0 and not ALLOW_TEST_OPERATION_EXECUTABLES:
            _expect(expected_target is not None,
                    f"{label} record: typed target required for successful probe")
            desired = expected_target["desired"]
            profile = expected_target["verification_profile"]
            if expected_probe_kind == "argocd-synced":
                _expect(set(fact) == {
                    "kind", "sync_status", "health_status", "revision"
                } and fact["sync_status"] == "Synced"
                    and fact["health_status"] == "Healthy"
                    and fact["revision"] == desired["render_commit"],
                    f"{label} record: Argo sync fact does not satisfy desired revision")
            elif expected_probe_kind == "deployment-observed-generation":
                _expect(set(fact) == {
                    "kind", "generation", "observed_generation", "available_replicas",
                    "deployment_uid", "pod_selector"
                } and isinstance(fact["generation"], int) and fact["generation"] >= 1
                    and isinstance(fact["observed_generation"], int)
                    and fact["observed_generation"] >= fact["generation"]
                    and isinstance(fact["available_replicas"], int)
                    and fact["available_replicas"] > 0
                    and fact["deployment_uid"] == profile["deployment_uid"]
                    and fact["pod_selector"] == profile["pod_selector"],
                    f"{label} record: Deployment generation fact does not satisfy desired state")
            elif expected_probe_kind == "pod-image-digest":
                _expect(set(fact) == {
                    "kind", "pod_count", "all_ready", "container_name", "pod_selector",
                    "image_digests"
                } and isinstance(fact["pod_count"], int) and fact["pod_count"] > 0
                    and fact["all_ready"] is True
                    and fact["container_name"] == profile["container_name"]
                    and fact["pod_selector"] == profile["pod_selector"]
                    and fact["image_digests"] == [desired["image_digest"]],
                    f"{label} record: application container digest fact is not desired/ready")
            elif expected_probe_kind == "service-selects-workload":
                _expect(set(fact) == {
                    "kind", "service_uid", "pod_selector", "service_port"
                } and fact["service_uid"] == profile["service_uid"]
                    and fact["pod_selector"] == profile["pod_selector"]
                    and fact["service_port"] == profile["service_port"],
                    f"{label} record: Service does not select the reviewed workload")
            elif expected_probe_kind == "ingress-routes-service":
                smoke_host = urlparse(profile["behavioral_smoke_url"]).hostname
                _expect(set(fact) == {
                    "kind", "ingress_uid", "host", "path", "service_name",
                    "service_port"
                } and fact["ingress_uid"] == profile["ingress_uid"]
                    and fact["host"] == smoke_host
                    and fact["path"] == profile["ingress_path"]
                    and fact["service_name"] == profile["service_name"]
                    and fact["service_port"] == profile["service_port"],
                    f"{label} record: Ingress does not route the smoke endpoint to the Service")
            elif expected_probe_kind == "behavioral-smoke":
                _expect(set(fact) == {"kind", "http_status"}
                        and fact["http_status"] == profile["behavioral_smoke_status"],
                        f"{label} record: smoke HTTP status differs from reviewed contract")
            else:
                _validate_internal_probe_projection(
                    fact, expected_probe_kind, expected_target, label
                )
    else:
        _expect(record["stderr"] == "" and expected_target is not None,
                f"{label} record: auto-sync adoption requires a typed target and omitted stderr")
        try:
            adoption = json.loads(record["stdout"])
        except json.JSONDecodeError as exc:
            _fail(f"{label} record: projected auto-sync adoption is invalid JSON: {exc}")
        if exit_code == 0:
            _expect(isinstance(adoption, dict) and set(adoption) == {
                "kind", "sync_status", "health_status", "revision", "observed"
            } and adoption["kind"] == "observed-auto-sync-v1",
                    f"{label} record: malformed auto-sync adoption projection")
            if expected_observed is not None:
                _expect(adoption["observed"] == expected_observed,
                        f"{label} record: adopted identity differs from claimed observation")
            _expect(adoption["sync_status"] == "Synced"
                    and adoption["health_status"] == "Healthy"
                    and adoption["revision"] == expected_target["desired"]["render_commit"],
                    f"{label} record: auto-sync did not converge at the exact desired render")
        else:
            _expect(adoption == {} and expected_observed is None,
                    f"{label} record: failed auto-sync observation cannot invent an adoption")
    _expect(record["stdout"] == _redact_output(record["stdout"].encode("utf-8"))
            and record["stderr"] == _redact_output(record["stderr"].encode("utf-8")),
            f"{label} record: secret-bearing output was persisted")
    _expect(
        _sha256_value(record.get("stdout_sha256"), f"{label} record.stdout_sha256")
        == _persisted_text_sha(record["stdout"])
        and _sha256_value(record.get("stderr_sha256"), f"{label} record.stderr_sha256")
        == _persisted_text_sha(record["stderr"]),
        f"{label} record: persisted output hash mismatch",
    )
    for key in (
        "stdout_truncated", "stderr_truncated", "background_processes_terminated",
        "timed_out",
    ):
        _expect(isinstance(record.get(key), bool), f"{label} record.{key}: expected bool")
    _expect(record.get("output_limit_bytes") == MAX_CAPTURE_BYTES,
            f"{label} record.output_limit_bytes: mismatch")
    return exit_code


def _validate_internal_probe_projection(
    fact: dict[str, Any], kind: str | None, target: dict[str, Any], label: str,
) -> None:
    """Validate persisted projections for the internal and east-west collectors."""
    profile = target["verification_profile"]
    if kind == "endpointslice-ready-backends":
        _expect(set(fact) == {
            "kind", "ready_endpoint_count", "target_uids", "service_name"
        } and isinstance(fact["ready_endpoint_count"], int)
            and fact["ready_endpoint_count"] > 0
            and isinstance(fact["target_uids"], list) and bool(fact["target_uids"])
            and fact["service_name"] == profile["service_name"],
                f"{label} record: EndpointSlice projection lacks ready reviewed backends")
    elif kind == "istio-probe-origin-ready":
        origin = profile["probe_origin"]
        _expect(set(fact) == {
            "kind", "pod_uid", "service_account_name", "app_ready", "proxy_ready",
            "container_image_digest",
        } and fact["pod_uid"] == origin["pod_uid"]
            and fact["service_account_name"] == origin["service_account_name"]
            and fact["app_ready"] is True and fact["proxy_ready"] is True
            and fact["container_image_digest"] == origin["container_image_digest"],
                f"{label} record: in-mesh probe origin identity/readiness mismatch")
    elif kind == "receiver-gateway-proxy-ready":
        receiver = profile["receiver"]
        _expect(set(fact) == {"kind", "pod_uid", "proxy_ready"}
                and fact["pod_uid"] == receiver["gateway_proxy_uid"]
                and fact["proxy_ready"] is True,
                f"{label} record: receiver gateway proxy identity/readiness mismatch")
    elif kind in {"sender-serviceentry-route-exact", "receiver-serviceentry-route-exact"}:
        side = profile["sender"] if kind.startswith("sender-") else profile["receiver"]
        endpoint_host = (
            side["eastwest_endpoint_host"] if kind.startswith("sender-")
            else profile["receiver"]["local_service_host"]
        )
        endpoint_port = (
            side["eastwest_endpoint_port"] if kind.startswith("sender-")
            else profile["receiver"]["local_service_port"]
        )
        _expect(set(fact) == {
            "kind", "uid", "host", "endpoint_host", "endpoint_port"
        } and fact["uid"] == side["service_entry_uid"]
            and fact["host"] == profile["global_service_host"]
            and fact["endpoint_host"] == endpoint_host
            and fact["endpoint_port"] == endpoint_port,
                f"{label} record: ServiceEntry projection differs from reviewed route")
    elif kind in {"sender-destinationrule-mtls-exact", "receiver-destinationrule-mtls-exact"}:
        sender = kind.startswith("sender-")
        side = profile["sender"] if sender else profile["receiver"]
        expected_host = profile["global_service_host"] if sender else side["local_service_host"]
        _expect(set(fact) == {"kind", "uid", "host", "tls_mode"}
                and fact["uid"] == side["destination_rule_uid"]
                and fact["host"] == expected_host and fact["tls_mode"] == "ISTIO_MUTUAL",
                f"{label} record: DestinationRule projection is not exact mTLS")
    elif kind == "receiver-envoyfilter-cluster-exact":
        receiver = profile["receiver"]
        _expect(set(fact) == {
            "kind", "uid", "cluster_name", "local_service_host", "local_service_port"
        } and fact["uid"] == receiver["envoy_filter_uid"]
            and fact["cluster_name"] == receiver["envoy_cluster_name"]
            and fact["local_service_host"] == receiver["local_service_host"]
            and fact["local_service_port"] == receiver["local_service_port"],
                f"{label} record: receiver EnvoyFilter projection differs from reviewed cluster")
    elif kind in {"sender-istio-xds-synced", "receiver-istio-xds-synced"}:
        side = profile["sender"] if kind.startswith("sender-") else profile["receiver"]
        if kind.startswith("sender-"):
            proxy = f"{side['proxy_pod']}.{profile['probe_origin']['namespace']}"
        else:
            proxy = f"{side['gateway_proxy_pod']}.{side['namespace']}"
        _expect(set(fact) == {"kind", "proxy", "xds_statuses"}
                and fact["proxy"] == proxy and isinstance(fact["xds_statuses"], list)
                and bool(fact["xds_statuses"])
                and all(str(status).casefold() == "synced" for status in fact["xds_statuses"]),
                f"{label} record: xDS projection is not fully synced")
    elif kind in {
        "sender-istio-cluster-healthy-endpoints",
        "receiver-istio-cluster-healthy-endpoints",
    }:
        _expect(set(fact) == {"kind", "cluster_name", "healthy_endpoints"}
                and fact["cluster_name"] == profile["receiver"]["envoy_cluster_name"]
                and isinstance(fact["healthy_endpoints"], int)
                and fact["healthy_endpoints"] > 0,
                f"{label} record: Envoy cluster lacks healthy endpoints")
    elif kind in {"internal-behavioral-smoke", "eastwest-behavioral-smoke"}:
        _expect(set(fact) == {"kind", "http_status"}
                and fact["http_status"] == profile["behavioral_smoke_status"],
                f"{label} record: internal HTTP status differs from reviewed contract")
    else:
        _fail(f"{label} record: unsupported successful verification kind {kind!r}")


def _git_output(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if proc.returncode != 0:
        _fail(f"git {' '.join(args)} failed in {repo}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _worktree_status(repo: Path, state_path: Path) -> str:
    """Return disallowed tracked/untracked worktree state.

    Milestone state and generated receipts are intentionally untracked and may
    change while a deterministic check runs.  Every other untracked file is an
    undeclared check input and is therefore rejected, closing the common
    ``test -f untracked-token`` bypass.  Ignored files remain outside Git's
    declared source contract and must not be used by project checks.
    """
    tracked = _git_output(repo, "status", "--porcelain", "--untracked-files=no").decode(
        "utf-8", errors="surrogateescape"
    )
    raw_untracked = _git_output(repo, "ls-files", "--others", "--exclude-standard", "-z")
    state_root = _state_dir(state_path).resolve()
    disallowed: list[str] = []
    for raw in raw_untracked.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="surrogateescape")
        candidate = (repo / rel).resolve()
        try:
            candidate.relative_to(state_root)
        except ValueError:
            disallowed.append(rel)
    return tracked + "".join(f"?? {path}\n" for path in sorted(disallowed))


def _diff_sha(repo: Path, base: str, head: str) -> str:
    return hashlib.sha256(_git_output(repo, "diff", "--binary", "--full-index", f"{base}..{head}")).hexdigest()


def _infra_topology_repo(repo: Path, remote: str) -> bool:
    """True when the repo is an infra-group clone regardless of its own tracked paths.

    Repos cloned under ``platform/infra/<name>/`` (e.g. ``ops-infra``) author live
    Kargo/Pulumi control-plane resources, but their tracked paths are repo-relative
    (``stacks/…``, ``docs/…``) — so neither the ``infra_names`` identity set nor the
    ``^infra/`` changed-path heuristic fires, structurally exempting every such
    milestone from the infra-safety review lane. Two topology signals close the gap:
    the GitLab group path (``…/example-org/platform/infra/<name>(.git)``) and the
    local clone's parent directory (``platform/infra/<name>/``). This is future-proof:
    any new repo added to the ``infra/`` group is covered without editing a name list.
    """
    if re.search(r"/infra/[^/]+?(?:\.git)?/?$", remote):
        return True
    try:
        return repo.resolve().parent.name == "infra"
    except OSError:
        return False


def _required_reviewers(repo: Path, base: str, head: str) -> set[str]:
    names = set(ALWAYS_REVIEWERS)
    changed = _git_output(repo, "diff", "--name-only", f"{base}..{head}").decode(errors="replace").splitlines()
    if any(re.search(
        r"(^|/)(frontend|web|ui|templates?|\.obsidian)/|"
        r"\.(?:[tj]sx?|vue|svelte|astro|css|scss|sass|less|html?|hbs|njk|canvas|base)$|"
        r"\.excalidraw\.md$|(?:^|/)roadmap_status_excalidraw\.py$",
        p,
        re.IGNORECASE,
    ) for p in changed):
        names.add("milestone-frontend-ux")
    infra_names = {
        "istio-system", "istio-gateway", "cert-manager", "aws-pca-issuer",
        "platform-infra", "crossplane", "kargo", "keycloak",
    }
    remote = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote_infra = any(re.search(rf"/{re.escape(name)}(?:\.git)?$", remote) for name in infra_names)
    if (
        repo.name in infra_names
        or remote_infra
        or _infra_topology_repo(repo, remote)
        or any(
            re.search(
                r"^infra/|^charts/(istio-system|istio-gateway|cert-manager|aws-pca-issuer|crossplane|kargo|keycloak)/|^pkg/irsa/",
                p,
            )
            for p in changed
        )
    ):
        names.add("milestone-infra-safety")
    return names


def _validate_review_receipt(
    receipt: Any,
    label: str,
    repo: Path,
    state: dict[str, Any],
    expected_base: str,
    expected_head: str,
    expected_remote_url: str,
    now: datetime | None = None,
    *,
    require_current_inputs: bool = True,
) -> dict[str, Any]:
    _expect(isinstance(receipt, dict), f"{label}: expected object")
    allowed = {
        "role", "stage", "provider", "model", "agent_task_id", "agent_body_path",
        "agent_body_snapshot_path", "agent_kit_commit", "workspace_root",
        "agent_body_sha256", "prompt_path", "prompt_sha256", "critique_path",
        "critique_sha256", "reviewed_base", "reviewed_head", "started_at", "completed_at",
        "verdict", "check_evidence_refs", "check_attempt_refs", "findings_register_sha256",
        "assessment_manifest_sha256", "operations_plan_sha256", "release_manifest_sha256",
        "findings_snapshot_path", "operations_plan_snapshot_path",
        "release_manifest_snapshot_path", "reviewed_remote_url",
        "delivery_requirements_sha256",
    }
    _strict_keys(receipt, allowed, label)
    required = allowed - {"model"}
    _require_keys(receipt, required, label)
    role = _nonempty_string(receipt.get("role"), f"{label}.role")
    stage = _nonempty_string(receipt.get("stage"), f"{label}.stage")
    _expect(stage in {"assessment", "closure", "operations"}, f"{label}.stage: invalid value")
    _nonempty_string(receipt.get("provider"), f"{label}.provider")
    if receipt.get("model") is not None:
        _expect(isinstance(receipt.get("model"), str),
                f"{label}.model: expected string or null")
    task_id = _nonempty_string(receipt.get("agent_task_id"), f"{label}.agent_task_id")
    _expect(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", task_id)),
            f"{label}.agent_task_id: unsafe path identifier")
    _expect(
        _nonempty_string(receipt.get("reviewed_remote_url"), f"{label}.reviewed_remote_url")
        == expected_remote_url,
        f"{label}.reviewed_remote_url: does not match frozen canonical origin",
    )
    body_rel = _nonempty_string(receipt.get("agent_body_path"), f"{label}.agent_body_path")
    _expect(not os.path.isabs(body_rel), f"{label}.agent_body_path: absolute paths are forbidden")
    expected_body_rel = f"data/agents/{role}.md"
    _expect(body_rel == expected_body_rel,
            f"{label}.agent_body_path: role {role!r} must declare source {expected_body_rel!r}")

    kit_commit = _commit(receipt.get("agent_kit_commit"), f"{label}.agent_kit_commit")
    _expect(kit_commit in _allowed_state_kit_commits(state),
            f"{label}.agent_kit_commit: review kit is outside explicit upgrade history")
    resolved_kit_commit = _commit(
        _git_output(AGENT_KIT_ROOT, "rev-parse", "--verify", f"{kit_commit}^{{commit}}")
        .decode().strip(),
        f"{label}.agent_kit_commit.resolved",
    )
    _expect(kit_commit == resolved_kit_commit,
            f"{label}.agent_kit_commit: must be the full immutable kit commit")
    canonical_body = _git_output(AGENT_KIT_ROOT, "show", f"{kit_commit}:{body_rel}")

    workspace_raw = _nonempty_string(receipt.get("workspace_root"), f"{label}.workspace_root")
    _expect(os.path.isabs(workspace_raw), f"{label}.workspace_root: expected absolute path")
    workspace = Path(workspace_raw).resolve()
    _expect(workspace.is_dir(), f"{label}.workspace_root: directory is missing: {workspace}")
    expected_workspace = _canonical_workspace_root()
    _expect(workspace == expected_workspace,
            f"{label}.workspace_root: expected canonical workspace {expected_workspace}")

    # Review execution binds a per-run body snapshot to an immutable agent-kit
    # commit. A later catalog edit does not invalidate the run, while a forged
    # or ambient working-tree body cannot impersonate the canonical role.
    snapshot_rel = _nonempty_string(
        receipt.get("agent_body_snapshot_path"), f"{label}.agent_body_snapshot_path"
    )
    _expect(not os.path.isabs(snapshot_rel),
            f"{label}.agent_body_snapshot_path: absolute paths are forbidden")
    expected_snapshot_rel = f"artifacts/reviews/{role}-{task_id}-agent.md"
    _expect(snapshot_rel == expected_snapshot_rel,
            f"{label}.agent_body_snapshot_path: expected {expected_snapshot_rel!r}")
    state_dir = _state_dir(Path(state["_state_path"]))
    body_path = (state_dir / snapshot_rel).resolve()
    try:
        body_path.relative_to((state_dir / "artifacts" / "reviews").resolve())
    except ValueError:
        _fail(f"{label}.agent_body_snapshot_path: must resolve below artifacts/reviews")
    _expect(body_path.is_file(), f"{label}.agent_body_snapshot_path: missing {body_path}")
    body_sha = _sha256_value(receipt.get("agent_body_sha256"), f"{label}.agent_body_sha256")
    _expect(_file_sha(body_path) == body_sha,
            f"{label}.agent_body_sha256: persisted body snapshot changed")
    _expect(body_path.read_bytes() == canonical_body,
            f"{label}.agent_body_snapshot_path: snapshot is not {body_rel} at agent_kit_commit")

    resolved_files: dict[str, Path] = {}
    for key, sha_key, root in (
        ("prompt_path", "prompt_sha256", _state_dir(Path(state["_state_path"]))),
        ("critique_path", "critique_sha256", repo),
    ):
        rel = _nonempty_string(receipt.get(key), f"{label}.{key}")
        _expect(not os.path.isabs(rel), f"{label}.{key}: absolute paths are forbidden")
        if key == "prompt_path":
            expected_prompt_rel = f"artifacts/reviews/{role}-{task_id}-prompt.md"
            _expect(rel == expected_prompt_rel,
                    f"{label}.prompt_path: expected {expected_prompt_rel!r}")
        path = (root / rel).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            _fail(f"{label}.{key}: path traversal is forbidden")
        _expect(path.is_file(), f"{label}.{key}: missing {path}")
        _expect(_file_sha(path) == _sha256_value(receipt.get(sha_key), f"{label}.{sha_key}"),
                f"{label}.{sha_key}: file content no longer matches the review receipt")
        resolved_files[key] = path

    base = _commit(receipt.get("reviewed_base"), f"{label}.reviewed_base")
    head = _commit(receipt.get("reviewed_head"), f"{label}.reviewed_head")
    _expect(base == expected_base.lower() and head == expected_head.lower(),
            f"{label}: reviewed range {base}..{head} does not match {expected_base}..{expected_head}")
    prompt_text = resolved_files["prompt_path"].read_text(encoding="utf-8")
    body_bytes = body_path.read_bytes()
    prompt_marker = "\n--- CANONICAL AGENT BODY ---\n"
    _expect(prompt_text.count(prompt_marker) == 1,
            f"{label}.prompt_path: expected one canonical body delimiter")
    header_text, prompt_body = prompt_text.split(prompt_marker, 1)
    header_lines = header_text.splitlines()
    _expect(header_lines and header_lines[0] == "MILESTONE_REVIEW_DISPATCH_V2",
            f"{label}.prompt_path: missing v2 dispatch header")
    header: dict[str, str] = {}
    for line in header_lines[1:]:
        _expect(": " in line, f"{label}.prompt_path: malformed header line {line!r}")
        key, value = line.split(": ", 1)
        _expect(key not in header, f"{label}.prompt_path: duplicate header {key!r}")
        header[key] = value
    critique_abs = str(resolved_files["critique_path"])
    delivery_requirements = _delivery_requirements(state)
    delivery_requirements_json = json.dumps(
        delivery_requirements, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    delivery_requirements_sha = _value_sha(delivery_requirements)
    check_refs_raw = receipt.get("check_evidence_refs")
    _expect(isinstance(check_refs_raw, list), f"{label}.check_evidence_refs: expected array")
    check_refs: list[dict[str, str]] = []
    check_abs_refs: list[dict[str, str]] = []
    for i, value in enumerate(check_refs_raw):
        ref_label = f"{label}.check_evidence_refs[{i}]"
        _expect(isinstance(value, dict), f"{ref_label}: expected object")
        _strict_keys(value, {"path", "sha256"}, ref_label)
        _require_keys(value, {"path", "sha256"}, ref_label)
        rel = _nonempty_string(value.get("path"), f"{ref_label}.path")
        digest = _sha256_value(value.get("sha256"), f"{ref_label}.sha256")
        _expect(not os.path.isabs(rel), f"{ref_label}.path: absolute path forbidden")
        candidate = (state_dir / rel).resolve()
        try:
            candidate.relative_to((state_dir / "artifacts").resolve())
        except ValueError:
            _fail(f"{ref_label}.path: must be below milestone artifacts")
        _expect(candidate.is_file(), f"{ref_label}.path: missing {candidate}")
        _expect(_file_sha(candidate) == digest, f"{ref_label}.sha256: file content changed")
        check_refs.append({"path": rel, "sha256": digest})
        check_abs_refs.append({"path": str(candidate), "sha256": digest})
    check_paths = [ref["path"] for ref in check_refs]
    _expect(len(check_paths) == len(set(check_paths)),
            f"{label}.check_evidence_refs: duplicate path")
    attempt_refs_raw = receipt.get("check_attempt_refs")
    _expect(isinstance(attempt_refs_raw, list), f"{label}.check_attempt_refs: expected array")
    check_attempt_refs: list[dict[str, str]] = []
    check_attempt_abs_refs: list[dict[str, str]] = []
    for i, value in enumerate(attempt_refs_raw):
        ref_label = f"{label}.check_attempt_refs[{i}]"
        _expect(isinstance(value, dict), f"{ref_label}: expected object")
        _strict_keys(value, {"path", "sha256"}, ref_label)
        _require_keys(value, {"path", "sha256"}, ref_label)
        rel = _nonempty_string(value.get("path"), f"{ref_label}.path")
        digest = _sha256_value(value.get("sha256"), f"{ref_label}.sha256")
        _expect(rel.startswith("artifacts/checks/"),
                f"{ref_label}.path: must be below artifacts/checks")
        candidate = (state_dir / rel).resolve()
        try:
            candidate.relative_to((state_dir / "artifacts" / "checks").resolve())
        except ValueError:
            _fail(f"{ref_label}.path: must be below artifacts/checks")
        _expect(candidate.is_file(), f"{ref_label}.path: missing {candidate}")
        _expect(_file_sha(candidate) == digest, f"{ref_label}.sha256: file content changed")
        check_attempt_refs.append({"path": rel, "sha256": digest})
        check_attempt_abs_refs.append({"path": str(candidate), "sha256": digest})
    attempt_paths = [ref["path"] for ref in check_attempt_refs]
    _expect(len(attempt_paths) == len(set(attempt_paths)),
            f"{label}.check_attempt_refs: duplicate path")

    def snapshot_path(field: str, suffix: str) -> Path:
        raw = _nonempty_string(receipt.get(field), f"{label}.{field}")
        expected = f"artifacts/reviews/{role}-{task_id}-{suffix}.json"
        _expect(raw == expected, f"{label}.{field}: expected {expected!r}")
        path = (state_dir / raw).resolve()
        try:
            path.relative_to((state_dir / "artifacts" / "reviews").resolve())
        except ValueError:
            _fail(f"{label}.{field}: must be below artifacts/reviews")
        _expect(path.is_file(), f"{label}.{field}: missing {path}")
        return path

    if stage == "assessment":
        _expect(receipt.get("delivery_requirements_sha256") is None,
                f"{label}.delivery_requirements_sha256: assessment receipt must be null")
        _expect(not check_refs,
                f"{label}.check_evidence_refs: assessment reviews must use an empty list")
        _expect(not check_attempt_refs,
                f"{label}.check_attempt_refs: assessment reviews must use an empty list")
        _expect(receipt.get("findings_register_sha256") is None,
                f"{label}.findings_register_sha256: assessment receipt must be null")
        _expect(receipt.get("assessment_manifest_sha256") is None,
                f"{label}.assessment_manifest_sha256: assessment receipt must be null")
        _expect(receipt.get("operations_plan_sha256") is None,
                f"{label}.operations_plan_sha256: assessment receipt must be null")
        _expect(receipt.get("release_manifest_sha256") is None,
                f"{label}.release_manifest_sha256: assessment receipt must be null")
        for field in (
            "findings_snapshot_path", "operations_plan_snapshot_path",
            "release_manifest_snapshot_path",
        ):
            _expect(receipt.get(field) is None, f"{label}.{field}: assessment receipt must be null")
        expected_header = {
            "ROLE": role,
            "STAGE": stage,
            "ID": state["id"],
            "REPO_ROOT": str(repo.resolve()),
            "WORKSPACE_ROOT": str(workspace),
            "COMMIT_RANGE": f"{base}..{head}",
            "CRITIQUE_PATH": critique_abs,
            "AGENT_KIT_COMMIT": kit_commit,
            "SOURCE_REMOTE_URL": expected_remote_url,
        }
    elif stage == "closure":
        _expect(
            _sha256_value(
                receipt.get("delivery_requirements_sha256"),
                f"{label}.delivery_requirements_sha256",
            ) == delivery_requirements_sha,
            f"{label}.delivery_requirements_sha256: delivery classification changed",
        )
        _expect(bool(check_refs), f"{label}.check_evidence_refs: closure requires passing checks")
        ledger_attempts = state.get("check_run_attempts")
        _expect(isinstance(ledger_attempts, list),
                "state.check_run_attempts: closure requires all-attempt ledger")
        _expect(ledger_attempts[:len(check_attempt_refs)] == check_attempt_refs,
                f"{label}.check_attempt_refs: historical attempts must be a ledger prefix")
        attempt_map = {ref["path"]: ref["sha256"] for ref in check_attempt_refs}
        for i, ref in enumerate(check_refs):
            _expect(attempt_map.get(ref["path"]) == ref["sha256"],
                    f"{label}.check_evidence_refs[{i}]: passing check is absent from attempt ledger")
            record = _load_json(
                state_dir / ref["path"], f"{label}.check_evidence_refs[{i}] record"
            )
            _expect(record.get("exit_code") == 0 and record.get("timed_out") is False,
                    f"{label}.check_evidence_refs[{i}]: check did not pass")
            _expect(record.get("head_before") == head and record.get("head_after") == head,
                    f"{label}.check_evidence_refs[{i}]: check is not for reviewed head")
            _expect(record.get("tracked_status_before") == ""
                    and record.get("tracked_status_after") == ""
                    and record.get("execution_status_after") == "",
                    f"{label}.check_evidence_refs[{i}]: check execution was not clean")
        if require_current_inputs:
            _expect(state.get("check_run_head") == expected_head,
                    "state.check_run_head: closure checks are not for FINAL_HEAD")
            expected_check_runs = state.get("check_run_hashes")
            _expect(isinstance(expected_check_runs, dict) and bool(expected_check_runs),
                    "state.check_run_hashes: closure requires deterministic check-run receipts")
            _expect({ref["path"]: ref["sha256"] for ref in check_refs} == expected_check_runs,
                    f"{label}.check_evidence_refs: must exactly equal state.check_run_hashes")
            expected_attempts = state.get("check_run_attempts")
            _expect(check_attempt_refs == expected_attempts,
                    f"{label}.check_attempt_refs: must exactly equal state.check_run_attempts")
        current_findings_path = (repo / _nonempty_string(
            state.get("findings_register"), "state.findings_register"
        )).resolve()
        _expect(current_findings_path.is_file(),
                f"state.findings_register: missing {current_findings_path}")
        findings_path = snapshot_path("findings_snapshot_path", "findings")
        findings_sha = _sha256_value(
            receipt.get("findings_register_sha256"),
            f"{label}.findings_register_sha256",
        )
        _expect(_file_sha(findings_path) == findings_sha,
                f"{label}.findings_register_sha256: findings snapshot changed")
        if require_current_inputs:
            _expect(findings_path.read_bytes() == current_findings_path.read_bytes(),
                    f"{label}.findings_snapshot_path: latest snapshot differs from current register")
        review_manifest_path = _safe_artifact_path(
            Path(state["_state_path"]), state, "review_manifest"
        )
        review_document = _load_json(review_manifest_path, "review_manifest")
        assessment_material = _review_projection(review_document, "assessment")
        assessment_sha = _sha256_value(
            receipt.get("assessment_manifest_sha256"),
            f"{label}.assessment_manifest_sha256",
        )
        _expect(_value_sha(assessment_material) == assessment_sha,
                f"{label}.assessment_manifest_sha256: assessment manifest changed")
        _expect(receipt.get("operations_plan_sha256") is None,
                f"{label}.operations_plan_sha256: code closure precedes release plan review")
        _expect(receipt.get("release_manifest_sha256") is None,
                f"{label}.release_manifest_sha256: code closure precedes publication")
        _expect(receipt.get("operations_plan_snapshot_path") is None,
                f"{label}.operations_plan_snapshot_path: closure receipt must be null")
        _expect(receipt.get("release_manifest_snapshot_path") is None,
                f"{label}.release_manifest_snapshot_path: closure receipt must be null")
        expected_header = {
            "ROLE": role,
            "STAGE": stage,
            "ID": state["id"],
            "REPO_ROOT": str(repo.resolve()),
            "WORKSPACE_ROOT": str(workspace),
            "BASE_COMMIT": base,
            "FINAL_HEAD": head,
            "FINDINGS_REGISTER": str(findings_path),
            "FINDINGS_REGISTER_SHA256": findings_sha,
            "REVIEW_MANIFEST": str(review_manifest_path),
            "ASSESSMENT_MANIFEST_SHA256": assessment_sha,
            "CHECK_EVIDENCE_REFS": json.dumps(
                check_abs_refs, separators=(",", ":"), ensure_ascii=False
            ),
            "CHECK_ATTEMPT_REFS": json.dumps(
                check_attempt_abs_refs, separators=(",", ":"), ensure_ascii=False
            ),
            "DELIVERY_REQUIREMENTS": delivery_requirements_json,
            "DELIVERY_REQUIREMENTS_SHA256": delivery_requirements_sha,
            "CLOSURE_PATH": critique_abs,
            "AGENT_KIT_COMMIT": kit_commit,
            "SOURCE_REMOTE_URL": expected_remote_url,
        }
    else:
        _expect(
            _sha256_value(
                receipt.get("delivery_requirements_sha256"),
                f"{label}.delivery_requirements_sha256",
            ) == delivery_requirements_sha,
            f"{label}.delivery_requirements_sha256: delivery classification changed",
        )
        _expect(not check_refs, f"{label}.check_evidence_refs: operations review must be empty")
        _expect(not check_attempt_refs,
                f"{label}.check_attempt_refs: operations review must be empty")
        _expect(receipt.get("findings_register_sha256") is None,
                f"{label}.findings_register_sha256: operations review must be null")
        _expect(receipt.get("assessment_manifest_sha256") is None,
                f"{label}.assessment_manifest_sha256: operations review must be null")
        _expect(receipt.get("findings_snapshot_path") is None,
                f"{label}.findings_snapshot_path: operations review must be null")
        current_operations_plan_path = _safe_artifact_path(
            Path(state["_state_path"]), state, "operations_plan"
        )
        current_release_manifest_path = _safe_artifact_path(
            Path(state["_state_path"]), state, "release_manifest"
        )
        operations_plan_path = snapshot_path(
            "operations_plan_snapshot_path", "operations-plan"
        )
        release_manifest_path = snapshot_path(
            "release_manifest_snapshot_path", "release-manifest"
        )
        operations_plan_sha = _sha256_value(
            receipt.get("operations_plan_sha256"), f"{label}.operations_plan_sha256"
        )
        release_manifest_sha = _sha256_value(
            receipt.get("release_manifest_sha256"), f"{label}.release_manifest_sha256"
        )
        _expect(_file_sha(operations_plan_path) == operations_plan_sha,
                f"{label}.operations_plan_sha256: reviewed plan changed")
        _expect(_file_sha(release_manifest_path) == release_manifest_sha,
                f"{label}.release_manifest_sha256: reviewed release changed")
        if require_current_inputs:
            _expect(operations_plan_path.read_bytes() == current_operations_plan_path.read_bytes(),
                    f"{label}.operations_plan_snapshot_path: latest snapshot differs from current plan")
            _expect(release_manifest_path.read_bytes() == current_release_manifest_path.read_bytes(),
                    f"{label}.release_manifest_snapshot_path: latest snapshot differs from current release")
        expected_header = {
            "ROLE": role,
            "STAGE": stage,
            "ID": state["id"],
            "REPO_ROOT": str(repo.resolve()),
            "WORKSPACE_ROOT": str(workspace),
            "BASE_COMMIT": base,
            "FINAL_HEAD": head,
            "RELEASE_MANIFEST": str(release_manifest_path),
            "RELEASE_MANIFEST_SHA256": release_manifest_sha,
            "OPERATIONS_PLAN": str(operations_plan_path),
            "OPERATIONS_PLAN_SHA256": operations_plan_sha,
            "DELIVERY_REQUIREMENTS": delivery_requirements_json,
            "DELIVERY_REQUIREMENTS_SHA256": delivery_requirements_sha,
            "OPERATIONS_REVIEW_PATH": critique_abs,
            "AGENT_KIT_COMMIT": kit_commit,
            "SOURCE_REMOTE_URL": expected_remote_url,
        }
    _expect(header == expected_header,
            f"{label}.prompt_path: dispatch header does not exactly bind the stage inputs")
    _expect(prompt_body.encode("utf-8") == body_bytes,
            f"{label}.prompt_path: canonical body snapshot is not the complete prompt body")
    critique_text = resolved_files["critique_path"].read_text(encoding="utf-8")
    if stage == "assessment":
        critic_name = role.removeprefix("milestone-").removesuffix("-adversary")
        if role == "milestone-adversary":
            critic_name = "adversary"
        critic_markers = re.findall(r"(?m)^\*\*Critic(?:\(s\))?:\*\*\s*(\S+)\s*$", critique_text)
        _expect(critic_markers == [critic_name],
                f"{label}.critique_path: **Critic:** must identify {critic_name!r}")
    range_marker = "**Diff range:**" if stage == "assessment" else "**Reviewed range:**"
    _expect(f"{range_marker} {base}..{head}" in critique_text,
            f"{label}.critique_path: report does not declare the exact reviewed range")
    started = _iso(receipt.get("started_at"), f"{label}.started_at")
    completed = _iso(receipt.get("completed_at"), f"{label}.completed_at")
    _expect(completed >= started, f"{label}: completed_at precedes started_at")
    if now is not None:
        _expect(completed <= now, f"{label}.completed_at: future-dated review forbidden")
    verdict = _nonempty_string(receipt.get("verdict"), f"{label}.verdict")
    marker = {
        "assessment": "Overall verdict",
        "closure": "Closure verdict",
        "operations": "Operations verdict",
    }[stage]
    allowed_verdicts = {"PASS", "FAIL"} if stage != "assessment" else {
        "SHIP", "SHIP-WITH-FIXES", "DO-NOT-SHIP",
    }
    _expect(verdict in allowed_verdicts, f"{label}.verdict: invalid value for {stage} review")
    report_verdicts = re.findall(
        rf"(?m)^(?:- )?\*\*{re.escape(marker)}:\*\*\s*({'|'.join(sorted(allowed_verdicts, key=len, reverse=True))})\s*$",
        critique_text,
    )
    _expect(len(report_verdicts) == 1,
            f"{label}.critique_path: expected exactly one **{marker}:** marker")
    _expect(report_verdicts[0] == verdict,
            f"{label}.verdict: receipt {verdict!r} conflicts with report {report_verdicts[0]!r}")
    return {
        "role": role,
        "stage": stage,
        "hash": _value_sha(receipt),
        "check_evidence_refs": check_refs,
        "check_attempt_refs": check_attempt_refs,
        "findings_register_sha256": receipt.get("findings_register_sha256"),
        "assessment_manifest_sha256": receipt.get("assessment_manifest_sha256"),
        "operations_plan_sha256": receipt.get("operations_plan_sha256"),
        "release_manifest_sha256": receipt.get("release_manifest_sha256"),
        "delivery_requirements_sha256": receipt.get("delivery_requirements_sha256"),
    }


def validate_review_manifest(
    data: dict[str, Any], state: dict[str, Any], state_path: Path, *,
    require_closure: bool = False, require_operations_review: bool = False,
    treat_latest_as_historical: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    label = "review_manifest"
    _artifact_envelope(data, label, state["id"], now)
    _strict_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "reviewed", "required_reviewers", "reviews", "closure_reviews", "operations_reviews",
    }, label)
    _require_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "reviewed", "required_reviewers", "reviews", "closure_reviews", "operations_reviews",
    }, label)
    repo = _repo_root(state_path)
    reviewed = data.get("reviewed")
    _expect(isinstance(reviewed, dict), f"{label}.reviewed: expected object")
    _strict_keys(reviewed, {"repo", "base_commit", "head_commit", "diff_sha256", "remote_url"}, f"{label}.reviewed")
    _require_keys(reviewed, {"repo", "base_commit", "head_commit", "diff_sha256", "remote_url"}, f"{label}.reviewed")
    _expect(_nonempty_string(reviewed.get("repo"), f"{label}.reviewed.repo") == repo.name,
            f"{label}.reviewed.repo: expected {repo.name!r}")
    remote_url = _validated_remote_url(
        reviewed.get("remote_url"), f"{label}.reviewed.remote_url"
    )
    live_remote_url = _git_output(repo, "remote", "get-url", "origin").decode().strip()
    _expect(remote_url == live_remote_url,
            f"{label}.reviewed.remote_url: current canonical origin changed")
    base = _commit(reviewed.get("base_commit"), f"{label}.reviewed.base_commit")
    head = _commit(reviewed.get("head_commit"), f"{label}.reviewed.head_commit")
    _expect(base == _commit(state.get("implementation_base"), "state.implementation_base"),
            f"{label}: base does not match state.implementation_base")
    state_commits = state.get("implementation_commits") or []
    _expect(isinstance(state_commits, list) and state_commits, "state.implementation_commits: expected non-empty list")
    expected_pre_rect_head = _commit(state_commits[-1], "state.implementation_commits[-1]")
    _expect(head == expected_pre_rect_head, f"{label}: assessment head must equal the implementation head")
    _expect(_diff_sha(repo, base, head) == _sha256_value(reviewed.get("diff_sha256"), f"{label}.reviewed.diff_sha256"),
            f"{label}.reviewed.diff_sha256: live diff no longer matches")

    required_raw = data.get("required_reviewers")
    _expect(isinstance(required_raw, list) and required_raw, f"{label}.required_reviewers: expected non-empty list")
    required = {_nonempty_string(v, f"{label}.required_reviewers[]") for v in required_raw}
    _expect(len(required) == len(required_raw), f"{label}.required_reviewers: duplicate role")
    deterministic = _required_reviewers(repo, base, head)
    _expect(required == deterministic,
            f"{label}.required_reviewers: expected {sorted(deterministic)}, got {sorted(required)}")

    reviews = data.get("reviews")
    _expect(isinstance(reviews, list), f"{label}.reviews: expected array")
    receipts = [
        _validate_review_receipt(
            v, f"{label}.reviews[{i}]", repo, state, base, head, remote_url, now
        )
        for i, v in enumerate(reviews)
    ]
    _expect(all(r["stage"] == "assessment" for r in receipts),
            f"{label}.reviews: only assessment-stage receipts are allowed; closure has its own field")
    roles = [r["role"] for r in receipts if r["stage"] == "assessment"]
    _expect(len(roles) == len(set(roles)), f"{label}.reviews: duplicate assessment role")
    _expect(set(roles) == required,
            f"{label}.reviews: assessment roles must exactly equal required_reviewers")
    task_ids = [
        _nonempty_string(item.get("agent_task_id"), f"{label}.reviews[].agent_task_id")
        for item in reviews
    ]
    _expect(len(task_ids) == len(set(task_ids)),
            f"{label}.reviews: every reviewer needs a distinct runtime task id")
    critique_paths = [
        _nonempty_string(item.get("critique_path"), f"{label}.reviews[].critique_path")
        for item in reviews
    ]
    critique_hashes = [
        _sha256_value(item.get("critique_sha256"), f"{label}.reviews[].critique_sha256")
        for item in reviews
    ]
    _expect(len(critique_paths) == len(set(critique_paths)),
            f"{label}.reviews: every reviewer needs a distinct critique path")
    _expect(len(critique_hashes) == len(set(critique_hashes)),
            f"{label}.reviews: every reviewer needs a distinct critique document")

    state_critics = state.get("critics_run")
    _expect(isinstance(state_critics, list) and set(state_critics) == required,
            f"state.critics_run must exactly equal deterministic required reviewers")
    state_files = state.get("critique_files")
    receipt_files = [r.get("critique_path") for r in reviews]
    _expect(isinstance(state_files, list) and sorted(state_files) == sorted(receipt_files),
            f"state.critique_files must exactly equal the review manifest critique set")

    findings_path_raw = _nonempty_string(state.get("findings_register"), "state.findings_register")
    _expect(not os.path.isabs(findings_path_raw), "state.findings_register: absolute path forbidden in v2")
    findings_path = (repo / findings_path_raw).resolve()
    try:
        findings_path.relative_to(repo.resolve())
    except ValueError:
        _fail("state.findings_register: path traversal forbidden")
    findings = _load_json(findings_path, "findings_register")
    _expect(findings.get("milestone_id") == state["id"],
            "findings_register.milestone_id does not match state.id (cross-run replay)")
    reg_files = findings.get("critique_files")
    _expect(isinstance(reg_files, list) and sorted(reg_files) == sorted(state_files),
            "findings_register.critique_files does not match the hash-bound review set")

    closure_reviews = data.get("closure_reviews")
    _expect(isinstance(closure_reviews, list), f"{label}.closure_reviews: expected array")
    closure_hash = None
    closure_receipt_hash = None
    closure_check_evidence_refs: list[dict[str, str]] = []
    closure_findings_sha = None
    closure_operations_plan_sha = None
    final_head = _commit(state.get("rectification_commit") or expected_pre_rect_head,
                         "state.rectification_commit")
    if closure_reviews or require_closure:
        final_required = _required_reviewers(repo, base, final_head)
        newly_required = sorted(final_required - required)
        _expect(not newly_required,
                "rectification introduced a surface requiring unrun specialist review: "
                f"{newly_required}; amend/revert that surface in this run or split it into a new milestone")
    all_task_ids = set(task_ids)
    closure_metas: list[dict[str, Any]] = []
    previous_completed: datetime | None = None
    for i, closure_attempt in enumerate(closure_reviews):
        attempt_label = f"{label}.closure_reviews[{i}]"
        is_latest = i == len(closure_reviews) - 1 and not treat_latest_as_historical
        attempt_head = (
            final_head if is_latest
            else _commit(closure_attempt.get("reviewed_head"), f"{attempt_label}.reviewed_head")
        )
        for ancestor, descendant, message in (
            (base, attempt_head, "does not descend from implementation base"),
            (attempt_head, final_head, "is not on the final implementation lineage"),
        ):
            lineage = subprocess.run(
                ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
                capture_output=True,
            )
            _expect(lineage.returncode == 0, f"{attempt_label}.reviewed_head: {message}")
        closure_meta = _validate_review_receipt(
            closure_attempt, attempt_label, repo, state, base, attempt_head,
            remote_url, now, require_current_inputs=is_latest,
        )
        _expect(closure_meta["role"] == "milestone-closure-verifier",
                f"{attempt_label}.role: expected milestone-closure-verifier")
        _expect(closure_meta["stage"] == "closure",
                f"{attempt_label}.stage: expected closure")
        closure_task = _nonempty_string(
            closure_attempt.get("agent_task_id"), f"{attempt_label}.agent_task_id"
        )
        _expect(closure_task not in all_task_ids,
                f"{attempt_label}.agent_task_id: every attempt must use a distinct runtime task")
        all_task_ids.add(closure_task)
        completed = _iso(closure_attempt.get("completed_at"), f"{attempt_label}.completed_at")
        if previous_completed is not None:
            _expect(completed >= previous_completed,
                    f"{label}.closure_reviews: attempts must be chronological")
        previous_completed = completed
        closure_metas.append(closure_meta)
    closure = closure_reviews[-1] if closure_reviews else None
    if closure is not None and closure.get("verdict") == "PASS":
        closure_meta = closure_metas[-1]
        # implementation-evidence binds the latest independent closure report;
        # prior FAIL attempts remain in the code projection and cannot vanish.
        closure_hash = closure["critique_sha256"]
        closure_receipt_hash = closure_meta["hash"]
        closure_check_evidence_refs = closure_meta["check_evidence_refs"]
        closure_findings_sha = closure_meta["findings_register_sha256"]
        closure_operations_plan_sha = closure_meta["operations_plan_sha256"]
    if require_closure:
        _expect(closure is not None, f"{label}.closure_reviews: required before code-complete")
        _expect(closure.get("verdict") == "PASS",
                f"{label}.closure_reviews: latest attempt must PASS before code-complete")

    operations_reviews = data.get("operations_reviews")
    _expect(isinstance(operations_reviews, list), f"{label}.operations_reviews: expected array")
    operations_review_hash = None
    operations_review_receipt_hash = None
    operations_metas: list[dict[str, Any]] = []
    previous_completed = None
    for i, operations_review in enumerate(operations_reviews):
        attempt_label = f"{label}.operations_reviews[{i}]"
        is_latest = i == len(operations_reviews) - 1 and not treat_latest_as_historical
        attempt_head = (
            final_head if is_latest
            else _commit(operations_review.get("reviewed_head"), f"{attempt_label}.reviewed_head")
        )
        _expect(attempt_head == final_head,
                f"{attempt_label}.reviewed_head: operations attempts must review final code head")
        operations_meta = _validate_review_receipt(
            operations_review, attempt_label, repo, state, base,
            attempt_head, remote_url, now, require_current_inputs=is_latest,
        )
        _expect(operations_meta["role"] == "milestone-operations-adversary",
                f"{attempt_label}.role: expected milestone-operations-adversary")
        _expect(operations_meta["stage"] == "operations",
                f"{attempt_label}.stage: expected operations")
        operations_task = _nonempty_string(
            operations_review.get("agent_task_id"), f"{attempt_label}.agent_task_id",
        )
        _expect(operations_task not in all_task_ids,
                f"{attempt_label}.agent_task_id: every attempt must use a distinct runtime task")
        all_task_ids.add(operations_task)
        completed = _iso(operations_review.get("completed_at"), f"{attempt_label}.completed_at")
        if previous_completed is not None:
            _expect(completed >= previous_completed,
                    f"{label}.operations_reviews: attempts must be chronological")
        previous_completed = completed
        operations_metas.append(operations_meta)
    operations_review = operations_reviews[-1] if operations_reviews else None
    if operations_review is not None and operations_review.get("verdict") == "PASS":
        _expect(closure is not None and closure.get("verdict") == "PASS",
                f"{label}.operations_reviews: operations review requires a passing closure")
        operations_review_hash = operations_review["critique_sha256"]
        operations_review_receipt_hash = operations_metas[-1]["hash"]
    if require_operations_review:
        _expect(operations_review is not None,
                f"{label}.operations_reviews: required before apply authorization")
        _expect(operations_review.get("verdict") == "PASS",
                f"{label}.operations_reviews: latest attempt must PASS before apply authorization")
    all_stage_receipts = [*reviews, *closure_reviews, *operations_reviews]
    all_paths = [str(item["critique_path"]) for item in all_stage_receipts]
    all_hashes = [str(item["critique_sha256"]) for item in all_stage_receipts]
    _expect(len(all_paths) == len(set(all_paths)),
            f"{label}: every review stage needs a distinct critique path")
    _expect(len(all_hashes) == len(set(all_hashes)),
            f"{label}: every review stage needs a distinct critique document")

    return {
        "review_hashes": {r["role"]: r["hash"] for r in receipts},
        "immutable_root_hash": _value_sha(_review_projection(data, "assessment")),
        "closure_hash": closure_hash,
        "closure_receipt_hash": closure_receipt_hash,
        "closure_attempt_hashes": [meta["hash"] for meta in closure_metas],
        "closure_check_evidence_refs": closure_check_evidence_refs,
        "closure_findings_sha": closure_findings_sha,
        "closure_operations_plan_sha": closure_operations_plan_sha,
        "operations_review_hash": operations_review_hash,
        "operations_review_receipt_hash": operations_review_receipt_hash,
        "operations_review_attempt_hashes": [meta["hash"] for meta in operations_metas],
    }


def validate_implementation_evidence(
    data: dict[str, Any], state: dict[str, Any], evidence_root: Path | None = None,
    now: datetime | None = None, *, verify_live_inputs: bool = True,
) -> dict[str, Any]:
    label = "implementation_evidence"
    _artifact_envelope(data, label, state["id"], now)
    _strict_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "repositories", "checks", "critique", "rectification", "generated_artifacts",
    }, label)
    _require_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "repositories", "checks", "critique", "rectification", "generated_artifacts",
    }, label)
    repos = data.get("repositories")
    _expect(isinstance(repos, list) and repos, f"{label}.repositories: expected non-empty array")
    _expect(len(repos) == 1,
            f"{label}.repositories: v2.0 supports exactly one source repository; "
            "split multi-repo delivery into separately reviewed milestones")
    repo_names: set[str] = set()
    for i, repo in enumerate(repos):
        rlabel = f"{label}.repositories[{i}]"
        _expect(isinstance(repo, dict), f"{rlabel}: expected object")
        _strict_keys(repo, {
            "repo", "path", "base_commit", "head_commit", "commit_range", "commits",
            "branch", "remote_url",
        }, rlabel)
        _require_keys(repo, {
            "repo", "path", "base_commit", "head_commit", "commit_range", "commits",
            "branch", "remote_url",
        }, rlabel)
        name = _nonempty_string(repo.get("repo"), f"{rlabel}.repo")
        _expect(name not in repo_names, f"{label}.repositories: duplicate repo {name!r}")
        repo_names.add(name)
        _nonempty_string(repo.get("path"), f"{rlabel}.path")
        base = _commit(repo.get("base_commit"), f"{rlabel}.base_commit")
        head = _commit(repo.get("head_commit"), f"{rlabel}.head_commit")
        _expect(repo.get("commit_range") == f"{base}..{head}", f"{rlabel}.commit_range: must be base..head")
        commits = repo.get("commits")
        _expect(isinstance(commits, list) and commits, f"{rlabel}.commits: expected non-empty array")
        normalized = [_commit(v, f"{rlabel}.commits[]") for v in commits]
        _expect(len(normalized) == len(set(normalized)), f"{rlabel}.commits: duplicates forbidden")
        _expect(normalized[-1] == head, f"{rlabel}.commits: last commit must equal head_commit")
        _nonempty_string(repo.get("branch"), f"{rlabel}.branch")
        _validated_remote_url(repo.get("remote_url"), f"{rlabel}.remote_url")

    checks = data.get("checks")
    _expect(isinstance(checks, list) and checks, f"{label}.checks: expected non-empty array")
    for i, check in enumerate(checks):
        clabel = f"{label}.checks[{i}]"
        _expect(isinstance(check, dict), f"{clabel}: expected object")
        _strict_keys(check, {
            "name", "argv", "command", "repo_head", "exit_code", "started_at",
            "completed_at", "executable_path", "executable_sha256", "evidence",
        }, clabel)
        _require_keys(check, {
            "name", "argv", "command", "repo_head", "exit_code", "started_at",
            "completed_at", "executable_path", "executable_sha256", "evidence",
        }, clabel)
        name = _nonempty_string(check.get("name"), f"{clabel}.name")
        argv = _check_command_argv(check.get("argv"), f"{clabel}.argv")
        command = _nonempty_string(check.get("command"), f"{clabel}.command")
        _expect(command == shlex.join(argv), f"{clabel}.command: must equal canonical argv")
        executable_path = _nonempty_string(
            check.get("executable_path"), f"{clabel}.executable_path"
        )
        _expect(os.path.isabs(executable_path), f"{clabel}.executable_path: expected absolute path")
        executable_sha = _sha256_value(
            check.get("executable_sha256"), f"{clabel}.executable_sha256"
        )
        expected_tracked_inputs: dict[str, str] | None = None
        expected_setup: dict[str, Any] | None = None
        if verify_live_inputs:
            resolved_path, resolved_sha = _resolved_executable(
                [executable_path], f"{clabel}.executable_path", require_absolute=True,
                expected_sha256=executable_sha,
            )
            _expect(
                resolved_path == str(Path(executable_path).resolve())
                and resolved_sha == executable_sha,
                f"{clabel}.executable_path: executable identity mismatch",
            )
            expected_tracked_inputs, _ = _check_command_inputs(
                Path(repos[0]["path"]), argv, executable_path
            )
            expected_setup = _check_setup_spec(
                Path(repos[0]["path"]), argv, executable_path, executable_sha
            )
        repo_head = _commit(check.get("repo_head"), f"{clabel}.repo_head")
        final_head = _commit(
            state.get("rectification_commit")
            or (state.get("implementation_commits") or [None])[-1],
            "state final implementation commit",
        )
        _expect(repo_head == final_head, f"{clabel}.repo_head: check did not run at final HEAD")
        _expect(_integer(check.get("exit_code"), f"{clabel}.exit_code") == 0,
                f"{clabel}.exit_code: project checks must pass")
        started = _iso(check.get("started_at"), f"{clabel}.started_at")
        completed = _iso(check.get("completed_at"), f"{clabel}.completed_at")
        _expect(completed >= started, f"{clabel}.completed_at: precedes check start")
        if now is not None:
            _expect(completed <= now, f"{clabel}.completed_at: future-dated check forbidden")
        _validate_evidence_ref(check.get("evidence"), f"{clabel}.evidence", evidence_root)
        _expect(check["evidence"].get("command") == command,
                f"{clabel}.evidence.command: must equal the recorded check command")
        if evidence_root is not None:
            evidence_path = (evidence_root / check["evidence"]["path"]).resolve()
            record = _load_json(evidence_path, f"{clabel}.evidence record")
            expected_record_fields = {
                "schema_version", "producer", "name", "argv", "command", "repo_root",
                "executable_path", "executable_sha256", "tracked_input_hashes",
                "runtime_interpreter", "setup", "environment",
                "head_before", "head_after", "tracked_status_before", "tracked_status_after",
                "execution_mode", "execution_head_after", "execution_status_after",
                "state_tree_sha256_before", "state_tree_sha256_after",
                "started_at", "completed_at", "exit_code", "stdout", "stderr",
                "stdout_sha256", "stderr_sha256", "stdout_truncated",
                "stderr_truncated", "output_limit_bytes",
                "background_processes_terminated", "timed_out",
            }
            _strict_keys(record, expected_record_fields, f"{clabel}.evidence record")
            _require_keys(record, expected_record_fields, f"{clabel}.evidence record")
            _expect(record.get("schema_version") == 1,
                    f"{clabel}.evidence record.schema_version: expected 1")
            _validate_deterministic_producer(
                record.get("producer"), state,
                f"{clabel}.evidence record.producer",
            )
            _expect(record.get("name") == name and record.get("argv") == argv
                    and record.get("command") == command,
                    f"{clabel}.evidence record: check identity mismatch")
            _expect(record.get("executable_path") == executable_path
                    and record.get("executable_sha256") == executable_sha,
                    f"{clabel}.evidence record: executable identity mismatch")
            recorded_interpreter = record.get("runtime_interpreter")
            _expect(recorded_interpreter == _runtime_interpreter(executable_path),
                    f"{clabel}.evidence record.runtime_interpreter: identity changed")
            if recorded_interpreter is not None:
                _expect(isinstance(recorded_interpreter, dict)
                        and set(recorded_interpreter) == {"path", "sha256"},
                        f"{clabel}.evidence record.runtime_interpreter: malformed")
                _sha256_value(recorded_interpreter.get("sha256"),
                              f"{clabel}.evidence record.runtime_interpreter.sha256")
            recorded_environment = record.get("environment")
            _expect(isinstance(recorded_environment, dict)
                    and set(recorded_environment) == {
                        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONNOUSERSITE"
                    }, f"{clabel}.evidence record.environment: unexpected ambient variables")
            _expect(recorded_environment.get("PYTHONNOUSERSITE") == "1"
                    and recorded_environment.get("LANG") == "C"
                    and recorded_environment.get("LC_ALL") == "C",
                    f"{clabel}.evidence record.environment: unsafe language/user-site settings")
            tracked_inputs = record.get("tracked_input_hashes")
            _expect(isinstance(tracked_inputs, dict),
                    f"{clabel}.evidence record.tracked_input_hashes: expected object")
            for input_path, digest in tracked_inputs.items():
                _expect(
                    not os.path.isabs(input_path)
                    and ".." not in PurePosixPath(input_path).parts,
                    f"{clabel}.evidence record.tracked_input_hashes: unsafe path",
                )
                _sha256_value(digest, f"{clabel}.evidence record.tracked_input_hashes")
            if verify_live_inputs:
                _expect(tracked_inputs == expected_tracked_inputs,
                        f"{clabel}.evidence record: tracked check inputs changed")
            recorded_setup = record.get("setup")
            if verify_live_inputs and expected_setup is None:
                _expect(recorded_setup is None,
                        f"{clabel}.evidence record.setup: unexpected dependency setup")
            elif recorded_setup is not None:
                _expect(isinstance(recorded_setup, dict),
                        f"{clabel}.evidence record.setup: required for package-manager check")
                setup_keys = {
                    "argv", "executable_path", "executable_sha256", "lockfile",
                    "lockfile_sha256", "runtime_interpreter",
                }
                _strict_keys(recorded_setup, setup_keys | {
                    "exit_code", "stdout", "stderr", "stdout_sha256", "stderr_sha256",
                    "stdout_truncated", "stderr_truncated", "output_limit_bytes",
                    "background_processes_terminated", "timed_out",
                }, f"{clabel}.evidence record.setup")
                _require_keys(recorded_setup, setup_keys | {
                    "exit_code", "stdout", "stderr", "stdout_sha256", "stderr_sha256",
                    "stdout_truncated", "stderr_truncated", "output_limit_bytes",
                    "background_processes_terminated", "timed_out",
                }, f"{clabel}.evidence record.setup")
                if verify_live_inputs:
                    _expect(expected_setup is not None,
                            f"{clabel}.evidence record.setup: unexpected dependency setup")
                    for key, value in expected_setup.items():
                        _expect(recorded_setup.get(key) == value,
                                f"{clabel}.evidence record.setup.{key}: setup identity changed")
                _check_command_argv(recorded_setup.get("argv"),
                                    f"{clabel}.evidence record.setup.argv")
                _expect(os.path.isabs(_nonempty_string(
                    recorded_setup.get("executable_path"),
                    f"{clabel}.evidence record.setup.executable_path",
                )), f"{clabel}.evidence record.setup.executable_path: expected absolute")
                _sha256_value(recorded_setup.get("executable_sha256"),
                              f"{clabel}.evidence record.setup.executable_sha256")
                _nonempty_string(recorded_setup.get("lockfile"),
                                 f"{clabel}.evidence record.setup.lockfile")
                _sha256_value(recorded_setup.get("lockfile_sha256"),
                              f"{clabel}.evidence record.setup.lockfile_sha256")
                _expect(recorded_setup.get("exit_code") == 0
                        and recorded_setup.get("timed_out") is False
                        and recorded_setup.get("stdout_truncated") is False
                        and recorded_setup.get("stderr_truncated") is False
                        and recorded_setup.get("background_processes_terminated") is False,
                        f"{clabel}.evidence record.setup: dependency setup did not pass")
                _expect(isinstance(recorded_setup.get("stdout"), str)
                        and isinstance(recorded_setup.get("stderr"), str),
                        f"{clabel}.evidence record.setup: stdout/stderr must be strings")
                _sha256_value(recorded_setup.get("stdout_sha256"),
                              f"{clabel}.evidence record.setup.stdout_sha256")
                _sha256_value(recorded_setup.get("stderr_sha256"),
                              f"{clabel}.evidence record.setup.stderr_sha256")
                _expect(recorded_setup["stdout_sha256"]
                        == _persisted_text_sha(recorded_setup["stdout"])
                        and recorded_setup["stderr_sha256"]
                        == _persisted_text_sha(recorded_setup["stderr"]),
                        f"{clabel}.evidence record.setup: persisted output hash mismatch")
                _expect(recorded_setup.get("output_limit_bytes") == MAX_CAPTURE_BYTES,
                        f"{clabel}.evidence record.setup.output_limit_bytes: mismatch")
            _expect(record.get("repo_root") == str(Path(repos[0]["path"]).resolve()),
                    f"{clabel}.evidence record.repo_root: wrong repository")
            _expect(record.get("head_before") == repo_head and record.get("head_after") == repo_head,
                    f"{clabel}.evidence record: HEAD moved or differs from receipt")
            _expect(record.get("tracked_status_before") == ""
                    and record.get("tracked_status_after") == "",
                    f"{clabel}.evidence record: tracked worktree was not clean")
            _expect(record.get("execution_mode") == "detached-git-worktree",
                    f"{clabel}.evidence record: detached worktree execution required")
            _expect(record.get("execution_head_after") == repo_head
                    and record.get("execution_status_after") == "",
                    f"{clabel}.evidence record: detached execution tree changed")
            _expect(record.get("state_tree_sha256_before")
                    == record.get("state_tree_sha256_after"),
                    f"{clabel}.evidence record: milestone state changed during check")
            _expect(record.get("started_at") == check["started_at"]
                    and record.get("completed_at") == check["completed_at"]
                    and record.get("exit_code") == check["exit_code"],
                    f"{clabel}.evidence record: timing/exit receipt mismatch")
            _expect(record.get("timed_out") is False,
                    f"{clabel}.evidence record: timed-out check cannot pass")
            _expect(record.get("stdout_truncated") is False
                    and record.get("stderr_truncated") is False
                    and record.get("background_processes_terminated") is False,
                    f"{clabel}.evidence record: truncated output cannot pass")
            _sha256_value(record.get("stdout_sha256"),
                          f"{clabel}.evidence record.stdout_sha256")
            _sha256_value(record.get("stderr_sha256"),
                          f"{clabel}.evidence record.stderr_sha256")
            _expect(record["stdout_sha256"] == _persisted_text_sha(record["stdout"])
                    and record["stderr_sha256"] == _persisted_text_sha(record["stderr"]),
                    f"{clabel}.evidence record: persisted output hash mismatch")
            _expect(record.get("output_limit_bytes") == MAX_CAPTURE_BYTES,
                    f"{clabel}.evidence record.output_limit_bytes: mismatch")
            _expect(isinstance(record.get("stdout"), str) and isinstance(record.get("stderr"), str),
                    f"{clabel}.evidence record: stdout/stderr must be strings")

    critique = data.get("critique")
    _expect(isinstance(critique, dict), f"{label}.critique: expected object")
    _strict_keys(critique, {"code_review_manifest_sha256", "findings_register_sha256", "gate_exit_code", "checked_at", "open_critical", "open_high"}, f"{label}.critique")
    _require_keys(critique, {"code_review_manifest_sha256", "findings_register_sha256", "gate_exit_code", "checked_at", "open_critical", "open_high"}, f"{label}.critique")
    _sha256_value(critique.get("code_review_manifest_sha256"), f"{label}.critique.code_review_manifest_sha256")
    _sha256_value(critique.get("findings_register_sha256"), f"{label}.critique.findings_register_sha256")
    _expect(_integer(critique.get("gate_exit_code"), f"{label}.critique.gate_exit_code") == 0,
            f"{label}.critique.gate_exit_code: findings gate must pass")
    _expect(_integer(critique.get("open_critical"), f"{label}.critique.open_critical") == 0,
            f"{label}.critique.open_critical: must be zero")
    _expect(_integer(critique.get("open_high"), f"{label}.critique.open_high") == 0,
            f"{label}.critique.open_high: must be zero")
    checked = _iso(critique.get("checked_at"), f"{label}.critique.checked_at")
    if now is not None:
        _expect(checked <= now, f"{label}.critique.checked_at: future-dated gate forbidden")

    rect = data.get("rectification")
    _expect(isinstance(rect, dict), f"{label}.rectification: expected object")
    _strict_keys(rect, {"commit", "not_required_reason", "closure_review_sha256"}, f"{label}.rectification")
    _require_keys(rect, {"commit", "not_required_reason", "closure_review_sha256"}, f"{label}.rectification")
    has_commit = rect.get("commit") is not None
    has_reason = isinstance(rect.get("not_required_reason"), str) and bool(rect["not_required_reason"].strip())
    _expect(has_commit ^ has_reason, f"{label}.rectification: exactly one of commit or not_required_reason is required")
    if has_commit:
        rect_commit = _commit(rect.get("commit"), f"{label}.rectification.commit")
        _expect(
            state.get("rectification_commit") is not None
            and rect_commit == _commit(state.get("rectification_commit"), "state.rectification_commit"),
            f"{label}.rectification.commit: does not match state.rectification_commit",
        )
    else:
        _expect(
            rect.get("not_required_reason") == state.get("rectification_not_required_reason"),
            f"{label}.rectification.not_required_reason: does not match state",
        )
    _sha256_value(rect.get("closure_review_sha256"), f"{label}.rectification.closure_review_sha256")
    generated = data.get("generated_artifacts")
    _expect(isinstance(generated, list), f"{label}.generated_artifacts: expected array")
    for i, ref in enumerate(generated):
        _validate_evidence_ref(ref, f"{label}.generated_artifacts[{i}]", evidence_root)
    return {"repositories": sorted(repo_names), "checks": len(checks)}


def _required_project_checks(repo: Path) -> set[str]:
    """Derive a small exact command contract from repository markers.

    This is deliberately conservative: the artifact may contain additional
    passing checks, but a generic `true` receipt cannot stand in for a build or
    test command that the repository itself declares.
    """
    required: set[str] = set()
    package_json = repo / "package.json"
    if package_json.is_file():
        package = _load_json(package_json, "package.json")
        scripts = package.get("scripts")
        _expect(isinstance(scripts, dict), "package.json.scripts: expected object")
        if (repo / "bun.lock").exists() or (repo / "bun.lockb").exists():
            build_cmd, test_cmd = "bun run build", "bun test"
        elif (repo / "pnpm-lock.yaml").exists():
            build_cmd, test_cmd = "pnpm run build", "pnpm test"
        elif (repo / "yarn.lock").exists():
            build_cmd, test_cmd = "yarn build", "yarn test"
        else:
            build_cmd, test_cmd = "npm run build", "npm test"
        if isinstance(scripts.get("build"), str) and scripts["build"].strip():
            required.add(build_cmd)
        if isinstance(scripts.get("test"), str) and scripts["test"].strip():
            required.add(test_cmd)
    if (repo / "go.mod").is_file():
        required.add("go test ./...")
    if (repo / "Cargo.toml").is_file():
        required.add("cargo test")
    python_markers = any((repo / name).is_file() for name in (
        "pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini",
    ))
    if python_markers and (repo / "tests").is_dir():
        required.add("python3 -m pytest")
    contract_path = repo / ".milestone-pipeline" / "checks.json"
    if contract_path.is_file():
        contract = _load_json(contract_path, ".milestone-pipeline/checks.json")
        _strict_keys(contract, {"schema_version", "checks"}, ".milestone-pipeline/checks.json")
        _require_keys(contract, {"schema_version", "checks"}, ".milestone-pipeline/checks.json")
        _expect(contract.get("schema_version") == 1,
                ".milestone-pipeline/checks.json.schema_version: expected 1")
        checks = contract.get("checks")
        _expect(isinstance(checks, list) and bool(checks),
                ".milestone-pipeline/checks.json.checks: expected non-empty array")
        for i, command in enumerate(checks):
            required.add(_nonempty_string(
                command, f".milestone-pipeline/checks.json.checks[{i}]"
            ))
    return required


def _trusted_executable_roots() -> tuple[Path, ...]:
    """System locations an unprivileged user cannot write to.

    On Windows the POSIX list was not merely wrong, it was VACUOUS:
    `Path("/usr")` resolves to `C:\\usr`, which does not exist, so the
    `if root.exists()` filter emptied the tuple and `any(())` refused every
    executable. The control looked strict and was in fact inoperable.

    A per-user Python install — the Windows default, under %LOCALAPPDATA% — is
    deliberately NOT trusted: it is user-writable, which is exactly what this
    check exists to exclude. Widening it is a trust-model decision, not a
    portability fix.
    """
    if os.name == "nt":
        values = (
            os.environ.get("SystemRoot", r"C:\Windows"),
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        )
    else:
        values = (
            "/bin", "/usr", "/opt/homebrew", "/Applications/Xcode.app",
            "/Library/Frameworks",
        )
    return tuple(Path(v).resolve() for v in values if v)


def _executable_is_trusted(resolved_executable: Path) -> bool:
    """Whether an executable sits under a trusted system root."""
    return any(
        resolved_executable == root or root in resolved_executable.parents
        for root in _trusted_executable_roots() if root.exists()
    )


def _check_command_inputs(
    repo: Path, argv: list[str], executable_path: str
) -> tuple[dict[str, str], str | None]:
    """Freeze source-backed check inputs and reject ambient executable code."""
    repo_resolved = repo.resolve()
    resolved_executable = Path(executable_path).resolve()
    repo_executable: str | None = None
    try:
        repo_executable = resolved_executable.relative_to(repo_resolved).as_posix()
    except ValueError:
        _expect(
            _executable_is_trusted(resolved_executable),
            f"check executable is outside trusted system roots and reviewed source: {resolved_executable}",
        )

    tracked_inputs: dict[str, str] = {}

    def bind_repo_file(raw: str, label: str) -> str:
        _expect(not os.path.isabs(raw), f"{label}: absolute input path forbidden")
        pure = PurePosixPath(raw)
        _expect(".." not in pure.parts and str(pure) not in {"", "."},
                f"{label}: repo-relative non-traversing path required")
        rel = pure.as_posix().removeprefix("./")
        tracked = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--stage", "--error-unmatch", "--", rel],
            capture_output=True, text=True,
        )
        _expect(tracked.returncode == 0, f"{label}: input is not tracked at final HEAD: {rel}")
        rows = [line for line in tracked.stdout.splitlines() if line.strip()]
        _expect(len(rows) == 1 and rows[0].split(maxsplit=1)[0] in {"100644", "100755"},
                f"{label}: input must be one regular tracked blob, not a symlink/submodule: {rel}")
        blob = _git_output(repo, "show", f"HEAD:{rel}")
        tracked_inputs[rel] = hashlib.sha256(blob).hexdigest()
        return rel

    if repo_executable is not None:
        bind_repo_file(repo_executable, "check executable")

    for i, part in enumerate(argv[1:], start=1):
        candidates = [part.split("=", 1)[1]] if "=" in part else [part]
        for candidate in candidates:
            _expect(not os.path.isabs(candidate),
                    f"check argv[{i}]: absolute input/output paths are forbidden")

    executable_name = resolved_executable.name.casefold()
    if executable_name in {"python", "python3", "node", "ruby", "perl"}:
        args = argv[1:]
        if executable_name in {"python", "python3"} and args[:1] == ["-m"]:
            _expect(len(args) >= 2 and bool(args[1].strip()),
                    "check argv: python -m requires a module")
        else:
            script = next((value for value in args if not value.startswith("-")), None)
            _expect(script is not None,
                    "check argv: language runner requires a tracked repo-relative script or -m module")
            bind_repo_file(script, "check script")
    return tracked_inputs, repo_executable


def _check_setup_spec(
    repo: Path, argv: list[str], executable_path: str, executable_sha256: str
) -> dict[str, Any] | None:
    manager = Path(argv[0]).name.casefold()
    specs = {
        "npm": ("package-lock.json", ["ci", "--ignore-scripts", "--no-audit", "--no-fund"]),
        "pnpm": ("pnpm-lock.yaml", ["install", "--frozen-lockfile", "--ignore-scripts"]),
        "yarn": ("yarn.lock", ["install", "--immutable", "--ignore-scripts"]),
        "bun": ("bun.lock", ["install", "--frozen-lockfile", "--ignore-scripts"]),
    }
    if manager not in specs:
        return None
    lock_name, setup_args = specs[manager]
    if manager == "bun" and not (repo / lock_name).is_file():
        lock_name = "bun.lockb"
    lock_path = repo / lock_name
    _expect(lock_path.is_file(),
            f"check-run: {manager} checks require reviewed lockfile {lock_name}")
    stage = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--stage", "--error-unmatch", "--", lock_name],
        capture_output=True, text=True,
    )
    _expect(stage.returncode == 0 and stage.stdout.split(maxsplit=1)[0] == "100644",
            f"check-run: lockfile must be one regular tracked blob: {lock_name}")
    lock_bytes = _git_output(repo, "show", f"HEAD:{lock_name}")
    return {
        "argv": [executable_path, *setup_args],
        "executable_path": executable_path,
        "executable_sha256": executable_sha256,
        "lockfile": lock_name,
        "lockfile_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "runtime_interpreter": _runtime_interpreter(executable_path),
    }


def _runtime_interpreter(executable_path: str) -> dict[str, str] | None:
    """Bind a script shebang interpreter instead of trusting ambient PATH."""
    path = Path(executable_path).resolve()
    try:
        first = path.open("rb").readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    try:
        shebang = shlex.split(first[2:].decode("utf-8", errors="strict").strip())
    except (UnicodeDecodeError, ValueError):
        _fail(f"check executable has an invalid shebang: {path}")
    _expect(bool(shebang), f"check executable has an empty shebang: {path}")
    if Path(shebang[0]).name == "env":
        _expect(len(shebang) == 2 and not shebang[1].startswith("-"),
                f"check executable uses unsupported env shebang: {path}")
        resolved_raw = shutil.which(shebang[1])
        _expect(resolved_raw is not None,
                f"check executable interpreter is unavailable: {shebang[1]}")
        interpreter = Path(resolved_raw).resolve()
    else:
        _expect(os.path.isabs(shebang[0]),
                f"check executable shebang interpreter must be absolute: {path}")
        interpreter = Path(shebang[0]).resolve()
    _expect(interpreter.is_file() and os.access(interpreter, os.X_OK),
            f"check executable interpreter is missing/not executable: {interpreter}")
    trusted_roots = tuple(Path(value).resolve() for value in (
        "/bin", "/usr", "/opt/homebrew", "/Applications/Xcode.app", "/Library/Frameworks",
    ) if Path(value).exists())
    _expect(any(interpreter == root or root in interpreter.parents for root in trusted_roots),
            f"check executable interpreter is outside trusted toolchain roots: {interpreter}")
    return {"path": str(interpreter), "sha256": _file_sha(interpreter)}


PUBLICATION_OBSERVATION_KEYS = {
    "schema_version", "producer", "argv", "command", "executable_path",
    "executable_sha256", "environment", "remote_url", "ref", "observed_commit",
    "observed_at", "exit_code", "stdout", "stderr", "stdout_sha256",
    "stderr_sha256", "stdout_truncated", "stderr_truncated", "output_limit_bytes",
    "background_processes_terminated", "timed_out",
}


def _validate_publication_observation_record(
    record: dict[str, Any], scope: dict[str, Any], observed_commit: str | None,
    observed_at: str, producer: dict[str, Any], label: str,
    evidence_command: str | None = None,
) -> None:
    _strict_keys(record, PUBLICATION_OBSERVATION_KEYS, label)
    _require_keys(record, PUBLICATION_OBSERVATION_KEYS, label)
    _expect(record.get("schema_version") == 1 and record.get("producer") == producer,
            f"{label}: deterministic writer required")
    argv = _check_command_argv(record.get("argv"), f"{label}.argv")
    expected_argv = [
        scope["git_executable_path"], "-c", "core.hooksPath=/dev/null",
        "ls-remote", "--heads", scope["remote_url"],
        f"refs/heads/{scope['branch']}",
    ]
    _expect(argv == expected_argv, f"{label}.argv: wrong remote observation")
    _expect(record.get("command") == shlex.join(argv), f"{label}.command: mismatch")
    if evidence_command is not None:
        _expect(record["command"] == evidence_command,
                f"{label}.command: evidence reference mismatch")
    _expect(record.get("executable_path") == scope["git_executable_path"]
            and record.get("executable_sha256") == scope["git_executable_sha256"],
            f"{label}: frozen git identity mismatch")
    _expect(record.get("environment") == scope["execution_environment"],
            f"{label}.environment: mismatch")
    _expect(record.get("remote_url") == scope["remote_url"]
            and record.get("ref") == f"refs/heads/{scope['branch']}"
            and record.get("observed_commit") == observed_commit
            and record.get("observed_at") == observed_at
            and record.get("exit_code") == 0,
            f"{label}: observation mismatch")
    _iso(record.get("observed_at"), f"{label}.observed_at")
    _expect(isinstance(record.get("stdout"), str) and isinstance(record.get("stderr"), str),
            f"{label}: stdout/stderr must be strings")
    _expect(record.get("stdout_sha256") == _persisted_text_sha(record["stdout"])
            and record.get("stderr_sha256") == _persisted_text_sha(record["stderr"]),
            f"{label}: persisted output hash mismatch")
    _expect(record.get("output_limit_bytes") == MAX_CAPTURE_BYTES
            and record.get("stdout_truncated") is False
            and record.get("stderr_truncated") is False
            and record.get("background_processes_terminated") is False
            and record.get("timed_out") is False,
            f"{label}: bounded observation failed")


PUBLICATION_EXECUTION_KEYS = {
    "schema_version", "producer", "intent_id", "intent_generation", "scope_hash",
    "result_kind", "argv", "command", "environment", "executable_path",
    "executable_sha256", "started_at", "completed_at", "exit_code", "stdout",
    "stderr", "stdout_sha256", "stderr_sha256", "stdout_truncated",
    "stderr_truncated", "output_limit_bytes", "background_processes_terminated",
    "timed_out",
}


def _validate_publication_execution_record(
    record: dict[str, Any], intent: dict[str, Any], label: str,
    *, require_success: bool,
) -> int | None:
    _strict_keys(record, PUBLICATION_EXECUTION_KEYS, label)
    _require_keys(record, PUBLICATION_EXECUTION_KEYS, label)
    scope = intent["scope"]
    action = scope["push_argv"]
    _expect(scope["mode"] == "publish" and isinstance(action, list),
            f"{label}: execution receipt requires publish mode")
    _expect(record.get("schema_version") == 1
            and record.get("producer") == intent["producer"]
            and record.get("intent_id") == intent["intent_id"]
            and record.get("intent_generation") == intent["generation"]
            and record.get("scope_hash") == intent["scope_hash"],
            f"{label}: receipt identity mismatch")
    _expect(record.get("argv") == action
            and record.get("command") == shlex.join(action)
            and record.get("environment") == scope["execution_environment"],
            f"{label}: authorized action mismatch")
    _expect(record.get("executable_path") == scope["git_executable_path"]
            and record.get("executable_sha256") == scope["git_executable_sha256"],
            f"{label}: frozen git identity mismatch")
    started = _iso(record.get("started_at"), f"{label}.started_at")
    completed = _iso(record.get("completed_at"), f"{label}.completed_at")
    _expect(_iso(intent["authorization"]["at"], f"{label}.authorization.at")
            <= started <= completed,
            f"{label}: execution timestamps precede authorization or are reversed")
    result_kind = record.get("result_kind")
    _expect(result_kind in {"executed", "ambiguous-observed-success"},
            f"{label}.result_kind: invalid")
    exit_code: int | None
    if result_kind == "executed":
        exit_code = _integer(record.get("exit_code"), f"{label}.exit_code")
    else:
        _expect(record.get("exit_code") is None,
                f"{label}: ambiguous recovery cannot invent an exit code")
        exit_code = None
    _expect(isinstance(record.get("stdout"), str) and isinstance(record.get("stderr"), str),
            f"{label}: stdout/stderr must be strings")
    _expect(record.get("stdout_sha256") == _persisted_text_sha(record["stdout"])
            and record.get("stderr_sha256") == _persisted_text_sha(record["stderr"]),
            f"{label}: persisted output hash mismatch")
    for field in (
        "stdout_truncated", "stderr_truncated", "background_processes_terminated",
        "timed_out",
    ):
        _expect(isinstance(record.get(field), bool), f"{label}.{field}: expected bool")
    _expect(record.get("output_limit_bytes") == MAX_CAPTURE_BYTES,
            f"{label}.output_limit_bytes: mismatch")
    if require_success:
        _expect(
            (result_kind == "executed" and exit_code == 0)
            or result_kind == "ambiguous-observed-success",
            f"{label}: successful publication requires exit 0 or ambiguous observed success",
        )
        _expect(record["stdout_truncated"] is False
                and record["stderr_truncated"] is False
                and record["background_processes_terminated"] is False
                and record["timed_out"] is False,
                f"{label}: unsafe/incomplete command capture")
    return exit_code


def validate_publication_intent(
    data: dict[str, Any], state: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    label = "publication_intent"
    _artifact_envelope(data, label, state["id"], now)
    _validate_deterministic_producer(data.get("producer"), state, f"{label}.producer")
    _strict_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "intent_id", "scope", "scope_hash", "precondition", "authorization",
        "superseded_intents", "execution_attempts",
    }, label)
    _require_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "intent_id", "scope", "scope_hash", "precondition", "authorization",
        "superseded_intents", "execution_attempts",
    }, label)
    _expect(state.get("publication_required") is True,
            f"{label}: publication intent requires publication_required=true")
    intent_id = _nonempty_string(data.get("intent_id"), f"{label}.intent_id")
    _expect(bool(MILESTONE_ID_RE.fullmatch(intent_id)),
            f"{label}.intent_id: unsafe identifier")
    scope = data.get("scope")
    _expect(isinstance(scope, dict), f"{label}.scope: expected object")
    _strict_keys(scope, {
        "mode", "repo", "remote", "remote_url", "branch", "commit",
        "expected_remote_head", "git_executable_path", "git_executable_sha256",
        "execution_environment", "ssh_known_hosts_path", "ssh_known_hosts_sha256",
        "isolated_git_dir", "alternate_object_directory", "push_argv", "delivery_effect"
    },
                 f"{label}.scope")
    _require_keys(scope, {
        "mode", "repo", "remote", "remote_url", "branch", "commit",
        "expected_remote_head", "git_executable_path", "git_executable_sha256",
        "execution_environment", "ssh_known_hosts_path", "ssh_known_hosts_sha256",
        "isolated_git_dir", "alternate_object_directory", "push_argv"
    },
                  f"{label}.scope")
    for field in ("repo", "remote", "remote_url", "branch"):
        _nonempty_string(scope.get(field), f"{label}.scope.{field}")
    _validated_remote_url(scope.get("remote_url"), f"{label}.scope.remote_url")
    _expect(scope.get("remote") == "origin", f"{label}.scope.remote: expected origin")
    _expect(not str(scope.get("branch")).startswith("-"),
            f"{label}.scope.branch: option-like value forbidden")
    _commit(scope.get("commit"), f"{label}.scope.commit")
    mode = scope.get("mode")
    _expect(mode in {"publish", "adopt-preexisting"}, f"{label}.scope.mode: invalid")
    delivery_effect = scope.get("delivery_effect")
    if delivery_effect is not None:
        _expect(mode == "publish",
                f"{label}.scope.delivery_effect: adoption cannot authorize past effects")
        _validate_delivery_effect(delivery_effect, f"{label}.scope.delivery_effect")
    if scope.get("expected_remote_head") is not None:
        _commit(scope.get("expected_remote_head"), f"{label}.scope.expected_remote_head")
    git_path = _nonempty_string(
        scope.get("git_executable_path"), f"{label}.scope.git_executable_path"
    )
    _expect(os.path.isabs(git_path), f"{label}.scope.git_executable_path: expected absolute")
    _sha256_value(scope.get("git_executable_sha256"),
                  f"{label}.scope.git_executable_sha256")
    publication_environment = scope.get("execution_environment")
    _expect(isinstance(publication_environment, dict)
            and set(publication_environment) <= {
                "PATH", "HOME", "XDG_CONFIG_HOME", "GIT_CONFIG_NOSYSTEM",
                "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_TERMINAL_PROMPT",
                "GIT_SSH_COMMAND", "SSH_AUTH_SOCK", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            }
            and publication_environment.get("PATH") == "/usr/bin:/bin:/usr/sbin:/sbin"
            and publication_environment.get("GIT_CONFIG_NOSYSTEM") == "1"
            and publication_environment.get("GIT_CONFIG_GLOBAL") == "/dev/null"
            and publication_environment.get("GIT_CONFIG_SYSTEM") == "/dev/null"
            and publication_environment.get("GIT_TERMINAL_PROMPT") == "0",
            f"{label}.scope.execution_environment: invalid frozen publication environment")
    expected_home = str(
        (_state_dir(Path(state["_state_path"])) / "artifacts" / "publication" / "isolated-home")
        .absolute()
    )
    _expect(publication_environment.get("HOME") == expected_home
            and publication_environment.get("XDG_CONFIG_HOME") == expected_home,
            f"{label}.scope.execution_environment: state-owned isolation HOME mismatch")
    _expect(all(isinstance(value, str) and bool(value) for value in publication_environment.values()),
            f"{label}.scope.execution_environment: values must be non-empty strings")
    known_hosts_path = scope.get("ssh_known_hosts_path")
    known_hosts_sha = scope.get("ssh_known_hosts_sha256")
    if known_hosts_path is None:
        _expect(known_hosts_sha is None and "GIT_SSH_COMMAND" not in publication_environment,
                f"{label}.scope: non-SSH publication cannot bind SSH host keys")
    else:
        _expect(isinstance(known_hosts_path, str) and os.path.isabs(known_hosts_path),
                f"{label}.scope.ssh_known_hosts_path: expected absolute path")
        _sha256_value(known_hosts_sha, f"{label}.scope.ssh_known_hosts_sha256")
        _expect("GIT_SSH_COMMAND" in publication_environment,
                f"{label}.scope.execution_environment: SSH command required")
    isolated_git_dir = _nonempty_string(
        scope.get("isolated_git_dir"), f"{label}.scope.isolated_git_dir"
    )
    alternate_objects = _nonempty_string(
        scope.get("alternate_object_directory"),
        f"{label}.scope.alternate_object_directory",
    )
    _expect(os.path.isabs(isolated_git_dir) and os.path.isabs(alternate_objects),
            f"{label}.scope: isolated git/object paths must be absolute")
    _expect(
        publication_environment.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        == alternate_objects,
        f"{label}.scope.execution_environment: alternate object directory mismatch",
    )
    if mode == "publish":
        _expect(scope.get("expected_remote_head") != scope.get("commit"),
                f"{label}.scope: publish intent cannot adopt an already-published commit")
        expected_push = [
            git_path, "-c", "core.hooksPath=/dev/null",
            f"--git-dir={isolated_git_dir}", "push",
            f"--force-with-lease=refs/heads/{scope['branch']}:"
            f"{scope['expected_remote_head'] or ''}", "--", scope["remote_url"],
            f"{scope['commit']}:refs/heads/{scope['branch']}",
        ]
        _expect(scope.get("push_argv") == expected_push,
                f"{label}.scope.push_argv: does not equal the exact CAS action")
    else:
        _expect(scope.get("expected_remote_head") == scope.get("commit"),
                f"{label}.scope: adoption requires the exact commit already published")
        _expect(scope.get("push_argv") is None,
                f"{label}.scope.push_argv: adoption performs no push")
    scope_hash = _sha256_value(data.get("scope_hash"), f"{label}.scope_hash")
    _expect(scope_hash == _value_sha(scope), f"{label}.scope_hash: stale scope digest")
    precondition = data.get("precondition")
    _expect(isinstance(precondition, dict), f"{label}.precondition: expected object")
    _strict_keys(precondition, {"observed_commit", "observed_at", "evidence"},
                 f"{label}.precondition")
    _require_keys(precondition, {"observed_commit", "observed_at", "evidence"},
                  f"{label}.precondition")
    if precondition.get("observed_commit") is not None:
        _commit(precondition.get("observed_commit"),
                f"{label}.precondition.observed_commit")
    _expect(precondition.get("observed_commit") == scope.get("expected_remote_head"),
            f"{label}.precondition.observed_commit: differs from authorized precondition")
    observed_at = _iso(precondition.get("observed_at"), f"{label}.precondition.observed_at")
    if now is not None:
        _expect(observed_at <= now, f"{label}.precondition.observed_at: future observation")
    evidence_root = (
        _state_dir(Path(state["_state_path"])) if state.get("_state_path") else None
    )
    _validate_evidence_ref(
        precondition.get("evidence"), f"{label}.precondition.evidence", evidence_root
    )
    # The executable path may contain spaces, so validate against the persisted
    # argv record rather than reconstructing it from shell text.
    if evidence_root is not None:
        evidence_path = (evidence_root / precondition["evidence"]["path"]).resolve()
        record = _load_json(evidence_path, f"{label}.precondition evidence record")
        _validate_publication_observation_record(
            record, scope, precondition["observed_commit"],
            precondition["observed_at"], data["producer"],
            f"{label}.precondition evidence record",
            precondition["evidence"].get("command"),
        )
        _strict_keys(record, {
            "schema_version", "producer", "argv", "command", "executable_path",
            "executable_sha256", "environment", "remote_url", "ref", "observed_commit",
            "observed_at", "exit_code", "stdout", "stderr", "stdout_sha256",
            "stderr_sha256", "stdout_truncated", "stderr_truncated",
            "output_limit_bytes", "background_processes_terminated", "timed_out",
        }, f"{label}.precondition evidence record")
        _require_keys(record, {
            "schema_version", "producer", "argv", "command", "executable_path",
            "executable_sha256", "environment", "remote_url", "ref", "observed_commit",
            "observed_at", "exit_code", "stdout", "stderr", "stdout_sha256",
            "stderr_sha256", "stdout_truncated", "stderr_truncated",
            "output_limit_bytes", "background_processes_terminated", "timed_out",
        }, f"{label}.precondition evidence record")
        _expect(record.get("schema_version") == 1
                and record.get("producer") == data["producer"],
                f"{label}.precondition evidence record: deterministic writer required")
        argv = _check_command_argv(record.get("argv"),
                                   f"{label}.precondition evidence record.argv")
        _expect(
            argv[1:] == [
                "-c", "core.hooksPath=/dev/null", "ls-remote", "--heads",
                scope["remote_url"],
                f"refs/heads/{scope['branch']}",
            ]
            and os.path.isabs(argv[0]),
            f"{label}.precondition evidence record.argv: wrong remote observation",
        )
        _expect(record.get("command") == shlex.join(argv)
                == precondition["evidence"].get("command"),
                f"{label}.precondition evidence record.command: mismatch")
        _expect(record.get("executable_path") == scope["git_executable_path"],
                f"{label}.precondition evidence record.executable_path: mismatch")
        _expect(record.get("executable_sha256") == scope["git_executable_sha256"],
                f"{label}.precondition evidence record.executable_sha256: mismatch")
        _expect(record.get("remote_url") == scope["remote_url"]
                and record.get("ref") == f"refs/heads/{scope['branch']}"
                and record.get("observed_commit") == precondition["observed_commit"]
                and record.get("observed_at") == precondition["observed_at"]
                and record.get("exit_code") == 0,
                f"{label}.precondition evidence record: observation mismatch")
        _expect(isinstance(record.get("stdout"), str) and isinstance(record.get("stderr"), str),
                f"{label}.precondition evidence record: stdout/stderr must be strings")
        _expect(record.get("environment") == scope["execution_environment"],
                f"{label}.precondition evidence record.environment: mismatch")
        _expect(record["stdout_sha256"] == _persisted_text_sha(record["stdout"])
                and record["stderr_sha256"] == _persisted_text_sha(record["stderr"]),
                f"{label}.precondition evidence record: persisted output hash mismatch")
        _expect(record.get("output_limit_bytes") == MAX_CAPTURE_BYTES
                and record.get("stdout_truncated") is False
                and record.get("stderr_truncated") is False
                and record.get("background_processes_terminated") is False
                and record.get("timed_out") is False,
                f"{label}.precondition evidence record: bounded observation failed")
    authorization = data.get("authorization")
    _expect(isinstance(authorization, dict), f"{label}.authorization: expected object")
    _strict_keys(authorization, {"decision", "by", "method", "at", "scope_hash"},
                 f"{label}.authorization")
    _require_keys(authorization, {"decision", "by", "method", "at", "scope_hash"},
                  f"{label}.authorization")
    expected_decision = "approved" if mode == "publish" else "acknowledged"
    _expect(authorization.get("decision") == expected_decision,
            f"{label}.authorization.decision: expected {expected_decision}")
    _human_name(authorization.get("by"), f"{label}.authorization.by")
    _expect(authorization.get("method") == "human-explicit",
            f"{label}.authorization.method: expected human-explicit")
    authorized_at = _iso(authorization.get("at"), f"{label}.authorization.at")
    _expect(observed_at <= authorized_at,
            f"{label}.authorization.at: must follow the remote precondition observation")
    if now is not None:
        _expect(authorized_at <= now, f"{label}.authorization.at: future authorization")
    _expect(
        _sha256_value(authorization.get("scope_hash"),
                      f"{label}.authorization.scope_hash") == scope_hash,
        f"{label}.authorization.scope_hash: authorization does not bind scope",
    )
    superseded = data.get("superseded_intents")
    _expect(isinstance(superseded, list), f"{label}.superseded_intents: expected array")
    supersession_hashes: list[str] = []
    prior_generations: list[int] = []
    for i, ref_value in enumerate(superseded):
        slabel = f"{label}.superseded_intents[{i}]"
        _validate_evidence_ref(ref_value, slabel, evidence_root)
        _expect(ref_value.get("media_type") == "application/json",
                f"{slabel}.media_type: archived intent must be JSON")
        supersession_hashes.append(ref_value["sha256"])
        if evidence_root is not None:
            archived = _load_json(evidence_root / ref_value["path"], slabel)
            _expect(archived.get("milestone_id") == state["id"],
                    f"{slabel}: cross-milestone archived intent")
            archived_meta = validate_publication_intent(archived, state, now)
            archived_generation = _integer(
                archived.get("generation"), f"{slabel}.generation", 1
            )
            prior_generations.append(archived_generation)
            if archived.get("scope_hash") == scope_hash:
                archived_attempts = archived.get("execution_attempts")
                _expect(
                    isinstance(archived_attempts, list)
                    and len(archived_attempts) == 1
                    and len(archived_meta["execution_hashes"]) == 1,
                    f"{slabel}: same-scope supersession requires exact nonzero execution",
                )
                retry = _load_json(
                    evidence_root / archived_attempts[0]["path"],
                    f"{slabel} failed execution",
                )
                retry_exit = _validate_publication_execution_record(
                    retry, archived, f"{slabel} failed execution", require_success=False
                )
                _expect(retry.get("result_kind") == "executed"
                        and retry_exit is not None and retry_exit != 0,
                        f"{slabel}: same-scope supersession requires exact nonzero execution")
    _expect(len(supersession_hashes) == len(set(supersession_hashes)),
            f"{label}.superseded_intents: duplicate archived intent")
    generation = _integer(data.get("generation"), f"{label}.generation", 1)
    _expect(generation == len(superseded) + 1,
            f"{label}.generation: must equal superseded intent count + 1")
    if prior_generations:
        _expect(prior_generations == list(range(1, generation)),
                f"{label}.superseded_intents: generations must be contiguous")
    execution_attempts = data.get("execution_attempts")
    _expect(isinstance(execution_attempts, list) and len(execution_attempts) <= 1,
            f"{label}.execution_attempts: expected at most one state-bound attempt")
    if mode == "adopt-preexisting":
        _expect(not execution_attempts,
                f"{label}.execution_attempts: adoption cannot contain a push attempt")
    execution_hashes: list[str] = []
    for i, execution_ref in enumerate(execution_attempts):
        elabel = f"{label}.execution_attempts[{i}]"
        _validate_evidence_ref(execution_ref, elabel, evidence_root)
        _expect(execution_ref.get("media_type") == "application/json"
                and execution_ref.get("command") == shlex.join(scope["push_argv"]),
                f"{elabel}: must reference the exact authorized push record")
        expected_path = (
            f"artifacts/publication/execution-g{generation:04d}-{scope_hash[:12]}.json"
        )
        _expect(execution_ref.get("path") == expected_path,
                f"{elabel}.path: noncanonical execution receipt")
        execution_hashes.append(execution_ref["sha256"])
        if evidence_root is not None:
            execution_record = _load_json(
                evidence_root / execution_ref["path"], f"{elabel} record"
            )
            _validate_publication_execution_record(
                execution_record, data, f"{elabel} record", require_success=False
            )
    return {
        "scope_hash": scope_hash,
        "authorization_hash": _value_sha(authorization),
        "supersession_hashes": supersession_hashes,
        "execution_hashes": execution_hashes,
    }


def validate_release_manifest(
    data: dict[str, Any], state: dict[str, Any], evidence_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    label = "release_manifest"
    _artifact_envelope(data, label, state["id"], now)
    _strict_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "publication_required", "not_required_reason", "delivery_kind", "source_revisions",
        "published_revisions", "rendered_revisions", "artifacts",
        "intermediate_revisions",
    }, label)
    _require_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "publication_required", "not_required_reason", "delivery_kind", "source_revisions",
        "published_revisions", "rendered_revisions", "artifacts",
    }, label)
    # intermediate_revisions is OPTIONAL (backward-compatible): a pre-existing manifest
    # that omits it stays valid. When present it binds the chart-bump hop (Capability C).
    intermediate = data.get("intermediate_revisions", [])
    _expect(isinstance(intermediate, list), f"{label}.intermediate_revisions: expected array")
    required = data.get("publication_required")
    _expect(isinstance(required, bool), f"{label}.publication_required: expected bool")
    _expect(required == state.get("publication_required"),
            f"{label}.publication_required does not match state")
    kind = data.get("delivery_kind")
    _expect(kind in {"source-only", "gitops", "mixed", "not-required"},
            f"{label}.delivery_kind: invalid value")
    sources = data.get("source_revisions")
    published = data.get("published_revisions")
    rendered = data.get("rendered_revisions")
    artifacts = data.get("artifacts")
    for field, value in (("source_revisions", sources), ("published_revisions", published),
                         ("rendered_revisions", rendered), ("artifacts", artifacts)):
        _expect(isinstance(value, list), f"{label}.{field}: expected array")
    if not required:
        _nonempty_string(data.get("not_required_reason"), f"{label}.not_required_reason")
        _expect(kind == "not-required", f"{label}.delivery_kind: expected not-required")
        _expect(not sources and not published and not rendered and not artifacts and not intermediate,
                f"{label}: not-required publication cannot carry release claims")
        return {"publication_required": False}
    _expect(data.get("not_required_reason") is None, f"{label}.not_required_reason: must be null when required")
    _expect(kind != "not-required", f"{label}.delivery_kind: not-required conflicts with required publication")
    _expect(bool(sources) and bool(published), f"{label}: required publication needs source and published revisions")

    source_keys: set[tuple[str, str]] = set()
    for i, item in enumerate(sources):
        ilabel = f"{label}.source_revisions[{i}]"
        _expect(isinstance(item, dict), f"{ilabel}: expected object")
        _strict_keys(item, {"repo", "commit"}, ilabel)
        _require_keys(item, {"repo", "commit"}, ilabel)
        key = (_nonempty_string(item.get("repo"), f"{ilabel}.repo"), _commit(item.get("commit"), f"{ilabel}.commit"))
        _expect(key not in source_keys, f"{label}.source_revisions: duplicate {key}")
        source_keys.add(key)

    published_keys: set[tuple[str, str]] = set()
    for i, item in enumerate(published):
        ilabel = f"{label}.published_revisions[{i}]"
        _expect(isinstance(item, dict), f"{ilabel}: expected object")
        _strict_keys(item, {"repo", "remote", "branch", "source_commit", "commit", "verification"}, ilabel)
        _require_keys(item, {"repo", "remote", "branch", "source_commit", "commit", "verification"}, ilabel)
        repo = _nonempty_string(item.get("repo"), f"{ilabel}.repo")
        remote_name = _nonempty_string(item.get("remote"), f"{ilabel}.remote")
        _expect(not remote_name.startswith("-"), f"{ilabel}.remote: option-like value forbidden")
        _nonempty_string(item.get("branch"), f"{ilabel}.branch")
        source_commit = _commit(item.get("source_commit"), f"{ilabel}.source_commit")
        commit = _commit(item.get("commit"), f"{ilabel}.commit")
        _expect(commit == source_commit,
                f"{ilabel}.commit: must be the exact reviewed source commit")
        pub_key = (repo, source_commit)
        _expect(pub_key not in published_keys, f"{label}.published_revisions: duplicate {pub_key}")
        published_keys.add(pub_key)
        verification = item.get("verification")
        _expect(isinstance(verification, dict), f"{ilabel}.verification: expected object")
        verification_keys = {
            "method", "publication_mode", "execution_evidence", "verified_at",
            "observed_commit", "source_matches_published", "exit_code", "evidence",
        }
        _strict_keys(verification, verification_keys, f"{ilabel}.verification")
        _require_keys(verification, verification_keys, f"{ilabel}.verification")
        _expect(verification.get("method") == "git-ls-remote+exact-commit",
                f"{ilabel}.verification.method: v2.0 requires reproducible git-ls-remote+exact-commit")
        publication_mode = verification.get("publication_mode")
        _expect(publication_mode in {"publish", "adopt-preexisting"},
                f"{ilabel}.verification.publication_mode: invalid")
        execution_evidence = verification.get("execution_evidence")
        if publication_mode == "publish":
            _validate_evidence_ref(
                execution_evidence, f"{ilabel}.verification.execution_evidence",
                evidence_root,
            )
            _expect(execution_evidence.get("media_type") == "application/json",
                    f"{ilabel}.verification.execution_evidence: JSON receipt required")
        else:
            _expect(execution_evidence is None,
                    f"{ilabel}.verification.execution_evidence: adoption performs no push")
        verified = _iso(verification.get("verified_at"), f"{ilabel}.verification.verified_at")
        if now is not None:
            _expect(verified <= now, f"{ilabel}.verification.verified_at: future-dated proof forbidden")
        _expect(_commit(verification.get("observed_commit"), f"{ilabel}.verification.observed_commit") == commit,
                f"{ilabel}.verification.observed_commit: must equal published commit")
        _expect(verification.get("source_matches_published") is True,
                f"{ilabel}.verification.source_matches_published: expected true")
        _expect(_integer(verification.get("exit_code"), f"{ilabel}.verification.exit_code") == 0,
                f"{ilabel}.verification.exit_code: expected zero")
        _validate_evidence_ref(verification.get("evidence"), f"{ilabel}.verification.evidence", evidence_root)
    _expect(source_keys == published_keys,
            f"{label}: published revisions must exactly equal source revisions")

    # Capability C — the intermediate (chart-bump) hop. A GitOps render for a *source*
    # milestone is triggered by a bump commit in a DIFFERENT repo (the chart), whose
    # provenance names the chart as its source_repo — not the reviewed Go source. These
    # entries let a rendered revision bind to that chart hop while still threading the
    # Go source through binds_image_tag (the tag the bump set == a source commit short-sha).
    intermediate_keys: set[tuple[str, str]] = set()
    for i, item in enumerate(intermediate):
        ilabel = f"{label}.intermediate_revisions[{i}]"
        _expect(isinstance(item, dict), f"{ilabel}: expected object")
        intermediate_item_keys = {
            "repo", "remote", "branch", "commit", "role", "binds_image_tag",
            "verified_at", "evidence",
        }
        _strict_keys(item, intermediate_item_keys, ilabel)
        _require_keys(item, intermediate_item_keys, ilabel)
        for field in ("repo", "remote", "branch"):
            _nonempty_string(item.get(field), f"{ilabel}.{field}")
        _expect(not str(item["remote"]).startswith("-"),
                f"{ilabel}.remote: option-like value forbidden")
        _expect(item.get("role") == "chart-bump",
                f"{ilabel}.role: v2.0 supports the chart-bump intermediate only")
        intermediate_key = (
            _nonempty_string(item.get("repo"), f"{ilabel}.repo"),
            _commit(item.get("commit"), f"{ilabel}.commit"),
        )
        _expect(intermediate_key not in intermediate_keys,
                f"{label}.intermediate_revisions: duplicate {intermediate_key}")
        intermediate_keys.add(intermediate_key)
        tag = _nonempty_string(item.get("binds_image_tag"), f"{ilabel}.binds_image_tag")
        # Go->chart link: the tag the chart bump set must be a prefix of a declared source
        # commit (the image is tagged with the source short-sha; see image_build.tag_scheme).
        _expect(any(commit[:len(tag)] == tag.lower() for _repo, commit in source_keys),
                f"{ilabel}.binds_image_tag: not a short-sha prefix of any declared source revision")
        verified = _iso(item.get("verified_at"), f"{ilabel}.verified_at")
        if now is not None:
            _expect(verified <= now, f"{ilabel}.verified_at: future-dated proof forbidden")
        _validate_evidence_ref(item.get("evidence"), f"{ilabel}.evidence", evidence_root)

    rendered_keys: set[tuple[str, str]] = set()
    for i, item in enumerate(rendered):
        ilabel = f"{label}.rendered_revisions[{i}]"
        _expect(isinstance(item, dict), f"{ilabel}: expected object")
        _strict_keys(item, {
            "repo", "remote", "branch", "commit", "source_repo", "source_commit",
            "target_ids", "provenance_path", "provenance_sha256", "verified_at", "evidence",
        }, ilabel)
        _require_keys(item, {
            "repo", "remote", "branch", "commit", "source_repo", "source_commit",
            "target_ids", "provenance_path", "provenance_sha256", "verified_at", "evidence",
        }, ilabel)
        for field in ("repo", "remote", "branch", "source_repo"):
            _nonempty_string(item.get(field), f"{ilabel}.{field}")
        _expect(not str(item["remote"]).startswith("-"),
                f"{ilabel}.remote: option-like value forbidden")
        rendered_key = (
            _nonempty_string(item.get("repo"), f"{ilabel}.repo"),
            _commit(item.get("commit"), f"{ilabel}.commit"),
        )
        _expect(rendered_key not in rendered_keys,
                f"{label}.rendered_revisions: duplicate {rendered_key}")
        rendered_keys.add(rendered_key)
        source_key = (
            _nonempty_string(item.get("source_repo"), f"{ilabel}.source_repo"),
            _commit(item.get("source_commit"), f"{ilabel}.source_commit"),
        )
        # A rendered revision binds to EITHER a declared source revision (renderer CI that
        # records the reviewed source directly) OR a declared intermediate/chart-bump hop
        # (the platform's gitops-provenance.py records the chart repo+commit as source).
        _expect(source_key in source_keys or source_key in intermediate_keys,
                f"{ilabel}: rendered revision is not bound to a declared source or intermediate revision")
        target_ids = item.get("target_ids")
        _expect(isinstance(target_ids, list), f"{ilabel}.target_ids: expected array")
        _expect(bool(target_ids) == bool(state.get("operations_required")),
                f"{ilabel}.target_ids: must be non-empty exactly when operations are required")
        normalized_target_ids = [
            _nonempty_string(value, f"{ilabel}.target_ids[]") for value in target_ids
        ]
        _expect(len(normalized_target_ids) == len(set(normalized_target_ids)),
                f"{ilabel}.target_ids: duplicate target")
        provenance_path = PurePosixPath(_nonempty_string(
            item.get("provenance_path"), f"{ilabel}.provenance_path"
        ))
        _expect(not provenance_path.is_absolute() and ".." not in provenance_path.parts,
                f"{ilabel}.provenance_path: must be a safe repository-relative path")
        _sha256_value(item.get("provenance_sha256"), f"{ilabel}.provenance_sha256")
        verified = _iso(item.get("verified_at"), f"{ilabel}.verified_at")
        if now is not None:
            _expect(verified <= now, f"{ilabel}.verified_at: future-dated proof forbidden")
        _validate_evidence_ref(item.get("evidence"), f"{ilabel}.evidence", evidence_root)
    artifact_keys: set[tuple[str, str]] = set()
    for i, item in enumerate(artifacts):
        ilabel = f"{label}.artifacts[{i}]"
        _expect(isinstance(item, dict), f"{ilabel}: expected object")
        _strict_keys(item, {"kind", "uri", "digest", "target_ids", "resolved_at", "evidence"}, ilabel)
        _require_keys(item, {"kind", "uri", "digest", "target_ids", "resolved_at", "evidence"}, ilabel)
        _expect(item.get("kind") == "container",
                f"{ilabel}.kind: v2.0 operational artifacts support containers only")
        uri = _nonempty_string(item.get("uri"), f"{ilabel}.uri")
        digest = _nonempty_string(item.get("digest"), f"{ilabel}.digest")
        _expect(digest.startswith("sha256:") and bool(SHA256_RE.fullmatch(digest[7:])),
                f"{ilabel}.digest: expected sha256:<64 hex>")
        _expect(uri.endswith(f"@{digest}"),
                f"{ilabel}.uri: immutable URI must be digest-qualified with {digest}")
        artifact_key = (uri, digest)
        _expect(artifact_key not in artifact_keys,
                f"{label}.artifacts: duplicate {artifact_key}")
        artifact_keys.add(artifact_key)
        target_ids = item.get("target_ids")
        _expect(isinstance(target_ids, list), f"{ilabel}.target_ids: expected array")
        _expect(bool(target_ids) == bool(state.get("operations_required")),
                f"{ilabel}.target_ids: must be non-empty exactly when operations are required")
        normalized_target_ids = [
            _nonempty_string(value, f"{ilabel}.target_ids[]") for value in target_ids
        ]
        _expect(len(normalized_target_ids) == len(set(normalized_target_ids)),
                f"{ilabel}.target_ids: duplicate target")
        resolved = _iso(item.get("resolved_at"), f"{ilabel}.resolved_at")
        if now is not None:
            _expect(resolved <= now, f"{ilabel}.resolved_at: future-dated proof forbidden")
        _validate_evidence_ref(item.get("evidence"), f"{ilabel}.evidence", evidence_root)
    if kind in {"gitops", "mixed"}:
        _expect(bool(rendered), f"{label}: {kind} delivery requires a rendered revision")
    if kind == "mixed":
        _expect(bool(artifacts), f"{label}: {kind} delivery requires an immutable artifact digest")
    if kind == "source-only":
        _expect(not rendered and not artifacts and not intermediate,
                f"{label}: source-only delivery cannot claim rendered, intermediate, or artifact revisions")
    if kind == "gitops":
        _expect(not artifacts, f"{label}: gitops artifacts require delivery_kind=mixed")
    return {"publication_required": True, "delivery_kind": kind}


def _remote_url(repo: Path, remote: str) -> str:
    resolved = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", remote],
        capture_output=True,
        text=True,
    )
    value = resolved.stdout.strip() if resolved.returncode == 0 else remote
    return _validated_remote_url(value, f"git remote {remote}")


def _validated_remote_url(value: Any, label: str) -> str:
    raw = _nonempty_string(value, label)
    parsed = urlparse(raw)
    _expect(
        not (parsed.scheme.casefold() in {"http", "https"}
             and (parsed.username is not None or parsed.password is not None)),
        f"{label}: HTTP(S) userinfo credentials are forbidden; use a credential helper",
    )
    return raw


def _remote_host(value: str) -> str | None:
    _validated_remote_url(value, "remote URL")
    parsed = urlparse(value)
    if parsed.scheme in {"ssh", "https", "http"} and parsed.hostname:
        return parsed.hostname.casefold()
    match = re.match(r"^(?:[^@/]+@)?([^:/]+):.+$", value)
    return match.group(1).casefold() if match else None


def _endpoint_prefix_match(value: str, prefix: str) -> bool:
    """Match an allowlisted endpoint at a structural boundary.

    Raw ``startswith`` would allow ``registry.example/team-evil`` through a
    reviewed ``registry.example/team`` prefix.  Exact matches are valid; longer
    values must continue at an endpoint/path delimiter.
    """
    if value == prefix:
        return True
    if not value.startswith(prefix):
        return False
    remainder = value[len(prefix):]
    return bool(remainder) and remainder[0] in {"/", ":", "@", "#", "?"}


def _validate_ci_render_block(ci: Any, label: str) -> None:
    """Validate one hash-bound GitLab CI render step (shared by both cascade shapes)."""
    _expect(isinstance(ci, dict), f"{label}: expected object")
    ci_keys = {
        "provider", "project", "source_ref", "pipeline_source", "config_sha256",
        "deploy_job", "protected_environment", "writes_only_render_target",
    }
    _strict_keys(ci, ci_keys, label)
    _require_keys(ci, ci_keys, label)
    _expect(ci.get("provider") == "gitlab",
            f"{label}.provider: v2 supports the reviewed GitLab renderer only")
    for field in ("project", "source_ref", "deploy_job", "protected_environment"):
        _nonempty_string(ci.get(field), f"{label}.{field}")
    _expect(ci.get("pipeline_source") == "push",
            f"{label}.pipeline_source: publication requires the exact push trigger")
    _sha256_value(ci.get("config_sha256"), f"{label}.config_sha256")
    _expect(ci.get("writes_only_render_target") is True,
            f"{label}: unbounded CI writes are forbidden")


def _validate_argo_target_block(
    target: Any, tlabel: str, expected_source_repo: str, expected_source_revision: str,
    *, extra_keys: frozenset[str] = frozenset(),
) -> str:
    """Validate one enumerated Argo auto-sync target and return its id.

    ``expected_source_repo``/``expected_source_revision`` are the protected render
    target the Argo Application must read from — the single render remote for the
    v1 cascade, or the target's own render LEG for the fanout cascade.
    """
    _expect(isinstance(target, dict), f"{tlabel}: expected object")
    # An ArgoCD Application destination is specified by a cluster server URL XOR a
    # registered-cluster name (the platform's commercial ApplicationSets use name).
    has_server = "destination_server" in target
    has_name = "destination_name" in target
    _expect(has_server != has_name,
            f"{tlabel}: exactly one of destination_server / destination_name is required")
    dest_key = "destination_server" if has_server else "destination_name"
    target_keys = {
        "id", "environment", "account", "cluster", "resource",
        "argocd_application", "argocd_server", "argocd_context",
        "argocd_config_path", "argocd_config_sha256",
        "certificate_authority_sha256", "argocd_project", "source_repo_url",
        "source_target_revision", "source_path", dest_key,
        "destination_namespace", "verification_action_sha256", "automated",
    } | set(extra_keys)
    _strict_keys(target, target_keys, tlabel)
    _require_keys(target, target_keys, tlabel)
    tid = _nonempty_string(target.get("id"), f"{tlabel}.id")
    for field in (
        "environment", "account", "cluster", "resource", "argocd_application",
        "argocd_context", "argocd_project", "source_target_revision",
        "source_path", "destination_namespace",
    ):
        _nonempty_string(target.get(field), f"{tlabel}.{field}")
    _expect(target["resource"] == f"Application/{target['argocd_application']}",
            f"{tlabel}.resource: must bind the exact Argo Application")
    _validated_remote_url(target.get("source_repo_url"), f"{tlabel}.source_repo_url")
    _expect(target["source_repo_url"] == expected_source_repo
            and target["source_target_revision"] == expected_source_revision,
            f"{tlabel}: Argo source must equal the protected render target")
    source_path = target["source_path"]
    _expect(not source_path.startswith("/")
            and ".." not in PurePosixPath(source_path).parts,
            f"{tlabel}.source_path: safe repo-relative path required")
    argocd_server = urlparse(_nonempty_string(target.get("argocd_server"), f"{tlabel}.argocd_server"))
    _expect(argocd_server.scheme.casefold() == "https" and not argocd_server.username,
            f"{tlabel}.argocd_server: credential-free HTTPS endpoint required")
    if has_server:
        dest = urlparse(_nonempty_string(target.get("destination_server"), f"{tlabel}.destination_server"))
        _expect(dest.scheme.casefold() == "https" and not dest.username,
                f"{tlabel}.destination_server: credential-free HTTPS endpoint required")
    else:
        _nonempty_string(target.get("destination_name"), f"{tlabel}.destination_name")
    config_path = _nonempty_string(
        target.get("argocd_config_path"), f"{tlabel}.argocd_config_path"
    )
    _expect(os.path.isabs(config_path), f"{tlabel}.argocd_config_path: absolute path required")
    _sha256_value(target.get("argocd_config_sha256"), f"{tlabel}.argocd_config_sha256")
    _sha256_value(
        target.get("certificate_authority_sha256"),
        f"{tlabel}.certificate_authority_sha256",
    )
    _sha256_value(
        target.get("verification_action_sha256"),
        f"{tlabel}.verification_action_sha256",
    )
    automated = target.get("automated")
    _expect(isinstance(automated, dict), f"{tlabel}.automated: expected object")
    _strict_keys(automated, {"enabled", "prune", "self_heal", "allow_empty"},
                 f"{tlabel}.automated")
    _require_keys(automated, {"enabled", "prune", "self_heal", "allow_empty"},
                  f"{tlabel}.automated")
    _expect(automated.get("enabled") is True,
            f"{tlabel}.automated.enabled: auto-sync must be explicitly enabled")
    _expect(all(isinstance(automated[field], bool) for field in automated),
            f"{tlabel}.automated: exact booleans required")
    return tid


def _validate_automatic_gitops_contract(
    value: Any, render_prefixes: list[Any], label: str,
    *, artifact_prefixes: list[Any] | None = None,
) -> dict[str, Any]:
    """Validate the reviewed, finite source -> CI -> render -> Argo cascade.

    Two reviewed shapes are accepted, discriminated by ``kind``:
      * ``ci-render-argocd-auto-sync-v1`` — one source publication, one CI render,
        one protected render publication, one Argo auto-sync per target.
      * ``ci-render-argocd-auto-sync-fanout-v1`` — the platform source -> image ->
        chart-bump -> N-deploy-repo fan-out: an image build, an intermediate
        chart-bump, one protected render leg per deploy repo, and one Argo
        auto-sync per target bound to its leg.
    Unknown or omitted cascade steps are never silently authorized.
    """
    _expect(isinstance(value, dict), f"{label}: expected object")
    kind = value.get("kind")
    if kind == "ci-render-argocd-auto-sync-v1":
        return _validate_single_leg_gitops(value, render_prefixes, label)
    if kind == "ci-render-argocd-auto-sync-fanout-v1":
        return _validate_fanout_gitops(value, render_prefixes, artifact_prefixes, label)
    _fail(f"{label}.kind: generic or unknown auto-sync is forbidden")


def _validate_single_leg_gitops(
    value: dict[str, Any], render_prefixes: list[Any], label: str,
) -> dict[str, Any]:
    _strict_keys(value, {"kind", "render", "ci_render", "targets", "cascade_steps"}, label)
    _require_keys(value, {"kind", "render", "ci_render", "targets", "cascade_steps"}, label)
    render = value.get("render")
    _expect(isinstance(render, dict), f"{label}.render: expected object")
    _strict_keys(render, {"remote", "branch", "protected", "provenance_path"},
                 f"{label}.render")
    _require_keys(render, {"remote", "branch", "protected", "provenance_path"},
                  f"{label}.render")
    render_remote = _validated_remote_url(render.get("remote"), f"{label}.render.remote")
    _expect(any(_endpoint_prefix_match(render_remote, str(prefix)) for prefix in render_prefixes),
            f"{label}.render.remote: outside reviewed render allowlist")
    render_branch = _nonempty_string(render.get("branch"), f"{label}.render.branch")
    _expect(not render_branch.startswith("-"), f"{label}.render.branch: option-like value")
    _expect(render.get("protected") is True,
            f"{label}.render.protected: auto-sync requires a protected deploy branch")
    provenance_path = _nonempty_string(
        render.get("provenance_path"), f"{label}.render.provenance_path"
    )
    _expect(not provenance_path.startswith("/")
            and ".." not in PurePosixPath(provenance_path).parts,
            f"{label}.render.provenance_path: safe repo-relative path required")
    _validate_ci_render_block(value.get("ci_render"), f"{label}.ci_render")
    targets = value.get("targets")
    _expect(isinstance(targets, list) and bool(targets),
            f"{label}.targets: exact non-empty target set required")
    target_ids: list[str] = []
    target_slugs: list[str] = []
    for i, target in enumerate(targets):
        tlabel = f"{label}.targets[{i}]"
        tid = _validate_argo_target_block(target, tlabel, render_remote, render_branch)
        _expect(tid not in target_ids, f"{label}.targets: duplicate id {tid!r}")
        target_ids.append(tid)
        target_slugs.append(re.sub(r"[^A-Za-z0-9._-]+", "-", tid).strip("-") or "target")
    _expect(len(target_slugs) == len(set(target_slugs)),
            f"{label}.targets: ids collide after path normalization")
    steps = value.get("cascade_steps")
    _expect(isinstance(steps, list), f"{label}.cascade_steps: expected array")
    expected_steps = [
        {"id": "source-publication", "kind": "source-publication", "depends_on": [],
         "target_id": None},
        {"id": "ci-render", "kind": "ci-render", "depends_on": ["source-publication"],
         "target_id": None},
        {"id": "render-publication", "kind": "render-publication",
         "depends_on": ["ci-render"], "target_id": None},
        *[
            {"id": f"argocd-auto-sync-{slug}", "kind": "argocd-auto-sync",
             "depends_on": ["render-publication"], "target_id": tid}
            for tid, slug in sorted(zip(target_ids, target_slugs))
        ],
    ]
    _expect(steps == expected_steps,
            f"{label}.cascade_steps: every material cascade step must be exact, ordered, and authorized")
    return json.loads(json.dumps(value))


def _validate_fanout_gitops(
    value: dict[str, Any], render_prefixes: list[Any],
    artifact_prefixes: list[Any] | None, label: str,
) -> dict[str, Any]:
    _strict_keys(value, {"kind", "image_build", "chart", "render_legs", "targets", "cascade_steps"}, label)
    _require_keys(value, {"kind", "image_build", "chart", "render_legs", "targets", "cascade_steps"}, label)
    # image-build hop: authorizes the source CI pushing to a reviewed ECR repo.
    image_build = value.get("image_build")
    _expect(isinstance(image_build, dict), f"{label}.image_build: expected object")
    ib_keys = {"provider", "project", "registry_repo", "tag_scheme"}
    _strict_keys(image_build, ib_keys, f"{label}.image_build")
    _require_keys(image_build, ib_keys, f"{label}.image_build")
    _expect(image_build.get("provider") == "gitlab",
            f"{label}.image_build.provider: v2 supports the reviewed GitLab builder only")
    _nonempty_string(image_build.get("project"), f"{label}.image_build.project")
    registry_repo = _nonempty_string(image_build.get("registry_repo"), f"{label}.image_build.registry_repo")
    _expect(bool(artifact_prefixes) and any(
        _endpoint_prefix_match(registry_repo, str(prefix)) for prefix in artifact_prefixes),
        f"{label}.image_build.registry_repo: outside reviewed artifact registry allowlist")
    _expect(image_build.get("tag_scheme") == "source-short-sha",
            f"{label}.image_build.tag_scheme: only source-short-sha is supported")
    # intermediate chart-bump hop: the repo the source CI bumps to trigger the render.
    chart = value.get("chart")
    _expect(isinstance(chart, dict), f"{label}.chart: expected object")
    _strict_keys(chart, {"remote", "branch", "bump_path"}, f"{label}.chart")
    _require_keys(chart, {"remote", "branch", "bump_path"}, f"{label}.chart")
    chart_remote = _validated_remote_url(chart.get("remote"), f"{label}.chart.remote")
    _expect(any(_endpoint_prefix_match(chart_remote, str(prefix)) for prefix in render_prefixes),
            f"{label}.chart.remote: outside reviewed render allowlist")
    chart_branch = _nonempty_string(chart.get("branch"), f"{label}.chart.branch")
    _expect(not chart_branch.startswith("-"), f"{label}.chart.branch: option-like value")
    bump_path = _nonempty_string(chart.get("bump_path"), f"{label}.chart.bump_path")
    _expect(not bump_path.startswith("/") and ".." not in PurePosixPath(bump_path).parts,
            f"{label}.chart.bump_path: safe repo-relative path required")
    # render legs: one protected deploy repo per leg.
    render_legs = value.get("render_legs")
    _expect(isinstance(render_legs, list) and bool(render_legs),
            f"{label}.render_legs: exact non-empty leg set required")
    legs: dict[str, dict[str, str]] = {}
    leg_id_re = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
    for i, leg in enumerate(render_legs):
        llabel = f"{label}.render_legs[{i}]"
        _expect(isinstance(leg, dict), f"{llabel}: expected object")
        leg_keys = {"id", "remote", "branch", "protected", "provenance_path", "ci_render"}
        _strict_keys(leg, leg_keys, llabel)
        _require_keys(leg, leg_keys, llabel)
        leg_id = _nonempty_string(leg.get("id"), f"{llabel}.id")
        _expect(bool(leg_id_re.fullmatch(leg_id)), f"{llabel}.id: unsafe render leg id")
        _expect(leg_id not in legs, f"{label}.render_legs: duplicate leg id {leg_id!r}")
        leg_remote = _validated_remote_url(leg.get("remote"), f"{llabel}.remote")
        _expect(any(_endpoint_prefix_match(leg_remote, str(prefix)) for prefix in render_prefixes),
                f"{llabel}.remote: outside reviewed render allowlist")
        leg_branch = _nonempty_string(leg.get("branch"), f"{llabel}.branch")
        _expect(not leg_branch.startswith("-"), f"{llabel}.branch: option-like value")
        _expect(leg.get("protected") is True,
                f"{llabel}.protected: auto-sync requires a protected deploy branch")
        leg_prov = _nonempty_string(leg.get("provenance_path"), f"{llabel}.provenance_path")
        _expect(not leg_prov.startswith("/") and ".." not in PurePosixPath(leg_prov).parts,
                f"{llabel}.provenance_path: safe repo-relative path required")
        _validate_ci_render_block(leg.get("ci_render"), f"{llabel}.ci_render")
        legs[leg_id] = {"remote": leg_remote, "branch": leg_branch}
    # targets, each bound to a declared leg.
    targets = value.get("targets")
    _expect(isinstance(targets, list) and bool(targets),
            f"{label}.targets: exact non-empty target set required")
    target_ids: list[str] = []
    target_slugs: list[str] = []
    target_leg: dict[str, str] = {}
    for i, target in enumerate(targets):
        tlabel = f"{label}.targets[{i}]"
        _expect(isinstance(target, dict), f"{tlabel}: expected object")
        leg_id = _nonempty_string(target.get("render_leg_id"), f"{tlabel}.render_leg_id")
        _expect(leg_id in legs,
                f"{tlabel}.render_leg_id: does not resolve to a declared render leg")
        leg = legs[leg_id]
        tid = _validate_argo_target_block(
            target, tlabel, leg["remote"], leg["branch"],
            extra_keys=frozenset({"render_leg_id"}),
        )
        _expect(tid not in target_ids, f"{label}.targets: duplicate id {tid!r}")
        target_ids.append(tid)
        target_slugs.append(re.sub(r"[^A-Za-z0-9._-]+", "-", tid).strip("-") or "target")
        target_leg[tid] = leg_id
    _expect(len(target_slugs) == len(set(target_slugs)),
            f"{label}.targets: ids collide after path normalization")
    # the exact fanned-out DAG.
    steps = value.get("cascade_steps")
    _expect(isinstance(steps, list), f"{label}.cascade_steps: expected array")
    expected_steps: list[dict[str, Any]] = [
        {"id": "source-publication", "kind": "source-publication", "depends_on": [],
         "target_id": None, "render_leg_id": None},
        {"id": "image-build", "kind": "image-build", "depends_on": ["source-publication"],
         "target_id": None, "render_leg_id": None},
        {"id": "chart-bump", "kind": "chart-bump", "depends_on": ["image-build"],
         "target_id": None, "render_leg_id": None},
    ]
    for leg_id in sorted(legs):
        expected_steps.append(
            {"id": f"ci-render-{leg_id}", "kind": "ci-render", "depends_on": ["chart-bump"],
             "target_id": None, "render_leg_id": leg_id})
        expected_steps.append(
            {"id": f"render-publication-{leg_id}", "kind": "render-publication",
             "depends_on": [f"ci-render-{leg_id}"], "target_id": None, "render_leg_id": leg_id})
    for tid, slug in sorted(zip(target_ids, target_slugs)):
        leg_id = target_leg[tid]
        expected_steps.append(
            {"id": f"argocd-auto-sync-{slug}", "kind": "argocd-auto-sync",
             "depends_on": [f"render-publication-{leg_id}"], "target_id": tid,
             "render_leg_id": leg_id})
    _expect(steps == expected_steps,
            f"{label}.cascade_steps: every material cascade step must be exact, ordered, and authorized")
    return json.loads(json.dumps(value))


def _reviewed_delivery_policy(
    repo: Path, reviewed_commit: str, source_remote: str, *, required: bool,
) -> dict[str, Any] | None:
    """Read the exact reviewed policy locally; never infer it from the worktree."""
    policy_path = ".milestone-pipeline/trust-policy.json"
    exists = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{reviewed_commit}:{policy_path}"],
        capture_output=True,
    )
    if exists.returncode != 0:
        _expect(
            not required,
            f"{policy_path}: required for publication but missing from reviewed commit",
        )
        return None
    policy_bytes = _git_output(repo, "show", f"{reviewed_commit}:{policy_path}")
    try:
        policy = json.loads(policy_bytes)
    except json.JSONDecodeError as exc:
        _fail(f"{policy_path}: invalid JSON: {exc.msg}")
    _expect(isinstance(policy, dict), f"{policy_path}: expected object")
    policy_keys = {
        "schema_version", "source_remote", "render_remote_prefixes",
        "artifact_registry_prefixes", "artifact_resolver", "automatic_gitops",
    }
    _strict_keys(policy, policy_keys, policy_path)
    _require_keys(policy, policy_keys - {"automatic_gitops"}, policy_path)
    version = policy.get("schema_version")
    _expect(version in {1, 2}, f"{policy_path}.schema_version: expected 1 or 2")
    _expect(policy.get("source_remote") == source_remote,
            f"{policy_path}.source_remote: differs from closure-bound origin")
    render_prefixes = policy.get("render_remote_prefixes")
    artifact_prefixes = policy.get("artifact_registry_prefixes")
    _expect(isinstance(render_prefixes, list) and isinstance(artifact_prefixes, list),
            f"{policy_path}: prefix fields must be arrays")
    for field, values in (
        ("render_remote_prefixes", render_prefixes),
        ("artifact_registry_prefixes", artifact_prefixes),
    ):
        for i, prefix in enumerate(values):
            _nonempty_string(prefix, f"{policy_path}.{field}[{i}]")
        _expect(len(values) == len(set(values)),
                f"{policy_path}.{field}: duplicate prefix")
    resolver = policy.get("artifact_resolver")
    if resolver is not None:
        _expect(isinstance(resolver, dict),
                f"{policy_path}.artifact_resolver: expected object or null")
        _strict_keys(resolver, {"path", "sha256"}, f"{policy_path}.artifact_resolver")
        _require_keys(resolver, {"path", "sha256"}, f"{policy_path}.artifact_resolver")
        resolver_path = _nonempty_string(
            resolver.get("path"), f"{policy_path}.artifact_resolver.path"
        )
        _expect(os.path.isabs(resolver_path),
                f"{policy_path}.artifact_resolver.path: expected absolute path")
        _sha256_value(resolver.get("sha256"), f"{policy_path}.artifact_resolver.sha256")
    if version == 1:
        _expect("automatic_gitops" not in policy,
                f"{policy_path}: automatic effects require schema_version 2")
    else:
        _require_keys(policy, {"automatic_gitops"}, policy_path)
        policy["automatic_gitops"] = _validate_automatic_gitops_contract(
            policy.get("automatic_gitops"), render_prefixes,
            f"{policy_path}.automatic_gitops",
            artifact_prefixes=artifact_prefixes,
        )
    return policy


def _reviewed_automatic_gitops(
    repo: Path, reviewed_commit: str, source_remote: str,
) -> dict[str, Any] | None:
    policy = _reviewed_delivery_policy(
        repo, reviewed_commit, source_remote, required=False,
    )
    if policy is None or policy["schema_version"] == 1:
        return None
    return policy["automatic_gitops"]


def _preflight_publication_delivery_policy(
    state: dict[str, Any], item: dict[str, Any], repo: Path,
) -> None:
    """Fail closed before remote discovery whenever publication is required."""
    if state.get("publication_required") is True:
        _reviewed_delivery_policy(
            repo, item["head_commit"], item["remote_url"], required=True,
        )


def _publication_scope_matches_implementation(
    scope: dict[str, Any], item: dict[str, Any],
) -> None:
    """Bind a preserved intent to the implementation revalidated for this apply."""
    expected = {
        "repo": item["repo"],
        "remote": "origin",
        "remote_url": item["remote_url"],
        "branch": item["branch"],
        "commit": item["head_commit"].lower(),
    }
    observed = {field: scope.get(field) for field in expected}
    _expect(
        observed == expected,
        "publication-apply: persisted intent scope does not equal the current closure-bound implementation",
    )


def _validate_delivery_endpoints(
    release: dict[str, Any], source_remote: str, repo: Path, reviewed_commit: str,
    *, verify_executables: bool = True,
) -> dict[str, Any]:
    if ALLOW_LOCAL_DELIVERY_ENDPOINTS:
        resolver = None
        if TEST_ARTIFACT_RESOLVER is not None:
            resolver = {
                "path": TEST_ARTIFACT_RESOLVER[0],
                "sha256": TEST_ARTIFACT_RESOLVER[1],
            }
        return {"artifact_resolver": resolver}
    source_host = _remote_host(source_remote)
    _expect(source_host is not None,
            "canonical source origin must be an ssh/http(s) remote, not a local path")
    policy_path = ".milestone-pipeline/trust-policy.json"
    policy = _reviewed_delivery_policy(
        repo, reviewed_commit, source_remote, required=True,
    )
    _expect(policy is not None, f"{policy_path}: required policy unexpectedly absent")
    render_prefixes = policy.get("render_remote_prefixes")
    artifact_prefixes = policy.get("artifact_registry_prefixes")
    for i, item in enumerate(release["rendered_revisions"]):
        _expect(any(_endpoint_prefix_match(item["remote"], prefix) for prefix in render_prefixes),
                f"release_manifest.rendered_revisions[{i}].remote: not allowed by reviewed trust policy")
    for i, item in enumerate(release["artifacts"]):
        _expect(any(_endpoint_prefix_match(item["uri"], prefix) for prefix in artifact_prefixes),
                f"release_manifest.artifacts[{i}].uri: not allowed by reviewed trust policy")
    resolver = policy.get("artifact_resolver")
    if resolver is not None:
        resolver_path = _nonempty_string(
            resolver.get("path"), f"{policy_path}.artifact_resolver.path"
        )
        resolver_sha = _sha256_value(
            resolver.get("sha256"), f"{policy_path}.artifact_resolver.sha256"
        )
        _expect(os.path.isabs(resolver_path),
                f"{policy_path}.artifact_resolver.path: expected absolute path")
        if verify_executables:
            _resolved_executable(
                [resolver_path], f"{policy_path}.artifact_resolver",
                require_absolute=True, expected_sha256=resolver_sha,
            )
            _validate_artifact_resolver_trust(
                resolver_path, f"{policy_path}.artifact_resolver"
            )
    if release["artifacts"]:
        _expect(
            resolver is not None,
            f"{policy_path}.artifact_resolver: required for immutable artifact verification",
        )
    return policy


def _validate_artifact_resolver_trust(path_raw: str, label: str) -> None:
    path = Path(path_raw).resolve()
    roots = tuple(
        candidate.resolve() for candidate in map(
            Path, ("/bin", "/usr/bin", "/usr/local/Cellar", "/opt/homebrew/Cellar")
        ) if candidate.exists()
    )
    _expect(path.name == "crane" and any(
        path == root or root in path.parents for root in roots
    ), f"{label}: v2 permits only a hashed system/package-manager crane binary")


def _remote_branch_head(remote: str, branch: str, label: str) -> str:
    git_raw = shutil.which("git")
    _expect(git_raw is not None, f"{label}: git executable unavailable")
    git_path = str(Path(git_raw).resolve())
    environment, _known_path, _known_sha = _publication_environment(remote)
    try:
        proc = subprocess.run(
            [git_path, "-c", "core.hooksPath=/dev/null", "ls-remote", "--heads",
             remote, f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=environment["HOME"],
            env=environment,
        )
    except subprocess.TimeoutExpired:
        _fail(f"{label}: git ls-remote timed out")
    _expect(proc.returncode == 0,
            f"{label}: git ls-remote failed with exit {proc.returncode}")
    rows = [line.split() for line in proc.stdout.splitlines() if line.strip()]
    _expect(len(rows) == 1 and len(rows[0]) == 2,
            f"{label}: expected exactly one remote branch ref")
    _expect(rows[0][1] == f"refs/heads/{branch}",
            f"{label}: remote returned an unexpected ref {rows[0][1]!r}")
    return _commit(rows[0][0], f"{label}.remote_head")


def _rendered_provenance(
    remote: str, branch: str, expected_commit: str, path: str, label: str
) -> bytes:
    """Fetch one rendered ref into an isolated temp repository and read its
    machine-owned source provenance blob. No target repository or remote state
    is mutated."""
    with tempfile.TemporaryDirectory(prefix="milestone-render-proof-") as td:
        repo = Path(td)
        git_raw = shutil.which("git")
        _expect(git_raw is not None, f"{label}: git executable unavailable")
        git_path = str(Path(git_raw).resolve())
        environment, _known_path, _known_sha = _publication_environment(remote)
        init = subprocess.run(
            [git_path, "-c", "core.hooksPath=/dev/null", "init", "-q", str(repo)],
            capture_output=True, env=environment, cwd=environment["HOME"],
        )
        _expect(init.returncode == 0, f"{label}: isolated provenance repo init failed")
        try:
            fetch = subprocess.run(
                [git_path, "-c", "core.hooksPath=/dev/null", "-C", str(repo),
                 "fetch", "--quiet", "--depth=1", "--no-tags",
                 remote, f"refs/heads/{branch}"],
                capture_output=True,
                timeout=60,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            _fail(f"{label}: rendered provenance fetch timed out")
        _expect(fetch.returncode == 0,
                f"{label}: rendered provenance fetch failed with exit {fetch.returncode}")
        rev_parse = subprocess.run(
            [git_path, "-c", "core.hooksPath=/dev/null", "-C", str(repo),
             "rev-parse", "FETCH_HEAD"], capture_output=True, env=environment,
        )
        _expect(rev_parse.returncode == 0, f"{label}: fetched commit is unreadable")
        fetched = _commit(
            rev_parse.stdout.decode().strip(),
            f"{label}.fetched_commit",
        )
        _expect(fetched == expected_commit.lower(),
                f"{label}: rendered branch moved while provenance was collected")
        show = subprocess.run(
            [git_path, "-c", "core.hooksPath=/dev/null", "-C", str(repo),
             "show", f"FETCH_HEAD:{path}"],
            capture_output=True,
            env=environment,
        )
        _expect(show.returncode == 0,
                f"{label}.provenance_path: file is absent from rendered commit")
        return show.stdout


def _validate_release_against_implementation(
    release: dict[str, Any], implementation: dict[str, Any], state_path: Path,
    *, verify_live_publication: bool = True,
) -> None:
    """Bind publication claims to the reviewed implementation.

    Live branch heads/render provenance/artifact resolution are publication-time
    facts. Later phases revalidate the immutable receipt and static cross-links
    without requiring shared branches or local toolchains to stay frozen.
    """
    if not release["publication_required"]:
        return
    repo_root = _repo_root(state_path)
    repos = implementation["repositories"]
    _expect(len(repos) == 1,
            "release binding requires the single-source-repository v2.0 contract")
    impl = repos[0]
    expected_sources = {(impl["repo"], impl["head_commit"].lower())}
    trust_policy = _validate_delivery_endpoints(
        release, impl["remote_url"], repo_root, impl["head_commit"].lower(),
        verify_executables=verify_live_publication,
    )
    actual_sources = {
        (item["repo"], item["commit"].lower())
        for item in release["source_revisions"]
    }
    _expect(actual_sources == expected_sources,
            "release_manifest.source_revisions must exactly equal the reviewed implementation head")

    for i, item in enumerate(release["published_revisions"]):
        label = f"release_manifest.published_revisions[{i}]"
        _expect(item["repo"] == impl["repo"],
                f"{label}.repo: publication is not for the reviewed source repository")
        _expect(item["source_commit"].lower() == impl["head_commit"].lower(),
                f"{label}.source_commit: publication is not bound to implementation head")
        _expect(item["commit"].lower() == item["source_commit"].lower(),
                f"{label}.commit: v2.0 publication must be the exact reviewed commit")
        _expect(item["remote"] == "origin",
                f"{label}.remote: v2.0 source publication must use canonical origin")
        _expect(item["branch"] == impl["branch"],
                f"{label}.branch: must equal reviewed implementation branch {impl['branch']!r}")
        if verify_live_publication:
            remote = _remote_url(repo_root, item["remote"])
            _expect(remote == impl["remote_url"],
                    f"{label}.remote: current origin differs from closure-bound remote_url")
            live_head = _remote_branch_head(remote, item["branch"], label)
            _expect(live_head == item["commit"].lower(),
                    f"{label}.commit: live remote head is {live_head}, not {item['commit'].lower()}")
            _expect(live_head == item["verification"]["observed_commit"].lower(),
                    f"{label}.verification.observed_commit: stale relative to live remote")
    provenance_artifacts: set[tuple[str, str, tuple[str, ...]]] = set()
    for i, item in enumerate(release["rendered_revisions"] if verify_live_publication else []):
        label = f"release_manifest.rendered_revisions[{i}]"
        live_head = _remote_branch_head(item["remote"], item["branch"], label)
        _expect(live_head == item["commit"].lower(),
                f"{label}.commit: live rendered remote head is {live_head}, not {item['commit'].lower()}")
        provenance = _rendered_provenance(
            item["remote"], item["branch"], item["commit"],
            item["provenance_path"], label,
        )
        _expect(hashlib.sha256(provenance).hexdigest() == item["provenance_sha256"],
                f"{label}.provenance_sha256: live provenance blob changed")
        try:
            claim = json.loads(provenance)
        except json.JSONDecodeError as exc:
            _fail(f"{label}.provenance_path: invalid JSON: {exc.msg}")
        _expect(isinstance(claim, dict) and set(claim) == {
            "source_repo", "source_commit", "target_ids", "artifacts"
        }, f"{label}.provenance_path: expected source_repo/source_commit/target_ids/artifacts")
        _expect(
            claim["source_repo"] == item["source_repo"]
            and _commit(claim["source_commit"], f"{label}.provenance.source_commit")
            == item["source_commit"].lower(),
            f"{label}.provenance_path: rendered commit is not bound to the reviewed source",
        )
        _expect(
            sorted(claim.get("target_ids") or []) == sorted(item["target_ids"]),
            f"{label}.provenance.target_ids: rendered target scope changed",
        )
        claim_artifacts = claim.get("artifacts")
        _expect(isinstance(claim_artifacts, list),
                f"{label}.provenance.artifacts: expected array")
        for j, artifact in enumerate(claim_artifacts):
            alabel = f"{label}.provenance.artifacts[{j}]"
            _expect(isinstance(artifact, dict) and set(artifact) == {"uri", "digest", "target_ids"},
                    f"{alabel}: expected exactly uri/digest/target_ids")
            uri = _nonempty_string(artifact.get("uri"), f"{alabel}.uri")
            digest = _nonempty_string(artifact.get("digest"), f"{alabel}.digest")
            _expect(digest.startswith("sha256:") and bool(SHA256_RE.fullmatch(digest[7:])),
                    f"{alabel}.digest: expected sha256 digest")
            _expect(uri.endswith(f"@{digest}"),
                    f"{alabel}.uri: expected digest-qualified immutable URI")
            artifact_target_ids = artifact.get("target_ids")
            _expect(isinstance(artifact_target_ids, list),
                    f"{alabel}.target_ids: expected array")
            normalized_target_ids = tuple(sorted(
                _nonempty_string(value, f"{alabel}.target_ids[]")
                for value in artifact_target_ids
            ))
            _expect(len(normalized_target_ids) == len(set(normalized_target_ids)),
                    f"{alabel}.target_ids: duplicate target")
            key = (uri, digest, normalized_target_ids)
            _expect(key not in provenance_artifacts,
                    f"{alabel}: duplicate rendered artifact provenance")
            provenance_artifacts.add(key)

    released_artifacts = {
        (item["uri"], item["digest"], tuple(sorted(item["target_ids"])))
        for item in release["artifacts"]
    }
    # Capability B1 — the platform renderer records NO image digest in provenance
    # (`artifacts: []`; the image is pinned by a mutable tag). Every provenance-bound
    # artifact must still be released, but a released artifact absent from provenance is
    # admissible only when it is "tag-bound": a declared intermediate chart-bump tag
    # (binds_image_tag == the source short-sha) live-resolves to that immutable digest.
    intermediate_tags = sorted({
        str(item["binds_image_tag"]).lower()
        for item in release.get("intermediate_revisions", [])
    })
    tag_bound: set[tuple[str, str, tuple[str, ...]]] = set()
    if verify_live_publication:
        _expect(provenance_artifacts <= released_artifacts,
                "every artifact bound by rendered provenance must also be released")
        tag_bound = released_artifacts - provenance_artifacts
        for uri, _digest, _target_ids in sorted(tag_bound):
            _expect(bool(intermediate_tags),
                    f"release artifact {uri}: not bound by rendered provenance and no intermediate "
                    "chart-bump tag is declared to bind it")
    if released_artifacts and verify_live_publication:
        resolver_policy = trust_policy.get("artifact_resolver")
        _expect(isinstance(resolver_policy, dict),
                "reviewed trust policy must bind the artifact resolver")
        resolver = _nonempty_string(
            resolver_policy.get("path"), "trust policy artifact_resolver.path"
        )
        resolver_sha = _sha256_value(
            resolver_policy.get("sha256"), "trust policy artifact_resolver.sha256"
        )
        _resolved_executable(
            [resolver], "trust policy artifact_resolver", require_absolute=True,
            expected_sha256=resolver_sha,
        )
        if not ALLOW_LOCAL_DELIVERY_ENDPOINTS:
            _validate_artifact_resolver_trust(
                resolver, "trust policy artifact_resolver"
            )
        resolver_home = Path(tempfile.gettempdir()) / f"workspace-resolver-isolation-{os.getpid()}"
        if resolver_home.exists():
            shutil.rmtree(resolver_home)
        resolver_home.mkdir(mode=0o700)
        resolver_environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(resolver_home), "XDG_CONFIG_HOME": str(resolver_home),
        }

        def _resolve_digest(reference: str) -> tuple[int, str]:
            try:
                proc = subprocess.run(
                    [str(resolver), "digest", reference], capture_output=True, text=True,
                    timeout=30, cwd=str(resolver_home), env=resolver_environment,
                )
            except subprocess.TimeoutExpired:
                _fail(f"artifact resolver timed out for {reference}")
            return proc.returncode, proc.stdout.strip()

        for uri, digest, target_ids in sorted(released_artifacts):
            code, observed = _resolve_digest(uri)
            _expect(code == 0,
                    f"artifact resolver failed for {uri} with exit {code}")
            _expect(observed == digest,
                    f"artifact resolver observed {observed!r}, expected {digest!r} for {uri}")
            if (uri, digest, target_ids) in tag_bound:
                # B1: prove the mutable chart-bump tag resolves to this immutable digest.
                registry_repo = uri.split("@", 1)[0]
                matched = False
                for tag in intermediate_tags:
                    tag_code, tag_observed = _resolve_digest(f"{registry_repo}:{tag}")
                    if tag_code == 0 and tag_observed == digest:
                        matched = True
                        break
                _expect(matched,
                        f"tag-bound artifact {uri}: no declared chart-bump tag resolves to this digest")


def _validate_publication_intent_against_release(
    intent: dict[str, Any], release: dict[str, Any], implementation: dict[str, Any],
    state_path: Path,
) -> None:
    _expect(len(implementation["repositories"]) == 1,
            "publication intent requires one reviewed source repository")
    impl = implementation["repositories"][0]
    scope = intent["scope"]
    expected_scope = {
        "mode": scope["mode"],
        "repo": impl["repo"],
        "remote": "origin",
        "remote_url": impl["remote_url"],
        "branch": impl["branch"],
        "commit": impl["head_commit"].lower(),
        "expected_remote_head": scope.get("expected_remote_head"),
        "git_executable_path": scope["git_executable_path"],
        "git_executable_sha256": scope["git_executable_sha256"],
        "execution_environment": scope["execution_environment"],
        "isolated_git_dir": scope["isolated_git_dir"],
        "alternate_object_directory": scope["alternate_object_directory"],
        "ssh_known_hosts_path": scope["ssh_known_hosts_path"],
        "ssh_known_hosts_sha256": scope["ssh_known_hosts_sha256"],
        "push_argv": scope["push_argv"],
    }
    _expect(scope == expected_scope,
            "publication_intent.scope: does not equal the closure-bound publication target")
    published = release.get("published_revisions")
    _expect(isinstance(published, list) and len(published) == 1,
            "publication intent v2.0 requires exactly one published source revision")
    item = published[0]
    _expect(
        item["repo"] == scope["repo"]
        and item["remote"] == scope["remote"]
        and item["branch"] == scope["branch"]
        and item["commit"].lower() == scope["commit"],
        "release_manifest publication does not equal the human-authorized intent scope",
    )
    verification = item["verification"]
    _expect(verification["publication_mode"] == scope["mode"],
            "release publication mode differs from the authorized intent")
    evidence_root = _state_dir(state_path)
    git_path = scope["git_executable_path"]
    ref = f"refs/heads/{scope['branch']}"
    expected_push = scope["push_argv"]
    if scope["mode"] == "publish":
        execution_ref = verification["execution_evidence"]
        _expect(intent.get("execution_attempts") == [execution_ref],
                "release publication execution is not the state-bound intent attempt")
        _expect(
            execution_ref["path"]
            == (
                f"artifacts/publication/execution-g{intent['generation']:04d}-"
                f"{intent['scope_hash'][:12]}.json"
            ),
            "release publication execution evidence path is noncanonical",
        )
        record = _load_json(
            evidence_root / execution_ref["path"], "publication execution evidence"
        )
        _validate_publication_execution_record(
            record, intent, "publication execution evidence", require_success=False
        )
        execution_keys = {
            "schema_version", "producer", "intent_id", "intent_generation",
            "scope_hash", "result_kind", "argv", "command", "environment",
            "executable_path", "executable_sha256",
            "started_at", "completed_at", "exit_code", "stdout", "stderr",
            "stdout_sha256", "stderr_sha256", "stdout_truncated", "stderr_truncated",
            "output_limit_bytes", "background_processes_terminated", "timed_out",
        }
        _strict_keys(record, execution_keys, "publication execution evidence")
        _require_keys(record, execution_keys, "publication execution evidence")
        _expect(record["schema_version"] == 1 and record["producer"] == intent["producer"],
                "publication execution evidence: deterministic writer required")
        _expect(record["intent_id"] == intent["intent_id"]
                and record["intent_generation"] == intent["generation"]
                and record["scope_hash"] == intent["scope_hash"]
                and record["argv"] == expected_push
                and record["command"] == shlex.join(expected_push)
                and record["environment"] == scope["execution_environment"],
                "publication execution evidence: authorized action mismatch")
        _expect(record["executable_path"] == git_path
                and record["executable_sha256"] == scope["git_executable_sha256"],
                "publication execution evidence: git identity mismatch")
        _expect(record["result_kind"] in {"executed", "ambiguous-observed-success"},
                "publication execution evidence.result_kind: invalid")
        if record["result_kind"] == "executed":
            _integer(record["exit_code"], "publication execution evidence.exit_code")
        else:
            _expect(record["exit_code"] is None,
                    "ambiguous publication recovery cannot invent an exit code")
        _expect(record["stdout_sha256"] == _persisted_text_sha(record["stdout"])
                and record["stderr_sha256"] == _persisted_text_sha(record["stderr"]),
                "publication execution evidence: persisted output hash mismatch")
        _expect(record["output_limit_bytes"] == MAX_CAPTURE_BYTES
                and record["stdout_truncated"] is False
                and record["stderr_truncated"] is False
                and record["background_processes_terminated"] is False
                and record["timed_out"] is False,
                "publication execution evidence: unsafe/incomplete command capture")
    else:
        _expect(verification["execution_evidence"] is None,
                "preexisting publication adoption cannot claim a push execution")
        _expect(intent.get("execution_attempts") == [],
                "preexisting publication adoption cannot contain push attempts")
    post_ref = verification["evidence"]
    _expect(
        post_ref["path"]
        == (
            f"artifacts/publication/postcondition-g{intent['generation']:04d}-"
            f"{intent['scope_hash'][:12]}.json"
        ),
        "release publication postcondition evidence path is noncanonical",
    )
    post = _load_json(evidence_root / post_ref["path"], "publication postcondition")
    _validate_publication_observation_record(
        post, scope, scope["commit"], verification["verified_at"],
        intent["producer"], "publication postcondition", post_ref.get("command"),
    )
    authorized_at = _iso(
        intent["authorization"]["at"], "publication_intent.authorization.at"
    )
    verified_at = _iso(
        verification["verified_at"],
        "release_manifest.published_revisions[0].verification.verified_at",
    )
    _expect(
        authorized_at <= verified_at,
        "release_manifest publication verification predates human authorization",
    )


def _validate_plan_against_release(plan: dict[str, Any], release: dict[str, Any]) -> None:
    """A frozen operations target may only desire identities declared by the
    hash-bound release manifest. This prevents a valid plan from applying the
    wrong source/render/digest."""
    source_commits = {item["commit"].lower() for item in release["source_revisions"]}
    rendered_commits = {item["commit"].lower() for item in release["rendered_revisions"]}
    artifact_digests = {item["digest"] for item in release["artifacts"]}
    plan_target_ids = {target["id"] for target in plan["targets"]}
    renders_by_target: dict[str, set[str]] = {target_id: set() for target_id in plan_target_ids}
    digests_by_target: dict[str, set[str]] = {target_id: set() for target_id in plan_target_ids}
    for item in release["rendered_revisions"]:
        unknown = sorted(set(item["target_ids"]) - plan_target_ids)
        _expect(not unknown,
                f"release rendered revision targets are absent from operations plan: {unknown}")
        for target_id in item["target_ids"]:
            renders_by_target[target_id].add(item["commit"].lower())
    for item in release["artifacts"]:
        unknown = sorted(set(item["target_ids"]) - plan_target_ids)
        _expect(not unknown,
                f"release artifact targets are absent from operations plan: {unknown}")
        for target_id in item["target_ids"]:
            digests_by_target[target_id].add(item["digest"])
    desired_sources: set[str] = set()
    desired_renders: set[str] = set()
    desired_digests: set[str] = set()
    for target in plan["targets"]:
        desired = target["desired"]
        prefix = f"operations_plan target {target['id']!r}"
        _expect(desired.get("source_commit") is not None,
                f"{prefix}: source_commit is mandatory for an operational target")
        source = desired["source_commit"].lower()
        _expect(source in source_commits,
                f"{prefix}: desired source_commit is absent from release_manifest")
        desired_sources.add(source)
        if rendered_commits:
            _expect(desired.get("render_commit") is not None,
                    f"{prefix}: rendered release requires target render_commit")
        if desired.get("render_commit") is not None:
            render = desired["render_commit"].lower()
            _expect(render in renders_by_target[target["id"]],
                    f"{prefix}: desired render_commit is not released for this target")
            desired_renders.add(render)
        if artifact_digests:
            _expect(desired.get("image_digest") is not None,
                    f"{prefix}: artifact release requires target image_digest")
        if desired.get("image_digest") is not None:
            digest = desired["image_digest"]
            _expect(digest in digests_by_target[target["id"]],
                    f"{prefix}: desired image_digest is not released for this target")
            desired_digests.add(digest)
    _expect(desired_sources == source_commits,
            "operations_plan source identities must exactly cover release_manifest")
    _expect(desired_renders == rendered_commits,
            "operations_plan render identities must exactly cover release_manifest")
    _expect(desired_digests == artifact_digests,
            "operations_plan artifact identities must exactly cover release_manifest")


def _validate_plan_against_publication_effect(
    plan: dict[str, Any], release: dict[str, Any], intent: dict[str, Any]
) -> None:
    """Bind every auto-observation target to the exact authorized publication effect."""
    effect = intent["scope"].get("delivery_effect")
    auto_targets = {
        target["id"]: target for target in plan["targets"]
        if target.get("apply_method") == "gitops-auto-sync-observe-v1"
    }
    if effect is None:
        _expect(not auto_targets,
                "operations_plan: auto-sync cannot be inferred without a publication delivery effect")
        return
    _validate_delivery_effect(effect, "publication_intent.scope.delivery_effect")
    effect_targets = {target["id"]: target for target in effect["targets"]}
    _expect(set(auto_targets) == set(effect_targets),
            "operations_plan: auto-sync target set must exactly equal the authorized delivery effect")
    effect_sha = _value_sha(effect)
    fanout_legs = (
        {leg["id"]: leg for leg in effect.get("render_legs", [])}
        if effect["kind"] == "ci-render-argocd-auto-sync-fanout-v1" else None
    )

    def _authorized_render(authorized: dict[str, Any]) -> tuple[str, str]:
        """The protected render the target reads from: its leg (fanout) or the single render."""
        if fanout_legs is not None:
            leg = fanout_legs[authorized["render_leg_id"]]
            return leg["remote"], leg["branch"]
        return effect["render"]["remote"], effect["render"]["branch"]

    rendered_by_target: dict[str, list[dict[str, Any]]] = {target_id: [] for target_id in auto_targets}
    for rendered in release["rendered_revisions"]:
        for target_id in rendered["target_ids"]:
            if target_id in rendered_by_target:
                rendered_by_target[target_id].append(rendered)
    for target_id, target in auto_targets.items():
        prefix = f"operations_plan auto target {target_id!r}"
        authorized = effect_targets[target_id]
        binding = target["auto_sync_binding"]
        profile = target["verification_profile"]
        contexts = target["execution_contexts"]
        render_remote, render_branch = _authorized_render(authorized)
        _expect(binding["publication_intent_id"] == intent["intent_id"]
                and binding["publication_scope_hash"] == intent["scope_hash"]
                and binding["delivery_effect_sha256"] == effect_sha
                and binding["target_id"] == target_id,
                f"{prefix}: publication intent/effect binding mismatch")
        _expect(binding["render_remote"] == render_remote
                and binding["render_branch"] == render_branch
                and binding["argocd_application_uid"] == authorized["argocd_application_uid"]
                and binding["verification_action_sha256"]
                == authorized["verification_action_sha256"]
                and binding["automated"] == authorized["automated"],
                f"{prefix}: resolved render/Application/policy binding mismatch")
        _expect(target["environment"] == authorized["environment"]
                and target["account"] == authorized["account"]
                and target["cluster"] == authorized["cluster"]
                and target["resource"] == authorized["resource"],
                f"{prefix}: target identity differs from authorized effect")
        # Destination is server XOR name; a name-addressed effect target (the profile
        # only carries destination_server today) yields a clean mismatch, not a KeyError.
        auth_dest_key = "destination_server" if "destination_server" in authorized else "destination_name"
        _expect(profile["argocd_application"] == authorized["argocd_application"]
                and profile["argocd_application_uid"] == authorized["argocd_application_uid"]
                and profile["argocd_project"] == authorized["argocd_project"]
                and profile["source_repo_url"] == authorized["source_repo_url"]
                and profile["source_target_revision"] == authorized["source_target_revision"]
                and profile["source_path"] == authorized["source_path"]
                and profile.get(auth_dest_key) == authorized[auth_dest_key]
                and profile["destination_namespace"] == authorized["destination_namespace"],
                f"{prefix}: verification profile differs from authorized Application")
        _expect(contexts["argocd"] == {
            "server": authorized["argocd_server"],
            "config_path": authorized["argocd_config_path"],
            "config_sha256": authorized["argocd_config_sha256"],
            "context": authorized["argocd_context"],
            "certificate_authority_sha256": authorized["certificate_authority_sha256"],
        }, f"{prefix}: Argo execution context differs from authorized effect")
        _expect(target["desired"]["source_commit"] == intent["scope"]["commit"],
                f"{prefix}: desired source commit differs from authorized publication")
        renders = rendered_by_target[target_id]
        _expect(len(renders) == 1
                and renders[0]["remote"] == render_remote
                and renders[0]["branch"] == render_branch
                and renders[0]["commit"] == target["desired"]["render_commit"],
                f"{prefix}: desired render is not the exact authorized CI render result")


def _validate_implementation_against_repo(
    implementation: dict[str, Any], state: dict[str, Any], state_path: Path,
    *, verify_current_checkout: bool = True,
) -> None:
    repo = _repo_root(state_path)
    candidates = [
        item for item in implementation["repositories"]
        if item["repo"] == repo.name and Path(item["path"]).expanduser().resolve() == repo.resolve()
    ]
    _expect(len(candidates) == 1,
            "implementation_evidence.repositories must contain exactly one canonical target-repo record")
    item = candidates[0]
    base = _commit(state.get("implementation_base"), "state.implementation_base")
    final = _commit(
        state.get("rectification_commit") or (state.get("implementation_commits") or [None])[-1],
        "state final implementation commit",
    )
    _expect(item["base_commit"].lower() == base, "implementation evidence base does not match state")
    _expect(item["head_commit"].lower() == final, "implementation evidence head does not match final rectification state")
    if verify_current_checkout:
        live_head = _commit(
            _git_output(repo, "rev-parse", "HEAD").decode().strip(), "repository HEAD"
        )
        _expect(live_head == final,
                f"repository HEAD moved after closure: expected {final}, observed {live_head}")
        live_branch = _git_output(repo, "branch", "--show-current").decode().strip()
        _expect(live_branch == item["branch"],
                f"implementation evidence branch {item['branch']!r} is not current branch {live_branch!r}")
        live_remote = _git_output(repo, "remote", "get-url", "origin").decode().strip()
        _expect(live_remote == item["remote_url"],
                "implementation evidence remote_url is not the current canonical origin")
        tracked_status = _worktree_status(repo, state_path)
        _expect(not tracked_status.strip(),
                "repository has tracked/index changes or untracked files outside the milestone "
                "state directory; checks and closure must bind declared committed inputs: "
                f"{tracked_status.strip()}")
    for commit in [base, final, *item["commits"]]:
        proc = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True,
        )
        _expect(proc.returncode == 0, f"implementation evidence references nonexistent commit {commit}")
    actual = _git_output(repo, "rev-list", "--reverse", f"{base}..{final}").decode().splitlines()
    _expect([v.lower() for v in item["commits"]] == [v.lower() for v in actual],
            "implementation evidence commit list does not equal the live base..final history")
    commands = {
        _nonempty_string(check.get("command"), "implementation_evidence.checks[].command")
        for check in implementation["checks"]
    }
    trivial = {"true", ":", "exit 0", "sh -c true", "bash -c true"}
    _expect(not any(command.strip() in trivial for command in commands),
            "implementation_evidence.checks: trivial success commands are not evidence")
    if verify_current_checkout:
        required = _required_project_checks(repo)
        _expect(
            bool(required),
            "implementation_evidence.checks: repository declares no deterministic project checks; "
            "add a reviewed .milestone-pipeline/checks.json contract",
        )
        missing = sorted(required - commands)
        _expect(not missing,
                f"implementation_evidence.checks: missing repository-required command(s): {missing}")


def _validate_code_cross_links(
    state: dict[str, Any], state_path: Path,
    cache: dict[str, tuple[Path, dict[str, Any], dict[str, Any]]],
) -> None:
    implementation = cache["implementation_evidence"][1]
    review = cache["review_manifest"][1]
    findings_path = (_repo_root(state_path) / state["findings_register"]).resolve()
    _expect(
        implementation["critique"]["code_review_manifest_sha256"]
        == _value_sha(_review_projection(review, "code")),
        "implementation_evidence.critique.code_review_manifest_sha256 is stale",
    )
    _expect(
        implementation["repositories"][0]["remote_url"] == review["reviewed"]["remote_url"],
        "implementation remote_url differs from the assessment/closure-bound origin",
    )
    _expect(implementation["critique"]["findings_register_sha256"] == _file_sha(findings_path),
            "implementation_evidence.critique.findings_register_sha256 is stale")
    _expect(
        implementation["critique"]["findings_register_sha256"]
        == cache["review_manifest"][2].get("closure_findings_sha"),
        "closure review findings hash does not equal implementation evidence",
    )
    closure_hash = cache["review_manifest"][2].get("closure_hash")
    _expect(implementation["rectification"]["closure_review_sha256"] == closure_hash,
            "implementation_evidence.rectification.closure_review_sha256 is stale")
    implementation_check_refs = sorted(
        ({"path": check["evidence"]["path"], "sha256": check["evidence"]["sha256"]}
         for check in implementation["checks"]),
        key=lambda ref: ref["path"],
    )
    closure_check_refs = sorted(
        cache["review_manifest"][2].get("closure_check_evidence_refs") or [],
        key=lambda ref: ref["path"],
    )
    _expect(closure_check_refs == implementation_check_refs,
            "closure review CHECK_EVIDENCE_REFS must exactly equal implementation check evidence")
    _expect(
        {ref["path"]: ref["sha256"] for ref in implementation_check_refs}
        == state["check_run_hashes"],
        "implementation checks must exactly equal deterministic state.check_run_hashes",
    )


def plan_hash(data: dict[str, Any]) -> str:
    material = {k: v for k, v in data.items() if k != "plan_hash"}
    return _value_sha(material)


def target_scope_hash(data: dict[str, Any], target: dict[str, Any]) -> str:
    return hashlib.sha256((plan_hash(data) + "\n").encode() + _canonical_bytes(target)).hexdigest()


def _validate_execution_environment(
    value: Any, target: dict[str, Any], label: str
) -> dict[str, str]:
    _expect(isinstance(value, dict), f"{label}: expected object")
    _expect(6 <= len(value) <= 32, f"{label}: expected 6..32 explicit variables")
    allowed_keys = {
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ",
        "KUBECONFIG", "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
        "ARGOCD_SERVER", "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CONFIG_DIR",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NO_PROXY",
        "MILESTONE_TARGET_ID", "MILESTONE_ENVIRONMENT", "MILESTONE_ACCOUNT",
        "MILESTONE_CLUSTER", "MILESTONE_RESOURCE",
    }
    secret_value = re.compile(
        r"(?i)(?:^|\s)bearer\s+\S+|AKIA[0-9A-Z]{16}|"
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|"
        r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]+:[^/@\s]+@|-----BEGIN [^-]+-----",
    )
    result: dict[str, str] = {}
    for key, item in value.items():
        _expect(isinstance(key, str) and bool(re.fullmatch(r"[A-Z_][A-Z0-9_]*", key)),
                f"{label}: invalid environment variable name {key!r}")
        _expect(key in allowed_keys,
                f"{label}.{key}: variable is outside the explicit non-secret allowlist")
        _expect(isinstance(item, str) and len(item) <= 4096,
                f"{label}.{key}: expected bounded string")
        _expect(not secret_value.search(item),
                f"{label}.{key}: value appears to contain credential material")
        if item.casefold().startswith(("http://", "https://")):
            _validated_remote_url(item, f"{label}.{key}")
        result[key] = item
    expected = {
        "MILESTONE_TARGET_ID": target["id"],
        "MILESTONE_ENVIRONMENT": target["environment"],
        "MILESTONE_ACCOUNT": target["account"],
        "MILESTONE_CLUSTER": target["cluster"],
        "MILESTONE_RESOURCE": target["resource"],
    }
    for key, expected_value in expected.items():
        _expect(result.get(key) == expected_value,
                f"{label}.{key}: must bind the reviewed target identity")
    path_value = _nonempty_string(result.get("PATH"), f"{label}.PATH")
    path_parts = path_value.split(os.pathsep)
    _expect(all(part and os.path.isabs(part) for part in path_parts),
            f"{label}.PATH: every entry must be absolute and non-empty")
    _expect(len(path_parts) == len(set(path_parts)), f"{label}.PATH: duplicate entries")
    allowed_path_entries = {"/usr/bin", "/bin", "/usr/sbin", "/sbin"}
    _expect(set(path_parts) <= allowed_path_entries,
            f"{label}.PATH: entries must be restricted to {sorted(allowed_path_entries)}")
    return result


def _option_value(argv: list[str], option: str, label: str) -> str | None:
    values: list[str] = []
    for i, value in enumerate(argv[1:], 1):
        if value == option:
            _expect(i + 1 < len(argv), f"{label}: {option} requires a value")
            values.append(argv[i + 1])
        elif value.startswith(option + "="):
            values.append(value.split("=", 1)[1])
    _expect(len(values) <= 1, f"{label}: duplicate {option} selectors")
    return values[0] if values else None


def _validate_execution_contexts(
    value: Any, target: dict[str, Any], environment: dict[str, str], label: str,
    *, verify_files: bool,
) -> dict[str, Any]:
    _expect(isinstance(value, dict), f"{label}: expected object")
    _strict_keys(value, {
        "kubernetes", "sender_kubernetes", "receiver_kubernetes", "argocd"
    }, label)
    result = json.loads(json.dumps(value))
    kube = value.get("kubernetes")
    if kube is not None:
        klabel = f"{label}.kubernetes"
        _expect(isinstance(kube, dict), f"{klabel}: expected object")
        kube_keys = {
            "kubeconfig_path", "kubeconfig_sha256", "context", "cluster_server",
            "certificate_authority_sha256",
        }
        _strict_keys(kube, kube_keys, klabel)
        _require_keys(kube, kube_keys, klabel)
        path_raw = _nonempty_string(kube.get("kubeconfig_path"), f"{klabel}.kubeconfig_path")
        _expect(os.path.isabs(path_raw), f"{klabel}.kubeconfig_path: expected absolute path")
        digest = _sha256_value(kube.get("kubeconfig_sha256"), f"{klabel}.kubeconfig_sha256")
        context = _nonempty_string(kube.get("context"), f"{klabel}.context")
        cluster_server = _nonempty_string(
            kube.get("cluster_server"), f"{klabel}.cluster_server"
        )
        parsed_server = urlparse(cluster_server)
        _expect(parsed_server.scheme.casefold() == "https" and not parsed_server.username,
                f"{klabel}.cluster_server: expected credential-free HTTPS endpoint")
        ca_sha = _sha256_value(
            kube.get("certificate_authority_sha256"),
            f"{klabel}.certificate_authority_sha256",
        )
        _expect(context == target["cluster"],
                f"{klabel}.context: must equal the reviewed target cluster")
        _expect(environment.get("KUBECONFIG") == path_raw,
                f"{klabel}.kubeconfig_path: must equal execution_environment.KUBECONFIG")
        if verify_files:
            path = Path(path_raw)
            _expect(path.is_file() and not path.is_symlink(),
                    f"{klabel}.kubeconfig_path: missing or symlinked config")
            _expect(_file_sha(path) == digest,
                    f"{klabel}.kubeconfig_sha256: credential context changed")
            _validate_json_kubeconfig(
                path, context, cluster_server, ca_sha, klabel
            )
    for context_key in ("sender_kubernetes", "receiver_kubernetes"):
        extra = value.get(context_key)
        if extra is None:
            continue
        xlabel = f"{label}.{context_key}"
        _expect(isinstance(extra, dict), f"{xlabel}: expected object")
        extra_keys = {
            "kubeconfig_path", "kubeconfig_sha256", "context", "cluster_server",
            "certificate_authority_sha256",
        }
        _strict_keys(extra, extra_keys, xlabel)
        _require_keys(extra, extra_keys, xlabel)
        path_raw = _nonempty_string(
            extra.get("kubeconfig_path"), f"{xlabel}.kubeconfig_path"
        )
        _expect(os.path.isabs(path_raw), f"{xlabel}.kubeconfig_path: expected absolute path")
        digest = _sha256_value(
            extra.get("kubeconfig_sha256"), f"{xlabel}.kubeconfig_sha256"
        )
        context = _nonempty_string(extra.get("context"), f"{xlabel}.context")
        server = _nonempty_string(extra.get("cluster_server"), f"{xlabel}.cluster_server")
        parsed = urlparse(server)
        _expect(parsed.scheme.casefold() == "https" and not parsed.username,
                f"{xlabel}.cluster_server: credential-free HTTPS endpoint required")
        ca_sha = _sha256_value(
            extra.get("certificate_authority_sha256"),
            f"{xlabel}.certificate_authority_sha256",
        )
        if verify_files:
            path = Path(path_raw)
            _expect(path.is_file() and not path.is_symlink() and _file_sha(path) == digest,
                    f"{xlabel}.kubeconfig_sha256: credential context changed")
            _validate_json_kubeconfig(path, context, server, ca_sha, xlabel)
    argocd = value.get("argocd")
    if argocd is not None:
        alabel = f"{label}.argocd"
        _expect(isinstance(argocd, dict), f"{alabel}: expected object")
        argocd_keys = {
            "server", "config_path", "config_sha256", "context",
            "certificate_authority_sha256",
        }
        _strict_keys(argocd, argocd_keys, alabel)
        _require_keys(argocd, argocd_keys, alabel)
        server = _nonempty_string(argocd.get("server"), f"{alabel}.server")
        parsed_server = urlparse(server)
        _expect(parsed_server.scheme.casefold() == "https" and not parsed_server.username,
                f"{alabel}.server: credential-free HTTPS endpoint required")
        config_raw = _nonempty_string(argocd.get("config_path"), f"{alabel}.config_path")
        _expect(os.path.isabs(config_raw), f"{alabel}.config_path: expected absolute path")
        config_sha = _sha256_value(argocd.get("config_sha256"), f"{alabel}.config_sha256")
        context_name = _nonempty_string(argocd.get("context"), f"{alabel}.context")
        ca_sha = _sha256_value(
            argocd.get("certificate_authority_sha256"),
            f"{alabel}.certificate_authority_sha256",
        )
        _expect(environment.get("ARGOCD_SERVER") == server,
                f"{alabel}.server: must equal execution_environment.ARGOCD_SERVER")
        if verify_files:
            config_path = Path(config_raw)
            _expect(config_path.is_file() and not config_path.is_symlink()
                    and _file_sha(config_path) == config_sha,
                    f"{alabel}.config_sha256: Argo credential context changed")
            _validate_json_argocd_config(
                config_path, context_name, server, ca_sha, alabel
            )
    return result


def _validate_json_kubeconfig(
    path: Path, context_name: str, cluster_server: str, ca_sha256: str, label: str,
) -> None:
    """Accept only a hash-bound JSON kubeconfig with no executable auth plugins."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(
            f"{label}.kubeconfig_path: v2 requires JSON-form kubeconfig so transitive "
            f"credential execution can be audited: {exc}"
        )
    _expect(isinstance(value, dict), f"{label}.kubeconfig_path: expected JSON object")
    contexts = value.get("contexts")
    clusters = value.get("clusters")
    users = value.get("users")
    _expect(isinstance(contexts, list) and isinstance(clusters, list)
            and isinstance(users, list),
            f"{label}.kubeconfig_path: contexts, clusters, and users arrays are required")
    context_rows = [item for item in contexts if isinstance(item, dict)
                    and item.get("name") == context_name]
    _expect(len(context_rows) == 1,
            f"{label}.context: must select exactly one kubeconfig context")
    context_value = context_rows[0].get("context")
    _expect(isinstance(context_value, dict), f"{label}.context: malformed context")
    cluster_name = _nonempty_string(context_value.get("cluster"), f"{label}.context.cluster")
    user_name = _nonempty_string(context_value.get("user"), f"{label}.context.user")
    cluster_rows = [item for item in clusters if isinstance(item, dict)
                    and item.get("name") == cluster_name]
    user_rows = [item for item in users if isinstance(item, dict)
                 and item.get("name") == user_name]
    _expect(len(cluster_rows) == 1 and len(user_rows) == 1,
            f"{label}: selected cluster/user identities must each be unique")
    cluster = cluster_rows[0].get("cluster")
    user = user_rows[0].get("user")
    _expect(isinstance(cluster, dict) and isinstance(user, dict),
            f"{label}: selected cluster/user records are malformed")
    _expect(cluster.get("server") == cluster_server,
            f"{label}.cluster_server: differs from selected kubeconfig endpoint")
    _expect(cluster.get("insecure-skip-tls-verify") is not True,
            f"{label}: insecure TLS verification is forbidden")
    _expect("proxy-url" not in cluster and "tls-server-name" not in cluster,
            f"{label}: cluster proxy/TLS identity overrides are forbidden")
    ca_data = _nonempty_string(
        cluster.get("certificate-authority-data"),
        f"{label}.certificate-authority-data",
    )
    _expect("certificate-authority" not in cluster,
            f"{label}: external mutable certificate-authority paths are forbidden")
    try:
        ca_bytes = base64.b64decode(ca_data, validate=True)
    except (ValueError, binascii.Error) as exc:
        _fail(f"{label}.certificate-authority-data: invalid base64: {exc}")
    _expect(bool(ca_bytes) and hashlib.sha256(ca_bytes).hexdigest() == ca_sha256,
            f"{label}.certificate_authority_sha256: selected cluster CA mismatch")
    forbidden_user_keys = {
        "exec", "auth-provider", "tokenFile", "as", "as-groups", "as-user-extra",
    }
    _expect(not (forbidden_user_keys & set(user)),
            f"{label}: exec/auth-provider/tokenFile credential plugins are forbidden in v2")


def _validate_json_argocd_config(
    path: Path, context_name: str, server_name: str, ca_sha256: str, label: str,
) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(
            f"{label}.config_path: v2 requires JSON-form Argo config for deterministic "
            f"TLS/auth context validation: {exc}"
        )
    _expect(isinstance(value, dict), f"{label}.config_path: expected JSON object")
    contexts = value.get("contexts")
    servers = value.get("servers")
    users = value.get("users")
    _expect(isinstance(contexts, list) and isinstance(servers, list)
            and isinstance(users, list),
            f"{label}.config_path: contexts, servers, and users arrays are required")
    context_rows = [row for row in contexts if isinstance(row, dict)
                    and row.get("name") == context_name]
    _expect(len(context_rows) == 1, f"{label}.context: must select one Argo context")
    context = context_rows[0]
    _expect(context.get("server") == server_name,
            f"{label}.context: selected server differs from reviewed endpoint")
    user_name = _nonempty_string(context.get("user"), f"{label}.context.user")
    server_rows = [row for row in servers if isinstance(row, dict)
                   and row.get("server") == server_name]
    user_rows = [row for row in users if isinstance(row, dict)
                 and row.get("name") == user_name]
    _expect(len(server_rows) == 1 and len(user_rows) == 1,
            f"{label}: selected Argo server/user identities must each be unique")
    server = server_rows[0]
    _expect(server.get("insecure") is False,
            f"{label}: Argo insecure TLS is forbidden")
    ca_data = _nonempty_string(
        server.get("certificate-authority-data"),
        f"{label}.certificate-authority-data",
    )
    try:
        ca_bytes = base64.b64decode(ca_data, validate=True)
    except (ValueError, binascii.Error) as exc:
        _fail(f"{label}.certificate-authority-data: invalid base64: {exc}")
    _expect(bool(ca_bytes) and hashlib.sha256(ca_bytes).hexdigest() == ca_sha256,
            f"{label}.certificate_authority_sha256: Argo CA mismatch")
    user = user_rows[0]
    _expect(isinstance(user.get("auth-token"), str) and bool(user["auth-token"]),
            f"{label}: selected Argo user requires a non-empty bound auth token")


def _validate_verification_profile(
    value: Any, target: dict[str, Any], label: str,
) -> dict[str, Any]:
    _expect(isinstance(value, dict), f"{label}: expected object")
    common = {
        "kind",
        "argocd_application", "argocd_application_uid", "argocd_project",
        "source_repo_url", "source_path", "destination_server",
        "destination_namespace", "deployment_name", "deployment_uid", "pod_selector",
        "container_name", "service_name", "service_uid", "service_port",
        "behavioral_smoke_url", "behavioral_smoke_status",
    }
    web_keys = common | {
        "ingress_name", "ingress_uid", "ingress_path",
    }
    internal_keys = common | {
        "source_target_revision", "resource_namespace", "service_port_name",
        "service_target_port", "service_host", "readiness_path", "probe_origin",
    }
    eastwest_keys = (internal_keys - {"service_host"}) | {
        "global_service_host", "sender", "receiver",
    }
    kind = value.get("kind")
    keys_by_kind = {
        "argocd-web-workload-v1": web_keys,
        "argocd-istio-internal-http-v1": internal_keys,
        "argocd-istio-eastwest-v1": eastwest_keys,
    }
    _expect(kind in keys_by_kind,
            f"{label}.kind: unsupported typed verification profile {kind!r}")
    keys = keys_by_kind[str(kind)]
    _strict_keys(value, keys, label)
    _require_keys(value, keys, label)
    result = json.loads(json.dumps(value))
    nested = {"probe_origin", "sender", "receiver"}
    for key in keys - {"behavioral_smoke_status", "service_port", "kind"} - nested:
        _nonempty_string(value.get(key), f"{label}.{key}")
    smoke_status = _integer(
        value.get("behavioral_smoke_status"), f"{label}.behavioral_smoke_status", 200
    )
    _expect(smoke_status <= 299,
            f"{label}.behavioral_smoke_status: mandatory smoke requires HTTP 2xx")
    service_port = _integer(value.get("service_port"), f"{label}.service_port", 1)
    _expect(service_port <= 65535, f"{label}.service_port: expected TCP port 1..65535")
    app_name = target["resource"].split("/", 1)[-1]
    _expect(value["argocd_application"] == app_name,
            f"{label}.argocd_application: must equal reviewed target resource name")
    _validated_remote_url(value["source_repo_url"], f"{label}.source_repo_url")
    destination = urlparse(value["destination_server"])
    _expect(destination.scheme.casefold() == "https" and not destination.username,
            f"{label}.destination_server: credential-free HTTPS URL required")
    smoke_url = urlparse(value["behavioral_smoke_url"])
    try:
        smoke_port = smoke_url.port
    except ValueError as exc:
        _fail(f"{label}.behavioral_smoke_url: invalid port: {exc}")
    _expect(not smoke_url.username and not smoke_url.password
            and not smoke_url.query and not smoke_url.fragment,
            f"{label}.behavioral_smoke_url: credentials/query/fragment are forbidden")
    _expect(not value["source_path"].startswith("/")
            and ".." not in PurePosixPath(value["source_path"]).parts,
            f"{label}.source_path: safe repo-relative path required")
    _expect(not value["destination_namespace"].startswith("-")
            and not value["deployment_name"].startswith("-")
            and not value["pod_selector"].startswith("-")
            and not value["service_name"].startswith("-"),
            f"{label}: option-like target selectors are forbidden")
    if kind == "argocd-web-workload-v1":
        _expect(smoke_url.scheme.casefold() == "https"
                and smoke_url.path == value["ingress_path"],
                f"{label}: public web profile requires exact credential-free HTTPS Ingress path")
        _expect(not value["ingress_name"].startswith("-"),
                f"{label}.ingress_name: option-like selector forbidden")
        return result
    _expect(smoke_url.scheme == "http" and smoke_port == service_port
            and smoke_url.path == value["readiness_path"],
            f"{label}.behavioral_smoke_url: must use exact HTTP service port/readiness path")
    origin = value.get("probe_origin")
    _expect(isinstance(origin, dict), f"{label}.probe_origin: expected object")
    origin_keys = {
        "namespace", "pod_name", "pod_uid", "service_account_name",
        "container_name", "container_image_digest", "curl_path", "istio_proxy_container",
    }
    _strict_keys(origin, origin_keys, f"{label}.probe_origin")
    _require_keys(origin, origin_keys, f"{label}.probe_origin")
    for field in origin_keys - {"container_image_digest"}:
        _nonempty_string(origin.get(field), f"{label}.probe_origin.{field}")
    digest = _nonempty_string(
        origin.get("container_image_digest"),
        f"{label}.probe_origin.container_image_digest",
    )
    _expect(digest.startswith("sha256:") and bool(SHA256_RE.fullmatch(digest[7:])),
            f"{label}.probe_origin.container_image_digest: expected sha256 digest")
    _expect(os.path.isabs(origin["curl_path"])
            and Path(origin["curl_path"]).name == "curl"
            and origin["istio_proxy_container"] == "istio-proxy",
            f"{label}.probe_origin: exact curl path and istio-proxy sidecar required")
    if kind == "argocd-istio-internal-http-v1":
        expected_host = (
            f"{value['service_name']}.{value['resource_namespace']}.svc.cluster.local"
        )
        _expect(value["service_host"] == expected_host
                and smoke_url.hostname == expected_host,
                f"{label}: same-cluster profile requires the exact Service FQDN")
        _expect(not value["service_host"].endswith(".global"),
                f"{label}.service_host: cross-cluster .global requires eastwest profile")
        return result
    global_host = value["global_service_host"]
    _expect(bool(re.fullmatch(
                r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?\.svc\.cluster-[a-z0-9-]+\.global",
                global_host,
            )) and smoke_url.hostname == global_host,
            f"{label}: east-west profile requires the exact tenant .global host")
    sender = value.get("sender")
    receiver = value.get("receiver")
    _expect(isinstance(sender, dict) and isinstance(receiver, dict),
            f"{label}: sender and receiver route identities are required")
    sender_keys = {
        "namespace", "service_entry_name", "service_entry_uid",
        "destination_rule_name", "destination_rule_uid", "eastwest_endpoint_host",
        "eastwest_endpoint_port", "proxy_pod", "proxy_uid",
    }
    receiver_keys = {
        "namespace", "service_entry_name", "service_entry_uid",
        "destination_rule_name", "destination_rule_uid", "envoy_filter_name",
        "envoy_filter_uid", "envoy_cluster_name", "local_service_host",
        "local_service_port", "gateway_proxy_pod", "gateway_proxy_uid",
    }
    for side, side_keys, side_label in (
        (sender, sender_keys, "sender"), (receiver, receiver_keys, "receiver")
    ):
        _strict_keys(side, side_keys, f"{label}.{side_label}")
        _require_keys(side, side_keys, f"{label}.{side_label}")
        for field in side_keys - {"eastwest_endpoint_port", "local_service_port"}:
            _nonempty_string(side.get(field), f"{label}.{side_label}.{field}")
    _expect(_integer(sender["eastwest_endpoint_port"],
                     f"{label}.sender.eastwest_endpoint_port", 1) <= 65535,
            f"{label}.sender.eastwest_endpoint_port: invalid port")
    _expect(_integer(receiver["local_service_port"],
                     f"{label}.receiver.local_service_port", 1) == service_port,
            f"{label}.receiver.local_service_port: must equal reviewed Service port")
    _expect(receiver["local_service_host"] ==
            f"{value['service_name']}.{value['resource_namespace']}.svc.cluster.local",
            f"{label}.receiver.local_service_host: exact local Service FQDN required")
    _expect(sender["proxy_pod"] == origin["pod_name"]
            and sender["proxy_uid"] == origin["pod_uid"],
            f"{label}.sender: proxy identity must equal the reviewed probe origin")
    return result


def _validate_command_context(
    argv: list[str], contexts: dict[str, Any], label: str,
) -> None:
    if ALLOW_TEST_OPERATION_EXECUTABLES:
        return
    tool = Path(argv[0]).name.casefold()
    if tool == "kubectl":
        kubeconfig = _option_value(argv, "--kubeconfig", label)
        context_name = _option_value(argv, "--context", label)
        matching_contexts = {
            (value.get("kubeconfig_path"), value.get("context"))
            for key, value in contexts.items()
            if key in {"kubernetes", "sender_kubernetes", "receiver_kubernetes"}
            and isinstance(value, dict)
            and value.get("kubeconfig_path") == kubeconfig
            and value.get("context") == context_name
        }
        _expect(len(matching_contexts) == 1,
                f"{label}: kubectl must bind exactly one frozen Kubernetes context")
        forbidden = {
            "--server", "-s", "--cluster", "--user", "--username", "--password",
            "--token", "--client-key", "--client-certificate",
            "--certificate-authority", "--insecure-skip-tls-verify", "--as",
            "--as-group", "--as-uid",
        }
        _expect(not any(
            value in forbidden
            or any(value.startswith(option + "=") for option in forbidden if option.startswith("--"))
            for value in argv[1:]
        ), f"{label}: kubectl credential/target override flags are forbidden")
    elif tool == "argocd":
        context = contexts.get("argocd")
        _expect(isinstance(context, dict),
                f"{label}: argocd requires a frozen server context")
        forbidden = {
            "--auth-token", "--header", "--client-crt", "--client-crt-key",
            "--core", "--kube-context",
            "--port-forward", "--port-forward-namespace",
        }
        _expect(not any(
            value in forbidden
            or any(value.startswith(option + "=") for option in forbidden)
            for value in argv[1:]
        ), f"{label}: credential/context override flags are forbidden")
        _expect(_option_value(argv, "--server", label) == context["server"],
                f"{label}: argocd must name the exact --server value")
        _expect(_option_value(argv, "--config", label) == context["config_path"]
                and _option_value(argv, "--argocd-context", label) == context["context"],
                f"{label}: argocd must name the exact --config and --argocd-context values")
    elif tool == "istioctl":
        kubeconfig = _option_value(argv, "--kubeconfig", label)
        context_name = _option_value(argv, "--context", label)
        matching_contexts = {
            (value.get("kubeconfig_path"), value.get("context"))
            for key, value in contexts.items()
            if key in {"kubernetes", "sender_kubernetes", "receiver_kubernetes"}
            and isinstance(value, dict)
            and value.get("kubeconfig_path") == kubeconfig
            and value.get("context") == context_name
        }
        _expect(len(matching_contexts) == 1,
                f"{label}: istioctl must bind exactly one frozen Kubernetes context")
        _expect(not any(part in {"--insecure", "--plaintext", "--token"} for part in argv[1:]),
                f"{label}: insecure or credential override flags are forbidden")


def _recheck_execution_context_files(contexts: dict[str, Any], label: str) -> None:
    for context_key in ("kubernetes", "sender_kubernetes", "receiver_kubernetes"):
        kube = contexts.get(context_key)
        if not isinstance(kube, dict):
            continue
        path = Path(kube["kubeconfig_path"])
        _expect(path.is_file() and not path.is_symlink()
                and _file_sha(path) == kube["kubeconfig_sha256"],
                f"{label}: {context_key} kubeconfig changed after authorization")
        _validate_json_kubeconfig(
            path, kube["context"], kube["cluster_server"],
            kube["certificate_authority_sha256"], f"{label}.{context_key}",
        )
    argocd = contexts.get("argocd")
    if isinstance(argocd, dict):
        path = Path(argocd["config_path"])
        _expect(path.is_file() and not path.is_symlink()
                and _file_sha(path) == argocd["config_sha256"],
                f"{label}: Argo config changed after authorization")
        _validate_json_argocd_config(
            path, argocd["context"], argocd["server"],
            argocd["certificate_authority_sha256"], f"{label}.argocd",
        )


def _validate_operational_executable_trust(
    argv: list[str], executable_sha256: str, state: dict[str, Any], label: str,
    apply_method: str,
) -> None:
    if ALLOW_TEST_OPERATION_EXECUTABLES:
        return
    resolved = Path(argv[0]).resolve()
    trusted_system_roots = tuple(
        path.resolve() for path in map(Path, ("/bin", "/usr/bin", "/usr/sbin", "/sbin"))
        if path.exists()
    )
    trusted_tool_names = {
        "gitops-manual-sync": {"argocd", "kubectl", "curl", "istioctl"},
        "gitops-auto-sync-observe-v1": {"argocd", "kubectl", "curl", "istioctl"},
    }[apply_method]
    if (
        resolved.name in trusted_tool_names
        and any(resolved == root or root in resolved.parents for root in trusted_system_roots)
    ):
        return
    cellar_roots = tuple(
        path.resolve() for path in map(Path, ("/opt/homebrew/Cellar", "/usr/local/Cellar"))
        if path.exists()
    )
    if (
        resolved.name in trusted_tool_names
        and any(resolved == root or root in resolved.parents for root in cellar_roots)
    ):
        return
    _fail(
        f"{label}: v2 permits only hashed system package-manager tools; source-backed "
        "operational wrappers are deferred until detached dependency/interpreter binding exists"
    )


def _validate_verification_command_semantics(argv: list[str], label: str) -> None:
    """Enforce a deliberately narrow, machine-auditable read-only command subset."""
    if ALLOW_TEST_OPERATION_EXECUTABLES:
        return
    tool = Path(argv[0]).name.casefold()
    tail = argv[1:]
    lowered = [value.casefold() for value in tail]
    _expect(not any(value in {"--show-secrets", "--show-secret"} for value in lowered),
            f"{label}: secret-revealing output flags are forbidden")
    _expect(not any(value == "--raw" or value.startswith("--raw=") for value in lowered),
            f"{label}: raw API queries are forbidden")
    file_selectors = {"-f", "--filename", "-k", "--kustomize"}
    _expect(not any(
        value in file_selectors
        or any(value.startswith(option + "=") for option in {"--filename", "--kustomize"})
        for value in lowered
    ), f"{label}: mutable file/kustomize selectors are forbidden")
    allowed = False
    if tool == "argocd":
        allowed = (
            tail[:1] == ["version"]
            or tail[:2] in (["app", "get"], ["app", "wait"], ["app", "diff"])
        )
    elif tool == "kubectl":
        allowed = bool(tail) and (
            tail[0] in {
                "get", "wait", "version", "cluster-info",
                "api-resources", "api-versions", "explain", "diff",
            }
            or tail[:2] == ["rollout", "status"]
            or tail[:2] == ["auth", "can-i"]
        )
        _expect(not any(
            value in {"cm", "cms"}
            or value.split("/", 1)[0].split(".", 1)[0].rstrip("s")
            in {"secret", "configmap"}
            or "/secrets/" in value or "/configmaps/" in value
            for value in lowered
        ), f"{label}: secret/config payload resources are forbidden in verification")
    _expect(
        allowed,
        f"{label}: verification executable/argv is outside the machine-enforced "
        "read-only policy; use an allowlisted query command or add a typed collector",
    )


def _expected_argocd_get(
    executable: str, contexts: dict[str, Any], profile: dict[str, Any]
) -> list[str]:
    return [
        executable, "app", "get", profile["argocd_application"],
        "--server", contexts["argocd"]["server"], "--output", "json",
        "--config", contexts["argocd"]["config_path"],
        "--argocd-context", contexts["argocd"]["context"],
    ]


def _validate_observation_command_semantics(
    argv: list[str], contexts: dict[str, Any], profile: dict[str, Any], label: str,
) -> None:
    if ALLOW_TEST_OPERATION_EXECUTABLES:
        return
    _expect(Path(argv[0]).name.casefold() == "argocd",
            f"{label}: v2 observation requires the typed Argo Application collector")
    _expect(argv == _expected_argocd_get(argv[0], contexts, profile),
            f"{label}: expected exact target-bound Argo JSON observation command")


def _validate_probe_command_semantics(
    kind: str, argv: list[str], timeout_seconds: int, contexts: dict[str, Any],
    profile: dict[str, Any], label: str,
) -> None:
    if ALLOW_TEST_OPERATION_EXECUTABLES:
        return
    resource_namespace = profile.get("resource_namespace", profile["destination_namespace"])
    kube = contexts.get("kubernetes")
    expected: list[str]

    def kube_get(
        context_key: str, resource: str, name: str, namespace: str,
    ) -> list[str]:
        context = contexts.get(context_key)
        _expect(isinstance(context, dict),
                f"{label}: {kind} requires frozen {context_key} context")
        return [
            argv[0], "--kubeconfig", context["kubeconfig_path"], "--context",
            context["context"], "get", resource, name, "--namespace", namespace,
            "--output", "json",
        ]

    def probe_exec(context_key: str) -> list[str]:
        context = contexts.get(context_key)
        _expect(isinstance(context, dict),
                f"{label}: {kind} requires frozen {context_key} context")
        origin = profile["probe_origin"]
        return [
            argv[0], "--kubeconfig", context["kubeconfig_path"], "--context",
            context["context"], "--namespace", origin["namespace"], "exec",
            origin["pod_name"], "--container", origin["container_name"], "--",
            origin["curl_path"], "--silent", "--show-error", "--output", "/dev/null",
            "--write-out", "%{http_code}", "--max-time", str(timeout_seconds),
            "--request", "GET", profile["behavioral_smoke_url"],
        ]

    if kind == "argocd-synced":
        expected = _expected_argocd_get(argv[0], contexts, profile)
        _expect(Path(argv[0]).name.casefold() == "argocd",
                f"{label}: argocd-synced requires argocd")
    elif kind == "deployment-observed-generation":
        expected = kube_get(
            "kubernetes", "deployment", profile["deployment_name"], resource_namespace
        )
        _expect(Path(argv[0]).name.casefold() == "kubectl",
                f"{label}: deployment-observed-generation requires kubectl")
    elif kind == "pod-image-digest":
        _expect(isinstance(kube, dict),
                f"{label}: pod collector requires frozen Kubernetes context")
        expected = [
            argv[0], "--kubeconfig", kube["kubeconfig_path"], "--context",
            kube["context"], "get", "pods", "--namespace", resource_namespace,
            "--selector", profile["pod_selector"],
            "--output", "json",
        ]
        _expect(Path(argv[0]).name.casefold() == "kubectl",
                f"{label}: pod-image-digest requires kubectl")
    elif kind == "service-selects-workload":
        expected = kube_get(
            "kubernetes", "service", profile["service_name"], resource_namespace
        )
        _expect(Path(argv[0]).name.casefold() == "kubectl",
                f"{label}: service-selects-workload requires kubectl")
    elif kind == "ingress-routes-service":
        _expect(profile["kind"] == "argocd-web-workload-v1",
                f"{label}: internal profiles cannot borrow an Ingress proof")
        expected = kube_get(
            "kubernetes", "ingress", profile["ingress_name"], resource_namespace
        )
        _expect(Path(argv[0]).name.casefold() == "kubectl",
                f"{label}: ingress-routes-service requires kubectl")
    elif kind == "behavioral-smoke":
        _expect(profile["kind"] == "argocd-web-workload-v1",
                f"{label}: public smoke belongs only to the web profile")
        _expect(Path(argv[0]).name.casefold() == "curl",
                f"{label}: behavioral-smoke requires curl")
        expected = [
            argv[0], "--disable", "--silent", "--show-error", "--max-time",
            str(timeout_seconds), "--output", "/dev/null", "--write-out",
            "%{http_code}", "--request", "GET",
            profile["behavioral_smoke_url"],
        ]
    elif kind == "endpointslice-ready-backends":
        _expect(isinstance(kube, dict),
                f"{label}: EndpointSlice collector requires frozen Kubernetes context")
        expected = [
            argv[0], "--kubeconfig", kube["kubeconfig_path"], "--context",
            kube["context"], "get", "endpointslices", "--namespace",
            resource_namespace, "--selector",
            f"kubernetes.io/service-name={profile['service_name']}", "--output", "json",
        ]
        _expect(Path(argv[0]).name.casefold() == "kubectl",
                f"{label}: endpointslice-ready-backends requires kubectl")
    elif kind == "istio-probe-origin-ready":
        origin = profile["probe_origin"]
        context_key = (
            "sender_kubernetes"
            if profile["kind"] == "argocd-istio-eastwest-v1"
            else "kubernetes"
        )
        expected = kube_get(
            context_key, "pod", origin["pod_name"], origin["namespace"]
        )
        _expect(Path(argv[0]).name.casefold() == "kubectl",
                f"{label}: probe-origin readiness requires kubectl")
    elif kind == "receiver-gateway-proxy-ready":
        _expect(profile["kind"] == "argocd-istio-eastwest-v1",
                f"{label}: gateway proxy proof requires eastwest profile")
        receiver = profile["receiver"]
        expected = kube_get(
            "receiver_kubernetes", "pod", receiver["gateway_proxy_pod"],
            receiver["namespace"],
        )
        _expect(Path(argv[0]).name.casefold() == "kubectl",
                f"{label}: gateway proxy readiness requires kubectl")
    elif kind == "internal-behavioral-smoke":
        _expect(profile["kind"] == "argocd-istio-internal-http-v1",
                f"{label}: same-cluster smoke requires internal profile")
        expected = probe_exec("kubernetes")
        _expect(Path(argv[0]).name.casefold() == "kubectl",
                f"{label}: internal smoke requires bounded kubectl exec")
    elif kind in {
        "sender-serviceentry-route-exact", "sender-destinationrule-mtls-exact",
        "receiver-serviceentry-route-exact", "receiver-destinationrule-mtls-exact",
        "receiver-envoyfilter-cluster-exact",
    }:
        _expect(profile["kind"] == "argocd-istio-eastwest-v1",
                f"{label}: east-west route proof requires eastwest profile")
        sender = kind.startswith("sender-")
        context_key = "sender_kubernetes" if sender else "receiver_kubernetes"
        side = profile["sender"] if sender else profile["receiver"]
        if "serviceentry" in kind:
            resource, name = "serviceentry", side["service_entry_name"]
        elif "destinationrule" in kind:
            resource, name = "destinationrule", side["destination_rule_name"]
        else:
            resource, name = "envoyfilter", side["envoy_filter_name"]
        expected = kube_get(context_key, resource, name, side["namespace"])
        _expect(Path(argv[0]).name.casefold() == "kubectl",
                f"{label}: typed Istio resource proof requires kubectl")
    elif kind in {"sender-istio-xds-synced", "receiver-istio-xds-synced"}:
        sender = kind.startswith("sender-")
        context_key = "sender_kubernetes" if sender else "receiver_kubernetes"
        context = contexts.get(context_key)
        side = profile["sender"] if sender else profile["receiver"]
        proxy = (
            f"{side['proxy_pod']}.{profile['probe_origin']['namespace']}"
            if sender else f"{side['gateway_proxy_pod']}.{side['namespace']}"
        )
        _expect(isinstance(context, dict), f"{label}: missing {context_key}")
        expected = [
            argv[0], "--kubeconfig", context["kubeconfig_path"], "--context",
            context["context"], "proxy-status", proxy, "--output", "json",
        ]
        _expect(Path(argv[0]).name.casefold() == "istioctl",
                f"{label}: xDS proof requires istioctl")
    elif kind in {
        "sender-istio-cluster-healthy-endpoints",
        "receiver-istio-cluster-healthy-endpoints",
    }:
        sender = kind.startswith("sender-")
        context_key = "sender_kubernetes" if sender else "receiver_kubernetes"
        context = contexts.get(context_key)
        side = profile["sender"] if sender else profile["receiver"]
        proxy = (
            f"{side['proxy_pod']}.{profile['probe_origin']['namespace']}"
            if sender else f"{side['gateway_proxy_pod']}.{side['namespace']}"
        )
        cluster = profile["receiver"]["envoy_cluster_name"]
        _expect(isinstance(context, dict), f"{label}: missing {context_key}")
        expected = [
            argv[0], "--kubeconfig", context["kubeconfig_path"], "--context",
            context["context"], "proxy-config", "endpoints", proxy,
            "--cluster", cluster, "--output", "json",
        ]
        _expect(Path(argv[0]).name.casefold() == "istioctl",
                f"{label}: endpoint proof requires istioctl")
    elif kind == "eastwest-behavioral-smoke":
        _expect(profile["kind"] == "argocd-istio-eastwest-v1",
                f"{label}: .global smoke requires eastwest profile")
        expected = probe_exec("sender_kubernetes")
        _expect(Path(argv[0]).name.casefold() == "kubectl",
                f"{label}: east-west smoke requires bounded sender kubectl exec")
    else:
        _fail(f"{label}: unsupported verification kind {kind!r}; add a typed collector")
    _expect(argv == expected,
            f"{label}: command does not equal the exact target-bound {kind} collector")


def _argocd_application_parts(
    raw: bytes, profile: dict[str, Any], label: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{label}: invalid Argo JSON: {exc}") from exc
    _expect(isinstance(value, dict), f"{label}: Argo output must be an object")
    metadata = value.get("metadata")
    spec = value.get("spec")
    status = value.get("status")
    _expect(isinstance(metadata, dict) and isinstance(spec, dict)
            and isinstance(status, dict), f"{label}: incomplete Argo Application object")
    source = spec.get("source")
    destination = spec.get("destination")
    _expect(isinstance(source, dict) and isinstance(destination, dict),
            f"{label}: v2 requires a single-source Argo Application")
    actual_profile = {
        "argocd_application": metadata.get("name"),
        "argocd_application_uid": metadata.get("uid"),
        "argocd_project": spec.get("project"),
        "source_repo_url": source.get("repoURL"),
        "source_path": source.get("path"),
        "destination_server": destination.get("server"),
        "destination_namespace": destination.get("namespace"),
    }
    expected_profile = {
        key: profile[key] for key in actual_profile
    }
    _expect(actual_profile == expected_profile,
            f"{label}: live Application UID/project/source/destination differs from reviewed profile")
    if "source_target_revision" in profile:
        _expect(source.get("targetRevision") == profile["source_target_revision"],
                f"{label}: live Application targetRevision differs from reviewed profile")
    resources = status.get("resources")
    _expect(isinstance(resources, list),
            f"{label}: Argo Application lacks tracked resource inventory")
    resource_namespace = profile.get("resource_namespace", profile["destination_namespace"])
    required_resources = [
        ("Deployment", profile["deployment_name"]),
        ("Service", profile["service_name"]),
    ]
    if profile["kind"] == "argocd-web-workload-v1":
        required_resources.append(("Ingress", profile["ingress_name"]))
    for kind, name in required_resources:
        matches = [row for row in resources if isinstance(row, dict)
                   and row.get("kind") == kind and row.get("name") == name
                   and row.get("namespace") == resource_namespace]
        _expect(len(matches) == 1 and matches[0].get("status") == "Synced",
                f"{label}: reviewed {kind}/{name} is not uniquely Argo-tracked and Synced")
    return metadata, spec, status


def _project_observed_identity(
    raw: bytes, target: dict[str, Any], label: str,
) -> dict[str, Any]:
    if ALLOW_TEST_OPERATION_EXECUTABLES:
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"{label}: invalid fixture observation: {exc}") from exc
        _expect(isinstance(value, dict), f"{label}: fixture observation must be object")
        _validate_observed(value, label)
        return value
    metadata, spec, status = _argocd_application_parts(
        raw, target["verification_profile"], label
    )
    if target.get("apply_method") == "gitops-auto-sync-observe-v1":
        binding = target.get("auto_sync_binding") or {}
        automated = ((spec.get("syncPolicy") or {}).get("automated"))
        _expect(isinstance(automated, dict),
                f"{label}: auto-sync observation requires automated policy")
        observed_policy = {
            "enabled": automated.get("enabled", True),
            "prune": automated.get("prune", False),
            "self_heal": automated.get("selfHeal", False),
            "allow_empty": automated.get("allowEmpty", False),
        }
        _expect(metadata.get("uid") == binding.get("argocd_application_uid")
                and observed_policy == binding.get("automated"),
                f"{label}: authorized auto-sync Application UID/policy drifted")
    annotations = metadata.get("annotations") or {}
    sync = status.get("sync") or {}
    _expect(isinstance(annotations, dict) and isinstance(sync, dict),
            f"{label}: malformed Application annotations/sync status")
    observed = {
        "source_commit": annotations.get("example.com/source-commit"),
        "render_commit": sync.get("revision"),
        "image_digest": annotations.get("example.com/image-digest"),
        # Deployment generation is intentionally owned by the typed Deployment
        # probe. Argo Application generation is a different object identity.
        "generation": None,
    }
    _validate_observed(observed, label)
    return observed


def _project_auto_sync_adoption(
    raw: bytes, target: dict[str, Any], label: str,
) -> dict[str, Any]:
    _expect(target.get("apply_method") == "gitops-auto-sync-observe-v1",
            f"{label}: auto-sync projection requires explicit auto-sync method")
    observed = _project_observed_identity(raw, target, label)
    if ALLOW_TEST_OPERATION_EXECUTABLES:
        # Test fixtures project only the desired identity; deterministic self-tests
        # can still exercise the writer without a live Argo Application document.
        return {
            "kind": "observed-auto-sync-v1", "sync_status": "Synced",
            "health_status": "Healthy", "revision": target["desired"]["render_commit"],
            "observed": observed,
        }
    _metadata, _spec, status = _argocd_application_parts(
        raw, target["verification_profile"], label
    )
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    adoption = {
        "kind": "observed-auto-sync-v1",
        "sync_status": sync.get("status"),
        "health_status": health.get("status"),
        "revision": sync.get("revision"),
        "observed": observed,
    }
    _expect(adoption["sync_status"] == "Synced"
            and adoption["health_status"] == "Healthy"
            and adoption["revision"] == target["desired"]["render_commit"],
            f"{label}: auto-sync has not converged at the exact desired render")
    _desired_matches(target["desired"], observed, f"{label}.observed")
    return adoption


def _project_probe_fact(
    raw: bytes, target: dict[str, Any], kind: str, command_ok: bool, label: str,
) -> tuple[dict[str, Any], bool]:
    if ALLOW_TEST_OPERATION_EXECUTABLES:
        return {"kind": kind, "fixture_command_ok": command_ok}, command_ok
    if not command_ok:
        return {"kind": kind, "command_ok": False}, False
    desired = target["desired"]
    profile = target["verification_profile"]
    resource_namespace = profile.get("resource_namespace", profile["destination_namespace"])
    try:
        if kind == "argocd-synced":
            _metadata, _spec, status = _argocd_application_parts(raw, profile, label)
            sync = status.get("sync") or {}
            health = status.get("health") or {}
            fact = {
                "kind": kind, "sync_status": sync.get("status"),
                "health_status": health.get("status"), "revision": sync.get("revision"),
            }
            passed = (
                fact["sync_status"] == "Synced" and fact["health_status"] == "Healthy"
                and fact["revision"] == desired["render_commit"]
            )
        elif kind == "deployment-observed-generation":
            value = json.loads(raw)
            _expect(isinstance(value, dict), f"{label}: deployment output must be object")
            metadata = value.get("metadata") or {}
            spec = value.get("spec") or {}
            status = value.get("status") or {}
            _expect(metadata.get("name") == profile["deployment_name"]
                    and metadata.get("namespace") == resource_namespace
                    and metadata.get("uid") == profile["deployment_uid"],
                    f"{label}: deployment identity differs from reviewed profile")
            selector = spec.get("selector") or {}
            _expect(not selector.get("matchExpressions"),
                    f"{label}: v2 requires a matchLabels-only Deployment selector")
            match_labels = selector.get("matchLabels")
            _expect(isinstance(match_labels, dict) and bool(match_labels)
                    and all(isinstance(k, str) and isinstance(v, str)
                            for k, v in match_labels.items()),
                    f"{label}: malformed Deployment matchLabels selector")
            canonical_selector = ",".join(
                f"{key}={match_labels[key]}" for key in sorted(match_labels)
            )
            _expect(canonical_selector == profile["pod_selector"],
                    f"{label}: live Deployment selector differs from reviewed pod selector")
            fact = {
                "kind": kind, "generation": metadata.get("generation"),
                "observed_generation": status.get("observedGeneration"),
                "available_replicas": status.get("availableReplicas", 0),
                "deployment_uid": metadata.get("uid"),
                "pod_selector": canonical_selector,
            }
            passed = (
                isinstance(fact["generation"], int)
                and fact["generation"] >= 1
                and isinstance(fact["observed_generation"], int)
                and fact["observed_generation"] >= fact["generation"]
                and isinstance(fact["available_replicas"], int)
                and fact["available_replicas"] > 0
            )
        elif kind == "pod-image-digest":
            value = json.loads(raw)
            items = value.get("items") if isinstance(value, dict) else None
            _expect(isinstance(items, list) and bool(items),
                    f"{label}: pod collector returned no pods")
            digests: set[str] = set()
            all_ready = True
            for item in items:
                _expect(isinstance(item, dict), f"{label}: malformed pod item")
                metadata = item.get("metadata") or {}
                _expect(metadata.get("namespace") == resource_namespace,
                        f"{label}: pod escaped reviewed namespace")
                labels = metadata.get("labels") or {}
                selector_pairs = [part.split("=", 1) for part in profile["pod_selector"].split(",")]
                _expect(isinstance(labels, dict) and all(
                    len(pair) == 2 and labels.get(pair[0]) == pair[1]
                    for pair in selector_pairs
                ), f"{label}: pod does not carry the reviewed Deployment selector")
                statuses = (item.get("status") or {}).get("containerStatuses")
                _expect(isinstance(statuses, list) and bool(statuses),
                        f"{label}: pod lacks container statuses")
                app_rows = [
                    row for row in statuses if isinstance(row, dict)
                    and row.get("name") == profile["container_name"]
                ]
                _expect(len(app_rows) == 1,
                        f"{label}: reviewed application container is missing/duplicated")
                row = app_rows[0]
                image_id = row.get("imageID")
                _expect(isinstance(image_id, str), f"{label}: missing imageID")
                match = re.search(r"sha256:[0-9a-f]{64}", image_id)
                _expect(match is not None, f"{label}: imageID is not digest pinned")
                digests.add(match.group(0))
                all_ready = all_ready and row.get("ready") is True
            fact = {
                "kind": kind, "pod_count": len(items), "all_ready": all_ready,
                "container_name": profile["container_name"],
                "pod_selector": profile["pod_selector"],
                "image_digests": sorted(digests),
            }
            passed = all_ready and digests == {desired["image_digest"]}
        elif kind == "service-selects-workload":
            value = json.loads(raw)
            _expect(isinstance(value, dict), f"{label}: Service output must be object")
            metadata = value.get("metadata") or {}
            spec = value.get("spec") or {}
            _expect(metadata.get("name") == profile["service_name"]
                    and metadata.get("namespace") == resource_namespace
                    and metadata.get("uid") == profile["service_uid"],
                    f"{label}: Service identity differs from reviewed profile")
            selector = spec.get("selector")
            _expect(isinstance(selector, dict) and bool(selector),
                    f"{label}: Service lacks a selector")
            canonical_selector = ",".join(
                f"{key}={selector[key]}" for key in sorted(selector)
            )
            ports = spec.get("ports")
            matching_ports = [row for row in ports or [] if isinstance(row, dict)
                              and row.get("port") == profile["service_port"]]
            fact = {
                "kind": kind, "service_uid": metadata.get("uid"),
                "pod_selector": canonical_selector,
                "service_port": profile["service_port"],
            }
            passed = (
                canonical_selector == profile["pod_selector"]
                and len(matching_ports) == 1
                and spec.get("type", "ClusterIP") == "ClusterIP"
                and (
                    profile["kind"] == "argocd-web-workload-v1"
                    or (
                        matching_ports[0].get("name") == profile["service_port_name"]
                        and matching_ports[0].get("targetPort") == profile["service_target_port"]
                    )
                )
            )
        elif kind == "endpointslice-ready-backends":
            value = json.loads(raw)
            items = value.get("items") if isinstance(value, dict) else None
            _expect(isinstance(items, list) and bool(items),
                    f"{label}: EndpointSlice collector returned no slices")
            ready = 0
            target_uids: set[str] = set()
            for item in items:
                _expect(isinstance(item, dict), f"{label}: malformed EndpointSlice")
                metadata = item.get("metadata") or {}
                _expect(metadata.get("namespace") == resource_namespace
                        and (metadata.get("labels") or {}).get(
                            "kubernetes.io/service-name"
                        ) == profile["service_name"],
                        f"{label}: EndpointSlice escaped reviewed Service identity")
                matching_ports = [
                    row for row in item.get("ports") or []
                    if isinstance(row, dict)
                    and row.get("name") == profile["service_port_name"]
                    and isinstance(row.get("port"), int)
                    and 1 <= row["port"] <= 65535
                ]
                _expect(len(matching_ports) == 1,
                        f"{label}: EndpointSlice port differs from reviewed Service")
                for endpoint in item.get("endpoints") or []:
                    conditions = endpoint.get("conditions") or {}
                    target_ref = endpoint.get("targetRef") or {}
                    if conditions.get("ready") is True and conditions.get("terminating") is not True:
                        ready += 1
                        if isinstance(target_ref.get("uid"), str):
                            target_uids.add(target_ref["uid"])
            fact = {
                "kind": kind, "ready_endpoint_count": ready,
                "target_uids": sorted(target_uids),
                "service_name": profile["service_name"],
            }
            passed = ready > 0 and bool(target_uids)
        elif kind == "istio-probe-origin-ready":
            value = json.loads(raw)
            _expect(isinstance(value, dict), f"{label}: probe Pod output must be object")
            metadata = value.get("metadata") or {}
            spec = value.get("spec") or {}
            status = value.get("status") or {}
            origin = profile["probe_origin"]
            _expect(metadata.get("name") == origin["pod_name"]
                    and metadata.get("namespace") == origin["namespace"]
                    and metadata.get("uid") == origin["pod_uid"]
                    and spec.get("serviceAccountName") == origin["service_account_name"],
                    f"{label}: probe origin identity differs from reviewed profile")
            annotations = metadata.get("annotations") or {}
            _expect(not any(key in annotations for key in (
                "traffic.sidecar.istio.io/excludeOutboundPorts",
                "traffic.sidecar.istio.io/excludeOutboundIPRanges",
            )), f"{label}: probe origin bypasses sidecar interception")
            statuses = status.get("containerStatuses") or []
            rows = {row.get("name"): row for row in statuses if isinstance(row, dict)}
            app = rows.get(origin["container_name"]) or {}
            proxy = rows.get(origin["istio_proxy_container"]) or {}
            image_id = str(app.get("imageID", ""))
            fact = {
                "kind": kind, "pod_uid": metadata.get("uid"),
                "service_account_name": spec.get("serviceAccountName"),
                "app_ready": app.get("ready"), "proxy_ready": proxy.get("ready"),
                "container_image_digest": origin["container_image_digest"],
            }
            passed = (
                app.get("ready") is True and proxy.get("ready") is True
                and origin["container_image_digest"] in image_id
            )
        elif kind == "receiver-gateway-proxy-ready":
            value = json.loads(raw)
            _expect(isinstance(value, dict), f"{label}: gateway Pod output must be object")
            metadata = value.get("metadata") or {}
            status = value.get("status") or {}
            receiver = profile["receiver"]
            _expect(metadata.get("name") == receiver["gateway_proxy_pod"]
                    and metadata.get("namespace") == receiver["namespace"]
                    and metadata.get("uid") == receiver["gateway_proxy_uid"],
                    f"{label}: receiver gateway identity differs from reviewed profile")
            statuses = status.get("containerStatuses") or []
            proxy_rows = [
                row for row in statuses
                if isinstance(row, dict) and row.get("name") == "istio-proxy"
            ]
            _expect(len(proxy_rows) == 1,
                    f"{label}: receiver gateway requires exactly one istio-proxy container")
            fact = {
                "kind": kind, "pod_uid": metadata.get("uid"),
                "proxy_ready": proxy_rows[0].get("ready"),
            }
            passed = proxy_rows[0].get("ready") is True
        elif kind in {
            "sender-serviceentry-route-exact", "receiver-serviceentry-route-exact",
        }:
            value = json.loads(raw)
            metadata = value.get("metadata") if isinstance(value, dict) else {}
            spec = value.get("spec") if isinstance(value, dict) else {}
            sender = kind.startswith("sender-")
            side = profile["sender"] if sender else profile["receiver"]
            _expect(isinstance(metadata, dict) and isinstance(spec, dict),
                    f"{label}: ServiceEntry output must be object")
            expected_host = profile["global_service_host"]
            _expect(metadata.get("name") == side["service_entry_name"]
                    and metadata.get("namespace") == side["namespace"]
                    and metadata.get("uid") == side["service_entry_uid"]
                    and spec.get("hosts") == [expected_host],
                    f"{label}: ServiceEntry identity/global host mismatch")
            ports = [row for row in spec.get("ports") or [] if isinstance(row, dict)]
            endpoints = [
                row for row in spec.get("endpoints") or [] if isinstance(row, dict)
            ]
            expected_endpoint = (
                side["eastwest_endpoint_host"] if sender
                else profile["receiver"]["local_service_host"]
            )
            expected_port = (
                side["eastwest_endpoint_port"] if sender
                else profile["receiver"]["local_service_port"]
            )
            fact = {
                "kind": kind, "uid": metadata.get("uid"), "host": expected_host,
                "endpoint_host": expected_endpoint, "endpoint_port": expected_port,
            }
            exact_ports = [
                row for row in ports
                if row.get("number") == profile["service_port"]
            ]
            exact_endpoints = [
                row for row in endpoints
                if row.get("address") == expected_endpoint
                and isinstance(row.get("ports"), dict)
                and expected_port in row["ports"].values()
            ]
            passed = (
                len(ports) == len(exact_ports) == 1
                and len(endpoints) == len(exact_endpoints) == 1
            )
        elif kind in {
            "sender-destinationrule-mtls-exact",
            "receiver-destinationrule-mtls-exact",
        }:
            value = json.loads(raw)
            metadata = value.get("metadata") if isinstance(value, dict) else {}
            spec = value.get("spec") if isinstance(value, dict) else {}
            sender = kind.startswith("sender-")
            side = profile["sender"] if sender else profile["receiver"]
            expected_host = (
                profile["global_service_host"] if sender
                else profile["receiver"]["local_service_host"]
            )
            tls = ((spec or {}).get("trafficPolicy") or {}).get("tls") or {}
            _expect(isinstance(metadata, dict) and isinstance(spec, dict),
                    f"{label}: DestinationRule output must be object")
            fact = {
                "kind": kind, "uid": metadata.get("uid"),
                "host": spec.get("host"), "tls_mode": tls.get("mode"),
            }
            passed = (
                metadata.get("name") == side["destination_rule_name"]
                and metadata.get("namespace") == side["namespace"]
                and metadata.get("uid") == side["destination_rule_uid"]
                and spec.get("host") == expected_host
                and tls.get("mode") == "ISTIO_MUTUAL"
            )
        elif kind == "receiver-envoyfilter-cluster-exact":
            value = json.loads(raw)
            metadata = value.get("metadata") if isinstance(value, dict) else {}
            spec = value.get("spec") if isinstance(value, dict) else {}
            receiver = profile["receiver"]

            def exact_socket(node: Any) -> bool:
                if isinstance(node, dict):
                    socket = node.get("socketAddress")
                    if (isinstance(socket, dict)
                            and socket.get("address") == receiver["local_service_host"]
                            and socket.get("portValue") == receiver["local_service_port"]):
                        return True
                    return any(exact_socket(item) for item in node.values())
                if isinstance(node, list):
                    return any(exact_socket(item) for item in node)
                return False

            config_patches = (
                spec.get("configPatches") if isinstance(spec, dict) else None
            )
            exact_patches: list[dict[str, Any]] = []
            if isinstance(config_patches, list):
                for item in config_patches:
                    if not isinstance(item, dict):
                        continue
                    match = item.get("match") or {}
                    patch = item.get("patch") or {}
                    patch_value = patch.get("value") or {}
                    if (item.get("applyTo") == "CLUSTER"
                            and isinstance(match, dict)
                            and match.get("context") == "GATEWAY"
                            and isinstance(patch, dict)
                            and patch.get("operation") == "ADD"
                            and isinstance(patch_value, dict)
                            and patch_value.get("name") == receiver["envoy_cluster_name"]
                            and exact_socket(patch_value)):
                        exact_patches.append(item)
            fact = {
                "kind": kind, "uid": metadata.get("uid"),
                "cluster_name": receiver["envoy_cluster_name"],
                "local_service_host": receiver["local_service_host"],
                "local_service_port": receiver["local_service_port"],
            }
            passed = (
                metadata.get("name") == receiver["envoy_filter_name"]
                and metadata.get("namespace") == receiver["namespace"]
                and metadata.get("uid") == receiver["envoy_filter_uid"]
                and isinstance(config_patches, list)
                and len(config_patches) == len(exact_patches) == 1
            )
        elif kind in {"sender-istio-xds-synced", "receiver-istio-xds-synced"}:
            value = json.loads(raw)
            serialized = json.dumps(value, sort_keys=True)
            side = profile["sender"] if kind.startswith("sender-") else profile["receiver"]
            proxy = (
                f"{side['proxy_pod']}.{profile['probe_origin']['namespace']}"
                if kind.startswith("sender-")
                else f"{side['gateway_proxy_pod']}.{side['namespace']}"
            )
            statuses = re.findall(r'(?i)"(?:cds|lds|eds|rds)"\s*:\s*"([^"]+)"', serialized)
            fact = {"kind": kind, "proxy": proxy, "xds_statuses": statuses}
            passed = proxy in serialized and bool(statuses) and all(
                status.casefold() == "synced" for status in statuses
            )
        elif kind in {
            "sender-istio-cluster-healthy-endpoints",
            "receiver-istio-cluster-healthy-endpoints",
        }:
            value = json.loads(raw)
            serialized = json.dumps(value, sort_keys=True)
            cluster = profile["receiver"]["envoy_cluster_name"]
            healthy = len(re.findall(r'(?i)"healthStatus"\s*:\s*"HEALTHY"', serialized))
            fact = {"kind": kind, "cluster_name": cluster, "healthy_endpoints": healthy}
            passed = cluster in serialized and healthy > 0
        elif kind == "ingress-routes-service":
            value = json.loads(raw)
            _expect(isinstance(value, dict), f"{label}: Ingress output must be object")
            metadata = value.get("metadata") or {}
            spec = value.get("spec") or {}
            _expect(metadata.get("name") == profile["ingress_name"]
                    and metadata.get("namespace") == profile["destination_namespace"]
                    and metadata.get("uid") == profile["ingress_uid"],
                    f"{label}: Ingress identity differs from reviewed profile")
            smoke = urlparse(profile["behavioral_smoke_url"])
            exact_backends: list[dict[str, Any]] = []
            for rule in spec.get("rules") or []:
                if not isinstance(rule, dict) or rule.get("host") != smoke.hostname:
                    continue
                for path_row in ((rule.get("http") or {}).get("paths") or []):
                    if (isinstance(path_row, dict)
                            and path_row.get("pathType") == "Exact"
                            and path_row.get("path") == profile["ingress_path"]):
                        exact_backends.append(path_row.get("backend") or {})
            expected_backend = {
                "service": {
                    "name": profile["service_name"],
                    "port": {"number": profile["service_port"]},
                }
            }
            fact = {
                "kind": kind, "ingress_uid": metadata.get("uid"),
                "host": smoke.hostname, "path": profile["ingress_path"],
                "service_name": profile["service_name"],
                "service_port": profile["service_port"],
            }
            passed = len(exact_backends) == 1 and exact_backends[0] == expected_backend
        elif kind in {
            "behavioral-smoke", "internal-behavioral-smoke",
            "eastwest-behavioral-smoke",
        }:
            text = raw.decode("ascii", errors="strict")
            _expect(bool(re.fullmatch(r"[0-9]{3}", text)),
                    f"{label}: curl did not emit one HTTP status")
            status_code = int(text)
            fact = {"kind": kind, "http_status": status_code}
            passed = status_code == profile["behavioral_smoke_status"]
        else:
            raise ValidationError(f"{label}: unsupported typed collector {kind!r}")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        return {"kind": kind, "semantic_valid": False}, False
    return fact, passed


def _validate_apply_command_semantics(
    argv: list[str], target: dict[str, Any], label: str,
) -> None:
    """v2 mutations are limited to an exact, remote, revision-pinned Argo CD sync."""
    if ALLOW_TEST_OPERATION_EXECUTABLES:
        return
    _expect(Path(argv[0]).name.casefold() == "argocd",
            f"{label}: v2 apply must use argocd")
    app_name = target["resource"].split("/", 1)[-1]
    _expect(argv[1:4] == ["app", "sync", app_name],
            f"{label}: expected exact 'argocd app sync {app_name}' target")
    render_commit = target["desired"].get("render_commit")
    _expect(render_commit is not None
            and _option_value(argv, "--revision", label) == render_commit,
            f"{label}: GitOps sync must pin --revision to desired.render_commit")
    value_options = {
        "--server", "--revision", "--timeout", "--config", "--argocd-context"
    }
    bool_options = {"--prune", "--grpc-web"}
    index = 4
    while index < len(argv):
        value = argv[index]
        if value in bool_options:
            index += 1
            continue
        equals_match = next(
            (option for option in value_options if value.startswith(option + "=")), None
        )
        if equals_match is not None:
            _expect(bool(value.split("=", 1)[1]),
                    f"{label}: {equals_match} requires a value")
            index += 1
            continue
        _expect(value in value_options,
                f"{label}: unsupported or unsafe Argo CD sync flag {value!r}")
        _expect(index + 1 < len(argv), f"{label}: {value} requires a value")
        index += 2


def _auto_verification_action(target: dict[str, Any]) -> dict[str, Any]:
    """Canonical active/read-only verification surface authorized before publish.

    The future render commit is deliberately excluded: publication policy can bind
    the executable/context/profile/action surface before CI creates that commit;
    release/plan cross-links bind the exact resulting render identity later.
    """
    return {
        "execution_environment": target["execution_environment"],
        "execution_contexts": target["execution_contexts"],
        "verification_profile": target["verification_profile"],
        "observation_command": target["observation_command"],
        "observation_executable_sha256": target["observation_executable_sha256"],
        "observation_timeout_seconds": target["observation_timeout_seconds"],
        "verification_contract": target["verification_contract"],
    }


def _required_probe_kinds(profile_kind: str, image_required: bool) -> set[str]:
    common = {"argocd-synced", "deployment-observed-generation", "service-selects-workload"}
    if profile_kind == "argocd-web-workload-v1":
        required = common | {"ingress-routes-service", "behavioral-smoke"}
    elif profile_kind == "argocd-istio-internal-http-v1":
        required = common | {
            "endpointslice-ready-backends", "istio-probe-origin-ready",
            "internal-behavioral-smoke",
        }
    elif profile_kind == "argocd-istio-eastwest-v1":
        required = common | {
            "endpointslice-ready-backends", "istio-probe-origin-ready",
            "receiver-gateway-proxy-ready",
            "sender-serviceentry-route-exact", "sender-destinationrule-mtls-exact",
            "receiver-serviceentry-route-exact", "receiver-destinationrule-mtls-exact",
            "receiver-envoyfilter-cluster-exact", "sender-istio-xds-synced",
            "receiver-istio-xds-synced", "sender-istio-cluster-healthy-endpoints",
            "receiver-istio-cluster-healthy-endpoints", "eastwest-behavioral-smoke",
        }
    else:  # pragma: no cover - profile validator rejects this first
        _fail(f"unsupported verification profile {profile_kind!r}")
    if image_required:
        required.add("pod-image-digest")
    return required


def validate_operations_plan(
    data: dict[str, Any], state: dict[str, Any], now: datetime | None = None,
    *, verify_executables: bool = True,
) -> dict[str, Any]:
    label = "operations_plan"
    _artifact_envelope(data, label, state["id"], now)
    _strict_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "operations_required", "not_required_reason", "plan_hash", "max_evidence_age_seconds", "targets",
    }, label)
    _require_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "operations_required", "not_required_reason", "plan_hash", "max_evidence_age_seconds", "targets",
    }, label)
    required = data.get("operations_required")
    _expect(isinstance(required, bool), f"{label}.operations_required: expected bool")
    _expect(required == state.get("operations_required"), f"{label}.operations_required does not match state")
    expected_hash = plan_hash(data)
    _expect(_sha256_value(data.get("plan_hash"), f"{label}.plan_hash") == expected_hash,
            f"{label}.plan_hash: stale or incorrectly canonicalized plan")
    max_age = _integer(data.get("max_evidence_age_seconds"), f"{label}.max_evidence_age_seconds", 60)
    _expect(max_age <= 604800, f"{label}.max_evidence_age_seconds: cannot exceed 7 days")
    targets = data.get("targets")
    _expect(isinstance(targets, list), f"{label}.targets: expected array")
    if not required:
        _nonempty_string(data.get("not_required_reason"), f"{label}.not_required_reason")
        _expect(not targets, f"{label}.targets: must be empty when operations are not required")
        return {"plan_hash": expected_hash, "target_ids": [], "scopes": {}}
    _expect(data.get("not_required_reason") is None, f"{label}.not_required_reason: must be null when required")
    _expect(bool(targets), f"{label}.targets: required operations need at least one target")
    ids: list[str] = []
    target_slugs: list[str] = []
    scopes: dict[str, str] = {}
    for i, target in enumerate(targets):
        tlabel = f"{label}.targets[{i}]"
        _expect(isinstance(target, dict), f"{tlabel}: expected object")
        target_keys = {
            "id", "environment", "account", "cluster", "resource", "apply_method",
            "execution_environment", "execution_contexts", "verification_profile",
            "desired", "auto_sync_binding", "apply_command", "apply_executable_sha256", "apply_timeout_seconds",
            "observation_command", "observation_executable_sha256", "observation_timeout_seconds",
            "verification_contract", "rollback", "operations_owner", "verification_owner",
        }
        _strict_keys(target, target_keys, tlabel)
        _require_keys(target, target_keys - {"auto_sync_binding"}, tlabel)
        tid = _nonempty_string(target.get("id"), f"{tlabel}.id")
        _expect(tid not in ids, f"{label}.targets: duplicate target id {tid!r}")
        ids.append(tid)
        target_slugs.append(
            re.sub(r"[^A-Za-z0-9._-]+", "-", tid).strip("-") or "target"
        )
        for field in ("environment", "account", "cluster", "resource", "operations_owner", "verification_owner"):
            _nonempty_string(target.get(field), f"{tlabel}.{field}")
        apply_method = target.get("apply_method")
        _expect(apply_method in {"gitops-manual-sync", "gitops-auto-sync-observe-v1"},
                f"{tlabel}.apply_method: generic or unknown auto-sync is forbidden")
        execution_environment = _validate_execution_environment(
            target.get("execution_environment"), target,
            f"{tlabel}.execution_environment",
        )
        execution_contexts = _validate_execution_contexts(
            target.get("execution_contexts"), target, execution_environment,
            f"{tlabel}.execution_contexts", verify_files=verify_executables,
        )
        verification_profile = _validate_verification_profile(
            target.get("verification_profile"), target,
            f"{tlabel}.verification_profile",
        )
        profile_kind = verification_profile["kind"]
        expected_context_keys = {"kubernetes", "argocd"}
        if profile_kind == "argocd-istio-eastwest-v1":
            expected_context_keys |= {"sender_kubernetes", "receiver_kubernetes"}
        if not ALLOW_TEST_OPERATION_EXECUTABLES:
            _expect(set(execution_contexts) == expected_context_keys,
                    f"{tlabel}.execution_contexts: profile requires exactly {sorted(expected_context_keys)}")
            if profile_kind == "argocd-istio-eastwest-v1":
                workload_context = execution_contexts["kubernetes"]
                sender_context = execution_contexts["sender_kubernetes"]
                receiver_context = execution_contexts["receiver_kubernetes"]
                _expect(
                    receiver_context["cluster_server"]
                    == workload_context["cluster_server"]
                    and receiver_context["certificate_authority_sha256"]
                    == workload_context["certificate_authority_sha256"],
                    f"{tlabel}.execution_contexts: receiver mesh and workload proofs must target the same cluster",
                )
                _expect(
                    sender_context["cluster_server"]
                    != receiver_context["cluster_server"],
                    f"{tlabel}.execution_contexts: east-west sender and receiver must be distinct clusters",
                )
        desired = target.get("desired")
        _expect(isinstance(desired, dict), f"{tlabel}.desired: expected object")
        _strict_keys(desired, {"source_commit", "render_commit", "image_digest"}, f"{tlabel}.desired")
        _expect(any(desired.get(k) is not None for k in desired), f"{tlabel}.desired: at least one desired identity is required")
        for field in ("source_commit", "render_commit"):
            if desired.get(field) is not None:
                _commit(desired.get(field), f"{tlabel}.desired.{field}")
        if desired.get("image_digest") is not None:
            digest = _nonempty_string(desired.get("image_digest"), f"{tlabel}.desired.image_digest")
            _expect(digest.startswith("sha256:") and bool(SHA256_RE.fullmatch(digest[7:])),
                    f"{tlabel}.desired.image_digest: expected sha256 digest")
        binding = target.get("auto_sync_binding")
        if apply_method == "gitops-manual-sync":
            _expect(binding is None, f"{tlabel}.auto_sync_binding: manual sync cannot inherit publication effects")
            apply_argv = _command_argv(target.get("apply_command"), f"{tlabel}.apply_command")
            _reject_secret_argv(apply_argv, f"{tlabel}.apply_command")
            apply_sha = _sha256_value(
                target.get("apply_executable_sha256"), f"{tlabel}.apply_executable_sha256"
            )
            _expect(os.path.isabs(apply_argv[0]), f"{tlabel}.apply_command: executable must be absolute")
            _validate_apply_command_semantics(apply_argv, target, f"{tlabel}.apply_command")
            _validate_command_context(apply_argv, execution_contexts, f"{tlabel}.apply_command")
            if verify_executables:
                _resolved_executable(
                    apply_argv, f"{tlabel}.apply_command", require_absolute=True,
                    expected_sha256=apply_sha,
                )
                _validate_operational_executable_trust(
                    apply_argv, apply_sha, state, f"{tlabel}.apply_command", apply_method,
                )
            apply_timeout = _integer(
                target.get("apply_timeout_seconds"), f"{tlabel}.apply_timeout_seconds", 1,
            )
            _expect(apply_timeout <= 900,
                    f"{tlabel}.apply_timeout_seconds: cannot exceed 900")
        else:
            _expect(profile_kind in {
                "argocd-istio-internal-http-v1", "argocd-istio-eastwest-v1"
            }, f"{tlabel}: auto-sync v1 is limited to typed internal/Istio profiles")
            _expect(target.get("apply_command") is None
                    and target.get("apply_executable_sha256") is None
                    and target.get("apply_timeout_seconds") is None,
                    f"{tlabel}: auto-sync observation performs no apply command")
            _expect(isinstance(binding, dict), f"{tlabel}.auto_sync_binding: expected object")
            binding_keys = {
                "publication_intent_id", "publication_scope_hash",
                "delivery_effect_sha256", "target_id", "render_remote",
                "render_branch", "argocd_application_uid",
                "verification_action_sha256", "automated",
            }
            _strict_keys(binding, binding_keys, f"{tlabel}.auto_sync_binding")
            _require_keys(binding, binding_keys, f"{tlabel}.auto_sync_binding")
            for field in (
                "publication_intent_id", "target_id", "render_remote", "render_branch",
                "argocd_application_uid",
            ):
                _nonempty_string(binding.get(field), f"{tlabel}.auto_sync_binding.{field}")
            for field in (
                "publication_scope_hash", "delivery_effect_sha256",
                "verification_action_sha256",
            ):
                _sha256_value(binding.get(field), f"{tlabel}.auto_sync_binding.{field}")
            _expect(binding["target_id"] == tid
                    and binding["argocd_application_uid"] == verification_profile["argocd_application_uid"]
                    and binding["render_remote"] == verification_profile["source_repo_url"]
                    and binding["render_branch"] == verification_profile["source_target_revision"],
                    f"{tlabel}.auto_sync_binding: target/Application/render identity mismatch")
            automated = binding.get("automated")
            _expect(isinstance(automated, dict), f"{tlabel}.auto_sync_binding.automated: expected object")
            _strict_keys(automated, {"enabled", "prune", "self_heal", "allow_empty"},
                         f"{tlabel}.auto_sync_binding.automated")
            _require_keys(automated, {"enabled", "prune", "self_heal", "allow_empty"},
                          f"{tlabel}.auto_sync_binding.automated")
            _expect(automated.get("enabled") is True,
                    f"{tlabel}.auto_sync_binding.automated.enabled: expected true")
            _expect(all(isinstance(automated[key], bool) for key in automated),
                    f"{tlabel}.auto_sync_binding.automated: exact booleans required")
        observation_argv = _command_argv(
            target.get("observation_command"), f"{tlabel}.observation_command"
        )
        _reject_secret_argv(observation_argv, f"{tlabel}.observation_command")
        observation_sha = _sha256_value(
            target.get("observation_executable_sha256"),
            f"{tlabel}.observation_executable_sha256",
        )
        _expect(os.path.isabs(observation_argv[0]),
                f"{tlabel}.observation_command: executable must be absolute")
        _validate_command_context(
            observation_argv, execution_contexts, f"{tlabel}.observation_command"
        )
        if verify_executables:
            _resolved_executable(
                observation_argv, f"{tlabel}.observation_command", require_absolute=True,
                expected_sha256=observation_sha,
            )
            _validate_operational_executable_trust(
                observation_argv, observation_sha, state,
                f"{tlabel}.observation_command", target["apply_method"],
            )
        _validate_observation_command_semantics(
            observation_argv, execution_contexts, verification_profile,
            f"{tlabel}.observation_command",
        )
        _integer(
            target.get("observation_timeout_seconds"),
            f"{tlabel}.observation_timeout_seconds", 1,
        )
        _expect(target["observation_timeout_seconds"] <= 300,
                f"{tlabel}.observation_timeout_seconds: cannot exceed 300")
        contract = target.get("verification_contract")
        _expect(isinstance(contract, list) and contract, f"{tlabel}.verification_contract: expected non-empty array")
        kinds: list[str] = []
        evidence_slugs: list[str] = []
        for j, probe in enumerate(contract):
            plabel = f"{tlabel}.verification_contract[{j}]"
            _expect(isinstance(probe, dict), f"{plabel}: expected object")
            _strict_keys(probe, {"kind", "command", "executable_sha256", "timeout_seconds"}, plabel)
            _require_keys(probe, {"kind", "command", "executable_sha256", "timeout_seconds"}, plabel)
            kind = _nonempty_string(probe.get("kind"), f"{plabel}.kind")
            kinds.append(kind)
            evidence_slugs.append(
                re.sub(r"[^A-Za-z0-9._-]+", "-", kind).strip("-") or "command"
            )
            probe_argv = _command_argv(probe.get("command"), f"{plabel}.command")
            _reject_secret_argv(probe_argv, f"{plabel}.command")
            probe_sha = _sha256_value(
                probe.get("executable_sha256"), f"{plabel}.executable_sha256"
            )
            _expect(os.path.isabs(probe_argv[0]),
                    f"{plabel}.command: executable must be absolute")
            _validate_command_context(probe_argv, execution_contexts, f"{plabel}.command")
            if verify_executables:
                _resolved_executable(
                    probe_argv, f"{plabel}.command", require_absolute=True,
                    expected_sha256=probe_sha,
                )
                _validate_operational_executable_trust(
                    probe_argv, probe_sha, state, plabel, target["apply_method"]
                )
            timeout = _integer(probe.get("timeout_seconds"), f"{plabel}.timeout_seconds", 1)
            _expect(timeout <= 300, f"{plabel}.timeout_seconds: cannot exceed 300")
            _validate_probe_command_semantics(
                kind, probe_argv, timeout, execution_contexts,
                verification_profile, plabel,
            )
        _expect(len(kinds) == len(set(kinds)), f"{tlabel}.verification_contract: duplicate probe kind")
        _expect(len(evidence_slugs) == len(set(evidence_slugs)),
                f"{tlabel}.verification_contract: probe names collide as evidence filenames")
        _expect("observed-identity" not in evidence_slugs,
                f"{tlabel}.verification_contract: probe name collides with observation evidence")
        required_kinds = _required_probe_kinds(
            profile_kind, desired.get("image_digest") is not None
        )
        _expect(set(kinds) == required_kinds,
                f"{tlabel}.verification_contract: expected exact typed probe set {sorted(required_kinds)}")
        _expect(desired.get("render_commit") is not None,
                f"{tlabel}.desired.render_commit: required for GitOps")
        if apply_method == "gitops-auto-sync-observe-v1":
            _expect(desired.get("source_commit") is not None,
                    f"{tlabel}.desired.source_commit: required for automatic publication effect")
            expected_action_sha = _value_sha(_auto_verification_action(target))
            _expect(binding["verification_action_sha256"] == expected_action_sha,
                    f"{tlabel}.auto_sync_binding.verification_action_sha256: active verification scope drifted")
        _nonempty_string(target.get("rollback"), f"{tlabel}.rollback")
        scopes[tid] = target_scope_hash(data, target)
    _expect(len(target_slugs) == len(set(target_slugs)),
            f"{label}.targets: target ids collide as evidence directory names")
    return {"plan_hash": expected_hash, "target_ids": ids, "scopes": scopes, "max_age": max_age}


def _validate_observed(observed: dict[str, Any], label: str) -> None:
    _strict_keys(observed, {"source_commit", "render_commit", "image_digest", "generation"}, label)
    for field in ("source_commit", "render_commit"):
        if observed.get(field) is not None:
            _commit(observed[field], f"{label}.{field}")
    if observed.get("image_digest") is not None:
        digest = _nonempty_string(observed["image_digest"], f"{label}.image_digest")
        _expect(digest.startswith("sha256:") and bool(SHA256_RE.fullmatch(digest[7:])),
                f"{label}.image_digest: expected sha256 digest")
    if observed.get("generation") is not None:
        _integer(observed["generation"], f"{label}.generation", 1)


def _desired_matches(desired: dict[str, Any], observed: dict[str, Any], label: str) -> None:
    _validate_observed(observed, label)
    mapping = {
        "source_commit": "source_commit",
        "render_commit": "render_commit",
        "image_digest": "image_digest",
    }
    for desired_key, observed_key in mapping.items():
        if desired.get(desired_key) is not None:
            _expect(observed.get(observed_key) == desired[desired_key],
                    f"{label}.{observed_key}: observed value does not match desired {desired_key}")


def _derive_attempt_status(attempt: dict[str, Any]) -> str:
    if attempt["verification"]["status"] == "verified":
        return "verified"
    if attempt["verification"]["status"] == "failed" or attempt["apply"]["status"] == "failed":
        return "failed"
    if attempt["apply"]["status"] == "applied":
        return "applied"
    if attempt["apply"]["status"] == "executing":
        return "executing"
    return "pending"


def validate_operations_evidence(
    data: dict[str, Any], state: dict[str, Any], plan: dict[str, Any], now: datetime,
    evidence_root: Path | None = None, *, enforce_latest_freshness: bool = True,
    verify_executables: bool = True,
) -> dict[str, Any]:
    label = "operations_evidence"
    _artifact_envelope(data, label, state["id"], now)
    _validate_deterministic_producer(data.get("producer"), state, f"{label}.producer")
    _strict_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "plan_hash", "targets",
    }, label)
    _require_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer",
        "plan_hash", "targets",
    }, label)
    pmeta = validate_operations_plan(
        plan, state, now, verify_executables=verify_executables
    )
    _expect(data.get("generation") == plan.get("generation"),
            f"{label}.generation: must equal operations_plan.generation")
    _expect(_sha256_value(data.get("plan_hash"), f"{label}.plan_hash") == pmeta["plan_hash"],
            f"{label}.plan_hash: evidence belongs to a different/stale plan")
    targets = data.get("targets")
    _expect(isinstance(targets, list), f"{label}.targets: expected array")
    plan_targets = {t["id"]: t for t in plan["targets"]}
    evidence_target_ids = [t.get("id") for t in targets if isinstance(t, dict)]
    _expect(len(evidence_target_ids) == len(targets),
            f"{label}.targets: every target must be an object")
    _expect(len(evidence_target_ids) == len(set(evidence_target_ids)),
            f"{label}.targets: duplicate target id")
    _expect(set(evidence_target_ids) == set(plan_targets),
            f"{label}.targets: must exactly match the frozen plan target set")
    attempt_hashes: dict[str, str] = {}
    verification_refresh_hashes: dict[str, str] = {}
    intent_hashes: dict[str, str] = {}
    apply_hashes: dict[str, str] = {}
    authorization_hashes: dict[str, str] = {}
    statuses: dict[str, str] = {}
    apply_statuses: dict[str, str] = {}
    verification_gaps: dict[str, list[str]] = {}
    identity_matches: dict[str, bool] = {}
    seen_global: set[str] = set()
    for i, target in enumerate(targets):
        tlabel = f"{label}.targets[{i}]"
        _expect(isinstance(target, dict), f"{tlabel}: expected object")
        _strict_keys(target, {
            "id", "status", "attempts", "verification_refresh_intents",
            "verification_refreshes",
        }, tlabel)
        _require_keys(target, {
            "id", "status", "attempts", "verification_refresh_intents",
            "verification_refreshes",
        }, tlabel)
        tid = _nonempty_string(target.get("id"), f"{tlabel}.id")
        planned = plan_targets[tid]
        attempts = target.get("attempts")
        _expect(isinstance(attempts, list), f"{tlabel}.attempts: expected array")
        refreshes = target.get("verification_refreshes")
        _expect(isinstance(refreshes, list),
                f"{tlabel}.verification_refreshes: expected array")
        refresh_intents = target.get("verification_refresh_intents")
        _expect(isinstance(refresh_intents, list),
                f"{tlabel}.verification_refresh_intents: expected array")
        previous_seq = 0
        previous_hash: str | None = None
        attempt_by_hash: dict[str, dict[str, Any]] = {}
        latest_attempt_hash_raw = _value_sha(attempts[-1]) if attempts else None
        latest_has_refresh = bool(
            refreshes
            and isinstance(refreshes[-1], dict)
            and refreshes[-1].get("source_attempt_sha256") == latest_attempt_hash_raw
        )
        contract_by_kind = _contract_by_kind(planned)
        last_gap = sorted(contract_by_kind)
        last_identity_matches = False
        for j, attempt in enumerate(attempts):
            is_latest = j == len(attempts) - 1 and not latest_has_refresh
            last_identity_matches = False
            alabel = f"{tlabel}.attempts[{j}]"
            _expect(isinstance(attempt, dict), f"{alabel}: expected object")
            _strict_keys(attempt, {"attempt_id", "sequence", "previous_attempt_sha256", "recorded_at", "authorization", "apply", "verification"}, alabel)
            _require_keys(attempt, {"attempt_id", "sequence", "previous_attempt_sha256", "recorded_at", "authorization", "apply", "verification"}, alabel)
            aid = _nonempty_string(attempt.get("attempt_id"), f"{alabel}.attempt_id")
            _expect(aid not in seen_global, f"{label}: duplicate attempt_id {aid!r}")
            seen_global.add(aid)
            seq = _integer(attempt.get("sequence"), f"{alabel}.sequence", 1)
            _expect(seq == previous_seq + 1, f"{alabel}.sequence: must be contiguous from 1")
            prev = attempt.get("previous_attempt_sha256")
            if previous_hash is None:
                _expect(prev is None, f"{alabel}.previous_attempt_sha256: first attempt must be null")
            else:
                _expect(_sha256_value(prev, f"{alabel}.previous_attempt_sha256") == previous_hash,
                        f"{alabel}.previous_attempt_sha256: append-only chain broken")
            recorded_at = _iso(attempt.get("recorded_at"), f"{alabel}.recorded_at")
            _expect(recorded_at <= now, f"{alabel}.recorded_at: future attempt record forbidden")
            is_auto = planned["apply_method"] == "gitops-auto-sync-observe-v1"
            auth = attempt.get("authorization")
            _expect(isinstance(auth, dict), f"{alabel}.authorization: expected object")
            auth_keys = {"decision", "by", "method", "at", "scope_hash"}
            if is_auto:
                auth_keys |= {
                    "publication_scope_hash", "delivery_effect_sha256", "target_id"
                }
            _strict_keys(auth, auth_keys, f"{alabel}.authorization")
            _require_keys(auth, auth_keys, f"{alabel}.authorization")
            _expect(auth.get("decision") == "approved", f"{alabel}.authorization.decision: expected approved")
            _human_name(auth.get("by"), f"{alabel}.authorization.by")
            _expect(auth.get("method") == (
                "publication-effect" if is_auto else "human-explicit"
            ), f"{alabel}.authorization.method: wrong authorization branch")
            auth_at = _iso(auth.get("at"), f"{alabel}.authorization.at")
            _expect(auth_at <= recorded_at <= now,
                    f"{alabel}: authorization must not be future or follow the attempt record")
            _expect(_sha256_value(auth.get("scope_hash"), f"{alabel}.authorization.scope_hash") == pmeta["scopes"][tid],
                    f"{alabel}.authorization.scope_hash: authorization replay or stale plan")
            if is_auto:
                binding = planned["auto_sync_binding"]
                _expect(auth["publication_scope_hash"] == binding["publication_scope_hash"]
                        and auth["delivery_effect_sha256"] == binding["delivery_effect_sha256"]
                        and auth["target_id"] == tid,
                        f"{alabel}.authorization: publication effect binding mismatch")
            apply = attempt.get("apply")
            _expect(isinstance(apply, dict), f"{alabel}.apply: expected object")
            _strict_keys(apply, {
                "kind",
                "status", "at", "actor", "idempotency_key", "intent_evidence",
                "observed", "evidence", "observation_evidence", "failure_reason",
                "recovered_from_ambiguous",
            }, f"{alabel}.apply")
            _require_keys(apply, {
                "status", "at", "actor", "idempotency_key", "intent_evidence",
                "observed", "evidence", "observation_evidence", "failure_reason",
                "recovered_from_ambiguous",
            }, f"{alabel}.apply")
            _expect(apply.get("status") in {"pending", "executing", "applied", "failed"},
                    f"{alabel}.apply.status: invalid")
            apply_at: datetime | None = None
            expected_apply_command = (
                None if is_auto else shlex.join(_command_argv(
                    planned["apply_command"], f"{alabel}.planned.apply_command"
                ))
            )
            expected_observation_command = shlex.join(_command_argv(
                planned["observation_command"],
                f"{alabel}.planned.observation_command",
            ))
            if apply.get("status") != "pending":
                apply_at = _iso(apply.get("at"), f"{alabel}.apply.at")
                _expect(recorded_at <= apply_at <= now,
                        f"{alabel}: apply must follow the authorized attempt record and not be future")
                _nonempty_string(apply.get("actor"), f"{alabel}.apply.actor")
                _expect(isinstance(apply.get("recovered_from_ambiguous"), bool),
                        f"{alabel}.apply.recovered_from_ambiguous: expected bool")
                if is_auto:
                    _expect(apply.get("kind") == "observed-auto-sync-v1"
                            and apply.get("actor") == "argocd-auto-sync-observer"
                            and apply.get("idempotency_key") is None
                            and apply.get("intent_evidence") is None
                            and apply.get("evidence") is None
                            and apply.get("recovered_from_ambiguous") is False,
                            f"{alabel}.apply: auto-sync adoption must be non-mutating observation only")
                else:
                    _expect(apply.get("kind") is None,
                            f"{alabel}.apply.kind: manual apply cannot claim auto-sync adoption")
                    _sha256_value(apply.get("idempotency_key"),
                                  f"{alabel}.apply.idempotency_key")
                    _validate_evidence_ref(
                        apply.get("intent_evidence"),
                        f"{alabel}.apply.intent_evidence", evidence_root,
                    )
                    _expect(apply["intent_evidence"].get("command") == expected_apply_command,
                            f"{alabel}.apply.intent_evidence.command: does not match frozen apply command")
            else:
                _expect(not is_auto,
                        f"{alabel}.apply: auto-sync adoption is appended only as a terminal observation")
                _expect(apply.get("kind") is None,
                        f"{alabel}.apply.kind: pending manual apply must omit kind")
                _expect(all(apply.get(k) is None for k in (
                    "at", "actor", "idempotency_key", "intent_evidence", "observed",
                    "evidence", "observation_evidence", "failure_reason",
                    "recovered_from_ambiguous",
                )),
                        f"{alabel}.apply: pending apply cannot carry terminal evidence")
            if apply.get("status") == "executing":
                _expect(not is_auto,
                        f"{alabel}.apply: auto-sync observer has no executing mutation state")
                _expect(all(apply.get(k) is None for k in (
                    "observed", "evidence", "observation_evidence", "failure_reason",
                )), f"{alabel}.apply: executing intent cannot claim an outcome")
                _expect(apply.get("recovered_from_ambiguous") is False,
                        f"{alabel}.apply: fresh executing intent cannot be a recovery")
            elif apply.get("status") in {"applied", "failed"}:
                recovered = apply["recovered_from_ambiguous"]
                if is_auto:
                    _expect(not recovered and apply.get("evidence") is None,
                            f"{alabel}.apply: auto-sync adoption cannot claim execution/recovery evidence")
                elif recovered:
                    _expect(apply.get("evidence") is None,
                            f"{alabel}.apply.evidence: ambiguous recovery cannot invent execution evidence")
                else:
                    _validate_evidence_ref(
                        apply.get("evidence"), f"{alabel}.apply.evidence", evidence_root
                    )
                    _expect(apply["evidence"].get("command") == expected_apply_command,
                            f"{alabel}.apply.evidence.command: does not match frozen apply command")
                    _expect(apply["evidence"].get("media_type") == "application/json",
                            f"{alabel}.apply.evidence.media_type: deterministic JSON required")
                    if evidence_root is not None:
                        apply_record = _load_json(
                            evidence_root / apply["evidence"]["path"],
                            f"{alabel}.apply.evidence record",
                        )
                        _strict_keys(apply_record, {
                            "argv", "environment", "exit_code", "stderr", "stdout",
                            "stdout_sha256", "stderr_sha256", "stdout_truncated",
                            "stderr_truncated", "output_limit_bytes",
                            "background_processes_terminated", "timed_out",
                            "output_capture_policy",
                        }, f"{alabel}.apply.evidence record")
                        _require_keys(apply_record, {
                            "argv", "environment", "exit_code", "stderr", "stdout",
                            "stdout_sha256", "stderr_sha256", "stdout_truncated",
                            "stderr_truncated", "output_limit_bytes",
                            "background_processes_terminated", "timed_out",
                            "output_capture_policy",
                        }, f"{alabel}.apply.evidence record")
                        _expect(apply_record.get("argv") == _command_argv(
                            planned["apply_command"], f"{alabel}.planned.apply_command"
                        ), f"{alabel}.apply.evidence record.argv: frozen command mismatch")
                        expected_environment = dict(planned["execution_environment"])
                        expected_environment["MILESTONE_IDEMPOTENCY_KEY"] = apply[
                            "idempotency_key"
                        ]
                        _expect(
                            apply_record.get("environment") == expected_environment,
                            f"{alabel}.apply.evidence record.environment: frozen environment mismatch",
                        )
                        apply_exit = _integer(
                            apply_record.get("exit_code"),
                            f"{alabel}.apply.evidence record.exit_code",
                        )
                        _expect(isinstance(apply_record.get("timed_out"), bool),
                                f"{alabel}.apply.evidence record.timed_out: expected bool")
                        _expect(isinstance(apply_record.get("stdout"), str)
                                and isinstance(apply_record.get("stderr"), str),
                                f"{alabel}.apply.evidence record: stdout/stderr must be strings")
                        _expect(apply_record.get("output_capture_policy") == "omitted"
                                and apply_record["stdout"] == ""
                                and apply_record["stderr"] == "",
                                f"{alabel}.apply.evidence record: operation output must be omitted")
                        _sha256_value(apply_record.get("stdout_sha256"),
                                      f"{alabel}.apply.evidence record.stdout_sha256")
                        _sha256_value(apply_record.get("stderr_sha256"),
                                      f"{alabel}.apply.evidence record.stderr_sha256")
                        _expect(
                            apply_record["stdout_sha256"]
                            == _persisted_text_sha(apply_record["stdout"])
                            and apply_record["stderr_sha256"]
                            == _persisted_text_sha(apply_record["stderr"]),
                            f"{alabel}.apply.evidence record: persisted output hash mismatch",
                        )
                        _expect(apply_record.get("output_limit_bytes") == MAX_CAPTURE_BYTES,
                                f"{alabel}.apply.evidence record.output_limit_bytes: mismatch")
                        _expect(isinstance(apply_record.get("stdout_truncated"), bool)
                                and isinstance(apply_record.get("stderr_truncated"), bool),
                                f"{alabel}.apply.evidence record: truncation flags must be bool")
                        _expect(isinstance(
                            apply_record.get("background_processes_terminated"), bool
                        ), f"{alabel}.apply.evidence record: background flag must be bool")
                        if apply.get("status") == "applied":
                            _expect(apply_exit == 0 and apply_record["timed_out"] is False
                                    and apply_record["stdout_truncated"] is False
                                    and apply_record["stderr_truncated"] is False
                                    and apply_record[
                                        "background_processes_terminated"
                                    ] is False,
                                    f"{alabel}.apply.evidence record: successful apply requires exit 0")
                _validate_evidence_ref(
                    apply.get("observation_evidence"),
                    f"{alabel}.apply.observation_evidence", evidence_root,
                )
                _expect(
                    apply["observation_evidence"].get("command")
                    == expected_observation_command,
                    f"{alabel}.apply.observation_evidence.command: does not match frozen observation command",
                )
                apply_observation_exit = _validate_frozen_command_record(
                    apply["observation_evidence"],
                    _command_argv(
                        planned["observation_command"],
                        f"{alabel}.planned.observation_command",
                    ),
                    planned["execution_environment"],
                    f"{alabel}.apply.observation_evidence", evidence_root,
                    expected_output_policy=(
                        "projected-auto-sync-adoption" if is_auto
                        else "projected-observed-identity"
                    ),
                    expected_observed=(
                        apply.get("observed") if isinstance(apply.get("observed"), dict)
                        else None
                    ),
                    expected_target=planned if is_auto else None,
                )
                if apply.get("status") == "applied" and apply_observation_exit is not None:
                    _expect(apply_observation_exit == 0,
                            f"{alabel}.apply.observation_evidence: successful apply requires observation exit 0")
            if apply.get("status") == "applied":
                _expect(isinstance(apply.get("observed"), dict),
                        f"{alabel}.apply.observed: expected object")
                _expect(apply.get("failure_reason") is None,
                        f"{alabel}.apply.failure_reason: successful apply must be null")
                _desired_matches(planned["desired"], apply["observed"], f"{alabel}.apply.observed")
            elif apply.get("status") == "failed":
                _nonempty_string(apply.get("failure_reason"), f"{alabel}.apply.failure_reason")
                if apply.get("observed") is not None:
                    _expect(isinstance(apply.get("observed"), dict),
                            f"{alabel}.apply.observed: expected object or null")
                    _validate_observed(apply["observed"], f"{alabel}.apply.observed")
            verification = attempt.get("verification")
            _expect(isinstance(verification, dict), f"{alabel}.verification: expected object")
            _strict_keys(
                verification,
                {"status", "observed_at", "observed", "observation_evidence", "probes"},
                f"{alabel}.verification",
            )
            _require_keys(
                verification,
                {"status", "observed_at", "observed", "observation_evidence", "probes"},
                f"{alabel}.verification",
            )
            _expect(verification.get("status") in {"pending", "verified", "failed"}, f"{alabel}.verification.status: invalid")
            probes = verification.get("probes")
            _expect(isinstance(probes, list), f"{alabel}.verification.probes: expected array")
            verification_at: datetime | None = None
            if verification.get("status") in {"verified", "failed"}:
                _expect(apply.get("status") == "applied" and apply_at is not None,
                        f"{alabel}: verification cannot be terminal before a successful apply")
                verification_at = _iso(verification.get("observed_at"), f"{alabel}.verification.observed_at")
                _expect(apply_at <= verification_at <= now,
                        f"{alabel}.verification.observed_at: must follow apply and not be future")
                if is_latest and enforce_latest_freshness:
                    _expect((now - verification_at).total_seconds() <= pmeta["max_age"],
                            f"{alabel}.verification: latest evidence is stale under the plan freshness contract")
                _expect(isinstance(verification.get("observed"), dict), f"{alabel}.verification.observed: expected object")
                _validate_observed(verification["observed"], f"{alabel}.verification.observed")
                _validate_evidence_ref(
                    verification.get("observation_evidence"),
                    f"{alabel}.verification.observation_evidence",
                    evidence_root,
                )
                expected_observation_command = shlex.join(
                    _command_argv(
                        planned["observation_command"],
                        f"{alabel}.planned.observation_command",
                    )
                )
                _expect(
                    verification["observation_evidence"].get("command")
                    == expected_observation_command,
                    f"{alabel}.verification.observation_evidence.command: "
                    "must equal the frozen observation command",
                )
                verification_observation_exit = _validate_frozen_command_record(
                    verification["observation_evidence"],
                    _command_argv(
                        planned["observation_command"],
                        f"{alabel}.planned.observation_command",
                    ),
                    planned["execution_environment"],
                    f"{alabel}.verification.observation_evidence", evidence_root,
                    expected_output_policy="projected-observed-identity",
                    expected_observed=verification["observed"],
                )
                if verification.get("status") == "verified" and verification_observation_exit is not None:
                    _expect(verification_observation_exit == 0,
                            f"{alabel}.verification.observation_evidence: verified result requires exit 0")
            else:
                _expect(verification.get("observed_at") is None,
                        f"{alabel}.verification.observed_at: pending verification must be null")
                _expect(
                    verification.get("observed") is None
                    and verification.get("observation_evidence") is None
                    and not probes,
                        f"{alabel}.verification: pending verification cannot carry observations or probes")
            by_kind: dict[str, dict[str, Any]] = {}
            passing: set[str] = set()
            for k, probe in enumerate(probes):
                plabel = f"{alabel}.verification.probes[{k}]"
                _expect(isinstance(probe, dict), f"{plabel}: expected object")
                _strict_keys(probe, {"kind", "exit_code", "observed_at", "evidence"}, plabel)
                _require_keys(probe, {"kind", "exit_code", "observed_at", "evidence"}, plabel)
                kind = _nonempty_string(probe.get("kind"), f"{plabel}.kind")
                _expect(kind in contract_by_kind,
                        f"{plabel}.kind: not present in the frozen verification contract")
                _expect(kind not in by_kind, f"{alabel}.verification.probes: duplicate kind {kind!r}")
                exit_code = _integer(probe.get("exit_code"), f"{plabel}.exit_code")
                probe_at = _iso(probe.get("observed_at"), f"{plabel}.observed_at")
                if apply_at is not None:
                    _expect(apply_at <= probe_at <= now,
                            f"{plabel}: probe must follow apply and not be future")
                else:
                    _expect(probe_at <= now, f"{plabel}: future probe evidence")
                if verification_at is not None:
                    _expect(probe_at <= verification_at,
                            f"{plabel}: probe cannot follow the verification summary timestamp")
                if is_latest and enforce_latest_freshness:
                    _expect((now - probe_at).total_seconds() <= pmeta["max_age"],
                            f"{plabel}: stale latest probe evidence")
                _validate_evidence_ref(probe.get("evidence"), f"{plabel}.evidence", evidence_root)
                expected_probe_command = shlex.join(
                    _command_argv(
                        contract_by_kind[kind]["command"],
                        f"{plabel}.planned.command",
                    )
                )
                _expect(probe["evidence"].get("command") == expected_probe_command,
                        f"{plabel}.evidence.command: must equal the frozen probe command")
                recorded_probe_exit = _validate_frozen_command_record(
                    probe["evidence"],
                    _command_argv(
                        contract_by_kind[kind]["command"],
                        f"{plabel}.planned.command",
                    ),
                    planned["execution_environment"], f"{plabel}.evidence",
                    evidence_root, expected_output_policy="projected-verification-fact",
                    expected_probe_kind=kind, expected_target=planned,
                )
                if recorded_probe_exit is not None:
                    _expect(recorded_probe_exit == exit_code,
                            f"{plabel}.exit_code: differs from command record")
                by_kind[kind] = probe
                if exit_code == 0:
                    passing.add(kind)
            last_gap = sorted(set(contract_by_kind) - passing)
            if verification.get("status") == "verified":
                _desired_matches(planned["desired"], verification["observed"], f"{alabel}.verification.observed")
                _expect(not last_gap, f"{alabel}.verification.probes: missing/failed contract probes {last_gap}")
                last_identity_matches = True
            elif verification.get("status") == "failed":
                try:
                    _desired_matches(
                        planned["desired"], verification["observed"],
                        f"{alabel}.verification.observed",
                    )
                    last_identity_matches = True
                except ValidationError:
                    last_identity_matches = False
            attempt_hash = _value_sha(attempt)
            attempt_by_hash[attempt_hash] = attempt
            prefix_key = f"{tid}/{aid}"
            authorization_hashes[prefix_key] = _value_sha({
                key: attempt[key]
                for key in (
                    "attempt_id", "sequence", "previous_attempt_sha256", "recorded_at",
                    "authorization",
                )
            })
            if apply.get("status") != "pending":
                intent_hashes[prefix_key] = _value_sha({
                    "attempt_id": attempt["attempt_id"],
                    "sequence": attempt["sequence"],
                    "previous_attempt_sha256": attempt["previous_attempt_sha256"],
                    "recorded_at": attempt["recorded_at"],
                    "authorization": attempt["authorization"],
                    "apply_intent": {
                        key: apply[key]
                        for key in (
                            "at", "actor", "idempotency_key", "intent_evidence",
                        )
                    },
                })
            if apply.get("status") in {"applied", "failed"}:
                apply_hashes[prefix_key] = _value_sha({
                    key: attempt[key]
                    for key in (
                        "attempt_id", "sequence", "previous_attempt_sha256", "recorded_at",
                        "authorization", "apply",
                    )
                })
            if verification.get("status") != "pending":
                attempt_hashes[prefix_key] = attempt_hash
            previous_hash = attempt_hash
            previous_seq = seq
        _expect(len(refresh_intents) in {len(refreshes), len(refreshes) + 1},
                f"{tlabel}: refresh intents/results must be paired with at most one unresolved intent")
        previous_intent_hash: str | None = None
        intent_by_hash: dict[str, dict[str, Any]] = {}
        for j, refresh_intent in enumerate(refresh_intents):
            ilabel = f"{tlabel}.verification_refresh_intents[{j}]"
            _expect(isinstance(refresh_intent, dict), f"{ilabel}: expected object")
            intent_keys = {
                "refresh_id", "sequence", "previous_intent_sha256",
                "previous_refresh_sha256", "source_attempt_sha256", "recorded_at",
                "authorization",
            }
            _strict_keys(refresh_intent, intent_keys, ilabel)
            _require_keys(refresh_intent, intent_keys, ilabel)
            refresh_id = _nonempty_string(
                refresh_intent.get("refresh_id"), f"{ilabel}.refresh_id"
            )
            _expect(refresh_id not in seen_global,
                    f"{label}: duplicate refresh/attempt id {refresh_id!r}")
            seen_global.add(refresh_id)
            sequence = _integer(refresh_intent.get("sequence"), f"{ilabel}.sequence", 1)
            _expect(sequence == j + 1, f"{ilabel}.sequence: must be contiguous from 1")
            prior_intent = refresh_intent.get("previous_intent_sha256")
            if previous_intent_hash is None:
                _expect(prior_intent is None,
                        f"{ilabel}.previous_intent_sha256: first intent must be null")
            else:
                _expect(_sha256_value(
                    prior_intent, f"{ilabel}.previous_intent_sha256"
                ) == previous_intent_hash,
                        f"{ilabel}.previous_intent_sha256: append-only chain broken")
            expected_prior_refresh = _value_sha(refreshes[j - 1]) if j else None
            _expect(refresh_intent.get("previous_refresh_sha256") == expected_prior_refresh,
                    f"{ilabel}.previous_refresh_sha256: result chain mismatch")
            source_hash = _sha256_value(
                refresh_intent.get("source_attempt_sha256"),
                f"{ilabel}.source_attempt_sha256",
            )
            source_attempt = attempt_by_hash.get(source_hash)
            _expect(source_attempt is not None,
                    f"{ilabel}.source_attempt_sha256: source attempt is absent/changed")
            _expect(
                source_attempt["apply"]["status"] == "applied"
                and source_attempt["verification"]["status"] in {"verified", "failed"},
                f"{ilabel}.source_attempt_sha256: only terminal verification after apply may refresh",
            )
            recorded_at = _iso(refresh_intent.get("recorded_at"), f"{ilabel}.recorded_at")
            source_verified_at = _iso(
                source_attempt["verification"]["observed_at"],
                f"{ilabel}.source_attempt.verification.observed_at",
            )
            _expect(source_verified_at <= recorded_at <= now,
                    f"{ilabel}.recorded_at: must follow source verification and not be future")
            authorization = refresh_intent.get("authorization")
            _expect(isinstance(authorization, dict), f"{ilabel}.authorization: expected object")
            _strict_keys(
                authorization, {"decision", "by", "method", "at", "scope_hash"},
                f"{ilabel}.authorization",
            )
            _require_keys(
                authorization, {"decision", "by", "method", "at", "scope_hash"},
                f"{ilabel}.authorization",
            )
            _expect(authorization.get("decision") == "approved"
                    and authorization.get("method") == "human-explicit",
                    f"{ilabel}.authorization: explicit human approval required")
            _human_name(authorization.get("by"), f"{ilabel}.authorization.by")
            _expect(_iso(authorization.get("at"), f"{ilabel}.authorization.at") == recorded_at,
                    f"{ilabel}.authorization.at: must equal durable intent time")
            expected_scope = _verification_refresh_scope(
                plan, planned,
                {
                    "id": tid,
                    "verification_refresh_intents": refresh_intents[:j],
                    "verification_refreshes": refreshes[:j],
                },
                source_attempt,
            )
            _expect(authorization.get("scope_hash") == _value_sha(expected_scope),
                    f"{ilabel}.authorization.scope_hash: stale/replayed preview")
            intent_hash = _value_sha(refresh_intent)
            intent_by_hash[intent_hash] = refresh_intent
            authorization_hashes[f"{tid}/refresh/{refresh_id}"] = intent_hash
            intent_hashes[f"{tid}/refresh/{refresh_id}"] = intent_hash
            previous_intent_hash = intent_hash

        previous_refresh_hash: str | None = None
        previous_refresh_time: datetime | None = None
        previous_refresh_source_index = -1
        current_refresh: dict[str, Any] | None = None
        current_refresh_gap = last_gap
        current_refresh_identity = last_identity_matches
        latest_attempt_hash = _value_sha(attempts[-1]) if attempts else None
        for j, refresh in enumerate(refreshes):
            rlabel = f"{tlabel}.verification_refreshes[{j}]"
            _expect(isinstance(refresh, dict), f"{rlabel}: expected object")
            refresh_keys = {
                "refresh_id", "sequence", "previous_refresh_sha256",
                "source_attempt_sha256", "intent_sha256", "recorded_at", "status",
                "observed_at", "observed", "observation_evidence", "probes",
            }
            _strict_keys(refresh, refresh_keys, rlabel)
            _require_keys(refresh, refresh_keys, rlabel)
            refresh_id = _nonempty_string(refresh.get("refresh_id"), f"{rlabel}.refresh_id")
            refresh_seq = _integer(refresh.get("sequence"), f"{rlabel}.sequence", 1)
            _expect(refresh_seq == j + 1, f"{rlabel}.sequence: must be contiguous from 1")
            prior_refresh = refresh.get("previous_refresh_sha256")
            if previous_refresh_hash is None:
                _expect(prior_refresh is None,
                        f"{rlabel}.previous_refresh_sha256: first refresh must be null")
            else:
                _expect(_sha256_value(
                    prior_refresh, f"{rlabel}.previous_refresh_sha256"
                ) == previous_refresh_hash,
                        f"{rlabel}.previous_refresh_sha256: append-only chain broken")
            intent_hash = _sha256_value(refresh.get("intent_sha256"), f"{rlabel}.intent_sha256")
            refresh_intent = intent_by_hash.get(intent_hash)
            _expect(refresh_intent is not None and refresh_intent is refresh_intents[j],
                    f"{rlabel}.intent_sha256: result does not bind its ordered authorization")
            source_hash = _sha256_value(
                refresh.get("source_attempt_sha256"), f"{rlabel}.source_attempt_sha256"
            )
            _expect(refresh_id == refresh_intent["refresh_id"]
                    and refresh_seq == refresh_intent["sequence"]
                    and source_hash == refresh_intent["source_attempt_sha256"],
                    f"{rlabel}: result identity differs from durable authorization")
            source_attempt = attempt_by_hash.get(source_hash)
            _expect(source_attempt is not None,
                    f"{rlabel}.source_attempt_sha256: source attempt is absent/changed")
            source_index = attempts.index(source_attempt)
            refresh_recorded = _iso(refresh.get("recorded_at"), f"{rlabel}.recorded_at")
            _expect(_iso(
                refresh_intent["recorded_at"], f"{rlabel}.intent.recorded_at"
            ) <= refresh_recorded <= now,
                    f"{rlabel}.recorded_at: must follow authorization and not be future")
            if previous_refresh_time is not None:
                _expect(refresh_recorded >= previous_refresh_time,
                        f"{rlabel}.recorded_at: refresh history must be chronological")
            _expect(source_index >= previous_refresh_source_index,
                    f"{rlabel}.source_attempt_sha256: cannot return to an older apply attempt")
            status_value = refresh.get("status")
            _expect(status_value in {"verified", "failed", "ambiguous"},
                    f"{rlabel}.status: invalid")
            if status_value == "ambiguous":
                _expect(refresh.get("observed_at") is None
                        and refresh.get("observed") is None
                        and refresh.get("observation_evidence") is None
                        and refresh.get("probes") == [],
                        f"{rlabel}: ambiguous recovery cannot invent verification evidence")
                refresh_gap = sorted(contract_by_kind)
                refresh_identity = False
                observed_at = None
            else:
                observed_at = _iso(refresh.get("observed_at"), f"{rlabel}.observed_at")
                _expect(refresh_recorded == observed_at,
                        f"{rlabel}.observed_at: must equal deterministic completion time")
                _expect(isinstance(refresh.get("observed"), dict),
                        f"{rlabel}.observed: expected object")
                _validate_observed(refresh["observed"], f"{rlabel}.observed")
                _validate_evidence_ref(
                    refresh.get("observation_evidence"),
                    f"{rlabel}.observation_evidence", evidence_root,
                )
                expected_observation_command = shlex.join(_command_argv(
                    planned["observation_command"], f"{rlabel}.planned.observation_command"
                ))
                _expect(refresh["observation_evidence"].get("command")
                        == expected_observation_command,
                        f"{rlabel}.observation_evidence.command: frozen command mismatch")
                refresh_observation_exit = _validate_frozen_command_record(
                    refresh["observation_evidence"],
                    _command_argv(
                        planned["observation_command"],
                        f"{rlabel}.planned.observation_command",
                    ),
                    planned["execution_environment"],
                    f"{rlabel}.observation_evidence", evidence_root,
                    expected_output_policy="projected-observed-identity",
                    expected_observed=refresh["observed"],
                )
                if status_value == "verified" and refresh_observation_exit is not None:
                    _expect(refresh_observation_exit == 0,
                            f"{rlabel}.observation_evidence: verified refresh requires exit 0")
                refresh_probes = refresh.get("probes")
                _expect(isinstance(refresh_probes, list), f"{rlabel}.probes: expected array")
                passing: set[str] = set()
                seen_kinds: set[str] = set()
                for k, probe in enumerate(refresh_probes):
                    plabel = f"{rlabel}.probes[{k}]"
                    _expect(isinstance(probe, dict), f"{plabel}: expected object")
                    _strict_keys(probe, {"kind", "exit_code", "observed_at", "evidence"}, plabel)
                    _require_keys(probe, {"kind", "exit_code", "observed_at", "evidence"}, plabel)
                    kind = _nonempty_string(probe.get("kind"), f"{plabel}.kind")
                    _expect(kind in contract_by_kind and kind not in seen_kinds,
                            f"{plabel}.kind: absent from contract or duplicated")
                    seen_kinds.add(kind)
                    exit_code = _integer(probe.get("exit_code"), f"{plabel}.exit_code")
                    _expect(_iso(probe.get("observed_at"), f"{plabel}.observed_at") == observed_at,
                            f"{plabel}.observed_at: must equal refresh observation time")
                    _validate_evidence_ref(
                        probe.get("evidence"), f"{plabel}.evidence", evidence_root
                    )
                    expected_probe_command = shlex.join(_command_argv(
                        contract_by_kind[kind]["command"], f"{plabel}.planned.command"
                    ))
                    _expect(probe["evidence"].get("command") == expected_probe_command,
                            f"{plabel}.evidence.command: frozen command mismatch")
                    recorded_probe_exit = _validate_frozen_command_record(
                        probe["evidence"],
                        _command_argv(
                            contract_by_kind[kind]["command"],
                            f"{plabel}.planned.command",
                        ),
                        planned["execution_environment"], f"{plabel}.evidence",
                        evidence_root,
                        expected_output_policy="projected-verification-fact",
                        expected_probe_kind=kind, expected_target=planned,
                    )
                    if recorded_probe_exit is not None:
                        _expect(recorded_probe_exit == exit_code,
                                f"{plabel}.exit_code: differs from command record")
                    if exit_code == 0:
                        passing.add(kind)
                _expect(seen_kinds == set(contract_by_kind),
                        f"{rlabel}.probes: must record every frozen probe exactly once")
                refresh_gap = sorted(set(contract_by_kind) - passing)
                try:
                    _desired_matches(
                        planned["desired"], refresh["observed"], f"{rlabel}.observed"
                    )
                    refresh_identity = True
                except ValidationError:
                    refresh_identity = False
                if status_value == "verified":
                    _expect(refresh_identity and not refresh_gap,
                            f"{rlabel}: verified refresh requires identity and all probes")
                else:
                    _expect(not refresh_identity or bool(refresh_gap),
                            f"{rlabel}: failed refresh has no observed failure")
            refresh_hash = _value_sha(refresh)
            verification_refresh_hashes[f"{tid}/{refresh_id}"] = refresh_hash
            previous_refresh_hash = refresh_hash
            previous_refresh_time = refresh_recorded
            previous_refresh_source_index = source_index
            if source_hash == latest_attempt_hash:
                if status_value == "ambiguous":
                    current_refresh = None
                    current_refresh_gap = sorted(contract_by_kind)
                    current_refresh_identity = False
                else:
                    current_refresh = refresh
                    current_refresh_gap = refresh_gap
                    current_refresh_identity = refresh_identity
                    if j == len(refreshes) - 1 and enforce_latest_freshness:
                        _expect(
                            observed_at is not None
                            and (now - observed_at).total_seconds() <= pmeta["max_age"],
                            f"{rlabel}: latest verification refresh is stale",
                        )
        status = _derive_attempt_status(attempts[-1]) if attempts else "pending"
        unresolved_refresh = len(refresh_intents) > len(refreshes)
        latest_ambiguous = bool(refreshes and refreshes[-1].get("status") == "ambiguous")
        if unresolved_refresh or latest_ambiguous:
            status = "applied"
            last_gap = sorted(contract_by_kind)
            last_identity_matches = False
        elif current_refresh is not None:
            status = current_refresh["status"]
            last_gap = current_refresh_gap
            last_identity_matches = current_refresh_identity
        _expect(target.get("status") == status,
                f"{tlabel}.status: stored {target.get('status')!r}, deterministic value is {status!r}")
        statuses[tid] = status
        apply_statuses[tid] = attempts[-1]["apply"]["status"] if attempts else "pending"
        verification_gaps[tid] = last_gap
        identity_matches[tid] = last_identity_matches
    return {
        "attempt_hashes": attempt_hashes,
        "verification_refresh_hashes": verification_refresh_hashes,
        "intent_hashes": intent_hashes,
        "apply_hashes": apply_hashes,
        "authorization_hashes": authorization_hashes,
        "statuses": statuses,
        "apply_statuses": apply_statuses,
        "verification_gaps": verification_gaps,
        "identity_matches": identity_matches,
        "plan_hash": pmeta["plan_hash"],
        "immutable_root_hash": _value_sha({
            "schema_version": data["schema_version"],
            "milestone_id": data["milestone_id"],
            "generation": data["generation"],
            "created_at": data["created_at"],
            "producer": data["producer"],
            "plan_hash": data["plan_hash"],
            "target_ids": [target["id"] for target in data["targets"]],
        }),
    }


def validate_waivers(
    data: dict[str, Any], state: dict[str, Any], plan: dict[str, Any], now: datetime,
    *, verify_executables: bool = True,
) -> dict[str, Any]:
    label = "waivers"
    _artifact_envelope(data, label, state["id"], now)
    _validate_deterministic_producer(data.get("producer"), state, f"{label}.producer")
    _strict_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer", "plan_hash", "waivers",
    }, label)
    _require_keys(data, {
        "schema_version", "milestone_id", "generation", "created_at", "producer", "plan_hash", "waivers",
    }, label)
    pmeta = validate_operations_plan(
        plan, state, now, verify_executables=verify_executables
    )
    _expect(data.get("generation") == plan.get("generation"),
            f"{label}.generation: must equal operations_plan.generation")
    _expect(_sha256_value(data.get("plan_hash"), f"{label}.plan_hash") == pmeta["plan_hash"],
            f"{label}.plan_hash: waivers belong to a stale/different plan")
    plan_targets = {t["id"]: t for t in plan["targets"]}
    values = data.get("waivers")
    _expect(isinstance(values, list), f"{label}.waivers: expected array")
    hashes: dict[str, str] = {}
    active: dict[str, dict[str, Any]] = {}
    for i, waiver in enumerate(values):
        wlabel = f"{label}.waivers[{i}]"
        _expect(isinstance(waiver, dict), f"{wlabel}: expected object")
        _strict_keys(waiver, {
            "waiver_id", "target_id", "scope_hash", "missing_contract", "decision", "approved_by",
            "approval_method", "approved_at", "reason", "created_at", "expires_at",
            "compensating_control", "follow_up_milestone",
        }, wlabel)
        _require_keys(waiver, {
            "waiver_id", "target_id", "scope_hash", "missing_contract", "decision", "approved_by",
            "approval_method", "approved_at", "reason", "created_at", "expires_at",
            "compensating_control", "follow_up_milestone",
        }, wlabel)
        wid = _nonempty_string(waiver.get("waiver_id"), f"{wlabel}.waiver_id")
        _expect(wid not in hashes, f"{label}.waivers: duplicate waiver_id {wid!r}")
        tid = _nonempty_string(waiver.get("target_id"), f"{wlabel}.target_id")
        _expect(tid in plan_targets, f"{wlabel}.target_id: not in operations plan")
        _expect(_sha256_value(waiver.get("scope_hash"), f"{wlabel}.scope_hash") == pmeta["scopes"][tid],
                f"{wlabel}.scope_hash: cross-target/plan waiver replay")
        missing_raw = waiver.get("missing_contract")
        _expect(isinstance(missing_raw, list) and missing_raw,
                f"{wlabel}.missing_contract: expected non-empty array")
        missing = [_nonempty_string(v, f"{wlabel}.missing_contract[]") for v in missing_raw]
        _expect(len(missing) == len(set(missing)), f"{wlabel}.missing_contract: duplicate probe")
        contract_kinds = _contract_kinds(plan_targets[tid])
        _expect(set(missing) <= contract_kinds,
                f"{wlabel}.missing_contract: contains probes outside target contract")
        _expect(set(missing) < contract_kinds,
                f"{wlabel}.missing_contract: a waiver cannot replace the entire verification contract")
        _expect(waiver.get("decision") == "approved", f"{wlabel}.decision: expected approved")
        _human_name(str(waiver.get("approved_by") or ""), f"{wlabel}.approved_by")
        _expect(waiver.get("approval_method") == "human-explicit",
                f"{wlabel}.approval_method: expected human-explicit")
        approved = _iso(waiver.get("approved_at"), f"{wlabel}.approved_at")
        created = _iso(waiver.get("created_at"), f"{wlabel}.created_at")
        expires = _iso(waiver.get("expires_at"), f"{wlabel}.expires_at")
        _expect(approved >= created, f"{wlabel}: approved_at precedes created_at")
        _expect(expires > approved, f"{wlabel}: expires_at must follow approval")
        _expect(expires <= approved + timedelta(days=30),
                f"{wlabel}.expires_at: waiver lifetime cannot exceed 30 days")
        _expect(created <= approved <= now, f"{wlabel}: creation/approval cannot be future-dated")
        for field in ("reason", "compensating_control", "follow_up_milestone"):
            _nonempty_string(waiver.get(field), f"{wlabel}.{field}")
        _expect(waiver.get("follow_up_milestone") != state["id"],
                f"{wlabel}.follow_up_milestone: must name a different milestone")
        hashes[wid] = _value_sha(waiver)
        if expires > now:
            _expect(tid not in active,
                    f"{label}.waivers: multiple simultaneously active waivers for target {tid!r}")
            active[tid] = {
                "waiver_id": wid,
                "missing_contract": sorted(missing),
                "expires_at": waiver["expires_at"],
            }
    return {
        "waiver_hashes": hashes,
        "waiver_order": [waiver["waiver_id"] for waiver in values],
        "immutable_root_hash": _value_sha({
            "schema_version": data["schema_version"],
            "milestone_id": data["milestone_id"],
            "generation": data["generation"],
            "created_at": data["created_at"],
            "producer": data["producer"],
            "plan_hash": data["plan_hash"],
        }),
        "active_waivers": active,
        "waived_targets": sorted(active),
    }


def _required_bindings_for_state(state: dict[str, Any]) -> set[str]:
    phase = state["phase"]
    required: set[str] = set()
    if phase in {
        "critique-complete", "rectify-running", "code-complete", "publish-running",
        "published", "plan-review-running", "plan-reviewed", "apply-running", "applied", "verify-running",
        "operationally-verified", "complete",
    }:
        required.add("review_manifest")
    if phase in {
        "code-complete", "publish-running", "published", "plan-review-running", "plan-reviewed", "apply-running", "applied",
        "verify-running", "operationally-verified", "complete",
    }:
        required.add("implementation_evidence")
    if state["publication_required"] and phase in {
        "published", "plan-review-running", "plan-reviewed", "apply-running", "applied", "verify-running",
        "operationally-verified", "complete",
    }:
        required.add("publication_intent")
        required.add("release_manifest")
    if state["operations_required"] and phase in {
        "plan-reviewed", "apply-running", "applied", "verify-running", "operationally-verified", "complete",
    }:
        required.add("operations_plan")
    if state["operations_required"] and phase in {
        "applied", "verify-running", "operationally-verified", "complete",
    }:
        required.add("operations_evidence")
    if state["operations_required"] and phase in {"operationally-verified", "complete"}:
        required.add("waivers")
    return required


def _kit_writer_sha256(commit: str, label: str) -> str:
    source = subprocess.run(
        [
            "git", "-C", str(AGENT_KIT_ROOT), "show",
            f"{commit}:data/scripts/milestone-pipeline-artifacts.py",
        ],
        capture_output=True,
    )
    _expect(source.returncode == 0,
            f"{label}: milestone writer is absent from kit commit {commit}")
    return hashlib.sha256(source.stdout).hexdigest()


def _validate_binding_receipt(kind: str, binding: Any, state: dict[str, Any]) -> None:
    label = f"state.artifact_bindings.{kind}"
    _expect(isinstance(binding, dict), f"{label}: expected object")
    common = {"path", "sha256", "generation", "phase"}
    extras = {
        "review_manifest": {
            "review_hashes", "closure_hash", "closure_receipt_hash",
            "closure_attempt_hashes", "operations_review_hash",
            "operations_review_receipt_hash", "operations_review_attempt_hashes",
            "immutable_root_hash",
        },
        "implementation_evidence": set(),
        "publication_intent": {
            "scope_hash", "authorization_hash", "supersession_hashes",
            "execution_hashes",
        },
        "release_manifest": set(),
        "operations_plan": {"plan_hash"},
        "operations_evidence": {
            "authorization_hashes", "intent_hashes", "apply_hashes", "attempt_hashes",
            "verification_refresh_hashes", "plan_hash"
            , "immutable_root_hash"
        },
        "waivers": {"waiver_hashes", "waiver_order", "plan_hash", "immutable_root_hash"},
    }[kind]
    _strict_keys(binding, common | extras, label)
    _require_keys(binding, common, label)
    _expect(binding.get("path") == f"artifacts/{POINTERS[kind]}",
            f"{label}.path: unexpected artifact path")
    _sha256_value(binding.get("sha256"), f"{label}.sha256")
    _integer(binding.get("generation"), f"{label}.generation", 1)
    bound_phase = _nonempty_string(binding.get("phase"), f"{label}.phase")
    history_phases = {entry.get("phase") for entry in state["phase_history"]}
    _expect(bound_phase in history_phases,
            f"{label}.phase: binding phase is absent from phase_history")
    for map_key in (
        "review_hashes", "authorization_hashes", "intent_hashes", "apply_hashes",
        "attempt_hashes", "verification_refresh_hashes", "waiver_hashes",
    ):
        if map_key in binding:
            value = binding[map_key]
            _expect(isinstance(value, dict), f"{label}.{map_key}: expected object")
            for key, digest in value.items():
                _nonempty_string(key, f"{label}.{map_key} key")
                _sha256_value(digest, f"{label}.{map_key}.{key}")
    for hash_key in (
        "closure_hash", "closure_receipt_hash", "operations_review_hash",
        "operations_review_receipt_hash", "plan_hash", "immutable_root_hash",
        "scope_hash", "authorization_hash",
    ):
        if hash_key in binding:
            _sha256_value(binding[hash_key], f"{label}.{hash_key}")
    for list_key in ("closure_attempt_hashes", "operations_review_attempt_hashes"):
        if list_key in binding:
            value = binding[list_key]
            _expect(isinstance(value, list), f"{label}.{list_key}: expected array")
            for i, digest in enumerate(value):
                _sha256_value(digest, f"{label}.{list_key}[{i}]")
    if kind == "publication_intent":
        for list_key in ("supersession_hashes", "execution_hashes"):
            hashes = binding.get(list_key)
            _expect(isinstance(hashes, list), f"{label}.{list_key}: required array")
            for i, digest in enumerate(hashes):
                _sha256_value(digest, f"{label}.{list_key}[{i}]")
    if kind == "review_manifest":
        _expect(bool(binding.get("review_hashes")), f"{label}.review_hashes: required")
        _expect(binding.get("immutable_root_hash") is not None,
                f"{label}.immutable_root_hash: required")
        _expect(isinstance(binding.get("closure_attempt_hashes"), list),
                f"{label}.closure_attempt_hashes: required")
        _expect(isinstance(binding.get("operations_review_attempt_hashes"), list),
                f"{label}.operations_review_attempt_hashes: required")
        if state["phase"] in {
            "code-complete", "publish-running", "published", "plan-review-running", "plan-reviewed", "apply-running", "applied",
            "verify-running", "operationally-verified", "complete",
        }:
            _expect(binding.get("closure_hash") is not None,
                    f"{label}.closure_hash: required after code closure")
            _expect(binding.get("closure_receipt_hash") is not None,
                    f"{label}.closure_receipt_hash: required after code closure")
        if state["phase"] in {
            "plan-reviewed", "apply-running", "applied", "verify-running",
            "operationally-verified", "complete",
        } and state["operations_required"]:
            _expect(binding.get("operations_review_hash") is not None,
                    f"{label}.operations_review_hash: required before operations")
            _expect(binding.get("operations_review_receipt_hash") is not None,
                    f"{label}.operations_review_receipt_hash: required before operations")
    elif kind == "publication_intent":
        _expect(binding.get("scope_hash") is not None,
                f"{label}.scope_hash: required")
        _expect(binding.get("authorization_hash") is not None,
                f"{label}.authorization_hash: required")
        _expect(isinstance(binding.get("execution_hashes"), list),
                f"{label}.execution_hashes: required")
    elif kind == "operations_plan":
        _expect(binding.get("plan_hash") is not None, f"{label}.plan_hash: required")
    elif kind == "operations_evidence":
        _expect(binding.get("immutable_root_hash") is not None,
                f"{label}.immutable_root_hash: required")
        _expect(bool(binding.get("authorization_hashes")),
                f"{label}.authorization_hashes: required")
        if state["phase"] in {"applied", "verify-running", "operationally-verified", "complete"}:
            _expect(bool(binding.get("apply_hashes")), f"{label}.apply_hashes: required")
        if state["phase"] in {"operationally-verified", "complete"}:
            _expect(bool(binding.get("attempt_hashes")), f"{label}.attempt_hashes: required")
    elif kind == "waivers":
        _expect(isinstance(binding.get("waiver_hashes"), dict),
                f"{label}.waiver_hashes: required")
        _expect(isinstance(binding.get("waiver_order"), list),
                f"{label}.waiver_order: required")
        _expect(binding.get("immutable_root_hash") is not None,
                f"{label}.immutable_root_hash: required")


def _validate_state(
    state: dict[str, Any], state_path: Path, *, allow_kit_upgrade: bool = False,
) -> None:
    _strict_keys(state, STATE_ALLOWED_FIELDS, "state")
    _expect(state.get("schema_version") == STATE_SCHEMA_VERSION,
            "state.json is not schema v2; run milestone-pipeline-migrate.py explicitly")
    kit_commit = _commit(state.get("agent_kit_commit"), "state.agent_kit_commit")
    resolved_kit = _commit(
        _git_output(AGENT_KIT_ROOT, "rev-parse", "--verify", f"{kit_commit}^{{commit}}")
        .decode().strip(),
        "state.agent_kit_commit.resolved",
    )
    _expect(kit_commit == resolved_kit,
            "state.agent_kit_commit: must be the full immutable commit")
    head = _commit(
        _git_output(AGENT_KIT_ROOT, "rev-parse", "HEAD").decode().strip(),
        "canonical kit HEAD",
    )
    if allow_kit_upgrade:
        reachable = subprocess.run(
            ["git", "-C", str(AGENT_KIT_ROOT), "merge-base", "--is-ancestor",
             kit_commit, head],
            capture_output=True,
        )
        _expect(
            reachable.returncode == 0,
            "state.agent_kit_commit: upgrade target is not a descendant of frozen kit",
        )
    else:
        _expect(
            head == kit_commit,
            "state.agent_kit_commit: executing kit revision differs from the frozen state; "
            "run kit-upgrade with explicit human approval or use the frozen checkout",
        )
    kit_status = subprocess.run(
        [
            "git", "-C", str(AGENT_KIT_ROOT), "status", "--porcelain",
            "--untracked-files=all", "--", *PIPELINE_KIT_PATHS,
        ],
        capture_output=True, text=True,
    )
    _expect(
        kit_status.returncode == 0 and not kit_status.stdout.strip(),
        "state.agent_kit_commit: canonical pipeline kit has uncommitted tracked/untracked "
        "changes and cannot supply reproducible writer semantics",
    )
    state_dir = _state_dir(state_path)

    def validate_check_ref(path: Any, digest: Any, label: str) -> tuple[str, str]:
        rel = _nonempty_string(path, f"{label}.path")
        _expect(rel.startswith("artifacts/checks/"),
                f"{label}.path: check evidence must be below artifacts/checks")
        normalized_digest = _sha256_value(digest, f"{label}.sha256")
        candidate = (state_dir / rel).resolve()
        try:
            candidate.relative_to((state_dir / "artifacts" / "checks").resolve())
        except ValueError:
            _fail(f"{label}.path: check evidence escapes artifacts/checks")
        _expect(candidate.is_file(), f"{label}.path: check evidence is missing: {candidate}")
        _expect(_file_sha(candidate) == normalized_digest,
                f"{label}.sha256: check evidence changed")
        return rel, normalized_digest

    check_run_hashes = state.get("check_run_hashes")
    _expect(isinstance(check_run_hashes, dict), "state.check_run_hashes: expected object")
    for path, digest in check_run_hashes.items():
        validate_check_ref(path, digest, f"state.check_run_hashes.{path}")
    attempts = state.get("check_run_attempts")
    _expect(isinstance(attempts, list), "state.check_run_attempts: expected array")
    attempt_paths: list[str] = []
    attempt_map: dict[str, str] = {}
    for i, ref in enumerate(attempts):
        label = f"state.check_run_attempts[{i}]"
        _expect(isinstance(ref, dict) and set(ref) == {"path", "sha256"},
                f"{label}: expected exactly path and sha256")
        rel, digest = validate_check_ref(ref.get("path"), ref.get("sha256"), label)
        attempt_paths.append(rel)
        attempt_map[rel] = digest
    _expect(len(attempt_paths) == len(set(attempt_paths)),
            "state.check_run_attempts: duplicate evidence path")
    for path, digest in check_run_hashes.items():
        _expect(attempt_map.get(path) == digest,
                f"state.check_run_hashes.{path}: active success is absent from all-attempt ledger")
    check_run_head = state.get("check_run_head")
    if check_run_head is not None:
        _commit(check_run_head, "state.check_run_head")
    check_run_history = state.get("check_run_history")
    _expect(isinstance(check_run_history, dict), "state.check_run_history: expected object")
    for historic_head, historic_runs in check_run_history.items():
        _commit(historic_head, "state.check_run_history key")
        _expect(isinstance(historic_runs, dict),
                f"state.check_run_history.{historic_head}: expected object")
        for path, digest in historic_runs.items():
            rel, normalized_digest = validate_check_ref(
                path, digest, f"state.check_run_history.{historic_head}.{path}"
            )
            _expect(attempt_map.get(rel) == normalized_digest,
                    f"state.check_run_history.{historic_head}.{rel}: archived success is "
                    "absent from all-attempt ledger")
    sid = _nonempty_string(state.get("id"), "state.id")
    _expect(bool(MILESTONE_ID_RE.fullmatch(sid)), "state.id: invalid milestone id")
    _expect(state_path.resolve().parent.name == sid,
            "state.id: does not match the milestone state directory (cross-run replay)")
    created_at = _iso(state.get("created_at"), "state.created_at")
    updated_at = _iso(state.get("updated_at"), "state.updated_at")
    _expect(created_at <= updated_at, "state.updated_at: precedes state.created_at")
    kit_upgrades = state.get("kit_upgrade_history")
    _expect(isinstance(kit_upgrades, list), "state.kit_upgrade_history: expected array")
    previous_kit: str | None = None
    previous_upgrade_at: datetime | None = None
    for i, upgrade in enumerate(kit_upgrades):
        label = f"state.kit_upgrade_history[{i}]"
        _expect(isinstance(upgrade, dict), f"{label}: expected object")
        keys = {
            "from_commit", "to_commit", "from_writer_sha256", "to_writer_sha256",
            "approved_by", "approval_method", "at",
        }
        _strict_keys(upgrade, keys, label)
        _require_keys(upgrade, keys, label)
        from_commit = _commit(upgrade.get("from_commit"), f"{label}.from_commit")
        to_commit = _commit(upgrade.get("to_commit"), f"{label}.to_commit")
        _expect(from_commit != to_commit, f"{label}: no-op upgrade forbidden")
        for field, commit in (("from_commit", from_commit), ("to_commit", to_commit)):
            resolved = _commit(
                _git_output(
                    AGENT_KIT_ROOT, "rev-parse", "--verify", f"{commit}^{{commit}}"
                ).decode().strip(),
                f"{label}.{field}.resolved",
            )
            _expect(resolved == commit,
                    f"{label}.{field}: must be the full immutable commit")
        if previous_kit is not None:
            _expect(from_commit == previous_kit,
                    f"{label}.from_commit: kit upgrade chain is broken")
        ancestor = subprocess.run(
            [
                "git", "-C", str(AGENT_KIT_ROOT), "merge-base", "--is-ancestor",
                from_commit, to_commit,
            ],
            capture_output=True,
        )
        _expect(ancestor.returncode == 0, f"{label}: upgrade must be fast-forward ancestry")
        _expect(_sha256_value(
            upgrade.get("from_writer_sha256"), f"{label}.from_writer_sha256"
        ) == _kit_writer_sha256(from_commit, f"{label}.from_writer_sha256"),
                f"{label}.from_writer_sha256: does not match frozen kit")
        _expect(_sha256_value(
            upgrade.get("to_writer_sha256"), f"{label}.to_writer_sha256"
        ) == _kit_writer_sha256(to_commit, f"{label}.to_writer_sha256"),
                f"{label}.to_writer_sha256: does not match upgraded kit")
        _human_name(upgrade.get("approved_by"), f"{label}.approved_by")
        _expect(upgrade.get("approval_method") == "human-explicit",
                f"{label}.approval_method: explicit human approval required")
        upgrade_at = _iso(upgrade.get("at"), f"{label}.at")
        _expect(created_at <= upgrade_at <= updated_at,
                f"{label}.at: outside state lifetime")
        if previous_upgrade_at is not None:
            _expect(upgrade_at >= previous_upgrade_at,
                    f"{label}.at: upgrade times must be monotonic")
        previous_kit = to_commit
        previous_upgrade_at = upgrade_at
    if kit_upgrades:
        _expect(kit_upgrades[-1]["to_commit"] == kit_commit,
                "state.agent_kit_commit: differs from kit upgrade chain head")
    history = state.get("phase_history")
    _expect(isinstance(history, list) and history, "state.phase_history: expected non-empty array")
    _expect(state.get("phase") in STATE_PHASES, f"state.phase: unknown phase {state.get('phase')!r}")
    _expect(history[-1].get("phase") == state.get("phase"),
            "state.phase_history: last entry must equal state.phase")
    previous_time: datetime | None = None
    previous_phase: str | None = None
    for i, entry in enumerate(history):
        _expect(isinstance(entry, dict) and set(entry) == {"phase", "at"},
                f"state.phase_history[{i}]: expected exactly phase and at")
        _expect(entry.get("phase") in STATE_PHASES,
                f"state.phase_history[{i}].phase: unknown phase {entry.get('phase')!r}")
        current = _iso(entry.get("at"), f"state.phase_history[{i}].at")
        _expect(created_at <= current <= updated_at,
                f"state.phase_history[{i}].at: must fall within state creation/update bounds")
        if previous_time is not None:
            _expect(current >= previous_time, "state.phase_history: timestamps must be monotonic")
        if previous_phase is not None:
            _expect(entry["phase"] in PHASE_EDGES[previous_phase],
                    f"state.phase_history[{i}]: illegal edge {previous_phase!r} -> {entry['phase']!r}")
        previous_time = current
        previous_phase = entry["phase"]
    migration = state.get("migration")
    if migration is None:
        _expect(history[0].get("phase") == "init",
                "state.phase_history: native v2 histories must start at init")
    else:
        _expect(isinstance(migration, dict), "state.migration: expected object or null")
        _strict_keys(migration, {
            "source_schema_version", "source_phase", "source_sha256", "migrated_at",
            "terminal_claim_downgraded", "legacy_external_writes",
        }, "state.migration")
        _require_keys(migration, {
            "source_schema_version", "source_phase", "source_sha256", "migrated_at",
            "terminal_claim_downgraded", "legacy_external_writes",
        }, "state.migration")
        _expect(migration.get("source_schema_version") == 1,
                "state.migration.source_schema_version: expected 1")
        source_phase = migration.get("source_phase")
        _expect(source_phase in MIGRATION_SOURCE_PHASES,
                "state.migration.source_phase: invalid v1 phase")
        _sha256_value(migration.get("source_sha256"), "state.migration.source_sha256")
        migrated_at = _iso(migration.get("migrated_at"), "state.migration.migrated_at")
        _expect(created_at <= migrated_at <= updated_at,
                "state.migration.migrated_at: must fall within state creation/update bounds")
        downgraded = migration.get("terminal_claim_downgraded")
        _expect(isinstance(downgraded, bool),
                "state.migration.terminal_claim_downgraded: expected bool")
        _expect(downgraded is (source_phase == "complete"),
                "state.migration.terminal_claim_downgraded: inconsistent with source phase")
        legacy = migration.get("legacy_external_writes")
        _expect(isinstance(legacy, dict),
                "state.migration.legacy_external_writes: expected object")
        _strict_keys(legacy, {"required", "completed", "authorized"},
                     "state.migration.legacy_external_writes")
        _require_keys(legacy, {"required", "completed", "authorized"},
                      "state.migration.legacy_external_writes")
        for field in ("required", "completed"):
            values = legacy.get(field)
            _expect(isinstance(values, list),
                    f"state.migration.legacy_external_writes.{field}: expected array")
            _expect(all(isinstance(value, str) for value in values),
                    f"state.migration.legacy_external_writes.{field}: items must be strings")
        _expect(isinstance(legacy.get("authorized"), bool),
                "state.migration.legacy_external_writes.authorized: expected bool")
        expected_initial = MIGRATION_PHASE_MAP.get(source_phase, source_phase)
        _expect(history[0].get("phase") == expected_initial,
                "state.phase_history: migrated history must start at its mapped source phase")
        backup_path = state_path.with_name("state.v1.json")
        _expect(backup_path.is_file() and not backup_path.is_symlink(),
                "state.migration: canonical state.v1.json backup is missing or unsafe")
        _expect(_file_sha(backup_path) == migration["source_sha256"],
                "state.migration.source_sha256: state.v1.json content mismatch")
        legacy_state = _load_json(backup_path, "state.v1.json")
        _expect(legacy_state.get("schema_version") in {None, 1},
                "state.v1.json: expected legacy schema 1/unversioned")
        _expect(legacy_state.get("id") == sid,
                "state.v1.json.id: does not match migrated milestone")
        _expect(legacy_state.get("phase") == source_phase,
                "state.v1.json.phase: does not match migration source_phase")
    for key, filename in POINTERS.items():
        _expect(state.get(key) == f"artifacts/{filename}",
                f"state.{key}: v2 uses the canonical pointer artifacts/{filename}")
    bindings = state.get("artifact_bindings")
    _expect(isinstance(bindings, dict), "state.artifact_bindings: expected object")
    unknown_bindings = sorted(set(bindings) - set(POINTERS))
    _expect(not unknown_bindings,
            f"state.artifact_bindings: unknown artifact(s): {unknown_bindings}")
    required_bindings = _required_bindings_for_state(state)
    missing_bindings = sorted(required_bindings - set(bindings))
    _expect(not missing_bindings,
            f"state.artifact_bindings: phase {state['phase']!r} is missing {missing_bindings}")
    for mutable_kind in ("publication_intent", "operations_evidence", "waivers"):
        mutable_path = state_dir / "artifacts" / POINTERS[mutable_kind]
        _expect(not mutable_path.exists() or mutable_kind in bindings,
                f"state.artifact_bindings.{mutable_kind}: required once the mutable artifact exists")
    for kind, binding in bindings.items():
        _validate_binding_receipt(kind, binding, state)
    for flag in ("publication_required", "operations_required"):
        _expect(isinstance(state.get(flag), bool), f"state.{flag}: expected bool")
    if not state["publication_required"]:
        _nonempty_string(state.get("publication_not_required_reason"), "state.publication_not_required_reason")
    else:
        _expect(state.get("publication_not_required_reason") is None,
                "state.publication_not_required_reason: must be null when publication is required")
    if not state["operations_required"]:
        _nonempty_string(state.get("operations_not_required_reason"), "state.operations_not_required_reason")
    else:
        _expect(state.get("operations_not_required_reason") is None,
                "state.operations_not_required_reason: must be null when operations are required")
    _expect(state.get("implementation_status") in {
        "pending", "in_progress", "committed", "validated", "published",
    }, "state.implementation_status: invalid value")
    _expect(state.get("operational_status") in {
        "pending", "applying", "applied", "verified", "failed", "waived", "not_required",
    }, "state.operational_status: invalid value")
    _expect(state.get("review_status") in {"pending", "assessed", "closed"},
            "state.review_status: invalid value")


def _binding(
    kind: str, path: Path, data: dict[str, Any], phase: str, meta: dict[str, Any],
    *, content_sha256: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": f"artifacts/{POINTERS[kind]}",
        "sha256": content_sha256 or _file_sha(path),
        "generation": data.get("generation"),
        "phase": phase,
    }
    for key in (
        "review_hashes", "closure_hash", "closure_receipt_hash",
        "closure_attempt_hashes",
        "operations_review_hash", "operations_review_receipt_hash",
        "operations_review_attempt_hashes",
        "scope_hash", "authorization_hash", "supersession_hashes", "execution_hashes",
        "authorization_hashes", "intent_hashes", "apply_hashes",
        "attempt_hashes", "verification_refresh_hashes", "waiver_hashes",
        "waiver_order", "plan_hash",
        "immutable_root_hash",
    ):
        if key in meta and meta[key] is not None:
            result[key] = meta[key]
    return result


def _check_prior_binding(
    kind: str, current: dict[str, Any], prior: dict[str, Any], *, allow_append: bool = False
) -> None:
    _expect(current.get("path") == prior.get("path"), f"{kind}: bound artifact path changed")
    if kind == "publication_intent":
        old_history = prior.get("supersession_hashes") or []
        new_history = current.get("supersession_hashes") or []
        old_executions = prior.get("execution_hashes") or []
        new_executions = current.get("execution_hashes") or []
        _expect(isinstance(old_history, list) and isinstance(new_history, list)
                and isinstance(old_executions, list) and isinstance(new_executions, list),
                "publication_intent: malformed append-only history")
        if allow_append:
            if current.get("generation") == prior.get("generation"):
                _expect(new_history == old_history,
                        "publication_intent: execution append changed supersession history")
                _expect(len(new_executions) == len(old_executions) + 1
                        and new_executions[:len(old_executions)] == old_executions,
                        "publication_intent: execution receipt must append exactly once")
                _expect(current.get("scope_hash") == prior.get("scope_hash")
                        and current.get("authorization_hash") == prior.get("authorization_hash"),
                        "publication_intent: execution append changed authorization")
            else:
                _expect(
                    current.get("generation") == prior.get("generation", 0) + 1,
                    "publication_intent: supersession must advance exactly one generation",
                )
                _expect(new_history == [*old_history, prior.get("sha256")],
                        "publication_intent: supersession must archive exactly the prior bound intent")
                _expect(new_executions == [],
                        "publication_intent: a new authorization cannot preclaim execution")
        else:
            _expect(current.get("generation") == prior.get("generation"),
                    "publication_intent: bound generation changed")
            _expect(current.get("sha256") == prior.get("sha256")
                    and new_history == old_history and new_executions == old_executions,
                    "publication_intent: changed outside deterministic supersession writer")
        return
    _expect(current.get("generation") == prior.get("generation"),
            f"{kind}: bound generation changed")
    if prior.get("plan_hash") is not None:
        _expect(current.get("plan_hash") == prior.get("plan_hash"),
                f"{kind}: bound plan hash changed")
    if prior.get("immutable_root_hash") is not None:
        _expect(current.get("immutable_root_hash") == prior.get("immutable_root_hash"),
                f"{kind}: immutable artifact envelope/root changed")
    if kind not in {"review_manifest", "operations_evidence", "waivers"}:
        _expect(current.get("sha256") == prior.get("sha256"),
                f"{kind}: artifact changed after it was hash-bound at {prior.get('phase')}")
        return
    if kind == "review_manifest" and prior.get("closure_hash") is not None and not allow_append:
        _expect(current.get("closure_hash") == prior.get("closure_hash"),
                "review_manifest: bound closure report changed/vanished")
        _expect(current.get("closure_receipt_hash") == prior.get("closure_receipt_hash"),
                "review_manifest: bound closure receipt changed/vanished")
    if (
        kind == "review_manifest"
        and prior.get("operations_review_hash") is not None
        and not allow_append
    ):
        _expect(current.get("operations_review_hash") == prior.get("operations_review_hash"),
                "review_manifest: bound operations review changed/vanished")
        _expect(
            current.get("operations_review_receipt_hash")
            == prior.get("operations_review_receipt_hash"),
                "review_manifest: bound operations review receipt changed/vanished",
        )
    if kind == "review_manifest":
        for list_key in ("closure_attempt_hashes", "operations_review_attempt_hashes"):
            old = prior.get(list_key) or []
            new = current.get(list_key) or []
            _expect(isinstance(old, list) and isinstance(new, list),
                    f"review_manifest: malformed {list_key}")
            if allow_append:
                _expect(new[:len(old)] == old,
                        f"review_manifest: prior {list_key} changed/vanished")
            else:
                _expect(new == old,
                        f"review_manifest: unbound {list_key} append/change detected")
    history_keys = {
        "review_manifest": ("review_hashes",),
        "operations_evidence": (
            "authorization_hashes", "intent_hashes", "apply_hashes", "attempt_hashes",
            "verification_refresh_hashes",
        ),
        "waivers": ("waiver_hashes",),
    }[kind]
    if kind == "waivers" and "waiver_order" in prior:
        old_order = prior.get("waiver_order") or []
        new_order = current.get("waiver_order") or []
        _expect(isinstance(old_order, list) and isinstance(new_order, list),
                "waivers: malformed waiver_order")
        if allow_append:
            _expect(new_order[:len(old_order)] == old_order,
                    "waivers: previously bound order changed/vanished")
        else:
            _expect(new_order == old_order,
                    "waivers: unbound reorder/append detected outside deterministic writer")
    for history_key in history_keys:
        if history_key not in prior:
            continue
        old = prior.get(history_key) or {}
        new = current.get(history_key) or {}
        _expect(isinstance(old, dict) and isinstance(new, dict), f"{kind}: malformed {history_key} map")
        if allow_append:
            changed = sorted(k for k, v in old.items() if new.get(k) != v)
            _expect(not changed,
                    f"{kind}: previously bound {history_key} entries changed/vanished: {changed}")
        else:
            _expect(new == old,
                    f"{kind}: unbound {history_key} append/change detected outside deterministic writer")


def _load_and_validate(
    kind: str,
    state: dict[str, Any],
    state_path: Path,
    now: datetime,
    *,
    require_closure: bool = False,
    require_operations_review: bool = False,
    enforce_latest_freshness: bool = True,
    snapshot_only: bool = False,
    cache: dict[str, tuple[Path, dict[str, Any], dict[str, Any]]] | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    cache = cache if cache is not None else {}
    if kind in cache and not (
        kind == "review_manifest" and (require_closure or require_operations_review)
    ):
        return cache[kind]
    path = _safe_artifact_path(state_path, state, kind)
    data = _load_json(path, kind)
    evidence_root = _state_dir(state_path)
    if kind == "review_manifest":
        meta = validate_review_manifest(
            data, state, state_path, require_closure=require_closure,
            require_operations_review=require_operations_review, now=now,
        )
    elif kind == "implementation_evidence":
        meta = validate_implementation_evidence(
            data, state, evidence_root, now, verify_live_inputs=not snapshot_only
        )
    elif kind == "publication_intent":
        meta = validate_publication_intent(data, state, now)
    elif kind == "release_manifest":
        meta = validate_release_manifest(data, state, evidence_root, now)
    elif kind == "operations_plan":
        meta = validate_operations_plan(
            data, state, now, verify_executables=not snapshot_only
        )
    elif kind == "operations_evidence":
        _, plan, _ = _load_and_validate(
            "operations_plan", state, state_path, now,
            snapshot_only=snapshot_only, cache=cache,
        )
        meta = validate_operations_evidence(
            data, state, plan, now, evidence_root,
            enforce_latest_freshness=enforce_latest_freshness,
            verify_executables=not snapshot_only,
        )
    elif kind == "waivers":
        _, plan, _ = _load_and_validate(
            "operations_plan", state, state_path, now,
            snapshot_only=snapshot_only, cache=cache,
        )
        meta = validate_waivers(
            data, state, plan, now, verify_executables=not snapshot_only
        )
    else:
        _fail(f"unknown artifact kind {kind!r}; valid: {', '.join(POINTERS)}")
    cache[kind] = (path, data, meta)
    return path, data, meta


def gate(state_path: Path, phase: str, now: datetime) -> dict[str, Any]:
    state = _load_state(state_path)
    state["_state_path"] = str(state_path.resolve())
    _validate_state(state, state_path)
    post_publication_phases = {
        "published", "plan-review-running", "plan-reviewed", "apply-running",
        "applied", "verify-running", "operationally-verified", "complete",
    }
    snapshot_only = state["phase"] in post_publication_phases and phase != "published"
    if phase == "published" and state.get("publication_required") is True:
        intent_binding = state.get("artifact_bindings", {}).get("publication_intent")
        _expect(intent_binding is not None,
                "published gate requires a publication-start state binding")
        _expect(intent_binding.get("phase") == "publish-running",
                "published gate requires intent bound before publication")
    if phase == "verify-running" and state["phase"] == "complete":
        _expect(state.get("operations_required") is True,
                "complete re-open requires operations_required=true")
        temporal_stale = False
        try:
            _load_and_validate(
                "operations_evidence", state, state_path, now,
                enforce_latest_freshness=True, snapshot_only=True, cache={},
            )
        except ValidationError as exc:
            message = str(exc)
            if (
                "latest evidence is stale under the plan freshness contract" in message
                or "stale latest probe evidence" in message
                or "latest verification refresh is stale" in message
            ):
                temporal_stale = True
            else:
                raise
        if not temporal_stale:
            temporal_cache: dict[str, tuple[Path, dict[str, Any], dict[str, Any]]] = {}
            _, _, evidence_meta = _load_and_validate(
                "operations_evidence", state, state_path, now,
                enforce_latest_freshness=False, snapshot_only=True,
                cache=temporal_cache,
            )
            waiver_path, waiver_data, waiver_meta = _load_and_validate(
                "waivers", state, state_path, now, snapshot_only=True,
                cache=temporal_cache,
            )
            del waiver_path
            active = waiver_meta["active_waivers"]
            for waiver in waiver_data["waivers"]:
                target_id = waiver["target_id"]
                if (
                    target_id not in active
                    and _iso(waiver["expires_at"], "waiver.expires_at") <= now
                    and waiver["missing_contract"]
                    == evidence_meta["verification_gaps"].get(target_id)
                ):
                    temporal_stale = True
                    break
        _expect(
            temporal_stale,
            "complete -> verify-running is reserved for stale verification evidence or an "
            "expired exact-gap waiver; current complete evidence is still fresh",
        )
    cache: dict[str, tuple[Path, dict[str, Any], dict[str, Any]]] = {}
    needed = set(ARTIFACTS_FOR_PHASE.get(phase, set()))
    if phase == "complete":
        needed = {"review_manifest", "implementation_evidence"}
        if state["publication_required"]:
            needed |= {"publication_intent", "release_manifest"}
        if state["operations_required"]:
            needed |= {"operations_plan", "operations_evidence", "waivers"}
    bindings: dict[str, Any] = {}
    for kind in sorted(needed):
        require_closure = kind == "review_manifest" and phase in {
            "code-complete", "publish-running", "published", "plan-review-running",
            "plan-reviewed", "apply-running", "applied", "verify-running",
            "operationally-verified", "complete",
        }
        require_operations_review = (
            kind == "review_manifest" and state["operations_required"] and phase in {
                "plan-reviewed", "apply-running", "applied", "verify-running", "operationally-verified", "complete",
            }
        )
        kind_snapshot_only = snapshot_only and not (
            kind == "operations_plan"
            and kind not in state.get("artifact_bindings", {})
        )
        path, data, meta = _load_and_validate(
            kind, state, state_path, now, require_closure=require_closure,
            require_operations_review=require_operations_review,
            enforce_latest_freshness=(phase not in {"apply-running", "verify-running"}),
            snapshot_only=kind_snapshot_only,
            cache=cache,
        )
        bindings[kind] = _binding(kind, path, data, phase, meta)

    # Validate every earlier receipt even when the artifact is not part of this
    # transition.  Immutable receipts may not change; append-only receipts may
    # only gain new independently hashed entries.
    for kind, prior in state.get("artifact_bindings", {}).items():
        _expect(kind in POINTERS, f"state.artifact_bindings: unknown artifact {kind!r}")
        path, data, meta = _load_and_validate(
            kind, state, state_path, now,
            require_closure=(kind == "review_manifest" and phase in {
                "code-complete", "publish-running", "published", "plan-review-running",
                "plan-reviewed", "apply-running", "applied", "verify-running",
                "operationally-verified", "complete",
            }),
            require_operations_review=(
                kind == "review_manifest" and state["operations_required"] and phase in {
                    "plan-reviewed", "apply-running", "applied", "verify-running",
                    "operationally-verified", "complete",
                }
            ),
            enforce_latest_freshness=(phase not in {"apply-running", "verify-running"}),
            snapshot_only=snapshot_only,
            cache=cache,
        )
        current = _binding(kind, path, data, phase, meta)
        _check_prior_binding(kind, current, prior)
        bindings.setdefault(kind, current)

    if phase in {"code-complete", "publish-running", "complete"}:
        _validate_implementation_against_repo(
            cache["implementation_evidence"][1], state, state_path,
            verify_current_checkout=not snapshot_only,
        )
        _validate_code_cross_links(state, state_path, cache)
    if state["publication_required"] and phase in {
        "published", "plan-review-running", "plan-reviewed", "apply-running", "applied",
        "verify-running", "operationally-verified", "complete"
    }:
        _, release, _ = _load_and_validate(
            "release_manifest", state, state_path, now,
            snapshot_only=snapshot_only, cache=cache,
        )
        _, implementation, _ = _load_and_validate(
            "implementation_evidence", state, state_path, now,
            snapshot_only=snapshot_only, cache=cache,
        )
        _validate_release_against_implementation(
            release, implementation, state_path,
            verify_live_publication=not snapshot_only,
        )
        _, publication_intent, _ = _load_and_validate(
            "publication_intent", state, state_path, now,
            snapshot_only=snapshot_only, cache=cache,
        )
        _validate_publication_intent_against_release(
            publication_intent, release, implementation, state_path
        )
    if state["operations_required"] and phase in {
        "plan-reviewed", "apply-running", "applied", "verify-running",
        "operationally-verified", "complete"
    }:
        _expect(state["publication_required"] is True,
                "required operations cannot be reconciled without publication")
        _, release, _ = _load_and_validate(
            "release_manifest", state, state_path, now,
            snapshot_only=snapshot_only, cache=cache,
        )
        _, plan, _ = _load_and_validate(
            "operations_plan", state, state_path, now,
            snapshot_only=snapshot_only, cache=cache,
        )
        _validate_plan_against_release(plan, release)
        _, publication_intent, _ = _load_and_validate(
            "publication_intent", state, state_path, now,
            snapshot_only=snapshot_only, cache=cache,
        )
        _validate_plan_against_publication_effect(plan, release, publication_intent)

    derived: dict[str, Any] = {}
    if phase == "critique-complete":
        derived["review_status"] = "assessed"
    elif phase == "code-complete":
        derived.update({"implementation_status": "validated", "review_status": "closed"})
    elif phase == "published":
        derived["implementation_status"] = "published"
    elif phase == "plan-reviewed":
        derived["review_status"] = "closed"
    elif phase == "apply-running":
        derived["operational_status"] = "applying"
    elif phase == "applied":
        statuses = cache["operations_evidence"][2]["statuses"]
        bad = {k: v for k, v in statuses.items() if v not in {"applied", "verified"}}
        _expect(not bad, f"applied gate: not every target has an applied attempt: {bad}")
        derived["operational_status"] = "applied"
    elif phase == "operationally-verified":
        emeta = cache["operations_evidence"][2]
        statuses = emeta["statuses"]
        active_waivers = cache["waivers"][2]["active_waivers"]
        waived: set[str] = set()
        unfinished: dict[str, str] = {}
        for target_id, status in statuses.items():
            if status == "verified":
                continue
            waiver = active_waivers.get(target_id)
            if (
                waiver is not None
                and emeta["apply_statuses"].get(target_id) == "applied"
                and emeta["identity_matches"].get(target_id) is True
                and waiver["missing_contract"] == emeta["verification_gaps"].get(target_id)
                and bool(waiver["missing_contract"])
            ):
                waived.add(target_id)
            else:
                unfinished[target_id] = status
        _expect(not unfinished, f"operational verification gate: incomplete targets: {unfinished}")
        derived["operational_status"] = "waived" if waived else "verified"
    elif phase == "complete":
        if not state["publication_required"]:
            _nonempty_string(state.get("publication_not_required_reason"), "state.publication_not_required_reason")
        if state["operations_required"]:
            emeta = cache["operations_evidence"][2]
            statuses = emeta["statuses"]
            active_waivers = cache["waivers"][2]["active_waivers"]
            waived: set[str] = set()
            unfinished: dict[str, str] = {}
            for target_id, status in statuses.items():
                if status == "verified":
                    continue
                waiver = active_waivers.get(target_id)
                if (
                    waiver is not None
                    and emeta["apply_statuses"].get(target_id) == "applied"
                    and emeta["identity_matches"].get(target_id) is True
                    and waiver["missing_contract"] == emeta["verification_gaps"].get(target_id)
                    and bool(waiver["missing_contract"])
                ):
                    waived.add(target_id)
                else:
                    unfinished[target_id] = status
            _expect(not unfinished, f"complete gate: operational targets incomplete: {unfinished}")
            derived["operational_status"] = "waived" if waived else "verified"
        else:
            _nonempty_string(state.get("operations_not_required_reason"), "state.operations_not_required_reason")
            derived["operational_status"] = "not_required"
        # Implementation remains a real, validated claim when publication is
        # intentionally not required.  Publication is a delivery decision, not
        # evidence that no implementation occurred.
        derived["implementation_status"] = "published" if state["publication_required"] else "validated"
    return {"ok": True, "phase": phase, "bindings": bindings, "derived": derived}


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_json_atomic(path: Path, data: dict[str, Any]) -> None:
    _save_bytes_atomic(path, _json_bytes(data))


def _save_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    platform_compat.fsync_directory(path.parent)


@contextmanager
def _state_lock(state_path: Path):
    absolute = Path(os.path.abspath(state_path))
    _expect(absolute.exists() and absolute.is_file() and not absolute.is_symlink(),
            "state path must be the canonical regular state.json (symlink aliases forbidden)")
    state_path = absolute.resolve()
    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        platform_compat.lock_file_exclusive(lock)
        try:
            yield
        finally:
            platform_compat.unlock_file(lock)


def _transaction_path(state_path: Path) -> Path:
    return state_path.with_name(state_path.name + ".txn")


def _check_transaction_path(state_path: Path) -> Path:
    return state_path.with_name(state_path.name + ".check-txn")


def _clear_transaction(state_path: Path) -> None:
    journal = _transaction_path(state_path)
    if journal.exists():
        journal.unlink()
        platform_compat.fsync_directory(journal.parent)


def _clear_check_transaction(state_path: Path) -> None:
    journal = _check_transaction_path(state_path)
    if journal.exists():
        journal.unlink()
        platform_compat.fsync_directory(journal.parent)


def _apply_check_record_to_state(
    state: dict[str, Any], ref: dict[str, str], record: dict[str, Any], state_path: Path,
) -> bool:
    evidence_rel = ref["path"]
    evidence_sha = ref["sha256"]
    attempts = state.setdefault("check_run_attempts", [])
    _expect(not any(item.get("path") == evidence_rel for item in attempts),
            "check-run: duplicate all-attempt evidence path")
    attempts.append({"path": evidence_rel, "sha256": evidence_sha})
    head_before = record["head_before"]
    if state.get("check_run_head") != head_before:
        previous_head = state.get("check_run_head")
        previous_runs = state.get("check_run_hashes") or {}
        if previous_head is not None and previous_runs:
            history = state.setdefault("check_run_history", {})
            _expect(previous_head not in history,
                    "check-run: prior HEAD generation is already archived")
            history[previous_head] = dict(previous_runs)
        state["check_run_head"] = head_before
        state["check_run_hashes"] = {}
    check_runs = state.setdefault("check_run_hashes", {})
    # Every attempt supersedes an older active pass with the same logical name
    # or canonical command. A pass followed by a failure therefore leaves no
    # active success for closure to reuse.
    for prior_path in list(check_runs):
        prior_record = _load_json(
            _state_dir(state_path) / prior_path, "prior active check evidence"
        )
        if (
            prior_record.get("name") == record.get("name")
            or prior_record.get("command") == record.get("command")
        ):
            del check_runs[prior_path]
    accepted = (
        record["exit_code"] == 0
        and record["timed_out"] is False
        and record["head_after"] == head_before
        and record["tracked_status_after"] == ""
        and record["execution_head_after"] == head_before
        and record["execution_status_after"] == ""
        and record["state_tree_sha256_before"] == record["state_tree_sha256_after"]
    )
    if accepted:
        _expect(evidence_rel not in check_runs,
                "check-run: duplicate active evidence path")
        check_runs[evidence_rel] = evidence_sha
    state["updated_at"] = record["completed_at"]
    return accepted


def _recover_check_transaction(state_path: Path) -> None:
    journal_path = _check_transaction_path(state_path)
    if not journal_path.exists():
        return
    journal = _load_json(journal_path, "check transaction journal")
    _strict_keys(journal, {
        "schema_version", "evidence_path", "evidence_sha256", "state_before_sha256",
        "state_after_sha256", "state_after",
    }, "check transaction journal")
    _require_keys(journal, {
        "schema_version", "evidence_path", "evidence_sha256", "state_before_sha256",
        "state_after_sha256", "state_after",
    }, "check transaction journal")
    _expect(journal.get("schema_version") == 1,
            "check transaction journal.schema_version: expected 1")
    rel = _nonempty_string(journal.get("evidence_path"), "check transaction evidence_path")
    _expect(rel.startswith("artifacts/checks/"),
            "check transaction evidence_path: must be below artifacts/checks")
    evidence_path = (_state_dir(state_path) / rel).resolve()
    try:
        evidence_path.relative_to((_state_dir(state_path) / "artifacts" / "checks").resolve())
    except ValueError:
        _fail("check transaction evidence path escapes artifacts/checks")
    evidence_sha = _sha256_value(
        journal.get("evidence_sha256"), "check transaction evidence_sha256"
    )
    before_sha = _sha256_value(
        journal.get("state_before_sha256"), "check transaction state_before_sha256"
    )
    after_sha = _sha256_value(
        journal.get("state_after_sha256"), "check transaction state_after_sha256"
    )
    state_after = journal.get("state_after")
    _expect(isinstance(state_after, dict), "check transaction state_after: expected object")
    _expect(hashlib.sha256(_json_bytes(state_after)).hexdigest() == after_sha,
            "check transaction state_after_sha256: content mismatch")
    current_sha = _file_sha(state_path)
    evidence_matches = evidence_path.is_file() and _file_sha(evidence_path) == evidence_sha
    if current_sha == after_sha:
        _expect(evidence_matches,
                "check transaction: state committed but evidence missing/changed")
        runtime = dict(state_after); runtime["_state_path"] = str(state_path.resolve())
        _validate_state(runtime, state_path)
        _clear_check_transaction(state_path)
        return
    _expect(current_sha == before_sha,
            "check transaction: current state matches neither pre-state nor post-state")
    if not evidence_matches:
        _clear_check_transaction(state_path)
        return
    before = _load_json(state_path, "check transaction pre-state")
    before["_state_path"] = str(state_path.resolve())
    _validate_state(before, state_path)
    record = _load_json(evidence_path, "check transaction evidence")
    expected = _persisted_state(before)
    _apply_check_record_to_state(
        expected, {"path": rel, "sha256": evidence_sha}, record, state_path
    )
    _expect(expected == state_after,
            "check transaction state_after is not the deterministic ledger result")
    runtime = dict(expected); runtime["_state_path"] = str(state_path.resolve())
    _validate_state(runtime, state_path)
    _save_json_atomic(state_path, expected)
    _clear_check_transaction(state_path)


def _recover_transaction(state_path: Path) -> None:
    """Finish or discard one interrupted mutable-artifact transaction.

    The journal is not trusted merely because it is local.  Recovery
    deterministically revalidates the artifact and reconstructs the only
    permitted post-state from the current pre-state before committing it.
    """
    journal_path = _transaction_path(state_path)
    if not journal_path.exists():
        return
    journal = _load_json(journal_path, "mutable transaction journal")
    _strict_keys(
        journal,
        {
            "schema_version", "kind", "artifact_path", "artifact_sha256",
            "state_before_sha256", "state_after_sha256", "state_after",
        },
        "mutable transaction journal",
    )
    _require_keys(
        journal,
        {
            "schema_version", "kind", "artifact_path", "artifact_sha256",
            "state_before_sha256", "state_after_sha256", "state_after",
        },
        "mutable transaction journal",
    )
    _expect(journal.get("schema_version") == 1,
            "mutable transaction journal.schema_version: expected 1")
    kind = _nonempty_string(journal.get("kind"), "mutable transaction journal.kind")
    _expect(kind in {
        "review_manifest", "publication_intent", "operations_evidence", "waivers"
    },
            "mutable transaction journal.kind: unsupported mutable artifact")
    expected_rel = f"artifacts/{POINTERS[kind]}"
    _expect(journal.get("artifact_path") == expected_rel,
            "mutable transaction journal.artifact_path: noncanonical path")
    artifact_sha = _sha256_value(
        journal.get("artifact_sha256"), "mutable transaction journal.artifact_sha256"
    )
    before_sha = _sha256_value(
        journal.get("state_before_sha256"), "mutable transaction journal.state_before_sha256"
    )
    after_sha = _sha256_value(
        journal.get("state_after_sha256"), "mutable transaction journal.state_after_sha256"
    )
    state_after = journal.get("state_after")
    _expect(isinstance(state_after, dict),
            "mutable transaction journal.state_after: expected object")
    _expect(hashlib.sha256(_json_bytes(state_after)).hexdigest() == after_sha,
            "mutable transaction journal.state_after_sha256: content mismatch")
    current_sha = _file_sha(state_path)
    artifact_path = (_state_dir(state_path) / expected_rel).resolve()
    expected_artifact_path = (
        _state_dir(state_path) / "artifacts" / POINTERS[kind]
    ).resolve()
    _expect(artifact_path == expected_artifact_path,
            "mutable transaction journal artifact path escapes canonical location")
    artifact_matches = artifact_path.is_file() and _file_sha(artifact_path) == artifact_sha
    if current_sha == after_sha:
        _expect(artifact_matches,
                "mutable transaction journal: state committed but artifact is missing or changed")
        runtime_after = dict(state_after)
        runtime_after["_state_path"] = str(state_path.resolve())
        _validate_state(runtime_after, state_path)
        _clear_transaction(state_path)
        return
    _expect(current_sha == before_sha,
            "mutable transaction journal: current state matches neither pre-state nor post-state")
    if not artifact_matches:
        # Atomic artifact replacement did not occur; the pre-state is intact,
        # so discarding the prepared journal is safe and the command can retry.
        _clear_transaction(state_path)
        return

    state_before = _load_json(state_path, "mutable transaction pre-state")
    state_before["_state_path"] = str(state_path.resolve())
    artifact = _load_json(artifact_path, kind)
    after_time = _iso(state_after.get("updated_at"), "transaction state_after.updated_at")
    if kind == "review_manifest":
        meta = validate_review_manifest(
            artifact, state_before, state_path, now=after_time
        )
    elif kind == "publication_intent":
        meta = validate_publication_intent(artifact, state_before, after_time)
    else:
        plan_path = _safe_artifact_path(state_path, state_before, "operations_plan")
        plan = _load_json(plan_path, "operations_plan")
        validate_operations_plan(plan, state_before, after_time)
    if kind == "operations_evidence":
        meta = validate_operations_evidence(
            artifact, state_before, plan, after_time, _state_dir(state_path)
        )
    elif kind == "waivers":
        meta = validate_waivers(artifact, state_before, plan, after_time)
    _check_mutable_binding(
        kind, state_before, artifact_path, artifact, meta, allow_append=True
    )
    expected_after = _persisted_state(state_before)
    expected_after.setdefault("artifact_bindings", {})[kind] = _binding(
        kind, artifact_path, artifact, state_before["phase"], meta,
        content_sha256=artifact_sha,
    )
    if kind == "operations_evidence":
        expected_after["operational_status"] = _operational_status_projection(
            meta, state_before["phase"]
        )
    expected_after["updated_at"] = state_after["updated_at"]
    _expect(expected_after == state_after,
            "mutable transaction journal.state_after is not the deterministic writer result")
    runtime_after = dict(expected_after)
    runtime_after["_state_path"] = str(state_path.resolve())
    _validate_state(runtime_after, state_path)
    _save_json_atomic(state_path, expected_after)
    _clear_transaction(state_path)


def _recover_pending_transactions(state_path: Path) -> None:
    """Recover the one journal permitted under the shared state lock.

    Every state writer calls this before loading state.  Seeing both journal
    kinds means a writer bypassed the lock/recovery contract; choosing an order
    would make one journal's pre-state unverifiable, so fail closed.
    """
    _expect(
        not (_transaction_path(state_path).exists()
             and _check_transaction_path(state_path).exists()),
        "state has simultaneous mutable-artifact and check transactions",
    )
    _recover_transaction(state_path)
    _recover_check_transaction(state_path)


def recover_state(state_path: Path) -> dict[str, Any]:
    with _state_lock(state_path):
        had_mutable = _transaction_path(state_path).exists()
        had_check = _check_transaction_path(state_path).exists()
        _recover_pending_transactions(state_path)
        state = _load_state(state_path)
        state["_state_path"] = str(state_path.resolve())
        _validate_state(state, state_path)
        return {
            "ok": True, "recovered_mutable_transaction": had_mutable,
            "recovered_check_transaction": had_check, "phase": state["phase"],
        }


def _kit_upgrade_scope(state: dict[str, Any]) -> dict[str, str]:
    old_commit = state["agent_kit_commit"]
    new_commit = _commit(
        _git_output(AGENT_KIT_ROOT, "rev-parse", "HEAD").decode().strip(),
        "kit-upgrade target",
    )
    _expect(old_commit != new_commit,
            "kit-upgrade: state already uses the executing kit revision")
    return {
        "from_commit": old_commit, "to_commit": new_commit,
        "from_writer_sha256": _kit_writer_sha256(
            old_commit, "kit-upgrade.from_writer_sha256"
        ),
        "to_writer_sha256": _kit_writer_sha256(
            new_commit, "kit-upgrade.to_writer_sha256"
        ),
    }


def kit_upgrade_preview(state_path: Path, now: datetime) -> dict[str, Any]:
    with _state_lock(state_path):
        _recover_pending_transactions(state_path)
        state = _load_state(state_path)
        state["_state_path"] = str(state_path.resolve())
        _validate_state(state, state_path, allow_kit_upgrade=True)
        _expect(now >= _iso(state.get("updated_at"), "state.updated_at"),
                "kit-upgrade-preview timestamp precedes state.updated_at")
        scope = _kit_upgrade_scope(state)
        return {"ok": True, "scope": scope, "scope_hash": _value_sha(scope)}


def kit_upgrade_state(
    state_path: Path, approved_by: str, expected_scope_hash: str, now: datetime,
) -> dict[str, Any]:
    """Explicitly advance frozen writer semantics along the reviewed kit ancestry."""
    with _state_lock(state_path):
        _recover_pending_transactions(state_path)
        state = _load_state(state_path)
        state["_state_path"] = str(state_path.resolve())
        _validate_state(state, state_path, allow_kit_upgrade=True)
        _expect(now >= _iso(state.get("updated_at"), "state.updated_at"),
                "kit-upgrade timestamp precedes state.updated_at")
        scope = _kit_upgrade_scope(state)
        scope_hash = _value_sha(scope)
        _expect(_sha256_value(expected_scope_hash, "--scope-hash") == scope_hash,
                "kit-upgrade: executing kit changed after preview")
        old_commit = scope["from_commit"]
        new_commit = scope["to_commit"]
        record = {
            **scope,
            "approved_by": _human_name(approved_by, "--approved-by"),
            "approval_method": "human-explicit", "at": _utc_text(now),
        }
        state.setdefault("kit_upgrade_history", []).append(record)
        state["agent_kit_commit"] = new_commit
        state["updated_at"] = _utc_text(now)
        persisted = _persisted_state(state)
        _WRITER_VERSION_CACHE.clear()
        verified = dict(persisted)
        verified["_state_path"] = str(state_path.resolve())
        _validate_state(verified, state_path)
        cache: dict[str, tuple[Path, dict[str, Any], dict[str, Any]]] = {}
        for kind, prior in persisted.get("artifact_bindings", {}).items():
            path, data, meta = _load_and_validate(
                kind, verified, state_path, now, snapshot_only=True,
                enforce_latest_freshness=False, cache=cache,
            )
            _check_prior_binding(
                kind, _binding(kind, path, data, persisted["phase"], meta), prior
            )
        _save_bytes_atomic(state_path, _json_bytes(persisted))
        return {
            "ok": True, "from_commit": old_commit, "to_commit": new_commit,
            "approved_by": record["approved_by"],
            "upgrade_index": len(persisted["kit_upgrade_history"]),
        }


def _writer_context(
    state_path: Path, now: datetime, allowed_phases: set[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _recover_pending_transactions(state_path)
    state = _load_state(state_path)
    state["_state_path"] = str(state_path.resolve())
    _validate_state(state, state_path)
    _expect(
        now >= _iso(state.get("updated_at"), "state.updated_at"),
        "operation writer timestamp precedes state.updated_at",
    )
    _expect(state.get("phase") in allowed_phases,
            f"writer requires phase in {sorted(allowed_phases)}, got {state.get('phase')!r}")
    plan_path = _safe_artifact_path(state_path, state, "operations_plan")
    plan = _load_json(plan_path, "operations_plan")
    pmeta = validate_operations_plan(plan, state, now)
    plan_prior = state.get("artifact_bindings", {}).get("operations_plan")
    _expect(plan_prior is not None, "operations_plan: missing frozen state binding")
    _check_prior_binding(
        "operations_plan",
        _binding("operations_plan", plan_path, plan, state["phase"], pmeta),
        plan_prior,
    )
    _expect(state.get("operations_required") is True,
            "operation writers require operations_required=true")
    cache: dict[str, tuple[Path, dict[str, Any], dict[str, Any]]] = {}
    for kind in (
        "review_manifest", "implementation_evidence", "publication_intent",
        "release_manifest",
    ):
        path, data, meta = _load_and_validate(
            kind, state, state_path, now,
            require_closure=(kind == "review_manifest"),
            require_operations_review=(kind == "review_manifest"),
            snapshot_only=(kind in {"implementation_evidence", "release_manifest"}),
            cache=cache,
        )
        prior = state.get("artifact_bindings", {}).get(kind)
        _expect(prior is not None, f"{kind}: missing pre-action state binding")
        _check_prior_binding(kind, _binding(kind, path, data, state["phase"], meta), prior)
    _validate_implementation_against_repo(
        cache["implementation_evidence"][1], state, state_path,
        verify_current_checkout=False,
    )
    _validate_code_cross_links(state, state_path, cache)
    _validate_release_against_implementation(
        cache["release_manifest"][1], cache["implementation_evidence"][1], state_path,
        verify_live_publication=False,
    )
    _validate_publication_intent_against_release(
        cache["publication_intent"][1], cache["release_manifest"][1],
        cache["implementation_evidence"][1], state_path,
    )
    _validate_plan_against_release(plan, cache["release_manifest"][1])
    _validate_plan_against_publication_effect(
        plan, cache["release_manifest"][1], cache["publication_intent"][1]
    )
    return state, plan, pmeta


def _artifact_file(state_path: Path, state: dict[str, Any], kind: str) -> Path:
    expected = (_state_dir(state_path) / "artifacts" / POINTERS[kind]).resolve()
    actual = (_state_dir(state_path) / state[kind]).resolve()
    _expect(actual == expected, f"state.{kind}: writer requires canonical artifact path")
    return actual


def _producer() -> dict[str, str]:
    return {
        "kind": "deterministic-tool",
        "name": "milestone-pipeline-artifacts.py",
        "provider": "local",
        "version": _file_sha(Path(__file__).resolve()),
    }


def _validate_deterministic_producer(
    value: Any, state: dict[str, Any], label: str,
) -> None:
    _expect(isinstance(value, dict), f"{label}: expected producer object")
    _expect(
        value.get("kind") == "deterministic-tool"
        and value.get("name") == "milestone-pipeline-artifacts.py"
        and value.get("provider") == "local",
        f"{label}: deterministic milestone writer identity required",
    )
    version = _sha256_value(value.get("version"), f"{label}.version")
    current_kit = _commit(state.get("agent_kit_commit"), "state.agent_kit_commit")
    history = state.get("kit_upgrade_history") or []
    allowed_commits = [
        _commit(history[0]["from_commit"], f"{label} writer lineage")
    ] if history else []
    allowed_commits.extend(
        _commit(item["to_commit"], f"{label} writer lineage") for item in history
    )
    if current_kit not in allowed_commits:
        allowed_commits.append(current_kit)
    key = tuple(allowed_commits)
    allowed = _WRITER_VERSION_CACHE.get(key)
    if allowed is None:
        allowed = {_producer()["version"]}
        for commit in allowed_commits:
            allowed.add(_kit_writer_sha256(commit, f"{label} writer lineage"))
        _WRITER_VERSION_CACHE[key] = allowed
    _expect(version in allowed,
            f"{label}.version: writer is outside the frozen-to-current reviewed lineage")


def _initial_operations_evidence(
    state: dict[str, Any], plan: dict[str, Any], now: datetime
) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "milestone_id": state["id"],
        "generation": plan["generation"],
        "created_at": _utc_text(now),
        "producer": _producer(),
        "plan_hash": plan["plan_hash"],
        "targets": [
            {
                "id": target["id"], "status": "pending", "attempts": [],
                "verification_refresh_intents": [],
                "verification_refreshes": [],
            }
            for target in plan["targets"]
        ],
    }


def _initial_waivers(state: dict[str, Any], plan: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "milestone_id": state["id"],
        "generation": plan["generation"],
        "created_at": _utc_text(now),
        "producer": _producer(),
        "plan_hash": plan["plan_hash"],
        "waivers": [],
    }


def _human_name(value: str, label: str) -> str:
    name = _nonempty_string(value, label)
    lowered = name.casefold()
    generic = {"human", "user", "operator", "approver", "unknown", "n/a"}
    automation = re.search(
        r"\b(agent|codex|automation|bot|system|pipeline|service[- ]?account)\b",
        lowered,
    )
    _expect(lowered not in generic and automation is None,
            f"{label}: name an accountable human, not an automation identity")
    return name


def _publication_environment(
    remote_url: str, neutral_home: Path | None = None,
) -> tuple[dict[str, str], str | None, str | None]:
    if neutral_home is None:
        neutral_home = Path(tempfile.gettempdir()) / (
            f"workspace-milestone-git-isolation-{os.getpid()}"
        )
    neutral_home = neutral_home.expanduser().absolute()
    if neutral_home.exists() or neutral_home.is_symlink():
        _expect(neutral_home.is_dir() and not neutral_home.is_symlink(),
                "publication writer: Git isolation root is unsafe")
        shutil.rmtree(neutral_home)
    neutral_home.mkdir(parents=True, mode=0o700)
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(neutral_home), "XDG_CONFIG_HOME": str(neutral_home),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
    }
    parsed = urlparse(remote_url)
    is_ssh = parsed.scheme.casefold() == "ssh" or bool(
        re.match(r"^(?:[^@/]+@)?[^:/]+:.+$", remote_url)
    )
    known_path: str | None = None
    known_sha: str | None = None
    if is_ssh:
        home_raw = os.environ.get("HOME")
        _expect(bool(home_raw), "publication writer: SSH requires an accountable HOME")
        known = Path(home_raw or "") / ".ssh" / "known_hosts"
        _expect(known.is_file() and not known.is_symlink(),
                "publication writer: SSH requires a regular ~/.ssh/known_hosts")
        known_path = str(known.resolve())
        known_sha = _file_sha(known)
        ssh_path = shutil.which("ssh")
        _expect(ssh_path is not None, "publication writer: ssh executable unavailable")
        environment["GIT_SSH_COMMAND"] = shlex.join([
            str(Path(ssh_path).resolve()), "-F", "/dev/null", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o",
            f"UserKnownHostsFile={known_path}", "-o",
            "GlobalKnownHostsFile=/etc/ssh/ssh_known_hosts",
        ])
        sock = os.environ.get("SSH_AUTH_SOCK")
        if sock:
            environment["SSH_AUTH_SOCK"] = sock
    return environment, known_path, known_sha


def _validate_publication_local_git_config(state_path: Path) -> None:
    repo = _repo_root(state_path)
    raw_git_dir = _git_output(repo, "rev-parse", "--git-dir").decode().strip()
    git_dir = Path(raw_git_dir)
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    config = git_dir.resolve() / "config"
    _expect(config.is_file() and not config.is_symlink(),
            "publication writer: local git config is missing or symlinked")
    text = config.read_text(encoding="utf-8", errors="strict")
    forbidden = re.compile(
        r"(?im)^\s*\[(?:url\b|include\b|includeif\b|http\b|credential\b)"
        r"|^\s*(?:hooksPath|sshCommand)\s*=",
    )
    _expect(not forbidden.search(text),
            "publication writer: local git config contains endpoint/credential/hook rewrites")
    _expect(not re.search(r"(?im)^\s*worktreeConfig\s*=\s*true\s*$", text),
            "publication writer: extensions.worktreeConfig is forbidden")
    worktree_config = git_dir.resolve() / "config.worktree"
    _expect(not worktree_config.exists(),
            "publication writer: per-worktree git config is forbidden")


def _publication_object_directory(state_path: Path) -> Path:
    repo = _repo_root(state_path)
    raw = _git_output(repo, "rev-parse", "--git-common-dir").decode().strip()
    common = Path(raw)
    if not common.is_absolute():
        common = repo / common
    objects = common.resolve() / "objects"
    _expect(objects.is_dir() and not objects.is_symlink(),
            "publication writer: reviewed repository object directory is missing/symlinked")
    return objects


def _prepare_publication_sandbox(
    state_path: Path, git_path: str, environment: dict[str, str]
) -> tuple[str, str]:
    """Recreate a configless bare repository so source .git config is never executed."""
    root = (_state_dir(state_path) / "artifacts" / "publication").resolve()
    root.mkdir(parents=True, exist_ok=True)
    sandbox = root / "push-sandbox.git"
    if sandbox.exists() or sandbox.is_symlink():
        _expect(sandbox.is_dir() and not sandbox.is_symlink(),
                "publication writer: isolated push repository path is unsafe")
        shutil.rmtree(sandbox)
    init_result = _run_bounded_process(
        [git_path, "-c", "core.hooksPath=/dev/null", "init", "--bare", "--quiet", str(sandbox)],
        cwd=Path(environment["HOME"]), timeout=30, env=dict(environment),
    )
    _expect(init_result["exit_code"] == 0 and not init_result["timed_out"],
            "publication writer: could not create isolated push repository")
    config = sandbox / "config"
    _expect(config.is_file() and not config.is_symlink(),
            "publication writer: isolated push config is missing")
    config_text = config.read_text(encoding="utf-8", errors="strict")
    _expect(not re.search(
        r"(?im)^\s*\[(?:remote|url|include|includeif|http|credential)\b|"
        r"^\s*(?:hooksPath|sshCommand)\s*=", config_text,
    ), "publication writer: isolated push config is not endpoint-neutral")
    hooks = sandbox / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)
    _expect(not hooks.exists(),
            "publication writer: isolated push hooks could not be removed")
    objects = _publication_object_directory(state_path)
    return str(sandbox), str(objects)


def _publication_context(
    state_path: Path, now: datetime
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    _recover_pending_transactions(state_path)
    state = _load_state(state_path)
    state["_state_path"] = str(state_path.resolve())
    _validate_state(state, state_path)
    _expect(state.get("phase") == "publish-running",
            "publication writer requires publish-running")
    _expect(now >= _iso(state.get("updated_at"), "state.updated_at"),
            "publication writer timestamp precedes state.updated_at")
    _expect(state.get("publication_required") is True,
            "publication writer requires publication_required=true")
    path = _artifact_file(state_path, state, "publication_intent")
    cache: dict[str, tuple[Path, dict[str, Any], dict[str, Any]]] = {}
    for kind in ("review_manifest", "implementation_evidence"):
        artifact_path, artifact, meta = _load_and_validate(
            kind, state, state_path, now,
            require_closure=(kind == "review_manifest"),
            snapshot_only=(kind == "implementation_evidence"), cache=cache,
        )
        prior = state.get("artifact_bindings", {}).get(kind)
        _expect(prior is not None, f"{kind}: missing pre-publication binding")
        _check_prior_binding(
            kind, _binding(kind, artifact_path, artifact, state["phase"], meta), prior
        )
    implementation = cache["implementation_evidence"][1]
    item = implementation["repositories"][0]
    _validate_implementation_against_repo(implementation, state, state_path)
    _validate_code_cross_links(state, state_path, cache)
    # The candidate is allowed to create its isolated sandbox and observe a remote,
    # so the reviewed policy must be resolved before it is ever constructed.
    _preflight_publication_delivery_policy(state, item, _repo_root(state_path))
    return state, item, path


def _publication_observe(
    state_path: Path, remote_url: str, ref: str, *, git_path: str | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[str | None, list[str], dict[str, Any]]:
    if git_path is None:
        git_raw = shutil.which("git")
        _expect(git_raw is not None, "publication writer: git executable is unavailable")
        git_path = str(Path(git_raw).resolve())
    _validate_publication_local_git_config(state_path)
    argv = [
        git_path, "-c", "core.hooksPath=/dev/null",
        "ls-remote", "--heads", remote_url, ref,
    ]
    if environment is None:
        environment, _known_path, _known_sha = _publication_environment(remote_url)
    result = _run_bounded_process(
        argv, cwd=Path(environment["HOME"]), timeout=30, env=dict(environment),
    )
    _expect(result["exit_code"] == 0 and not result["stdout_truncated"]
            and not result["stderr_truncated"],
            "publication writer: remote precondition observation failed")
    rows = [
        line.split()
        for line in result["stdout_bytes"].decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    _expect(len(rows) <= 1 and all(len(row) == 2 and row[1] == ref for row in rows),
            "publication writer: remote returned an ambiguous branch precondition")
    observed = _commit(rows[0][0], "publication remote head") if rows else None
    return observed, argv, result


def _validate_effect_argocd_executable(value: dict[str, Any], label: str) -> None:
    executable = _nonempty_string(
        value.get("argocd_executable_path"), f"{label}.argocd_executable_path"
    )
    _expect(os.path.isabs(executable) and Path(executable).name.casefold() == "argocd",
            f"{label}.argocd_executable_path: exact absolute argocd path required")
    _sha256_value(value.get("argocd_executable_sha256"),
                  f"{label}.argocd_executable_sha256")


def _effect_declaration_targets(resolved_targets: Any, label: str) -> list[dict[str, Any]]:
    _expect(isinstance(resolved_targets, list) and bool(resolved_targets),
            f"{label}.targets: exact non-empty target set required")
    declaration_targets: list[dict[str, Any]] = []
    for i, target in enumerate(resolved_targets):
        tlabel = f"{label}.targets[{i}]"
        _expect(isinstance(target, dict), f"{tlabel}: expected object")
        _require_keys(target, {"argocd_application_uid"}, tlabel)
        _nonempty_string(target.get("argocd_application_uid"),
                         f"{tlabel}.argocd_application_uid")
        declaration_targets.append({
            key: item for key, item in target.items()
            if key != "argocd_application_uid"
        })
    return declaration_targets


def _validate_delivery_effect(value: Any, label: str) -> dict[str, Any]:
    _expect(isinstance(value, dict), f"{label}: expected object")
    kind = value.get("kind")
    if kind == "ci-render-argocd-auto-sync-v1":
        return _validate_single_leg_effect(value, label)
    if kind == "ci-render-argocd-auto-sync-fanout-v1":
        return _validate_fanout_effect(value, label)
    _fail(f"{label}.kind: generic auto-sync is forbidden")


def _validate_single_leg_effect(value: dict[str, Any], label: str) -> dict[str, Any]:
    keys = {
        "kind", "declaration_sha256", "argocd_executable_path",
        "argocd_executable_sha256", "render", "ci_render", "cascade_steps", "targets",
    }
    _strict_keys(value, keys, label)
    _require_keys(value, keys, label)
    _validate_effect_argocd_executable(value, label)
    render = value.get("render")
    _expect(isinstance(render, dict), f"{label}.render: expected object")
    _strict_keys(
        render, {"remote", "branch", "protected", "provenance_path", "expected_remote_head"},
        f"{label}.render",
    )
    _require_keys(
        render, {"remote", "branch", "protected", "provenance_path", "expected_remote_head"},
        f"{label}.render",
    )
    if render.get("expected_remote_head") is not None:
        _commit(render.get("expected_remote_head"), f"{label}.render.expected_remote_head")
    declaration_targets = _effect_declaration_targets(value.get("targets"), label)
    automatic = {
        "kind": value["kind"],
        "render": {
            key: item for key, item in render.items()
            if key != "expected_remote_head"
        },
        "ci_render": value["ci_render"],
        "targets": declaration_targets,
        "cascade_steps": value["cascade_steps"],
    }
    _validate_automatic_gitops_contract(
        automatic, [automatic["render"]["remote"]], f"{label}.declaration"
    )
    _expect(
        _sha256_value(value.get("declaration_sha256"), f"{label}.declaration_sha256")
        == _value_sha(automatic),
        f"{label}.declaration_sha256: resolved effect differs from reviewed declaration",
    )
    return json.loads(json.dumps(value))


def _validate_fanout_effect(value: dict[str, Any], label: str) -> dict[str, Any]:
    keys = {
        "kind", "declaration_sha256", "argocd_executable_path",
        "argocd_executable_sha256", "image_build", "chart", "render_legs",
        "cascade_steps", "targets",
    }
    _strict_keys(value, keys, label)
    _require_keys(value, keys, label)
    _validate_effect_argocd_executable(value, label)
    render_legs = value.get("render_legs")
    _expect(isinstance(render_legs, list) and bool(render_legs),
            f"{label}.render_legs: exact non-empty leg set required")
    declaration_legs: list[dict[str, Any]] = []
    leg_remotes: list[str] = []
    for i, leg in enumerate(render_legs):
        llabel = f"{label}.render_legs[{i}]"
        _expect(isinstance(leg, dict), f"{llabel}: expected object")
        leg_keys = {
            "id", "remote", "branch", "protected", "provenance_path",
            "ci_render", "expected_remote_head",
        }
        _strict_keys(leg, leg_keys, llabel)
        _require_keys(leg, leg_keys, llabel)
        if leg.get("expected_remote_head") is not None:
            _commit(leg.get("expected_remote_head"), f"{llabel}.expected_remote_head")
        declaration_legs.append({
            key: item for key, item in leg.items() if key != "expected_remote_head"
        })
        leg_remotes.append(_nonempty_string(leg.get("remote"), f"{llabel}.remote"))
    declaration_targets = _effect_declaration_targets(value.get("targets"), label)
    chart = value.get("chart")
    _expect(isinstance(chart, dict), f"{label}.chart: expected object")
    image_build = value.get("image_build")
    _expect(isinstance(image_build, dict), f"{label}.image_build: expected object")
    automatic = {
        "kind": value["kind"],
        "image_build": image_build,
        "chart": chart,
        "render_legs": declaration_legs,
        "targets": declaration_targets,
        "cascade_steps": value["cascade_steps"],
    }
    _validate_automatic_gitops_contract(
        automatic, leg_remotes + [chart.get("remote")], f"{label}.declaration",
        artifact_prefixes=[image_build.get("registry_repo")],
    )
    _expect(
        _sha256_value(value.get("declaration_sha256"), f"{label}.declaration_sha256")
        == _value_sha(automatic),
        f"{label}.declaration_sha256: resolved effect differs from reviewed declaration",
    )
    return json.loads(json.dumps(value))


def _argocd_effect_target(
    state_path: Path, executable: str, target: dict[str, Any],
) -> dict[str, Any]:
    config_path = Path(target["argocd_config_path"])
    _expect(config_path.is_file() and not config_path.is_symlink()
            and _file_sha(config_path) == target["argocd_config_sha256"],
            f"automatic_gitops target {target['id']!r}: Argo config identity changed")
    _validate_json_argocd_config(
        config_path, target["argocd_context"], target["argocd_server"],
        target["certificate_authority_sha256"],
        f"automatic_gitops target {target['id']!r}",
    )
    argv = [
        executable, "app", "get", target["argocd_application"],
        "--server", target["argocd_server"], "--output", "json",
        "--config", target["argocd_config_path"],
        "--argocd-context", target["argocd_context"],
    ]
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str((_state_dir(state_path) / "artifacts" / "publication" / "isolated-home")),
        "LANG": "C", "LC_ALL": "C",
    }
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
    result = _run_bounded_process(
        argv, cwd=_repo_root(state_path), timeout=30, env=environment
    )
    _expect(result["exit_code"] == 0 and not result["timed_out"]
            and not result["stdout_truncated"] and not result["stderr_truncated"],
            f"automatic_gitops target {target['id']!r}: live Argo read failed")
    try:
        value = json.loads(result["stdout_bytes"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"automatic_gitops target {target['id']!r}: invalid Argo JSON: {exc}")
    _expect(isinstance(value, dict),
            f"automatic_gitops target {target['id']!r}: Argo output must be object")
    metadata = value.get("metadata") or {}
    spec = value.get("spec") or {}
    source = spec.get("source")
    destination = spec.get("destination")
    sync_policy = spec.get("syncPolicy") or {}
    automated = sync_policy.get("automated")
    _expect(isinstance(metadata, dict) and isinstance(spec, dict)
            and isinstance(source, dict) and isinstance(destination, dict)
            and isinstance(automated, dict),
            f"automatic_gitops target {target['id']!r}: single-source automated Application required")
    observed_automated = {
        "enabled": automated.get("enabled", True),
        "prune": automated.get("prune", False),
        "self_heal": automated.get("selfHeal", False),
        "allow_empty": automated.get("allowEmpty", False),
    }
    # Destination is server-URL XOR registered-cluster-name; compare whichever the
    # reviewed target declared against the same field of the live Application.
    if "destination_server" in target:
        dest_expected = {"destination_server": target["destination_server"]}
        dest_observed = {"destination_server": destination.get("server")}
    else:
        dest_expected = {"destination_name": target["destination_name"]}
        dest_observed = {"destination_name": destination.get("name")}
    expected = {
        "argocd_application": target["argocd_application"],
        "argocd_project": target["argocd_project"],
        "source_repo_url": target["source_repo_url"],
        "source_target_revision": target["source_target_revision"],
        "source_path": target["source_path"],
        **dest_expected,
        "destination_namespace": target["destination_namespace"],
        "automated": target["automated"],
    }
    observed = {
        "argocd_application": metadata.get("name"),
        "argocd_project": spec.get("project"),
        "source_repo_url": source.get("repoURL"),
        "source_target_revision": source.get("targetRevision"),
        "source_path": source.get("path"),
        **dest_observed,
        "destination_namespace": destination.get("namespace"),
        "automated": observed_automated,
    }
    _expect(observed == expected,
            f"automatic_gitops target {target['id']!r}: live Application identity/policy drifted")
    uid = _nonempty_string(metadata.get("uid"),
                           f"automatic_gitops target {target['id']!r}.uid")
    return {**target, "argocd_application_uid": uid}


def _resolve_delivery_effect(
    state_path: Path, item: dict[str, Any], mode: str,
) -> dict[str, Any] | None:
    automatic = _reviewed_automatic_gitops(
        _repo_root(state_path), item["head_commit"], item["remote_url"]
    )
    if automatic is None:
        return None
    _expect(mode == "publish",
            "publication adoption cannot retroactively authorize CI/render/Argo effects")
    argocd_raw = shutil.which("argocd")
    _expect(argocd_raw is not None,
            "automatic GitOps publication preview requires the Argo CD CLI")
    argocd_path = str(Path(argocd_raw).resolve())
    _expect(Path(argocd_path).name.casefold() == "argocd",
            "automatic GitOps preview resolved an unexpected executable")
    argocd_sha = _file_sha(Path(argocd_path))
    _validate_operational_executable_trust(
        [argocd_path], argocd_sha, {},
        "automatic GitOps publication preview", "gitops-auto-sync-observe-v1",
    )
    resolved_targets = [
        _argocd_effect_target(state_path, argocd_path, target)
        for target in automatic["targets"]
    ]
    base = {
        "kind": automatic["kind"],
        "declaration_sha256": _value_sha(automatic),
        "argocd_executable_path": argocd_path,
        "argocd_executable_sha256": argocd_sha,
        "cascade_steps": automatic["cascade_steps"],
        "targets": resolved_targets,
    }
    if automatic["kind"] == "ci-render-argocd-auto-sync-v1":
        _expect(automatic["ci_render"]["source_ref"] == item["branch"],
                "automatic GitOps CI source_ref must equal the exact published branch")
        render = automatic["render"]
        render_head = _remote_branch_head(
            render["remote"], render["branch"], "automatic GitOps render precondition"
        )
        effect = {
            **base,
            "render": {**render, "expected_remote_head": render_head},
            "ci_render": automatic["ci_render"],
        }
    else:
        # Fanout: the render is triggered by the chart-bump commit landing on the chart
        # branch, NOT by the source push -- so every leg's CI source_ref binds the chart
        # branch, and each deploy leg has its own live render precondition.
        chart_branch = automatic["chart"]["branch"]
        resolved_legs = []
        for leg in automatic["render_legs"]:
            _expect(leg["ci_render"]["source_ref"] == chart_branch,
                    f"automatic GitOps fanout leg {leg['id']!r} CI source_ref must equal the chart branch")
            leg_head = _remote_branch_head(
                leg["remote"], leg["branch"],
                f"automatic GitOps render precondition ({leg['id']})",
            )
            resolved_legs.append({**leg, "expected_remote_head": leg_head})
        effect = {
            **base,
            "image_build": automatic["image_build"],
            "chart": automatic["chart"],
            "render_legs": resolved_legs,
        }
    return _validate_delivery_effect(effect, "publication delivery effect")


def _publication_candidate(
    state_path: Path, item: dict[str, Any], mode: str
) -> tuple[dict[str, Any], list[str] | None, list[str], dict[str, Any]]:
    _expect(mode in {"publish", "adopt-preexisting"}, "publication mode is invalid")
    ref = f"refs/heads/{item['branch']}"
    isolation_home = _state_dir(state_path) / "artifacts" / "publication" / "isolated-home"
    environment, known_hosts_path, known_hosts_sha = _publication_environment(
        item["remote_url"], isolation_home
    )
    git_raw = shutil.which("git")
    _expect(git_raw is not None, "publication writer: git executable is unavailable")
    git_path = str(Path(git_raw).resolve())
    sandbox_path, object_directory = _prepare_publication_sandbox(
        state_path, git_path, environment
    )
    environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = object_directory
    observed, observe_argv, result = _publication_observe(
        state_path, item["remote_url"], ref, git_path=git_path,
        environment=environment
    )
    intended = item["head_commit"].lower()
    if mode == "publish":
        _expect(observed != intended,
                "publication preview: target is already published; use explicit adoption mode")
        if observed is not None:
            exists = subprocess.run(
                ["git", "-C", str(_repo_root(state_path)), "cat-file", "-e",
                 f"{observed}^{{commit}}"], capture_output=True,
            )
            _expect(exists.returncode == 0,
                    "publication preview: remote head is not in the local reviewed object graph; fetch and re-preview")
            ancestor = subprocess.run(
                ["git", "-C", str(_repo_root(state_path)), "merge-base", "--is-ancestor",
                 observed, intended], capture_output=True,
            )
            _expect(ancestor.returncode == 0,
                    "publication preview: normal publication must be a fast-forward; non-FF rewrites are unsupported")
    else:
        _expect(observed == intended,
                "publication adoption requires the exact reviewed commit already on the branch")
    scope = {
        "mode": mode,
        "repo": item["repo"], "remote": "origin", "remote_url": item["remote_url"],
        "branch": item["branch"], "commit": intended,
        "expected_remote_head": observed,
        "git_executable_path": observe_argv[0],
        "git_executable_sha256": _file_sha(Path(observe_argv[0])),
        "execution_environment": environment,
        "isolated_git_dir": sandbox_path,
        "alternate_object_directory": object_directory,
        "ssh_known_hosts_path": known_hosts_path,
        "ssh_known_hosts_sha256": known_hosts_sha,
        "push_argv": None,
    }
    delivery_effect = _resolve_delivery_effect(state_path, item, mode)
    if delivery_effect is not None:
        scope["delivery_effect"] = delivery_effect
    action = None if mode == "adopt-preexisting" else [
        observe_argv[0], "-c", "core.hooksPath=/dev/null",
        f"--git-dir={sandbox_path}", "push",
        f"--force-with-lease={ref}:{observed or ''}", "--",
        item["remote_url"], f"{intended}:{ref}",
    ]
    scope["push_argv"] = action
    return scope, action, observe_argv, result


def _publication_observation_record(
    scope: dict[str, Any], argv: list[str], result: dict[str, Any], now: datetime
) -> dict[str, Any]:
    stdout = _redact_output(result["stdout_bytes"])
    stderr = _redact_output(result["stderr_bytes"])
    return {
        "schema_version": 1, "producer": _producer(), "argv": argv,
        "command": shlex.join(argv), "environment": scope["execution_environment"],
        "executable_path": scope["git_executable_path"],
        "executable_sha256": scope["git_executable_sha256"],
        "remote_url": scope["remote_url"],
        "ref": f"refs/heads/{scope['branch']}",
        "observed_commit": scope["expected_remote_head"], "observed_at": _utc_text(now),
        "exit_code": result["exit_code"], "stdout": stdout, "stderr": stderr,
        "stdout_sha256": _persisted_text_sha(stdout),
        "stderr_sha256": _persisted_text_sha(stderr),
        "stdout_truncated": result["stdout_truncated"],
        "stderr_truncated": result["stderr_truncated"],
        "output_limit_bytes": MAX_CAPTURE_BYTES,
        "background_processes_terminated": result["background_processes_terminated"],
        "timed_out": result["timed_out"],
    }


def _publication_execution_path(
    state_path: Path, intent: dict[str, Any]
) -> Path:
    return (
        _state_dir(state_path) / "artifacts" / "publication"
        / (
            f"execution-g{intent['generation']:04d}-"
            f"{intent['scope_hash'][:12]}.json"
        )
    )


def _publication_same_scope_retry_allowed(
    state_path: Path, intent: dict[str, Any]
) -> bool:
    """Only a durably receipted, nonzero push may receive same-scope reauthorization."""
    attempts = intent.get("execution_attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        return False
    ref_value = attempts[0]
    _validate_evidence_ref(
        ref_value, "publication retry execution", _state_dir(state_path)
    )
    record = _load_json(
        _state_dir(state_path) / ref_value["path"], "publication retry execution"
    )
    exit_code = _validate_publication_execution_record(
        record, intent, "publication retry execution", require_success=False
    )
    return record["result_kind"] == "executed" and exit_code is not None and exit_code != 0


def publication_preview(
    state_path: Path, mode: str, now: datetime
) -> dict[str, Any]:
    """Read-only discovery of the exact scope a human may later authorize."""
    with _state_lock(state_path):
        state, item, path = _publication_context(state_path, now)
        scope, action, _observe, _result = _publication_candidate(state_path, item, mode)
        scope_hash = _value_sha(scope)
        generation = 1
        supersedes_scope_hash: str | None = None
        if path.exists():
            current = _load_json(path, "publication_intent")
            meta = validate_publication_intent(current, state, now)
            prior = state.get("artifact_bindings", {}).get("publication_intent")
            _expect(prior is not None,
                    "publication preview: existing intent is not state-bound")
            _check_prior_binding(
                "publication_intent",
                _binding("publication_intent", path, current, state["phase"], meta),
                prior,
            )
            supersedes_scope_hash = current["scope_hash"]
            _expect(
                scope_hash != supersedes_scope_hash
                or _publication_same_scope_retry_allowed(state_path, current),
                "publication preview: existing authorization still matches current scope; "
                "same-scope reauthorization requires a receipted failed execution",
            )
            generation = current["generation"] + 1
        return {
            "ok": True, "mode": mode, "scope": scope,
            "scope_hash": scope_hash, "proposed_action": action,
            "delivery_effect_sha256": (
                _value_sha(scope["delivery_effect"])
                if scope.get("delivery_effect") is not None else None
            ),
            "execution_environment": scope["execution_environment"],
            "generation": generation,
            "supersedes_scope_hash": supersedes_scope_hash,
        }


def publication_authorize(
    state_path: Path, approved_by: str, expected_scope_hash: str, mode: str,
    now: datetime,
) -> dict[str, Any]:
    """Recheck a previewed scope, then persist explicit human authorization."""
    with _state_lock(state_path):
        state, item, path = _publication_context(state_path, now)
        scope, action, observe_argv, result = _publication_candidate(state_path, item, mode)
        scope_hash = _value_sha(scope)
        _expect(_sha256_value(expected_scope_hash, "--scope-hash") == scope_hash,
                "publication authorize: preview scope changed; inspect a new preview")
        generation = 1
        superseded_intents: list[dict[str, Any]] = []
        if path.exists():
            current = _load_json(path, "publication_intent")
            current_meta = validate_publication_intent(current, state, now)
            prior = state.get("artifact_bindings", {}).get("publication_intent")
            _expect(prior is not None,
                    "publication authorize: existing intent is not state-bound")
            _check_prior_binding(
                "publication_intent",
                _binding(
                    "publication_intent", path, current, state["phase"], current_meta
                ),
                prior,
            )
            _expect(
                current["scope_hash"] != scope_hash
                or _publication_same_scope_retry_allowed(state_path, current),
                "publication authorize: existing authorization still matches current scope; "
                "same-scope reauthorization requires a receipted failed execution",
            )
            archive_path = (
                _state_dir(state_path) / "artifacts" / "publication"
                / (
                    f"superseded-intent-g{current['generation']:04d}-"
                    f"{current['scope_hash'][:12]}.json"
                )
            )
            current_bytes = path.read_bytes()
            if archive_path.exists():
                _expect(archive_path.read_bytes() == current_bytes,
                        "publication authorize: superseded intent archive collision")
            else:
                _save_bytes_atomic(archive_path, current_bytes)
            superseded_intents = [
                *current["superseded_intents"],
                _evidence_ref_from_file(
                    state_path, archive_path, _producer()["name"],
                    "application/json", None,
                ),
            ]
            generation = current["generation"] + 1
        precondition_path = (
            _state_dir(state_path) / "artifacts" / "publication"
            / f"precondition-g{generation:04d}-{scope_hash[:12]}.json"
        )
        record = _publication_observation_record(scope, observe_argv, result, now)
        _save_bytes_atomic(precondition_path, _json_bytes(record))
        evidence = _evidence_ref_from_file(
            state_path, precondition_path, _producer()["name"], "application/json",
            shlex.join(observe_argv),
        )
        intent = {
            "schema_version": ARTIFACT_SCHEMA_VERSION, "milestone_id": state["id"],
            "generation": generation, "created_at": _utc_text(now), "producer": _producer(),
            "intent_id": (
                f"{state['id']}-publication-g{generation:04d}-{scope_hash[:12]}"
            ),
            "scope": scope, "scope_hash": scope_hash,
            "precondition": {
                "observed_commit": scope["expected_remote_head"],
                "observed_at": _utc_text(now), "evidence": evidence,
            },
            "authorization": {
                "decision": "approved" if mode == "publish" else "acknowledged",
                "by": _human_name(approved_by, "--approved-by"),
                "method": "human-explicit", "at": _utc_text(now),
                "scope_hash": scope_hash,
            },
            "superseded_intents": superseded_intents,
            "execution_attempts": [],
        }
        meta = validate_publication_intent(intent, state, now)
        _commit_mutable_artifact("publication_intent", state_path, state, path, intent, meta, now)
        return {
            "ok": True, "intent_id": intent["intent_id"], "mode": mode,
            "scope_hash": scope_hash, "authorized_action": action,
            "delivery_effect_sha256": (
                _value_sha(scope["delivery_effect"])
                if scope.get("delivery_effect") is not None else None
            ),
        }


def publication_apply(state_path: Path, now: datetime) -> dict[str, Any]:
    """Execute the authorized CAS push (or adoption observation) and receipt it."""
    with _state_lock(state_path):
        # Rehydrate the closure-bound implementation and re-run the same
        # commit-bound policy preflight as preview/authorization. An intent
        # created by an earlier kit must never bypass a newly required policy.
        state, item, _intent_pointer = _publication_context(state_path, now)
        intent_path, intent, meta = _load_and_validate(
            "publication_intent", state, state_path, now
        )
        prior = state.get("artifact_bindings", {}).get("publication_intent")
        _expect(prior is not None, "publication-apply: intent is not state-bound")
        _check_prior_binding(
            "publication_intent",
            _binding("publication_intent", intent_path, intent, state["phase"], meta), prior,
        )
        scope = intent["scope"]
        _publication_scope_matches_implementation(scope, item)
        ref = f"refs/heads/{scope['branch']}"
        _expect(Path(scope["git_executable_path"]).is_file()
                and _file_sha(Path(scope["git_executable_path"]))
                == scope["git_executable_sha256"],
                "publication-apply: frozen git executable identity changed")
        if scope["ssh_known_hosts_path"] is not None:
            known_hosts = Path(scope["ssh_known_hosts_path"])
            _expect(known_hosts.is_file() and not known_hosts.is_symlink()
                    and _file_sha(known_hosts) == scope["ssh_known_hosts_sha256"],
                    "publication-apply: SSH known-host identity changed")
        _validate_publication_local_git_config(state_path)
        observed_before, observe_argv, before_result = _publication_observe(
            state_path, scope["remote_url"], ref,
            git_path=scope["git_executable_path"],
            environment=scope["execution_environment"],
        )
        if scope.get("delivery_effect") is not None and observed_before != scope["commit"]:
            current_effect = _resolve_delivery_effect(state_path, item, scope["mode"])
            _expect(current_effect == scope["delivery_effect"],
                    "publication-apply: conditional CI/render/Argo effect drifted after authorization")
        execution_ref: dict[str, Any] | None = None
        execution_exit: int | None = None
        if scope["mode"] == "publish":
            action = list(scope["push_argv"])
            execution_path = _publication_execution_path(state_path, intent)
            if execution_path.exists():
                execution_record = _load_json(execution_path, "publication execution")
                execution_exit = _validate_publication_execution_record(
                    execution_record, intent, "publication execution",
                    require_success=False,
                )
            else:
                started_at = _utc_text(now)
                if observed_before == scope["commit"]:
                    # The process may have died after the authorized CAS push
                    # but before writing its receipt. Preserve that ambiguity;
                    # do not fabricate an execution result or replay the push.
                    execution_exit = None
                    stdout = ""
                    stderr = ""
                    execution_record = {
                        "schema_version": 1, "producer": intent["producer"],
                        "intent_id": intent["intent_id"],
                        "intent_generation": intent["generation"],
                        "scope_hash": intent["scope_hash"],
                        "result_kind": "ambiguous-observed-success", "argv": action,
                        "command": shlex.join(action),
                        "environment": scope["execution_environment"],
                        "executable_path": action[0],
                        "executable_sha256": _file_sha(Path(action[0])),
                        "started_at": started_at, "completed_at": _utc_text(now),
                        "exit_code": None, "stdout": stdout, "stderr": stderr,
                        "stdout_sha256": _persisted_text_sha(stdout),
                        "stderr_sha256": _persisted_text_sha(stderr),
                        "stdout_truncated": False, "stderr_truncated": False,
                        "output_limit_bytes": MAX_CAPTURE_BYTES,
                        "background_processes_terminated": False, "timed_out": False,
                    }
                else:
                    _expect(observed_before == scope["expected_remote_head"],
                            "publication-apply: remote precondition changed after authorization")
                    sandbox_path, object_directory = _prepare_publication_sandbox(
                        state_path, scope["git_executable_path"],
                        scope["execution_environment"],
                    )
                    _expect(
                        sandbox_path == scope["isolated_git_dir"]
                        and object_directory == scope["alternate_object_directory"],
                        "publication-apply: isolated object source changed after authorization",
                    )
                    execution_result = _run_bounded_process(
                        action, cwd=Path(scope["execution_environment"]["HOME"]), timeout=120,
                        env=scope["execution_environment"],
                    )
                    execution_exit = execution_result["exit_code"]
                    stdout = _redact_output(execution_result["stdout_bytes"])
                    stderr = _redact_output(execution_result["stderr_bytes"])
                    execution_record = {
                        "schema_version": 1, "producer": intent["producer"],
                        "intent_id": intent["intent_id"],
                        "intent_generation": intent["generation"],
                        "scope_hash": intent["scope_hash"], "result_kind": "executed",
                        "argv": action, "command": shlex.join(action),
                        "environment": scope["execution_environment"],
                        "executable_path": action[0],
                        "executable_sha256": _file_sha(Path(action[0])),
                        "started_at": started_at, "completed_at": _utc_text(now),
                        "exit_code": execution_exit, "stdout": stdout, "stderr": stderr,
                        "stdout_sha256": _persisted_text_sha(stdout),
                        "stderr_sha256": _persisted_text_sha(stderr),
                        "stdout_truncated": execution_result["stdout_truncated"],
                        "stderr_truncated": execution_result["stderr_truncated"],
                        "output_limit_bytes": MAX_CAPTURE_BYTES,
                        "background_processes_terminated": execution_result[
                            "background_processes_terminated"
                        ], "timed_out": execution_result["timed_out"],
                    }
                _save_bytes_atomic(execution_path, _json_bytes(execution_record))
            execution_ref = _evidence_ref_from_file(
                state_path, execution_path, intent["producer"]["name"], "application/json",
                shlex.join(action),
            )
            if not intent["execution_attempts"]:
                intent["execution_attempts"].append(execution_ref)
                meta = validate_publication_intent(intent, state, now)
                _commit_mutable_artifact(
                    "publication_intent", state_path, state, intent_path,
                    intent, meta, now,
                )
            else:
                _expect(intent["execution_attempts"] == [execution_ref],
                        "publication-apply: execution receipt differs from state-bound attempt")
        else:
            _expect(observed_before == scope["commit"],
                    "publication adoption: reviewed commit is no longer published")
        observed_after, post_argv, post_result = _publication_observe(
            state_path, scope["remote_url"], ref,
            git_path=scope["git_executable_path"],
            environment=scope["execution_environment"],
        )
        if observed_after != scope["commit"]:
            _expect(scope["mode"] == "publish" and execution_ref is not None,
                    "publication-apply: adoption postcondition changed unexpectedly")
            return {
                "ok": False, "mode": scope["mode"],
                "scope_hash": intent["scope_hash"],
                "execution_exit_code": execution_exit,
                "execution_evidence": execution_ref,
                "observed_commit": observed_after,
                "failure_reason": "publication postcondition does not equal reviewed commit",
            }
        post_scope = dict(scope)
        post_scope["expected_remote_head"] = observed_after
        post_path = (
            _state_dir(state_path) / "artifacts" / "publication"
            / (
                f"postcondition-g{intent['generation']:04d}-"
                f"{intent['scope_hash'][:12]}.json"
            )
        )
        post_record = _publication_observation_record(post_scope, post_argv, post_result, now)
        _save_bytes_atomic(post_path, _json_bytes(post_record))
        post_ref = _evidence_ref_from_file(
            state_path, post_path, _producer()["name"], "application/json",
            shlex.join(post_argv),
        )
        return {
            "ok": True, "mode": scope["mode"], "scope_hash": intent["scope_hash"],
            "delivery_effect_sha256": (
                _value_sha(scope["delivery_effect"])
                if scope.get("delivery_effect") is not None else None
            ),
            "execution_exit_code": execution_exit,
            "execution_evidence": execution_ref,
            "verification": {
                "method": "git-ls-remote+exact-commit",
                "publication_mode": scope["mode"],
                "execution_evidence": execution_ref,
                "verified_at": _utc_text(now),
                "observed_commit": observed_after, "source_matches_published": True,
                "exit_code": 0, "evidence": post_ref,
            },
        }


def review_append(
    state_path: Path, stage: str, receipt_path: Path, now: datetime
) -> dict[str, Any]:
    field_by_stage = {
        "closure": ("closure_reviews", "rectify-running", "milestone-closure-verifier"),
        "operations": (
            "operations_reviews", "plan-review-running", "milestone-operations-adversary"
        ),
    }
    _expect(stage in field_by_stage, "review-append: stage must be closure or operations")
    field, required_phase, required_role = field_by_stage[stage]
    with _state_lock(state_path):
        _recover_pending_transactions(state_path)
        state = _load_state(state_path)
        state["_state_path"] = str(state_path.resolve())
        _validate_state(state, state_path)
        _expect(now >= _iso(state.get("updated_at"), "state.updated_at"),
                "review-append timestamp precedes state.updated_at")
        _expect(state["phase"] == required_phase,
                f"review-append {stage} requires {required_phase}, got {state['phase']!r}")
        receipt_resolved = receipt_path.expanduser().resolve()
        reviews_root = (_state_dir(state_path) / "artifacts" / "reviews").resolve()
        try:
            receipt_resolved.relative_to(reviews_root)
        except ValueError:
            _fail(f"review-append receipt must be below {reviews_root}")
        _expect(receipt_resolved.name.endswith("-receipt.json"),
                "review-append receipt filename must end with -receipt.json")
        receipt = _load_json(receipt_resolved, "review receipt")
        _expect(
            receipt.get("agent_kit_commit") == state["agent_kit_commit"],
            "review-append: new receipts must be produced by the currently authorized kit; "
            "historical lineage is accepted only for already-appended immutable receipts",
        )
        _expect(receipt.get("stage") == stage,
                f"review-append receipt.stage must be {stage!r}")
        _expect(receipt.get("role") == required_role,
                f"review-append receipt.role must be {required_role!r}")
        manifest_path = _artifact_file(state_path, state, "review_manifest")
        manifest = _load_json(manifest_path, "review_manifest")
        current_meta = validate_review_manifest(
            manifest, state, state_path, treat_latest_as_historical=True, now=now
        )
        prior = state.get("artifact_bindings", {}).get("review_manifest")
        _expect(prior is not None, "review_manifest: missing pre-append state binding")
        _check_prior_binding(
            "review_manifest",
            _binding(
                "review_manifest", manifest_path, manifest, state["phase"], current_meta
            ),
            prior,
        )
        attempts = manifest.get(field)
        _expect(isinstance(attempts, list), f"review_manifest.{field}: expected array")
        attempts.append(receipt)
        meta = validate_review_manifest(manifest, state, state_path, now=now)
        _commit_mutable_artifact(
            "review_manifest", state_path, state, manifest_path, manifest, meta, now
        )
        return {
            "ok": True,
            "stage": stage,
            "attempt": len(attempts),
            "verdict": receipt.get("verdict"),
            "receipt_hash": _value_sha(receipt),
        }


def run_check(
    state_path: Path, name: str, argv: list[str], timeout_seconds: int
) -> dict[str, Any]:
    with _state_lock(state_path):
        _recover_pending_transactions(state_path)
        state = _load_state(state_path)
        state["_state_path"] = str(state_path.resolve())
        _validate_state(state, state_path)
        _expect(state["phase"] == "rectify-running",
                f"check-run requires rectify-running, got {state['phase']!r}")
        repo = _repo_root(state_path)
        check_name = _nonempty_string(name, "--name")
        command_argv = _check_command_argv(argv, "check argv")
        if "/" in command_argv[0] and not os.path.isabs(command_argv[0]):
            candidate = (repo / command_argv[0]).resolve()
            try:
                candidate.relative_to(repo.resolve())
            except ValueError:
                _fail("check argv[0]: repo-relative executable escapes target repository")
            _expect(candidate.is_file() and os.access(candidate, os.X_OK),
                    f"check argv[0]: repo-relative executable is missing/not executable: {candidate}")
            executable_path, executable_sha = str(candidate), _file_sha(candidate)
        else:
            executable_path, executable_sha = _resolved_executable(
                command_argv, "check argv", require_absolute=False
            )
        tracked_input_hashes, repo_executable = _check_command_inputs(
            repo, command_argv, executable_path
        )
        runtime_interpreter = _runtime_interpreter(executable_path)
        setup_spec = _check_setup_spec(
            repo, command_argv, executable_path, executable_sha
        )
        timeout = _integer(timeout_seconds, "--timeout", 1)
        _expect(timeout <= 3600, "--timeout: cannot exceed 3600 seconds")
        head_before = _commit(
            _git_output(repo, "rev-parse", "HEAD").decode().strip(), "check-run HEAD"
        )
        expected_head = _commit(
            state.get("rectification_commit")
            or (state.get("implementation_commits") or [None])[-1],
            "state final implementation commit",
        )
        _expect(head_before == expected_head,
                "check-run: repository HEAD is not the final implementation commit")
        status_before = _worktree_status(repo, state_path)
        _expect(
            not status_before.strip(),
            "check-run: tracked worktree/index and untracked files outside the milestone "
            "state directory must be clean",
        )
        started = datetime.now(timezone.utc)
        _expect(
            started >= _iso(state.get("updated_at"), "state.updated_at"),
            "check-run clock precedes state.updated_at",
        )
        for i, part in enumerate(command_argv[1:], start=1):
            candidates = [part]
            if "=" in part:
                candidates.append(part.split("=", 1)[1])
            for candidate_raw in candidates:
                if not os.path.isabs(candidate_raw):
                    continue
                candidate = Path(candidate_raw).resolve()
                try:
                    candidate.relative_to(repo.resolve())
                except ValueError:
                    continue
                _fail(
                    f"check argv[{i}]: absolute target-repository path forbidden; "
                    "checks execute in a detached worktree and must use repo-relative inputs"
                )
        timed_out = False
        state_tree_before = _tree_sha256(_state_dir(state_path))
        execution_status_after = ""
        execution_head_after = head_before
        setup_record: dict[str, Any] | None = None
        # Execute from a detached Git worktree containing committed source
        # only. No ignored dependency/cache tree is mounted; projects needing
        # setup must use a reviewed tracked wrapper that installs from their
        # lockfile inside this disposable worktree.
        with tempfile.TemporaryDirectory(prefix=f".{repo.name}-milestone-check-") as td:
            checkout = Path(td) / "worktree"
            added = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--detach", "--quiet",
                 str(checkout), head_before],
                capture_output=True,
            )
            _expect(added.returncode == 0,
                    "check-run: cannot create detached execution worktree: "
                    + added.stderr.decode(errors="replace").strip())
            try:
                execution_executable = (
                    str((checkout / repo_executable).resolve())
                    if repo_executable is not None else executable_path
                )
                execution_argv = [execution_executable, *command_argv[1:]]
                check_home = Path(td) / "home"
                check_tmp = Path(td) / "tmp"
                check_home.mkdir()
                check_tmp.mkdir()
                toolchain_dirs = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
                for interpreter in (
                    runtime_interpreter,
                    setup_spec.get("runtime_interpreter") if setup_spec else None,
                ):
                    if interpreter is not None:
                        toolchain_dirs.insert(0, str(Path(interpreter["path"]).parent))
                toolchain_dirs = list(dict.fromkeys(toolchain_dirs))
                check_environment = {
                    "PATH": os.pathsep.join(toolchain_dirs),
                    "HOME": str(check_home),
                    "TMPDIR": str(check_tmp),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PYTHONNOUSERSITE": "1",
                }
                setup_failed = False
                execution_result: dict[str, Any] | None = None
                if setup_spec is not None:
                    setup_result = _run_bounded_process(
                        setup_spec["argv"], cwd=checkout, timeout=timeout,
                        env=check_environment,
                    )
                    setup_exit = setup_result["exit_code"]
                    setup_timed_out = setup_result["timed_out"]
                    setup_record = {
                        **setup_spec,
                        "exit_code": setup_exit,
                        "stdout": _redact_output(setup_result["stdout_bytes"]),
                        "stderr": _redact_output(setup_result["stderr_bytes"]),
                        "stdout_truncated": setup_result["stdout_truncated"],
                        "stderr_truncated": setup_result["stderr_truncated"],
                        "output_limit_bytes": MAX_CAPTURE_BYTES,
                        "background_processes_terminated": setup_result[
                            "background_processes_terminated"
                        ],
                        "timed_out": setup_timed_out,
                    }
                    setup_record["stdout_sha256"] = _persisted_text_sha(
                        setup_record["stdout"]
                    )
                    setup_record["stderr_sha256"] = _persisted_text_sha(
                        setup_record["stderr"]
                    )
                    setup_failed = setup_exit != 0 or setup_timed_out
                if setup_failed:
                    timed_out = bool(setup_record and setup_record["timed_out"])
                    exit_code = int(setup_record["exit_code"] if setup_record else 1)
                    stdout = b""
                    stderr = b"detached dependency setup failed; see setup receipt"
                else:
                    execution_result = _run_bounded_process(
                        execution_argv, cwd=checkout, timeout=timeout,
                        env=check_environment,
                    )
                    timed_out = execution_result["timed_out"]
                    exit_code = execution_result["exit_code"]
                    stdout = execution_result["stdout_bytes"]
                    stderr = execution_result["stderr_bytes"]
                execution_head_after = _commit(
                    _git_output(checkout, "rev-parse", "HEAD").decode().strip(),
                    "check-run execution HEAD",
                )
                execution_status_after = _git_output(
                    checkout, "status", "--porcelain", "--untracked-files=all"
                ).decode("utf-8", errors="surrogateescape")
            finally:
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "remove", "--force", str(checkout)],
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "prune"], capture_output=True
                )
        completed = datetime.now(timezone.utc)
        head_after = _commit(
            _git_output(repo, "rev-parse", "HEAD").decode().strip(), "check-run final HEAD"
        )
        status_after = _worktree_status(repo, state_path)
        state_tree_after = _tree_sha256(_state_dir(state_path))
        if execution_head_after != head_before:
            status_after += f"execution HEAD moved to {execution_head_after}\n"
        if execution_status_after.strip():
            status_after += "detached execution worktree changed:\n" + execution_status_after
        if state_tree_after != state_tree_before:
            status_after += "milestone state tree changed during detached check\n"
        command = shlex.join(command_argv)
        record = {
            "schema_version": 1,
            "producer": _producer(),
            "name": check_name,
            "argv": command_argv,
            "command": command,
            "executable_path": executable_path,
            "executable_sha256": executable_sha,
            "tracked_input_hashes": tracked_input_hashes,
            "runtime_interpreter": runtime_interpreter,
            "setup": setup_record,
            "environment": check_environment,
            "repo_root": str(repo.resolve()),
            "head_before": head_before,
            "head_after": head_after,
            "tracked_status_before": status_before,
            "tracked_status_after": status_after,
            "execution_mode": "detached-git-worktree",
            "execution_head_after": execution_head_after,
            "execution_status_after": execution_status_after,
            "state_tree_sha256_before": state_tree_before,
            "state_tree_sha256_after": state_tree_after,
            "started_at": _utc_text(started),
            "completed_at": _utc_text(completed),
            "exit_code": exit_code,
            "stdout": _redact_output(stdout),
            "stderr": _redact_output(stderr),
            "stdout_truncated": (
                execution_result["stdout_truncated"] if execution_result is not None else False
            ),
            "stderr_truncated": (
                execution_result["stderr_truncated"] if execution_result is not None else False
            ),
            "output_limit_bytes": MAX_CAPTURE_BYTES,
            "background_processes_terminated": (
                execution_result["background_processes_terminated"]
                if execution_result is not None else False
            ),
            "timed_out": timed_out,
        }
        record["stdout_sha256"] = _persisted_text_sha(record["stdout"])
        record["stderr_sha256"] = _persisted_text_sha(record["stderr"])
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", check_name).strip("-") or "check"
        run_stamp = started.strftime("%Y%m%dT%H%M%S%fZ")
        evidence_path = (
            _state_dir(state_path) / "artifacts" / "checks"
            / f"{slug}-{head_before[:12]}-{run_stamp}.json"
        )
        evidence_bytes = (
            json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        evidence_rel = str(evidence_path.relative_to(_state_dir(state_path)))
        evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
        evidence_ref = {
            "path": evidence_rel,
            "sha256": evidence_sha,
            "media_type": "application/json",
            "size_bytes": len(evidence_bytes),
            "collector": _producer()["name"],
            "command": command,
        }
        receipt = {
            "name": check_name,
            "argv": command_argv,
            "command": command,
            "repo_head": head_before,
            "executable_path": executable_path,
            "executable_sha256": executable_sha,
            "exit_code": exit_code,
            "started_at": record["started_at"],
            "completed_at": record["completed_at"],
            "evidence": evidence_ref,
        }
        expected_state_sha = state.get("_loaded_state_sha256")
        _expect(isinstance(expected_state_sha, str)
                and _file_sha(state_path) == expected_state_sha,
                "check-run: state changed concurrently during command execution")
        state_before_sha = expected_state_sha
        persisted = _persisted_state(state)
        _apply_check_record_to_state(
            persisted, {"path": evidence_rel, "sha256": evidence_sha}, record, state_path
        )
        state_after_bytes = _json_bytes(persisted)
        journal = {
            "schema_version": 1,
            "evidence_path": evidence_rel,
            "evidence_sha256": evidence_sha,
            "state_before_sha256": state_before_sha,
            "state_after_sha256": hashlib.sha256(state_after_bytes).hexdigest(),
            "state_after": persisted,
        }
        _save_json_atomic(_check_transaction_path(state_path), journal)
        _save_bytes_atomic(evidence_path, evidence_bytes)
        if TEST_FAIL_AFTER_CHECK_EVIDENCE_WRITE:
            raise ValidationError("simulated failure after check evidence write")
        _save_bytes_atomic(state_path, state_after_bytes)
        _clear_check_transaction(state_path)
        _expect(head_after == head_before,
                "check-run: command changed repository HEAD; evidence ledgered but rejected")
        _expect(
            not status_after.strip(),
            "check-run: command changed tracked worktree/index or created an untracked file "
            "outside the milestone state directory; evidence ledgered but rejected",
        )
        return receipt


def _target_record(data: dict[str, Any], target_id: str, label: str) -> dict[str, Any]:
    matches = [t for t in data.get("targets", []) if isinstance(t, dict) and t.get("id") == target_id]
    _expect(len(matches) == 1, f"{label}: target {target_id!r} not found exactly once")
    return matches[0]


def _check_mutable_binding(
    kind: str, state: dict[str, Any], path: Path, data: dict[str, Any], meta: dict[str, Any],
    *, allow_append: bool = False,
) -> None:
    prior = state.get("artifact_bindings", {}).get(kind)
    if prior is not None:
        _expect(path.is_file(), f"{kind}: previously bound artifact is missing")
        current = _binding(kind, path, data, state["phase"], meta)
        _check_prior_binding(kind, current, prior, allow_append=allow_append)


def _commit_mutable_artifact(
    kind: str, state_path: Path, state: dict[str, Any], path: Path,
    data: dict[str, Any], meta: dict[str, Any], now: datetime,
) -> None:
    expected_state_sha = state.get("_loaded_state_sha256")
    _expect(isinstance(expected_state_sha, str)
            and _file_sha(state_path) == expected_state_sha,
            f"{kind}: state changed concurrently before append")
    prior_binding = state.get("artifact_bindings", {}).get(kind)
    if prior_binding is not None:
        _expect(path.is_file() and _file_sha(path) == prior_binding.get("sha256"),
                f"{kind}: artifact changed concurrently before append")
    _check_mutable_binding(kind, state, path, data, meta, allow_append=True)
    if kind == "operations_evidence":
        state["operational_status"] = _operational_status_projection(
            meta, state["phase"]
        )
    artifact_bytes = _json_bytes(data)
    artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
    state_before_sha = expected_state_sha
    state.setdefault("artifact_bindings", {})[kind] = _binding(
        kind, path, data, state["phase"], meta, content_sha256=artifact_sha
    )
    state["updated_at"] = _utc_text(now)
    persisted = _persisted_state(state)
    state_after_bytes = _json_bytes(persisted)
    journal = {
        "schema_version": 1,
        "kind": kind,
        "artifact_path": f"artifacts/{POINTERS[kind]}",
        "artifact_sha256": artifact_sha,
        "state_before_sha256": state_before_sha,
        "state_after_sha256": hashlib.sha256(state_after_bytes).hexdigest(),
        "state_after": persisted,
    }
    _save_json_atomic(_transaction_path(state_path), journal)
    _save_bytes_atomic(path, artifact_bytes)
    if TEST_FAIL_AFTER_ARTIFACT_WRITE:
        raise ValidationError("self-test crash after mutable artifact write")
    _save_bytes_atomic(state_path, state_after_bytes)
    state["_loaded_state_sha256"] = hashlib.sha256(state_after_bytes).hexdigest()
    _clear_transaction(state_path)


def _operational_status_projection(meta: dict[str, Any], phase: str) -> str:
    statuses = meta.get("statuses")
    _expect(isinstance(statuses, dict) and bool(statuses),
            "operations evidence status projection requires target statuses")
    values = list(statuses.values())
    if any(value == "failed" for value in values):
        return "failed"
    if phase == "apply-running":
        return (
            "applied"
            if all(value in {"applied", "verified"} for value in values)
            else "applying"
        )
    # Verification success becomes a top-level `verified`/`waived` claim only
    # when the operationally-verified gate is checkpointed. Until then the
    # system has applied content whose evidence is still under evaluation.
    if phase == "verify-running":
        return "applied"
    return "applied" if all(
        value in {"applied", "verified"} for value in values
    ) else "applying"


def _target_action_preview(plan: dict[str, Any], target_id: str) -> dict[str, Any]:
    planned = _target_record(plan, target_id, "operation preview")
    return {
        "target": {
            key: planned[key]
            for key in ("id", "environment", "account", "cluster", "resource", "desired")
        },
        "environment": dict(planned["execution_environment"]),
        "execution_contexts": dict(planned["execution_contexts"]),
        "verification_profile": dict(planned["verification_profile"]),
        "apply_method": planned["apply_method"],
        "auto_sync_binding": planned.get("auto_sync_binding"),
        "apply_argv": (
            list(planned["apply_command"])
            if planned["apply_command"] is not None else None
        ),
        "apply_timeout_seconds": planned["apply_timeout_seconds"],
        "post_apply_observation_argv": list(planned["observation_command"]),
        "post_apply_observation_timeout_seconds": planned["observation_timeout_seconds"],
        "verification": [
            {
                "kind": probe["kind"], "argv": list(probe["command"]),
                "timeout_seconds": probe["timeout_seconds"],
            }
            for probe in planned["verification_contract"]
        ],
    }


def _verification_action_preview(planned: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": {
            key: planned[key]
            for key in ("id", "environment", "account", "cluster", "resource", "desired")
        },
        "environment": dict(planned["execution_environment"]),
        "execution_contexts": dict(planned["execution_contexts"]),
        "verification_profile": dict(planned["verification_profile"]),
        "observation": {
            "argv": list(planned["observation_command"]),
            "executable_sha256": planned["observation_executable_sha256"],
            "timeout_seconds": planned["observation_timeout_seconds"],
        },
        "probes": [
            {
                "kind": probe["kind"], "argv": list(probe["command"]),
                "executable_sha256": probe["executable_sha256"],
                "timeout_seconds": probe["timeout_seconds"],
            }
            for probe in planned["verification_contract"]
        ],
    }


def _verification_refresh_scope(
    plan: dict[str, Any], planned: dict[str, Any], target: dict[str, Any],
    attempt: dict[str, Any],
) -> dict[str, Any]:
    intents = target["verification_refresh_intents"]
    refreshes = target["verification_refreshes"]
    _expect(len(intents) == len(refreshes),
            "verification refresh: prior authorization intent is unresolved; recover it first")
    return {
        "kind": "verification-refresh",
        "plan_hash": plan["plan_hash"],
        "target_scope_hash": target_scope_hash(plan, planned),
        "target_id": target["id"],
        "attempt_id": attempt["attempt_id"],
        "source_attempt_sha256": _value_sha(attempt),
        "sequence": len(intents) + 1,
        "previous_intent_sha256": _value_sha(intents[-1]) if intents else None,
        "previous_refresh_sha256": _value_sha(refreshes[-1]) if refreshes else None,
        "authorized_action": _verification_action_preview(planned),
    }


def attempt_preview(
    state_path: Path, target_id: str, now: datetime, attempt_id: str | None = None,
) -> dict[str, Any]:
    with _state_lock(state_path):
        state, plan, pmeta = _writer_context(
            state_path, now, {"apply-running", "verify-running"}
        )
        scope = pmeta["scopes"].get(target_id)
        _expect(scope is not None, f"operation preview: unknown target {target_id!r}")
        if state["phase"] == "verify-running":
            path = _artifact_file(state_path, state, "operations_evidence")
            evidence = _load_json(path, "operations_evidence")
            meta = validate_operations_evidence(
                evidence, state, plan, now, _state_dir(state_path),
                enforce_latest_freshness=False,
            )
            _check_mutable_binding(
                "operations_evidence", state, path, evidence, meta
            )
            _expect(attempt_id is not None,
                    "verification preview requires --attempt-id")
            target, attempt = _latest_attempt(
                evidence, target_id, attempt_id, "verification preview"
            )
            _expect(attempt["apply"]["status"] == "applied",
                    "verification preview: latest attempt was not applied")
            if attempt["verification"]["status"] == "pending":
                return {
                    "ok": True, "mode": "initial-verification",
                    "approval_required": False, "scope_hash": scope,
                    "authorized_action": _verification_action_preview(
                        _target_record(plan, target_id, "verification preview plan")
                    ),
                }
            refresh_scope = _verification_refresh_scope(
                plan, _target_record(plan, target_id, "verification preview plan"),
                target, attempt,
            )
            return {
                "ok": True, "mode": "verification-refresh",
                "approval_required": True,
                "scope": refresh_scope, "scope_hash": _value_sha(refresh_scope),
                "authorized_action": refresh_scope["authorized_action"],
            }
        planned = _target_record(plan, target_id, "operation preview plan")
        is_auto = planned["apply_method"] == "gitops-auto-sync-observe-v1"
        return {
            "ok": True,
            "mode": "auto-sync-observation" if is_auto else "apply",
            "approval_required": not is_auto,
            "scope_hash": scope,
            "authorized_action": _target_action_preview(plan, target_id),
        }


def attempt_start(
    state_path: Path, target_id: str, approved_by: str,
    expected_scope_hash: str, now: datetime,
) -> dict[str, Any]:
    with _state_lock(state_path):
        state, plan, pmeta = _writer_context(state_path, now, {"apply-running"})
        waiver_path = _artifact_file(state_path, state, "waivers")
        if not waiver_path.exists():
            waivers = _initial_waivers(state, plan, now)
            waiver_meta = validate_waivers(waivers, state, plan, now)
            _commit_mutable_artifact(
                "waivers", state_path, state, waiver_path, waivers, waiver_meta, now
            )
        path = _artifact_file(state_path, state, "operations_evidence")
        if path.is_file():
            evidence = _load_json(path, "operations_evidence")
            existing_meta = validate_operations_evidence(
                evidence, state, plan, now, _state_dir(state_path),
                enforce_latest_freshness=False,
            )
            _check_mutable_binding(
                "operations_evidence", state, path, evidence, existing_meta
            )
        else:
            evidence = _initial_operations_evidence(state, plan, now)
        target = _target_record(evidence, target_id, "attempt-start")
        planned = _target_record(plan, target_id, "attempt-start plan")
        _expect(planned["apply_method"] == "gitops-manual-sync",
                "attempt-start: auto-sync uses attempt-adopt-auto-sync and no second human apply authorization")
        attempts = target["attempts"]
        if attempts:
            latest = attempts[-1]
            _expect(
                _derive_attempt_status(latest) in {"failed", "verified"},
                "attempt-start: latest attempt is unresolved; recover/verify it before "
                "authorizing another live mutation",
            )
        sequence = len(attempts) + 1
        scope = pmeta["scopes"].get(target_id)
        _expect(scope is not None, f"attempt-start: target {target_id!r} is absent from frozen plan")
        _expect(_sha256_value(expected_scope_hash, "--scope-hash") == scope,
                "attempt-start: operation scope differs from the human-reviewed preview")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", target_id).strip("-").lower() or "target"
        attempt_id = f"{slug}-a{sequence:04d}-{scope[:12]}"
        _expect(not any(
            attempt_id == attempt.get("attempt_id")
            for value in evidence["targets"] for attempt in value["attempts"]
        ), f"attempt-start: generated duplicate attempt id {attempt_id}")
        stamp = _utc_text(now)
        attempts.append({
            "attempt_id": attempt_id,
            "sequence": sequence,
            "previous_attempt_sha256": _value_sha(attempts[-1]) if attempts else None,
            "recorded_at": stamp,
            "authorization": {
                "decision": "approved",
                "by": _human_name(approved_by, "--approved-by"),
                "method": "human-explicit",
                "at": stamp,
                "scope_hash": scope,
            },
            "apply": {
                "status": "pending", "at": None, "actor": None, "observed": None,
                "idempotency_key": None, "intent_evidence": None, "evidence": None,
                "observation_evidence": None, "failure_reason": None,
                "recovered_from_ambiguous": None,
            },
            "verification": {
                "status": "pending", "observed_at": None, "observed": None,
                "observation_evidence": None, "probes": [],
            },
        })
        target["status"] = "pending"
        meta = validate_operations_evidence(evidence, state, plan, now, _state_dir(state_path))
        _commit_mutable_artifact(
            "operations_evidence", state_path, state, path, evidence, meta, now
        )
        return {
            "ok": True,
            "target": target_id,
            "attempt_id": attempt_id,
            "scope_hash": scope,
            "authorized_action": _target_action_preview(plan, target_id),
        }


def _load_object(path: Path, label: str) -> dict[str, Any]:
    return _load_json(path, label)


def _evidence_ref_from_file(
    state_path: Path, path: Path, collector: str, media_type: str, command: str | None
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    root = (_state_dir(state_path) / "artifacts").resolve()
    try:
        rel = resolved.relative_to(_state_dir(state_path).resolve())
        resolved.relative_to(root)
    except ValueError:
        _fail(f"evidence file must be below {root}")
    _expect(resolved.is_file(), f"evidence file not found: {resolved}")
    ref: dict[str, Any] = {
        "path": str(rel),
        "sha256": _file_sha(resolved),
        "media_type": _nonempty_string(media_type, "--media-type"),
        "size_bytes": resolved.stat().st_size,
        "collector": _nonempty_string(collector, "--collector"),
    }
    if command is not None:
        ref["command"] = _nonempty_string(command, "--command")
    return ref


def _latest_attempt(
    evidence: dict[str, Any], target_id: str, attempt_id: str, command: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    target = _target_record(evidence, target_id, command)
    _expect(bool(target["attempts"]), f"{command}: target has no attempts")
    attempt = target["attempts"][-1]
    _expect(attempt.get("attempt_id") == attempt_id,
            f"{command}: only the latest attempt may be extended")
    return target, attempt


def _run_frozen_command(
    state_path: Path, target_id: str, attempt_id: str, name: str,
    argv: list[str], executable_sha256: str, timeout_seconds: int, collector: str,
    category: str = "verification",
    execution_environment: dict[str, str] | None = None,
    execution_contexts: dict[str, Any] | None = None,
    extra_env: dict[str, str] | None = None,
    operation_target: dict[str, Any] | None = None,
    verification_kind: str | None = None,
) -> tuple[int, bytes, dict[str, Any]]:
    _resolved_executable(
        argv, f"frozen command {name}", require_absolute=True,
        expected_sha256=executable_sha256,
    )
    safe_target = re.sub(r"[^A-Za-z0-9._-]+", "-", target_id).strip("-") or "target"
    safe_attempt = re.sub(r"[^A-Za-z0-9._-]+", "-", attempt_id).strip("-") or "attempt"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "command"
    _expect(category in {"apply", "verification"},
            "frozen command category must be apply or verification")
    output_path = (
        _state_dir(state_path) / "artifacts" / "operations" / safe_target
        / safe_attempt / category / f"{safe_name}.json"
    )
    command_env = dict(execution_environment or {})
    if extra_env:
        command_env.update(extra_env)
    _recheck_execution_context_files(
        execution_contexts or {}, f"frozen command {name}"
    )
    result = _run_bounded_process(
        argv, cwd=_repo_root(state_path), timeout=timeout_seconds, env=command_env
    )
    exit_code = result["exit_code"]
    raw_stdout = result["stdout_bytes"]
    command_ok = (
        exit_code == 0 and not result["timed_out"]
        and not result["stdout_truncated"] and not result["stderr_truncated"]
        and not result["background_processes_terminated"]
    )
    if name == "adopt-auto-sync":
        _expect(operation_target is not None,
                "frozen command adopt-auto-sync: typed target is required")
        output_capture_policy = "projected-auto-sync-adoption"
        try:
            if not command_ok:
                raise ValidationError("auto-sync observation command did not complete safely")
            parsed = _project_auto_sync_adoption(
                raw_stdout, operation_target, "live auto-sync adoption"
            )
            stdout_text = json.dumps(
                parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        except ValidationError:
            stdout_text = "{}"
            if exit_code == 0:
                exit_code = 65
    elif name in {"observed-identity", "post-apply-observation", "pre-apply-target"}:
        _expect(operation_target is not None,
                f"frozen command {name}: typed target is required")
        output_capture_policy = "projected-observed-identity"
        try:
            if not command_ok:
                raise ValidationError("observation command did not complete safely")
            parsed = _project_observed_identity(
                raw_stdout, operation_target, "live projected observation"
            )
            stdout_text = json.dumps(
                parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        except ValidationError:
            stdout_text = "{}"
            if exit_code == 0:
                exit_code = 65
    elif verification_kind is not None:
        _expect(operation_target is not None,
                f"frozen command {name}: typed target is required")
        output_capture_policy = "projected-verification-fact"
        fact, semantic_ok = _project_probe_fact(
            raw_stdout, operation_target, verification_kind, command_ok,
            f"live {verification_kind} probe",
        )
        stdout_text = json.dumps(
            fact, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        if not semantic_ok and exit_code == 0:
            exit_code = 65
    else:
        output_capture_policy = "omitted"
        stdout_text = ""
    # Operational commands frequently expose credentials in diagnostics. The
    # receipt retains exit/timing/truncation facts but never persists stderr.
    stderr_text = ""
    record = {
        "argv": argv,
        "environment": command_env,
        "exit_code": exit_code,
        "stderr": stderr_text,
        "stdout": stdout_text,
        "stdout_sha256": _persisted_text_sha(stdout_text),
        "stderr_sha256": _persisted_text_sha(stderr_text),
        "stdout_truncated": result["stdout_truncated"],
        "stderr_truncated": result["stderr_truncated"],
        "output_limit_bytes": MAX_CAPTURE_BYTES,
        "background_processes_terminated": result["background_processes_terminated"],
        "timed_out": result["timed_out"],
        "output_capture_policy": output_capture_policy,
    }
    _save_bytes_atomic(output_path, json.dumps(
        record, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8") + b"\n")
    evidence = _evidence_ref_from_file(
        state_path, output_path, collector, "application/json", shlex.join(argv)
    )
    return exit_code, stdout_text.encode("utf-8"), evidence


def _write_apply_intent(
    state_path: Path, state: dict[str, Any], plan: dict[str, Any], target: dict[str, Any],
    attempt_id: str, actor: str, collector: str, now: datetime,
    preflight_evidence: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    scope_hash = target_scope_hash(plan, target)
    idempotency_key = hashlib.sha256(
        (state["id"] + "\n" + plan["plan_hash"] + "\n" + scope_hash + "\n" + attempt_id)
        .encode("utf-8")
    ).hexdigest()
    safe_target = re.sub(r"[^A-Za-z0-9._-]+", "-", target["id"]).strip("-") or "target"
    safe_attempt = re.sub(r"[^A-Za-z0-9._-]+", "-", attempt_id).strip("-") or "attempt"
    path = (
        _state_dir(state_path) / "artifacts" / "operations" / safe_target
        / safe_attempt / "apply" / "intent.json"
    )
    argv = _command_argv(target["apply_command"], "operations_plan.target.apply_command")
    record = {
        "schema_version": 1,
        "producer": _producer(),
        "milestone_id": state["id"],
        "target_id": target["id"],
        "attempt_id": attempt_id,
        "plan_hash": plan["plan_hash"],
        "scope_hash": scope_hash,
        "authorized_actor": actor,
        "idempotency_key": idempotency_key,
        "recorded_at": _utc_text(now),
        "argv": argv,
        "executable_sha256": target["apply_executable_sha256"],
        "timeout_seconds": target["apply_timeout_seconds"],
        "preflight_evidence": preflight_evidence,
    }
    _save_bytes_atomic(
        path,
        json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        + b"\n",
    )
    return idempotency_key, _evidence_ref_from_file(
        state_path, path, collector, "application/json", shlex.join(argv)
    )


def attempt_apply(
    state_path: Path, target_id: str, attempt_id: str, actor: str,
    collector: str, now: datetime,
) -> dict[str, Any]:
    with _state_lock(state_path):
        state, plan, _ = _writer_context(state_path, now, {"apply-running"})
        path = _artifact_file(state_path, state, "operations_evidence")
        evidence = _load_json(path, "operations_evidence")
        existing_meta = validate_operations_evidence(
            evidence, state, plan, now, _state_dir(state_path),
            enforce_latest_freshness=False,
        )
        _check_mutable_binding(
            "operations_evidence", state, path, evidence, existing_meta
        )
        target, attempt = _latest_attempt(evidence, target_id, attempt_id, "attempt-apply")
        prior_status = attempt["apply"]["status"]
        _expect(prior_status in {"pending", "executing"},
                "attempt-apply: apply receipt is already terminal")
        _expect(attempt["verification"]["status"] == "pending",
                "attempt-apply: verification is already terminal")
        planned = _target_record(plan, target_id, "attempt-apply plan")
        _expect(planned["apply_method"] == "gitops-manual-sync",
                "attempt-apply: auto-sync effects may only be observed, never replayed")
        actor_name = _nonempty_string(actor, "--actor")
        execute_apply = prior_status == "pending"
        if execute_apply:
            preflight_argv = _command_argv(
                planned["observation_command"],
                "operations_plan.target.observation_command",
            )
            preflight_exit, _preflight_stdout, preflight_evidence = _run_frozen_command(
                state_path, target_id, attempt_id, "pre-apply-target",
                preflight_argv, planned["observation_executable_sha256"],
                planned["observation_timeout_seconds"], collector, "apply",
                planned["execution_environment"], planned["execution_contexts"],
                operation_target=planned,
            )
            _expect(preflight_exit == 0,
                    "attempt-apply: live Argo Application identity/destination preflight failed; no mutation executed")
            idempotency_key, intent_evidence = _write_apply_intent(
                state_path, state, plan, planned, attempt_id, actor_name, collector,
                now, preflight_evidence,
            )
            attempt["apply"] = {
                "status": "executing",
                "at": _utc_text(now),
                "actor": actor_name,
                "idempotency_key": idempotency_key,
                "intent_evidence": intent_evidence,
                "observed": None,
                "evidence": None,
                "observation_evidence": None,
                "failure_reason": None,
                "recovered_from_ambiguous": False,
            }
            target["status"] = _derive_attempt_status(attempt)
            intent_meta = validate_operations_evidence(
                evidence, state, plan, now, _state_dir(state_path)
            )
            _commit_mutable_artifact(
                "operations_evidence", state_path, state, path, evidence, intent_meta, now
            )
            if TEST_FAIL_AFTER_APPLY_INTENT:
                raise ValidationError("simulated failure after durable apply intent")
        else:
            _expect(attempt["apply"].get("actor") == actor_name,
                    "attempt-apply: recovery actor must equal the durable intent actor")
            idempotency_key = _sha256_value(
                attempt["apply"].get("idempotency_key"),
                "attempt-apply durable idempotency key",
            )
            intent_evidence = attempt["apply"]["intent_evidence"]
        apply_argv = _command_argv(
            planned["apply_command"], "operations_plan.target.apply_command"
        )
        apply_exit: int | None = None
        apply_evidence: dict[str, Any] | None = None
        if execute_apply:
            apply_exit, _apply_stdout, apply_evidence = _run_frozen_command(
                state_path, target_id, attempt_id, "apply", apply_argv,
                planned["apply_executable_sha256"], planned["apply_timeout_seconds"],
                collector, "apply", planned["execution_environment"],
                planned["execution_contexts"],
                {"MILESTONE_IDEMPOTENCY_KEY": idempotency_key},
            )
        observed: dict[str, Any] | None = None
        observation_evidence: dict[str, Any]
        failure_reason: str | None = None
        observation_argv = _command_argv(
            planned["observation_command"],
            "operations_plan.target.observation_command",
        )
        observation_exit, observation_stdout, observation_evidence = _run_frozen_command(
            state_path, target_id, attempt_id, "post-apply-observation",
            observation_argv, planned["observation_executable_sha256"],
            planned["observation_timeout_seconds"], collector, "apply",
            planned["execution_environment"],
            planned["execution_contexts"],
            operation_target=planned,
        )
        if observation_exit != 0:
            failure_reason = f"post-apply observation command exited {observation_exit}"
        else:
            try:
                parsed = json.loads(observation_stdout)
                if not isinstance(parsed, dict):
                    raise ValidationError("post-apply observation did not emit a JSON object")
                _validate_observed(parsed, "post-apply observed")
                _desired_matches(planned["desired"], parsed, "post-apply observed")
                observed = parsed
            except (json.JSONDecodeError, ValidationError) as exc:
                failure_reason = f"post-apply identity validation failed: {exc}"
        if execute_apply and apply_exit != 0:
            exit_failure = f"frozen apply command exited {apply_exit}"
            failure_reason = (
                exit_failure if failure_reason is None
                else f"{exit_failure}; {failure_reason}"
            )
        status = "applied" if failure_reason is None else "failed"
        attempt["apply"] = {
            "status": status,
            "at": attempt["apply"]["at"],
            "actor": actor_name,
            "idempotency_key": idempotency_key,
            "intent_evidence": intent_evidence,
            "observed": observed,
            "evidence": apply_evidence,
            "observation_evidence": observation_evidence,
            "failure_reason": failure_reason,
            "recovered_from_ambiguous": not execute_apply,
        }
        target["status"] = _derive_attempt_status(attempt)
        meta = validate_operations_evidence(evidence, state, plan, now, _state_dir(state_path))
        _commit_mutable_artifact(
            "operations_evidence", state_path, state, path, evidence, meta, now
        )
        return {
            "ok": status == "applied", "target": target_id,
            "attempt_id": attempt_id, "status": target["status"],
            "failure_reason": failure_reason,
        }


def attempt_adopt_auto_sync(
    state_path: Path, target_id: str, collector: str, now: datetime,
) -> dict[str, Any]:
    """Append a terminal, non-mutating observation of a preauthorized Argo effect."""
    with _state_lock(state_path):
        state, plan, pmeta = _writer_context(state_path, now, {"apply-running"})
        planned = _target_record(plan, target_id, "attempt-adopt-auto-sync plan")
        _expect(planned["apply_method"] == "gitops-auto-sync-observe-v1",
                "attempt-adopt-auto-sync: target is not an explicit auto-sync observer")
        waiver_path = _artifact_file(state_path, state, "waivers")
        if not waiver_path.exists():
            waivers = _initial_waivers(state, plan, now)
            waiver_meta = validate_waivers(waivers, state, plan, now)
            _commit_mutable_artifact(
                "waivers", state_path, state, waiver_path, waivers, waiver_meta, now
            )
        path = _artifact_file(state_path, state, "operations_evidence")
        if path.is_file():
            evidence = _load_json(path, "operations_evidence")
            existing_meta = validate_operations_evidence(
                evidence, state, plan, now, _state_dir(state_path),
                enforce_latest_freshness=False,
            )
            _check_mutable_binding(
                "operations_evidence", state, path, evidence, existing_meta
            )
        else:
            evidence = _initial_operations_evidence(state, plan, now)
        target = _target_record(evidence, target_id, "attempt-adopt-auto-sync")
        attempts = target["attempts"]
        if attempts:
            _expect(_derive_attempt_status(attempts[-1]) == "failed",
                    "attempt-adopt-auto-sync: only a failed observation may be retried")
        intent_path = _artifact_file(state_path, state, "publication_intent")
        intent = _load_json(intent_path, "publication_intent")
        intent_meta = validate_publication_intent(intent, state, now)
        _check_mutable_binding(
            "publication_intent", state, intent_path, intent, intent_meta
        )
        binding = planned["auto_sync_binding"]
        effect = intent["scope"].get("delivery_effect")
        _expect(isinstance(effect, dict)
                and intent["intent_id"] == binding["publication_intent_id"]
                and intent["scope_hash"] == binding["publication_scope_hash"]
                and _value_sha(effect) == binding["delivery_effect_sha256"],
                "attempt-adopt-auto-sync: publication effect binding drifted")
        sequence = len(attempts) + 1
        scope = pmeta["scopes"][target_id]
        slug = re.sub(r"[^A-Za-z0-9]+", "-", target_id).strip("-").lower() or "target"
        attempt_id = f"{slug}-auto-{sequence:04d}-{scope[:12]}"
        observation_argv = _command_argv(
            planned["observation_command"], "operations_plan.target.observation_command"
        )
        observation_exit, observation_stdout, observation_evidence = _run_frozen_command(
            state_path, target_id, attempt_id, "adopt-auto-sync",
            observation_argv, planned["observation_executable_sha256"],
            planned["observation_timeout_seconds"], collector, "apply",
            planned["execution_environment"], planned["execution_contexts"],
            operation_target=planned,
        )
        observed: dict[str, Any] | None = None
        failure_reason: str | None = None
        if observation_exit == 0:
            try:
                adoption = json.loads(observation_stdout)
                _expect(isinstance(adoption, dict)
                        and isinstance(adoption.get("observed"), dict),
                        "auto-sync adoption projection is malformed")
                observed = adoption["observed"]
                _desired_matches(planned["desired"], observed, "auto-sync adopted identity")
            except (json.JSONDecodeError, ValidationError) as exc:
                failure_reason = f"auto-sync adoption validation failed: {exc}"
        else:
            failure_reason = f"auto-sync observation command exited {observation_exit}"
        status = "applied" if failure_reason is None else "failed"
        stamp = _utc_text(now)
        publication_authorization = intent["authorization"]
        attempts.append({
            "attempt_id": attempt_id,
            "sequence": sequence,
            "previous_attempt_sha256": _value_sha(attempts[-1]) if attempts else None,
            "recorded_at": stamp,
            "authorization": {
                "decision": "approved", "by": publication_authorization["by"],
                "method": "publication-effect", "at": publication_authorization["at"],
                "scope_hash": scope,
                "publication_scope_hash": intent["scope_hash"],
                "delivery_effect_sha256": _value_sha(effect), "target_id": target_id,
            },
            "apply": {
                "kind": "observed-auto-sync-v1", "status": status, "at": stamp,
                "actor": "argocd-auto-sync-observer", "idempotency_key": None,
                "intent_evidence": None, "observed": observed, "evidence": None,
                "observation_evidence": observation_evidence,
                "failure_reason": failure_reason, "recovered_from_ambiguous": False,
            },
            "verification": {
                "status": "pending", "observed_at": None, "observed": None,
                "observation_evidence": None, "probes": [],
            },
        })
        target["status"] = _derive_attempt_status(attempts[-1])
        meta = validate_operations_evidence(
            evidence, state, plan, now, _state_dir(state_path),
            enforce_latest_freshness=False,
        )
        _commit_mutable_artifact(
            "operations_evidence", state_path, state, path, evidence, meta, now
        )
        return {
            "ok": status == "applied", "target": target_id,
            "attempt_id": attempt_id, "status": target["status"],
            "mutations_executed": False, "failure_reason": failure_reason,
        }


def attempt_verify(
    state_path: Path, target_id: str, attempt_id: str, collector: str, now: datetime,
    approved_by: str | None = None, expected_scope_hash: str | None = None,
) -> dict[str, Any]:
    with _state_lock(state_path):
        state, plan, _ = _writer_context(state_path, now, {"verify-running"})
        path = _artifact_file(state_path, state, "operations_evidence")
        evidence = _load_json(path, "operations_evidence")
        existing_meta = validate_operations_evidence(
            evidence, state, plan, now, _state_dir(state_path),
            enforce_latest_freshness=False,
        )
        _check_mutable_binding(
            "operations_evidence", state, path, evidence, existing_meta
        )
        target, attempt = _latest_attempt(evidence, target_id, attempt_id, "attempt-verify")
        _expect(attempt["apply"]["status"] == "applied",
                "attempt-verify: latest attempt was not successfully applied")
        is_refresh = attempt["verification"]["status"] != "pending"
        refreshes = target["verification_refreshes"]
        refresh_intents = target["verification_refresh_intents"]
        if is_refresh and existing_meta["statuses"].get(target_id) == "verified":
            latest_observed_at = (
                refreshes[-1]["observed_at"]
                if refreshes
                and refreshes[-1]["source_attempt_sha256"] == _value_sha(attempt)
                else attempt["verification"]["observed_at"]
            )
            max_age = _integer(
                plan.get("max_evidence_age_seconds"),
                "operations_plan.max_evidence_age_seconds", 60,
            )
            _expect(
                (now - _iso(latest_observed_at, "latest verification observed_at"))
                .total_seconds() > max_age,
                "attempt-verify: current verification is still fresh",
            )
        refresh_id: str | None = None
        refresh_intent_hash: str | None = None
        execution_id = attempt_id
        planned = _target_record(plan, target_id, "attempt-verify plan")
        if is_refresh:
            _expect(approved_by is not None,
                    "attempt-verify refresh requires --approved-by after exact action preview")
            _expect(expected_scope_hash is not None,
                    "attempt-verify refresh requires --scope-hash from exact action preview")
            refresh_scope = _verification_refresh_scope(
                plan, planned, target, attempt
            )
            scope_hash = _value_sha(refresh_scope)
            _expect(_sha256_value(expected_scope_hash, "--scope-hash") == scope_hash,
                    "attempt-verify: verification refresh scope changed; inspect a new preview")
            refresh_id = f"{attempt_id}-v{len(refresh_intents) + 1:04d}"
            _expect(not any(item.get("refresh_id") == refresh_id for item in refresh_intents),
                    "attempt-verify: generated duplicate verification refresh id")
            execution_id = refresh_id
            refresh_intent = {
                "refresh_id": refresh_id,
                "sequence": refresh_scope["sequence"],
                "previous_intent_sha256": refresh_scope["previous_intent_sha256"],
                "previous_refresh_sha256": refresh_scope["previous_refresh_sha256"],
                "source_attempt_sha256": refresh_scope["source_attempt_sha256"],
                "recorded_at": _utc_text(now),
                "authorization": {
                    "decision": "approved",
                    "by": _human_name(approved_by or "", "--approved-by"),
                    "method": "human-explicit", "at": _utc_text(now),
                    "scope_hash": scope_hash,
                },
            }
            refresh_intents.append(refresh_intent)
            refresh_intent_hash = _value_sha(refresh_intent)
            # The authorization is state-bound before any generic observation
            # command can execute. An abrupt exit leaves an unresolved intent
            # that must be conservatively recovered as ambiguous, never replayed.
            target["status"] = "applied"
            intent_meta = validate_operations_evidence(
                evidence, state, plan, now, _state_dir(state_path),
                enforce_latest_freshness=False,
            )
            _commit_mutable_artifact(
                "operations_evidence", state_path, state, path, evidence,
                intent_meta, now,
            )
            if TEST_FAIL_AFTER_REFRESH_INTENT:
                raise ValidationError(
                    "self-test crash after durable verification refresh intent"
                )
        observation_argv = _command_argv(
            planned["observation_command"], "operations_plan.target.observation_command"
        )
        observation_exit, observation_stdout, observation_evidence = _run_frozen_command(
            state_path, target_id, execution_id, "observed-identity",
            observation_argv, planned["observation_executable_sha256"],
            planned["observation_timeout_seconds"], collector,
            execution_environment=planned["execution_environment"],
            execution_contexts=planned["execution_contexts"],
            operation_target=planned,
        )
        observed: dict[str, Any] = {}
        observation_valid = observation_exit == 0
        if observation_valid:
            try:
                parsed = json.loads(observation_stdout)
                observation_valid = isinstance(parsed, dict)
                if observation_valid:
                    observed = parsed
            except json.JSONDecodeError:
                observation_valid = False
        if observation_valid:
            try:
                _validate_observed(observed, "verification observed")
            except ValidationError:
                observation_valid = False
                observed = {}
        if observation_valid:
            try:
                _desired_matches(planned["desired"], observed, "verification observed")
            except ValidationError:
                observation_valid = False
        probes: list[dict[str, Any]] = []
        for i, spec in enumerate(planned["verification_contract"]):
            label = f"operations_plan.target.verification_contract[{i}]"
            kind = _nonempty_string(spec.get("kind"), f"{label}.kind")
            argv = _command_argv(spec.get("command"), f"{label}.command")
            exit_code, _stdout, probe_evidence = _run_frozen_command(
                state_path, target_id, execution_id, kind, argv,
                spec["executable_sha256"],
                _integer(spec.get("timeout_seconds"), f"{label}.timeout_seconds", 1),
                collector,
                execution_environment=planned["execution_environment"],
                execution_contexts=planned["execution_contexts"],
                operation_target=planned, verification_kind=kind,
            )
            probes.append({
                "kind": kind,
                "exit_code": exit_code,
                "observed_at": _utc_text(now),
                "evidence": probe_evidence,
            })
        status = "verified" if observation_valid and all(
            probe["exit_code"] == 0 for probe in probes
        ) else "failed"
        verification_result = {
            "status": status,
            "observed_at": _utc_text(now),
            "observed": observed,
            "observation_evidence": observation_evidence,
            "probes": probes,
        }
        if is_refresh:
            previous_refresh_sha = _value_sha(refreshes[-1]) if refreshes else None
            _expect(refresh_intent_hash is not None,
                    "attempt-verify: internal refresh intent hash is missing")
            refreshes.append({
                "refresh_id": refresh_id,
                "sequence": len(refreshes) + 1,
                "previous_refresh_sha256": previous_refresh_sha,
                "source_attempt_sha256": _value_sha(attempt),
                "intent_sha256": refresh_intent_hash,
                "recorded_at": _utc_text(now),
                **verification_result,
            })
            target["status"] = status
        else:
            attempt["verification"] = verification_result
            target["status"] = _derive_attempt_status(attempt)
        meta = validate_operations_evidence(
            evidence, state, plan, now, _state_dir(state_path),
            enforce_latest_freshness=False,
        )
        _commit_mutable_artifact(
            "operations_evidence", state_path, state, path, evidence, meta, now
        )
        return {
            "ok": status == "verified", "target": target_id, "attempt_id": attempt_id,
            "refresh_id": refresh_id, "status": target["status"],
            "verification_gaps": meta["verification_gaps"][target_id],
        }


def attempt_verify_recover(
    state_path: Path, target_id: str, refresh_id: str, now: datetime,
) -> dict[str, Any]:
    """Close one unresolved refresh authorization without replaying any command."""
    with _state_lock(state_path):
        state, plan, _ = _writer_context(state_path, now, {"verify-running"})
        path = _artifact_file(state_path, state, "operations_evidence")
        evidence = _load_json(path, "operations_evidence")
        meta = validate_operations_evidence(
            evidence, state, plan, now, _state_dir(state_path),
            enforce_latest_freshness=False,
        )
        _check_mutable_binding("operations_evidence", state, path, evidence, meta)
        target = _target_record(evidence, target_id, "attempt-verify-recover")
        intents = target["verification_refresh_intents"]
        refreshes = target["verification_refreshes"]
        _expect(len(intents) == len(refreshes) + 1,
                "attempt-verify-recover: no unresolved refresh intent")
        intent = intents[-1]
        _expect(intent.get("refresh_id") == refresh_id,
                "attempt-verify-recover: only the latest unresolved refresh may recover")
        refreshes.append({
            "refresh_id": refresh_id,
            "sequence": intent["sequence"],
            "previous_refresh_sha256": intent["previous_refresh_sha256"],
            "source_attempt_sha256": intent["source_attempt_sha256"],
            "intent_sha256": _value_sha(intent),
            "recorded_at": _utc_text(now),
            "status": "ambiguous", "observed_at": None, "observed": None,
            "observation_evidence": None, "probes": [],
        })
        target["status"] = "applied"
        recovered_meta = validate_operations_evidence(
            evidence, state, plan, now, _state_dir(state_path),
            enforce_latest_freshness=False,
        )
        _commit_mutable_artifact(
            "operations_evidence", state_path, state, path, evidence,
            recovered_meta, now,
        )
        return {
            "ok": True, "target": target_id, "refresh_id": refresh_id,
            "status": "ambiguous", "commands_replayed": False,
        }


def waiver_append(
    state_path: Path, target_id: str, approved_by: str, missing_contract: list[str],
    reason: str, expires_at: str, compensating_control: str,
    follow_up_milestone: str, waiver_id: str | None, now: datetime,
) -> dict[str, Any]:
    with _state_lock(state_path):
        state, plan, pmeta = _writer_context(state_path, now, {"verify-running"})
        evidence_path = _artifact_file(state_path, state, "operations_evidence")
        evidence = _load_json(evidence_path, "operations_evidence")
        emeta = validate_operations_evidence(
            evidence, state, plan, now, _state_dir(state_path)
        )
        _check_mutable_binding(
            "operations_evidence", state, evidence_path, evidence, emeta
        )
        missing = sorted({_nonempty_string(v, "--missing-contract") for v in missing_contract})
        _expect(bool(missing), "--missing-contract: provide at least one probe")
        _expect(emeta["apply_statuses"].get(target_id) == "applied",
                "waiver-append: target must have a successful latest apply")
        _expect(emeta["identity_matches"].get(target_id) is True,
                "waiver-append: live observed identity mismatch is not waivable")
        _expect(missing == emeta["verification_gaps"].get(target_id),
                "waiver-append: missing contract must exactly equal current verification gaps")
        path = _artifact_file(state_path, state, "waivers")
        if path.is_file():
            waivers = _load_json(path, "waivers")
            current = validate_waivers(waivers, state, plan, now)
            _check_mutable_binding("waivers", state, path, waivers, current)
            _expect(target_id not in current["active_waivers"],
                    "waiver-append: target already has an active waiver")
        else:
            waivers = _initial_waivers(state, plan, now)
        expires = _iso(expires_at, "--expires-at")
        _expect(now < expires <= now + timedelta(days=30),
                "--expires-at: waiver must expire within 30 days")
        existing_ids = {w.get("waiver_id") for w in waivers["waivers"]}
        if waiver_id is None:
            slug = re.sub(r"[^A-Za-z0-9]+", "-", target_id).strip("-").lower() or "target"
            waiver_id = f"{slug}-w{len(waivers['waivers']) + 1:04d}-{pmeta['scopes'][target_id][:12]}"
        waiver_id = _nonempty_string(waiver_id, "--waiver-id")
        _expect(waiver_id not in existing_ids, f"waiver-append: duplicate waiver id {waiver_id!r}")
        stamp = _utc_text(now)
        waivers["waivers"].append({
            "waiver_id": waiver_id,
            "target_id": target_id,
            "scope_hash": pmeta["scopes"][target_id],
            "missing_contract": missing,
            "decision": "approved",
            "approved_by": _human_name(approved_by, "--approved-by"),
            "approval_method": "human-explicit",
            "approved_at": stamp,
            "reason": _nonempty_string(reason, "--reason"),
            "created_at": stamp,
            "expires_at": _utc_text(expires),
            "compensating_control": _nonempty_string(compensating_control, "--compensating-control"),
            "follow_up_milestone": _nonempty_string(follow_up_milestone, "--follow-up-milestone"),
        })
        meta = validate_waivers(waivers, state, plan, now)
        _commit_mutable_artifact("waivers", state_path, state, path, waivers, meta, now)
        return {"ok": True, "target": target_id, "waiver_id": waiver_id, "expires_at": _utc_text(expires)}


def _validate_one(kind: str, path: Path, state_path: Path | None, now: datetime) -> dict[str, Any]:
    _expect(state_path is not None, f"validate {kind}: --state is required for identity binding")
    state = _load_state(state_path)
    state["_state_path"] = str(state_path.resolve())
    _validate_state(state, state_path)
    expected_path = _safe_artifact_path(state_path, state, kind)
    _expect(path.resolve() == expected_path,
            f"validate path {path.resolve()} does not match state.{kind} ({expected_path})")
    _, _, meta = _load_and_validate(kind, state, state_path, now)
    return {"ok": True, "kind": kind, "sha256": _file_sha(path), "meta": meta}


def self_test() -> int:
    """Adversarial fixtures for stale, replayed, partial, and mutable evidence."""
    global AGENT_KIT_ROOT, ALLOW_LOCAL_DELIVERY_ENDPOINTS, TEST_ARTIFACT_RESOLVER
    global TEST_FAIL_AFTER_ARTIFACT_WRITE, TEST_FAIL_AFTER_CHECK_EVIDENCE_WRITE
    global TEST_FAIL_AFTER_APPLY_INTENT, TEST_FAIL_AFTER_REFRESH_INTENT
    global ALLOW_TEST_OPERATION_EXECUTABLES
    ALLOW_LOCAL_DELIVERY_ENDPOINTS = True
    ALLOW_TEST_OPERATION_EXECUTABLES = True
    failures = 0
    skipped = 0

    def skip(name: str, reason: str) -> None:
        """Record a case this host cannot execute, loudly.

        A case that silently vanishes is the skip-to-green pattern M2 exists to
        remove, so skips are printed with a machine-readable reason and counted
        in the summary line. They are NOT failures — the code under test is
        unexercised here, not broken.
        """
        nonlocal skipped
        skipped += 1
        print(f"  {name}: SKIP {reason}")

    # Every check-run case drives a real subprocess through `sys.executable`. If
    # THIS interpreter is one the trust control refuses — a per-user Python
    # under %LOCALAPPDATA%, the Windows default, is user-writable and correctly
    # refused — the control fires before the behaviour under test is reached.
    # The case cannot run here; that is a property of the host, not of the code.
    # Recognised once, centrally, so every call site is covered and the control
    # itself is untouched: in production the same refusal is still a hard error.
    _UNTRUSTED_INTERPRETER = "check executable is outside trusted system roots"
    _interpreter_trusted = _executable_is_trusted(Path(sys.executable).resolve())

    def check(name: str, fn, contains: str | None = None) -> None:
        nonlocal failures
        try:
            fn()
        except ValidationError as exc:
            if not _interpreter_trusted and _UNTRUSTED_INTERPRETER in str(exc):
                skip(name, f"REQUIRES:trusted-interpreter-path ({sys.executable})")
                return
            ok = contains is not None and contains in str(exc)
            print(f"  {name}: {'ok' if ok else 'FAIL ' + str(exc)[:160]}")
            failures += 0 if ok else 1
        else:
            ok = contains is None
            print(f"  {name}: {'ok' if ok else 'FAIL expected refusal'}")
            failures += 0 if ok else 1

    def producer() -> dict[str, str]:
        return {"kind": "deterministic-tool", "name": "fixture", "provider": "local", "version": "1"}

    now = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=5)
    stamp = _utc_text(now - timedelta(days=1))
    ops_stamp = _utc_text(now)
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td) / "workspace"
        (workspace / ".codex" / "agents").mkdir(parents=True)
        (workspace / ".claude").mkdir(parents=True)
        (workspace / "AGENTS.md").write_text("fixture workspace\n", encoding="utf-8")
        (workspace / "CLAUDE.md").write_text("fixture workspace\n", encoding="utf-8")
        kit = workspace / "GitLab" / "group" / "agent-kit"
        (kit / "data" / "agents").mkdir(parents=True)
        (kit / "data" / "scripts").mkdir(parents=True)
        # Layout links, not symlink-semantics assertions: a Windows account
        # without SeCreateSymbolicLinkPrivilege gets an NTFS junction, which
        # reads through identically. (The cases that test symlink REFUSAL are
        # guarded on supports_symlinks() instead — a junction is not a symlink.)
        platform_compat.create_directory_link(
            kit / "data" / "scripts", workspace / ".claude" / "scripts"
        )
        platform_compat.create_directory_link(
            kit / "data" / "agents", workspace / ".claude" / "agents"
        )
        fixture_roles = [
            "milestone-adversary", "milestone-delivery-integrity-adversary",
            "milestone-closure-verifier", "milestone-operations-adversary",
        ]
        for role in fixture_roles:
            shutil.copyfile(
                SCRIPT_ROOT / "data" / "agents" / f"{role}.md",
                kit / "data" / "agents" / f"{role}.md",
            )
        shutil.copyfile(
            Path(__file__).resolve(),
            kit / "data" / "scripts" / "milestone-pipeline-artifacts.py",
        )
        subprocess.run(["git", "init", "-q", str(kit)], check=True)
        subprocess.run(["git", "-C", str(kit), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(kit), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(kit), "add", "."], check=True)
        subprocess.run(["git", "-C", str(kit), "commit", "-qm", "fixture kit"], check=True)
        kit_commit = _git_output(kit, "rev-parse", "HEAD").decode().strip()
        AGENT_KIT_ROOT = kit

        root = workspace / "GitLab" / "group" / "repo"
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        bare_remote = Path(td) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare_remote)], check=True)
        remote_url = str(bare_remote)
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", remote_url], check=True)
        (root / "x.txt").write_text("base\n", encoding="utf-8")
        fixture_check = root / "fixture_check.py"
        fixture_check.write_text("print('ok')\n", encoding="utf-8")
        fixture_fail = root / "fixture_fail.py"
        fixture_fail.write_text("raise SystemExit(1)\n", encoding="utf-8")
        flaky_flag = Path(td) / "fixture-flaky-fail"
        fixture_flaky = root / "fixture_flaky.py"
        fixture_flaky.write_text(
            "from pathlib import Path\n"
            f"raise SystemExit(1 if Path({str(flaky_flag)!r}).exists() else 0)\n",
            encoding="utf-8",
        )
        fixture_secret = root / "fixture_secret.py"
        fixture_secret.write_text(
            "print('Authorization: Bearer planted-super-secret')\nraise SystemExit(1)\n",
            encoding="utf-8",
        )
        fixture_oversize = root / "fixture_oversize.py"
        fixture_oversize.write_text(
            f"import sys\nsys.stdout.write('x' * {MAX_CAPTURE_BYTES + 4096})\n",
            encoding="utf-8",
        )
        fixture_background = root / "fixture_background.py"
        fixture_background.write_text(
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n",
            encoding="utf-8",
        )
        check_command = shlex.join([sys.executable, "fixture_check.py"])
        (root / ".milestone-pipeline").mkdir()
        (root / ".milestone-pipeline" / "checks.json").write_text(json.dumps({
            "schema_version": 1,
            "checks": [check_command],
        }), encoding="utf-8")
        subprocess.run([
            "git", "-C", str(root), "add", "x.txt", "fixture_check.py", "fixture_fail.py",
            "fixture_flaky.py",
            "fixture_secret.py", "fixture_oversize.py", "fixture_background.py",
            ".milestone-pipeline/checks.json",
        ], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
        base = _git_output(root, "rev-parse", "HEAD").decode().strip()
        (root / "x.txt").write_text("head\n", encoding="utf-8")
        (root / ".milestone-pipeline" / "trust-policy.json").write_text(
            json.dumps({
                "schema_version": 1,
                "source_remote": remote_url,
                "render_remote_prefixes": [str(Path(td) / "render-remote.git")],
                "artifact_registry_prefixes": ["registry/"],
                "artifact_resolver": None,
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        subprocess.run([
            "git", "-C", str(root), "add", "x.txt",
            ".milestone-pipeline/trust-policy.json",
        ], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "head"], check=True)
        head = _git_output(root, "rev-parse", "HEAD").decode().strip()
        subprocess.run(["git", "-C", str(root), "branch", "-M", "dev"], check=True)
        subprocess.run(["git", "-C", str(root), "push", "-q", "origin", "dev"], check=True)
        state_dir = root / ".claude" / "notes" / "milestones" / "m1"
        art = state_dir / "artifacts"
        art.mkdir(parents=True)
        state_path = state_dir / "state.json"
        findings_path = state_dir / "findings.json"
        critique_paths = [
            ".claude/notes/milestones/m1/artifacts/reviews/m1-adversary-critique.md",
            ".claude/notes/milestones/m1/artifacts/reviews/m1-delivery-critique.md",
        ]
        (art / "reviews").mkdir()
        critic_names = ["adversary", "delivery-integrity"]
        for path, critic_name in zip(critique_paths, critic_names):
            (root / path).write_text(
                f"**Critic:** {critic_name}\n**Diff range:** {base}..{head}\n"
                "- **Overall verdict:** SHIP\n",
                encoding="utf-8",
            )
        findings_path.write_text(json.dumps({
            "schema_version": 1, "milestone_id": "m1", "critique_files": critique_paths, "findings": []
        }), encoding="utf-8")
        state = {
            "schema_version": 2, "id": "m1", "created_at": stamp, "updated_at": stamp,
            "phase": "critique-running", "phase_history": [
                {"phase": phase, "at": stamp} for phase in (
                    "init", "research-running", "research-complete", "implement-running",
                    "implement-complete", "critique-running",
                )
            ],
            "agent_kit_commit": kit_commit,
            "kit_upgrade_history": [],
            "check_run_head": None, "check_run_hashes": {}, "check_run_history": {},
            "check_run_attempts": [],
            "implementation_base": base, "implementation_commits": [head], "implementation_commit_range": f"{base}..{head}",
            "critics_run": sorted(ALWAYS_REVIEWERS), "critique_files": critique_paths,
            "findings_register": ".claude/notes/milestones/m1/findings.json", "rectification_commit": head,
            "publication_required": True, "publication_not_required_reason": None,
            "operations_required": True, "operations_not_required_reason": None,
            "implementation_status": "in_progress", "operational_status": "pending", "review_status": "pending",
            "artifact_bindings": {},
            **{k: f"artifacts/{v}" for k, v in POINTERS.items()},
        }
        state_path.write_text(json.dumps(state), encoding="utf-8")
        policyless_item = {"head_commit": base, "remote_url": remote_url}
        check("operations-required preflight rejects a missing reviewed policy", lambda: (
            _preflight_publication_delivery_policy(
                {"publication_required": True, "operations_required": True},
                policyless_item, root,
            )
        ), "required for publication")
        check("publication preflight rejects a policy-less reviewed commit even when operations are not required", lambda: (
            _preflight_publication_delivery_policy(
                {
                    "publication_required": True,
                    "operations_required": False,
                    "operations_not_required_reason": "local-only fixture",
                }, policyless_item, root,
            )
        ), "required for publication")
        check("local source-only preflight permits a policy-less reviewed commit", lambda: (
            _preflight_publication_delivery_policy(
                {
                    "publication_required": False,
                    "publication_not_required_reason": "local-only fixture",
                    "operations_required": False,
                    "operations_not_required_reason": "no live target",
                }, policyless_item, root,
            )
        ))
        check("reviewed schema-v1 policy preflight remains valid", lambda: (
            _preflight_publication_delivery_policy(
                {"publication_required": True, "operations_required": True},
                {"head_commit": head, "remote_url": remote_url}, root,
            )
        ))
        # A junction is not a symlink (os.path.islink() is False), so this case
        # cannot be faked on a Windows account without the privilege — the
        # refusal it asserts is specifically about symlink-ness.
        if platform_compat.supports_symlinks():
            state_alias = state_dir / "state-alias.json"
            state_alias.symlink_to(state_path)
            def exercise_state_alias() -> None:
                with _state_lock(state_alias):
                    pass
            check("state symlink aliases are refused", exercise_state_alias,
                  "symlink aliases forbidden")
            state_alias.unlink()
        else:
            skip("state symlink aliases are refused", "REQUIRES:symlink-privilege")
        (kit / "upgrade-marker.txt").write_text("compatible fixture upgrade\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(kit), "add", "upgrade-marker.txt"], check=True)
        subprocess.run(["git", "-C", str(kit), "commit", "-qm", "fixture kit upgrade"], check=True)
        check("kit drift requires explicit upgrade authorization", lambda: (
            lambda candidate: _validate_state(candidate, state_path)
        )({**_load_json(state_path, "pre-upgrade state"), "_state_path": str(state_path)}),
              "run kit-upgrade")
        upgrade_time = _iso(stamp, "fixture stamp") + timedelta(seconds=1)
        upgrade_preview = kit_upgrade_preview(state_path, upgrade_time)
        upgrade_result = kit_upgrade_state(
            state_path, "Chris Dare", upgrade_preview["scope_hash"], upgrade_time
        )
        kit_commit = upgrade_result["to_commit"]
        state = _load_json(state_path, "upgraded state")
        check("kit upgrade is append-only and human-attributed", lambda: _expect(
            len(state["kit_upgrade_history"]) == 1
            and state["kit_upgrade_history"][0]["approved_by"] == "Chris Dare"
            and state["agent_kit_commit"] == kit_commit,
            "kit upgrade receipt missing or unbound",
        ))
        reviews = []
        body_map = {
            "milestone-adversary": kit / "data/agents/milestone-adversary.md",
            "milestone-delivery-integrity-adversary": kit / "data/agents/milestone-delivery-integrity-adversary.md",
        }
        review_dir = art / "reviews"
        prompts = []
        critique_by_role = dict(zip(sorted(ALWAYS_REVIEWERS), critique_paths))
        for idx, role in enumerate(sorted(ALWAYS_REVIEWERS)):
            task_id = f"task-{idx}"
            body_snapshot = review_dir / f"{role}-{task_id}-agent.md"
            body_snapshot.write_bytes(body_map[role].read_bytes())
            p = review_dir / f"{role}-{task_id}-prompt.md"
            prompt_header = (
                "MILESTONE_REVIEW_DISPATCH_V2\n"
                f"ROLE: {role}\nSTAGE: assessment\nID: m1\n"
                f"REPO_ROOT: {root.resolve()}\nWORKSPACE_ROOT: {workspace.resolve()}\n"
                f"COMMIT_RANGE: {base}..{head}\n"
                f"CRITIQUE_PATH: {(root / critique_by_role[role]).resolve()}\n"
                f"AGENT_KIT_COMMIT: {kit_commit}\nSOURCE_REMOTE_URL: {remote_url}"
            )
            p.write_bytes(
                prompt_header.encode("utf-8")
                + b"\n--- CANONICAL AGENT BODY ---\n"
                + body_snapshot.read_bytes()
            )
            prompts.append(p)
        for idx, role in enumerate(sorted(ALWAYS_REVIEWERS)):
            body = body_map[role]
            critique = root / critique_paths[idx]
            reviews.append({
                "role": role, "stage": "assessment", "provider": "codex", "model": None,
                # .as_posix(), not str(): the validator compares against the
                # literal "data/agents/<role>.md", and str(PurePath) is
                # OS-native, so this produced "data\\agents\\..." and failed on
                # Windows. Unreachable until the self-test stopped crashing at
                # import (M2, gates-green-t-fcntl-datascripts).
                "agent_task_id": f"task-{idx}", "agent_body_path": body.relative_to(kit).as_posix(),
                "agent_body_snapshot_path": f"artifacts/reviews/{role}-task-{idx}-agent.md",
                "agent_kit_commit": kit_commit, "workspace_root": str(workspace.resolve()),
                "reviewed_remote_url": remote_url,
                "agent_body_sha256": _file_sha(review_dir / f"{role}-task-{idx}-agent.md"),
                "prompt_path": f"artifacts/reviews/{prompts[idx].name}",
                "prompt_sha256": _file_sha(prompts[idx]), "critique_path": critique_paths[idx],
                "critique_sha256": _file_sha(critique), "reviewed_base": base, "reviewed_head": head,
                "started_at": stamp, "completed_at": stamp, "verdict": "SHIP",
                "check_evidence_refs": [], "check_attempt_refs": [],
                "findings_register_sha256": None,
                "assessment_manifest_sha256": None, "operations_plan_sha256": None,
                "release_manifest_sha256": None,
                "delivery_requirements_sha256": None,
                "findings_snapshot_path": None, "operations_plan_snapshot_path": None,
                "release_manifest_snapshot_path": None,
            })
        review = {
            "schema_version": 2, "milestone_id": "m1", "generation": 1, "created_at": stamp,
            "producer": producer(), "reviewed": {
                "repo": "repo", "base_commit": base, "head_commit": head,
                "diff_sha256": _diff_sha(root, base, head), "remote_url": remote_url,
            },
            "required_reviewers": sorted(ALWAYS_REVIEWERS), "reviews": reviews,
            "closure_reviews": [], "operations_reviews": [],
        }
        (art / POINTERS["review_manifest"]).write_text(json.dumps(review), encoding="utf-8")
        state_runtime = dict(state); state_runtime["_state_path"] = str(state_path)
        check("valid blind review manifest", lambda: validate_review_manifest(review, state_runtime, state_path))
        missing_reviewer = json.loads(json.dumps(review))
        missing_reviewer["required_reviewers"] = ["milestone-adversary"]
        missing_reviewer["reviews"] = [r for r in missing_reviewer["reviews"] if r["role"] == "milestone-adversary"]
        check("missing mandatory adversary refused", lambda: validate_review_manifest(missing_reviewer, state_runtime, state_path), "expected")
        findings_path.write_text(json.dumps({
            "schema_version": 1, "milestone_id": "other", "critique_files": critique_paths, "findings": []
        }), encoding="utf-8")
        check("findings register cross-run replay refused", lambda: validate_review_manifest(review, state_runtime, state_path), "cross-run replay")
        findings_path.write_text(json.dumps({
            "schema_version": 1, "milestone_id": "m1", "critique_files": critique_paths, "findings": []
        }), encoding="utf-8")

        assessment_meta = validate_review_manifest(review, state_runtime, state_path)
        state["phase"] = "rectify-running"
        state["phase_history"].extend([
            {"phase": "critique-complete", "at": stamp},
            {"phase": "rectify-running", "at": stamp},
        ])
        state["artifact_bindings"] = {
            "review_manifest": _binding(
                "review_manifest", art / POINTERS["review_manifest"], review,
                "critique-complete", assessment_meta,
            )
        }
        state_path.write_text(json.dumps(state), encoding="utf-8")
        bypass_token = root / "untracked-source-input.zzz"
        bypass_token.write_text("not reviewed\n", encoding="utf-8")
        check(
            "untracked project input blocks check-run",
            lambda: run_check(
                state_path, "bypass", [sys.executable, "fixture_check.py"], 60
            ),
            "untracked files outside",
        )
        bypass_token.unlink()
        TEST_FAIL_AFTER_CHECK_EVIDENCE_WRITE = True
        check(
            "check writer crash is journaled",
            lambda: run_check(
                state_path, "fixture-check-crash", [sys.executable, "fixture_fail.py"], 60
            ),
            "simulated failure after check evidence write",
        )
        TEST_FAIL_AFTER_CHECK_EVIDENCE_WRITE = False
        # These two run_check calls are NOT wrapped in check(), so the central
        # untrusted-interpreter recognition above never saw them: on a host
        # whose Python is a per-user install under %LOCALAPPDATA% — the Windows
        # default, user-writable, and correctly refused — the control fired
        # here and took the whole self-test down as a FAILURE. The control is
        # right; the call site was simply unguarded.
        #
        # Everything below depends on the receipts these produce, so the
        # section is skipped rather than faked. Fabricating a receipt would let
        # the assertions that follow verify fiction, which is worse than not
        # running them.
        if not _interpreter_trusted:
            skip(
                "check-run section",
                f"REQUIRES:trusted-interpreter-path ({sys.executable}) -- "
                "check-run receipts, redaction, evidence refs and everything "
                "derived from them were NOT verified on this host",
            )
            verdict = "OK" if failures == 0 else f"{failures} failure(s)"
            verdict += f" ({skipped} skipped on {sys.platform})"
            print(f"milestone-pipeline-artifacts self-test: {verdict}")
            return 0 if failures == 0 else 1
        check_receipt = run_check(
            state_path, "fixture-check", [sys.executable, "fixture_check.py"], 60
        )
        secret_receipt = run_check(
            state_path, "fixture-secret", [sys.executable, "fixture_secret.py"], 60
        )
        secret_record = _load_json(
            state_dir / secret_receipt["evidence"]["path"], "secret check evidence"
        )
        check("check output is redacted before persistence", lambda: _expect(
            "planted-super-secret" not in json.dumps(secret_record)
            and "[REDACTED]" in secret_record["stdout"],
            "secret-bearing output reached the milestone artifact",
        ))
        oversize_receipt = run_check(
            state_path, "fixture-oversize", [sys.executable, "fixture_oversize.py"], 60
        )
        oversize_record = _load_json(
            state_dir / oversize_receipt["evidence"]["path"], "oversize check evidence"
        )
        check("oversized check output is bounded and cannot pass", lambda: _expect(
            oversize_receipt["exit_code"] == 125
            and oversize_record["stdout_truncated"] is True
            and len(oversize_record["stdout"].encode("utf-8")) <= MAX_CAPTURE_BYTES,
            "oversized output was unbounded or accepted",
        ))
        background_receipt = run_check(
            state_path, "fixture-background", [sys.executable, "fixture_background.py"], 2
        )
        background_record = _load_json(
            state_dir / background_receipt["evidence"]["path"],
            "background check evidence",
        )
        check("background descendant retaining pipes is terminated", lambda: _expect(
            background_receipt["exit_code"] == 125
            and background_record["background_processes_terminated"] is True,
            "background descendant escaped the bounded runner",
        ))
        flaky_pass = run_check(
            state_path, "fixture-flaky", [sys.executable, "fixture_flaky.py"], 60
        )
        flaky_flag.write_text("fail\n", encoding="utf-8")
        flaky_fail = run_check(
            state_path, "fixture-flaky", [sys.executable, "fixture_flaky.py"], 60
        )
        state_after_failure = _load_json(state_path, "state after failed check rerun")
        check("pass then fail invalidates active check", lambda: _expect(
            flaky_fail["exit_code"] == 1
            and flaky_pass["evidence"]["path"] not in state_after_failure["check_run_hashes"],
            "prior passing receipt remained active after a newer failure",
        ))
        flaky_flag.unlink()
        flaky_recovery = run_check(
            state_path, "fixture-flaky", [sys.executable, "fixture_flaky.py"], 60
        )
        state_after_recovery = _load_json(state_path, "state after recovered check rerun")
        check("pass-fail-pass restores only latest success", lambda: _expect(
            state_after_recovery["check_run_hashes"].get(
                flaky_recovery["evidence"]["path"]
            ) == flaky_recovery["evidence"]["sha256"]
            and flaky_pass["evidence"]["path"] not in state_after_recovery["check_run_hashes"],
            "check recovery did not replace the stale pass",
        ))
        flaky_flag.write_text("fail\n", encoding="utf-8")
        flaky_cleanup = run_check(
            state_path, "fixture-flaky", [sys.executable, "fixture_flaky.py"], 60
        )
        flaky_flag.unlink()
        review_completed_at = flaky_cleanup["completed_at"]
        check("check journal recovers on next writer", lambda: _expect(
            not _check_transaction_path(state_path).exists(),
            "check transaction journal was not cleared",
        ))
        state = _load_json(state_path, "fixture state after check-run")
        state_runtime = dict(state); state_runtime["_state_path"] = str(state_path)
        real_ref = check_receipt["evidence"]
        evidence_file = state_dir / real_ref["path"]
        evidence_dir = evidence_file.parent

        def evref(name: str, command: str = "fixture-check") -> dict[str, Any]:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
            value = evidence_dir / f"{safe}.log"
            if not value.exists():
                value.write_text(f"evidence:{name}\n", encoding="utf-8")
            return {
                "path": str(value.relative_to(state_dir)), "sha256": _file_sha(value),
                "media_type": "text/plain", "size_bytes": value.stat().st_size,
                "collector": "fixture", "command": command,
            }

        def command_ref(
            name: str, argv: list[str], environment: dict[str, str] | None = None,
            *, exit_code: int = 0, timed_out: bool = False,
        ) -> dict[str, Any]:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
            value = evidence_dir / f"{safe}-command.json"
            observation_record = "observation" in name
            probe_facts = {
                "argocd-synced": {
                    "kind": "argocd-synced", "sync_status": "Synced",
                    "health_status": "Healthy", "revision": render_head,
                },
                "deployment-observed-generation": {
                    "kind": "deployment-observed-generation", "generation": 1,
                    "observed_generation": 1, "available_replicas": 1,
                    "deployment_uid": "fixture-deployment-uid",
                    "pod_selector": "app.kubernetes.io/name=app",
                },
                "pod-image-digest": {
                    "kind": "pod-image-digest", "pod_count": 1,
                    "all_ready": True, "container_name": "app",
                    "pod_selector": "app.kubernetes.io/name=app",
                    "image_digests": [image_digest],
                },
                "service-selects-workload": {
                    "kind": "service-selects-workload",
                    "service_uid": "fixture-service-uid",
                    "pod_selector": "app.kubernetes.io/name=app",
                    "service_port": 443,
                },
                "ingress-routes-service": {
                    "kind": "ingress-routes-service",
                    "ingress_uid": "fixture-ingress-uid",
                    "host": "app.example.invalid", "path": "/healthz",
                    "service_name": "app", "service_port": 443,
                },
                "behavioral-smoke": {
                    "kind": "behavioral-smoke", "http_status": 200,
                },
            }
            if observation_record:
                stdout_value = json.dumps({
                    "source_commit": head, "render_commit": render_head,
                    "image_digest": image_digest, "generation": 1,
                }, sort_keys=True, separators=(",", ":"))
                output_policy = "projected-observed-identity"
            elif name in probe_facts:
                stdout_value = json.dumps(
                    probe_facts[name], sort_keys=True, separators=(",", ":")
                )
                output_policy = "projected-verification-fact"
            else:
                stdout_value = ""
                output_policy = "omitted"
            stderr_value = ""
            value.write_text(json.dumps({
                "argv": argv,
                "environment": environment or {},
                "exit_code": exit_code,
                "stderr": stderr_value,
                "stdout": stdout_value,
                "stdout_sha256": _persisted_text_sha(stdout_value),
                "stderr_sha256": _persisted_text_sha(stderr_value),
                "stdout_truncated": False,
                "stderr_truncated": False,
                "output_limit_bytes": MAX_CAPTURE_BYTES,
                "background_processes_terminated": False,
                "timed_out": timed_out,
                "output_capture_policy": output_policy,
            }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return {
                "path": str(value.relative_to(state_dir)), "sha256": _file_sha(value),
                "media_type": "application/json", "size_bytes": value.stat().st_size,
                "collector": "fixture", "command": shlex.join(argv),
            }

        closure_role = "milestone-closure-verifier"
        delivery_requirements_json = json.dumps(
            _delivery_requirements(state), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        )
        delivery_requirements_sha = _value_sha(_delivery_requirements(state))
        closure_body = kit / f"data/agents/{closure_role}.md"
        closure_snapshot = review_dir / f"{closure_role}-task-closure-agent.md"
        closure_snapshot.write_bytes(closure_body.read_bytes())
        closure_prompt = review_dir / f"{closure_role}-task-closure-prompt.md"
        closure_check_refs = [{"path": real_ref["path"], "sha256": real_ref["sha256"]}]
        closure_abs_refs = [{
            "path": str((state_dir / real_ref["path"]).resolve()),
            "sha256": real_ref["sha256"],
        }]
        closure_attempt_refs = list(state["check_run_attempts"])
        closure_attempt_abs_refs = [{
            "path": str((state_dir / ref["path"]).resolve()),
            "sha256": ref["sha256"],
        } for ref in closure_attempt_refs]
        assessment_sha = _value_sha(_review_projection(review, "assessment"))
        findings_sha = _file_sha(findings_path)
        closure_findings_snapshot = (
            review_dir / f"{closure_role}-task-closure-findings.json"
        )
        closure_findings_snapshot.write_bytes(findings_path.read_bytes())
        closure_rel = ".claude/notes/milestones/m1/artifacts/reviews/m1-closure.md"
        closure_path = root / closure_rel
        closure_header = (
            "MILESTONE_REVIEW_DISPATCH_V2\n"
            f"ROLE: {closure_role}\nSTAGE: closure\nID: m1\n"
            f"REPO_ROOT: {root.resolve()}\nWORKSPACE_ROOT: {workspace.resolve()}\n"
            f"BASE_COMMIT: {base}\nFINAL_HEAD: {head}\n"
            f"FINDINGS_REGISTER: {closure_findings_snapshot.resolve()}\n"
            f"FINDINGS_REGISTER_SHA256: {findings_sha}\n"
            f"REVIEW_MANIFEST: {(art / POINTERS['review_manifest']).resolve()}\n"
            f"ASSESSMENT_MANIFEST_SHA256: {assessment_sha}\n"
            "CHECK_EVIDENCE_REFS: "
            + json.dumps(closure_abs_refs, separators=(",", ":"), ensure_ascii=False)
            + "\n"
            "CHECK_ATTEMPT_REFS: "
            + json.dumps(closure_attempt_abs_refs, separators=(",", ":"), ensure_ascii=False)
            + "\n"
            f"DELIVERY_REQUIREMENTS: {delivery_requirements_json}\n"
            f"DELIVERY_REQUIREMENTS_SHA256: {delivery_requirements_sha}\n"
            f"CLOSURE_PATH: {closure_path.resolve()}\n"
            f"AGENT_KIT_COMMIT: {kit_commit}\nSOURCE_REMOTE_URL: {remote_url}"
        )
        closure_prompt.write_bytes(
            closure_header.encode("utf-8")
            + b"\n--- CANONICAL AGENT BODY ---\n"
            + closure_snapshot.read_bytes()
        )
        closure_path.write_text(
            f"**Closure verdict:** PASS\n**Reviewed range:** {base}..{head}\n",
            encoding="utf-8",
        )
        closure_receipt = {
            "role": closure_role, "stage": "closure", "provider": "codex", "model": None,
            "agent_task_id": "task-closure", "agent_body_path": f"data/agents/{closure_role}.md",
            "agent_body_snapshot_path": f"artifacts/reviews/{closure_role}-task-closure-agent.md",
            "agent_kit_commit": kit_commit, "workspace_root": str(workspace.resolve()),
            "reviewed_remote_url": remote_url,
            "agent_body_sha256": _file_sha(closure_snapshot),
            "prompt_path": f"artifacts/reviews/{closure_prompt.name}",
            "prompt_sha256": _file_sha(closure_prompt), "critique_path": closure_rel,
            "critique_sha256": _file_sha(closure_path), "reviewed_base": base, "reviewed_head": head,
            "started_at": review_completed_at,
            "completed_at": review_completed_at, "verdict": "PASS",
            "check_evidence_refs": closure_check_refs,
            "check_attempt_refs": closure_attempt_refs,
            "findings_register_sha256": findings_sha,
            "assessment_manifest_sha256": assessment_sha,
            "operations_plan_sha256": None, "release_manifest_sha256": None,
            "delivery_requirements_sha256": delivery_requirements_sha,
            "findings_snapshot_path": (
                f"artifacts/reviews/{closure_role}-task-closure-findings.json"
            ),
            "operations_plan_snapshot_path": None,
            "release_manifest_snapshot_path": None,
        }
        fail_task = "task-closure-fail"
        fail_snapshot = review_dir / f"{closure_role}-{fail_task}-agent.md"
        fail_snapshot.write_bytes(closure_body.read_bytes())
        fail_findings = review_dir / f"{closure_role}-{fail_task}-findings.json"
        fail_findings.write_bytes(findings_path.read_bytes())
        fail_rel = (
            ".claude/notes/milestones/m1/artifacts/reviews/"
            "m1-closure-fail.md"
        )
        fail_path = root / fail_rel
        fail_path.write_text(
            f"**Closure verdict:** FAIL\n**Reviewed range:** {base}..{head}\n",
            encoding="utf-8",
        )
        fail_prompt = review_dir / f"{closure_role}-{fail_task}-prompt.md"
        fail_header = closure_header.replace(
            str(closure_findings_snapshot.resolve()), str(fail_findings.resolve())
        ).replace(str(closure_path.resolve()), str(fail_path.resolve()))
        fail_prompt.write_bytes(
            fail_header.encode("utf-8")
            + b"\n--- CANONICAL AGENT BODY ---\n"
            + fail_snapshot.read_bytes()
        )
        fail_receipt = json.loads(json.dumps(closure_receipt))
        fail_receipt.update({
            "agent_task_id": fail_task,
            "agent_body_snapshot_path": f"artifacts/reviews/{closure_role}-{fail_task}-agent.md",
            "agent_body_sha256": _file_sha(fail_snapshot),
            "prompt_path": f"artifacts/reviews/{fail_prompt.name}",
            "prompt_sha256": _file_sha(fail_prompt),
            "critique_path": fail_rel,
            "critique_sha256": _file_sha(fail_path),
            "verdict": "FAIL",
            "findings_snapshot_path": f"artifacts/reviews/{closure_role}-{fail_task}-findings.json",
        })
        fail_receipt_file = review_dir / f"{closure_role}-{fail_task}-receipt.json"
        fail_receipt_file.write_text(json.dumps(fail_receipt), encoding="utf-8")
        append_time = _iso(review_completed_at, "fixture check completion")
        check("failed closure attempt is append-only", lambda: review_append(
            state_path, "closure", fail_receipt_file, append_time
        ))
        closure_receipt_file = review_dir / f"{closure_role}-task-closure-receipt.json"
        closure_receipt_file.write_text(json.dumps(closure_receipt), encoding="utf-8")
        check("locked closure attempt append", lambda: review_append(
            state_path, "closure", closure_receipt_file, append_time
        ))
        review = _load_json(art / POINTERS["review_manifest"], "review after closure append")
        state = _load_json(state_path, "state after closure append")
        state_runtime = dict(state); state_runtime["_state_path"] = str(state_path)
        check("valid independent closure receipt", lambda: validate_review_manifest(
            review, state_runtime, state_path, require_closure=True
        ))
        contradictory = json.loads(json.dumps(review))
        closure_path.write_text(
            f"**Closure verdict:** FAIL\n**Reviewed range:** {base}..{head}\n",
            encoding="utf-8",
        )
        contradictory["closure_reviews"][-1]["critique_sha256"] = _file_sha(closure_path)
        check("closure report/receipt verdict mismatch refused", lambda: validate_review_manifest(
            contradictory, state_runtime, state_path, require_closure=True
        ), "conflicts with report")
        closure_path.write_text(
            f"**Closure verdict:** PASS\n**Reviewed range:** {base}..{head}\n",
            encoding="utf-8",
        )
        review["closure_reviews"][-1]["critique_sha256"] = _file_sha(closure_path)
        (art / POINTERS["review_manifest"]).write_text(json.dumps(review), encoding="utf-8")

        check("real evidence reference validates", lambda: _validate_evidence_ref(real_ref, "fixture", state_dir))
        dangling_ref = dict(real_ref); dangling_ref["path"] = "artifacts/evidence/missing.log"
        check("dangling evidence reference refused", lambda: _validate_evidence_ref(dangling_ref, "fixture", state_dir), "missing")
        altered_ref = dict(real_ref); altered_ref["sha256"] = "c" * 64
        check("evidence content hash mismatch refused", lambda: _validate_evidence_ref(altered_ref, "fixture", state_dir), "content changed")

        render_repo = Path(td) / "render-repo"
        subprocess.run(["git", "init", "-q", str(render_repo)], check=True)
        subprocess.run(["git", "-C", str(render_repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(render_repo), "config", "user.name", "Test"], check=True)
        provenance_path = ".workspace/source-revision.json"
        target_id = "dev/core/app"
        image_digest = "sha256:" + "a" * 64
        image_uri = f"registry/app@{image_digest}"
        provenance_file = render_repo / provenance_path
        provenance_file.parent.mkdir(parents=True)
        provenance_file.write_text(json.dumps({
            "source_repo": "repo", "source_commit": head, "target_ids": [target_id],
            "artifacts": [{
                "uri": image_uri, "digest": image_digest, "target_ids": [target_id],
            }],
        }, sort_keys=True) + "\n", encoding="utf-8")
        (render_repo / "rendered.yaml").write_text("kind: Application\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(render_repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(render_repo), "commit", "-qm", "render"], check=True)
        render_head = _git_output(render_repo, "rev-parse", "HEAD").decode().strip()
        render_remote = Path(td) / "render-remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(render_remote)], check=True)
        subprocess.run(["git", "-C", str(render_repo), "remote", "add", "origin", str(render_remote)], check=True)
        subprocess.run(["git", "-C", str(render_repo), "push", "-q", "origin", "HEAD:refs/heads/dev"], check=True)
        release = {
            "schema_version": 2, "milestone_id": "m1", "generation": 1, "created_at": stamp,
            "producer": producer(), "publication_required": True, "not_required_reason": None,
            "delivery_kind": "mixed",
            "source_revisions": [{"repo": "repo", "commit": head}],
            "published_revisions": [{
                "repo": "repo", "remote": "origin", "branch": "dev", "source_commit": head,
                "commit": head, "verification": {
                    "method": "git-ls-remote+exact-commit",
                    "publication_mode": "publish",
                    "execution_evidence": command_ref(
                        "publication-execution-fixture",
                        [str(Path(shutil.which("git") or "git").resolve()), "push"],
                        _publication_environment(remote_url)[0],
                    ),
                    "verified_at": _utc_text(now),
                    "observed_commit": head, "source_matches_published": True, "exit_code": 0,
                    "evidence": evref("remote"),
                },
            }],
            "rendered_revisions": [{
                "repo": "deploy", "remote": str(render_remote), "branch": "dev", "commit": render_head,
                "source_repo": "repo", "source_commit": head, "verified_at": stamp,
                "target_ids": [target_id],
                "provenance_path": provenance_path,
                "provenance_sha256": _file_sha(provenance_file),
                "evidence": evref("render"),
            }],
            "artifacts": [{
                "kind": "container", "uri": image_uri, "digest": image_digest,
                "target_ids": [target_id],
                "resolved_at": stamp, "evidence": evref("image"),
            }],
        }
        (art / POINTERS["release_manifest"]).write_text(json.dumps(release), encoding="utf-8")
        artifact_resolver = Path(td) / "fixture-artifact-resolver"
        # Digest-qualified references (crane digest repo@sha256:...) echo their digest;
        # a mutable-tag reference (crane digest repo:tag) resolves to the fixed B1 tag
        # digest, so the tag-bound artifact path (Capability B1) is exercisable.
        b1_tag_digest = "sha256:" + "b" * 64
        artifact_resolver.write_text(
            "#!/bin/sh\ncase \"$2\" in\n"
            "  *@sha256:*) printf '%s\\n' \"${2##*@}\" ;;\n"
            f"  *) printf '%s\\n' \"{b1_tag_digest}\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        artifact_resolver.chmod(0o755)
        TEST_ARTIFACT_RESOLVER = (str(artifact_resolver.resolve()), _file_sha(artifact_resolver))
        check("valid mixed release", lambda: validate_release_manifest(release, state))
        missing_render = json.loads(json.dumps(release)); missing_render["rendered_revisions"] = []
        check("published but GitOps render missing", lambda: validate_release_manifest(missing_render, state), "rendered revision")

        # Capability C — the chart-bump intermediate hop lets a rendered revision whose
        # provenance names the CHART repo (the platform's gitops-provenance.py) still bind,
        # while threading the Go source through binds_image_tag.
        chart_commit = "c" * 40
        with_intermediate = json.loads(json.dumps(release))
        with_intermediate["intermediate_revisions"] = [{
            "repo": "charts/app", "remote": str(render_remote), "branch": "main",
            "commit": chart_commit, "role": "chart-bump", "binds_image_tag": head[:8],
            "verified_at": stamp, "evidence": evref("chart"),
        }]
        with_intermediate["rendered_revisions"][0]["source_repo"] = "charts/app"
        with_intermediate["rendered_revisions"][0]["source_commit"] = chart_commit
        check("chart-sourced render binds via intermediate revision",
              lambda: validate_release_manifest(with_intermediate, state))
        orphan_render = json.loads(json.dumps(with_intermediate)); orphan_render["intermediate_revisions"] = []
        check("chart-sourced render without its intermediate is refused",
              lambda: validate_release_manifest(orphan_render, state),
              "not bound to a declared source or intermediate revision")
        unlinked_tag = json.loads(json.dumps(with_intermediate))
        unlinked_tag["intermediate_revisions"][0]["binds_image_tag"] = "deadbeef"
        check("intermediate chart bump unlinked from source is refused",
              lambda: validate_release_manifest(unlinked_tag, state),
              "not a short-sha prefix")
        source_only_intermediate = json.loads(json.dumps(with_intermediate))
        source_only_intermediate["delivery_kind"] = "source-only"
        source_only_intermediate["rendered_revisions"] = []
        source_only_intermediate["artifacts"] = []
        check("source-only cannot carry an intermediate revision",
              lambda: validate_release_manifest(source_only_intermediate, state),
              "source-only delivery cannot claim")

        branch = _git_output(root, "branch", "--show-current").decode().strip()
        implementation = {
            "schema_version": 2, "milestone_id": "m1", "generation": 1, "created_at": stamp,
            "producer": producer(),
            "repositories": [{
                "repo": "repo", "path": str(root), "base_commit": base, "head_commit": head,
                "commit_range": f"{base}..{head}", "commits": [head], "branch": branch,
                "remote_url": remote_url,
            }],
            "checks": [check_receipt],
            "critique": {
                "code_review_manifest_sha256": _value_sha(_review_projection(review, "code")),
                "findings_register_sha256": _file_sha(findings_path), "gate_exit_code": 0,
                "checked_at": stamp, "open_critical": 0, "open_high": 0,
            },
            "rectification": {
                "commit": head, "not_required_reason": None,
                "closure_review_sha256": _file_sha(closure_path),
            },
            "generated_artifacts": [],
        }
        implementation_path = art / POINTERS["implementation_evidence"]
        implementation_path.write_text(json.dumps(implementation), encoding="utf-8")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        code_gate = gate(state_path, "code-complete", now)
        checkpoint_state = _load_json(state_path, "checkpoint state")
        checkpoint_state["artifact_bindings"].update(code_gate["bindings"])
        checkpoint_state.update(code_gate["derived"])
        checkpoint_state["phase"] = "code-complete"
        checkpoint_state["updated_at"] = _utc_text(now)
        checkpoint_state["phase_history"].append({
            "phase": "code-complete", "at": _utc_text(now),
        })
        state_path.write_text(json.dumps(checkpoint_state), encoding="utf-8")
        check("end-to-end findings/artifact code-complete gate", lambda: _expect(
            code_gate["derived"].get("implementation_status") == "validated",
            "code-complete gate did not validate implementation",
        ))
        state = checkpoint_state
        phase_stamp = state["updated_at"]
        code_receipt = {
            "bindings": checkpoint_state["artifact_bindings"],
            "derived": {
                "implementation_status": checkpoint_state["implementation_status"],
                "review_status": checkpoint_state["review_status"],
            },
        }
        check("checkpoint persisted validated implementation state", lambda: _expect(
            code_receipt["derived"]["implementation_status"] == "validated",
            "code checkpoint did not persist validated implementation",
        ))
        changed_closure_receipt = json.loads(json.dumps(review))
        changed_closure_receipt["closure_reviews"][-1]["provider"] = "forged-provider"
        changed_closure_meta = validate_review_manifest(
            changed_closure_receipt, {**state, "_state_path": str(state_path)},
            state_path, require_closure=True,
        )
        changed_closure_binding = _binding(
            "review_manifest", art / POINTERS["review_manifest"],
            changed_closure_receipt, "complete", changed_closure_meta,
        )
        check("bound closure receipt metadata mutation refused", lambda: _check_prior_binding(
            "review_manifest", changed_closure_binding,
            code_receipt["bindings"]["review_manifest"],
        ), "closure receipt changed")
        code_only_state = json.loads(json.dumps(checkpoint_state))
        code_only_state["publication_required"] = False
        code_only_state["publication_not_required_reason"] = "local-only tooling"
        code_only_state["operations_required"] = False
        code_only_state["operations_not_required_reason"] = "no live target"
        state_path.write_text(json.dumps(code_only_state), encoding="utf-8")
        check("post-closure delivery downgrade is refused", lambda: gate(
            state_path, "complete", now
        ), "delivery classification changed")
        state_path.write_text(json.dumps(checkpoint_state), encoding="utf-8")
        multi_repo = json.loads(json.dumps(implementation))
        multi_repo["repositories"].append(dict(multi_repo["repositories"][0], repo="other"))
        check("v2.0 multi-source milestone refused", lambda: validate_implementation_evidence(
            multi_repo, state, state_dir
        ), "exactly one source repository")

        # milestone-multi-repo-delivery-m1 S1.1/S1.3 — pin the FRONT gate.
        # The refusal above is the backstop and fires at code-complete, by which
        # point the state is unrecoverable (implementation_commits freezes at
        # implement-complete; PHASE_EDGES has no backward edge). The init-time
        # refusal is what keeps the failure recoverable, so it is pinned here:
        # without this, the wall could silently slide back to code-complete.
        # Shells out to the real bash script because no Python-layer assertion
        # can prove an exit code.
        def _init_gate_rc(repos: list[str], mid: str) -> int:
            gate_repo = Path(td) / f"init-gate-{mid}"
            reg_dir = gate_repo / ".claude" / "notes" / "roadmaps" / "gate"
            reg_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", str(gate_repo)], check=True)
            (reg_dir / "milestones.json").write_text(json.dumps({
                "schema_version": 1, "slug": "gate", "roadmap_doc": "plans/gate-roadmap.md",
                "generated_by": "self-test", "generated_at": stamp,
                "milestones": [{"id": mid, "title": "fixture", "lane": "now",
                                "status": "pending", "depends_on": [], "repos": repos}],
            }), encoding="utf-8")
            return subprocess.run(
                ["bash", str(SCRIPT_ROOT / "data" / "scripts" / "milestone-pipeline-init-state.sh"),
                 mid, "--repo-root", str(gate_repo), "--brief", "self-test fixture"],
                capture_output=True,
            ).returncode

        def _probe_multi_repo_refused() -> None:
            rc = _init_gate_rc(["source/a", "charts/a"], "gate-multi")
            if rc != 6:
                raise ValidationError(
                    f"init multi-repo gate: expected exit 6 for a 2-repo register, got {rc}"
                )
            leftover = Path(td) / "init-gate-gate-multi" / ".claude" / "notes" / "milestones"
            if leftover.exists():
                raise ValidationError(
                    "init multi-repo gate: refusal must leave no state behind (it ran before mkdir)"
                )

        def _probe_sibling_register_refused() -> None:
            # H1 (m1adv01, independently reproduced): `--find` searches only
            # REPO_ROOT, so the gate was blind to a milestone whose register lives
            # in a SIBLING clone — the shape of dna-rem-m7 and
            # svcreg-per-user-homescreen-m3, which declare no repo hosting their
            # own register and so could never be caught. Pins the platform sweep.
            plat = Path(td) / "sibling-plat" / "grp"
            home, target = plat / "repoA", plat / "repoB"
            (home / ".claude" / "notes" / "roadmaps" / "sib").mkdir(parents=True, exist_ok=True)
            target.mkdir(parents=True, exist_ok=True)
            for r in (home, target):
                subprocess.run(["git", "init", "-q", str(r)], check=True)
            (home / ".claude" / "notes" / "roadmaps" / "sib" / "milestones.json").write_text(
                json.dumps({
                    "schema_version": 1, "slug": "sib", "roadmap_doc": "plans/sib.md",
                    "generated_by": "self-test", "generated_at": stamp,
                    "milestones": [{"id": "sib-m1", "title": "fixture", "lane": "now",
                                    "status": "pending", "depends_on": [],
                                    "repos": ["repoB", "repoC"]}],
                }), encoding="utf-8")
            rc = subprocess.run(
                ["bash", str(SCRIPT_ROOT / "data" / "scripts" / "milestone-pipeline-init-state.sh"),
                 "sib-m1", "--repo-root", str(target), "--brief", "self-test fixture"],
                capture_output=True,
            ).returncode
            if rc != 6:
                raise ValidationError(
                    "init multi-repo gate: register in a sibling clone was not swept; "
                    f"expected exit 6, got {rc}"
                )

        def _write_fixture_register(reg_dir: Path, mid: str, repos: list[str]) -> None:
            reg_dir.mkdir(parents=True, exist_ok=True)
            (reg_dir / "milestones.json").write_text(
                json.dumps({
                    "schema_version": 1, "slug": "fx", "roadmap_doc": "plans/fx.md",
                    "generated_by": "self-test", "generated_at": stamp,
                    "milestones": [{"id": mid, "title": "fixture", "lane": "now",
                                    "status": "pending", "depends_on": [],
                                    "repos": repos}],
                }), encoding="utf-8")

        def _probe_platform_root_register_refused() -> None:
            # V-H1 (m1dia04/m1dia05, independently reproduced): the sweep's fixed
            # "$REPO_ROOT/../.." anchor + "*/*/" glob encoded depth-2 twice, so a
            # register at the PLATFORM ROOT was invisible — the live shape of
            # session-logout-idle-timeout-m2 (3 repos), which declares no repo
            # hosting its own register, so `--find` misses it too and init
            # returned rc=0. Pins the ancestor walk at depth 0.
            plat = Path(td) / "root-plat"
            target = plat / "tools" / "kit"
            target.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            _write_fixture_register(plat / ".claude" / "notes" / "roadmaps" / "fx",
                                    "rootreg-m1", ["a", "b", "c"])
            rc = subprocess.run(
                ["bash", str(SCRIPT_ROOT / "data" / "scripts" / "milestone-pipeline-init-state.sh"),
                 "rootreg-m1", "--repo-root", str(target), "--brief", "self-test fixture"],
                capture_output=True,
            ).returncode
            if rc != 6:
                raise ValidationError(
                    "init multi-repo gate: register at the platform root was not swept; "
                    f"expected exit 6, got {rc}"
                )

        def _probe_depth1_repo_root_refused() -> None:
            # V-H1, second direction: with REPO_ROOT one level below the platform
            # root (the live shape of ci-cd-templates, plans, platform-model and
            # sandbox), the old "../.." anchor overshot the platform entirely and
            # the gate saw zero registers — so even dispatcher-receipt-authz-m2,
            # which the old gate DID catch from a depth-2 clone, initialized clean
            # from a depth-1 one. Coverage must not depend on which repo you
            # happen to init from.
            plat = Path(td) / "d1-plat"
            target = plat / "ci-cd-templates"
            target.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            _write_fixture_register(plat / "grp" / "repoA" / ".claude" / "notes" / "roadmaps" / "fx",
                                    "d1-m1", ["repoB", "repoC"])
            rc = subprocess.run(
                ["bash", str(SCRIPT_ROOT / "data" / "scripts" / "milestone-pipeline-init-state.sh"),
                 "d1-m1", "--repo-root", str(target), "--brief", "self-test fixture"],
                capture_output=True,
            ).returncode
            if rc != 6:
                raise ValidationError(
                    "init multi-repo gate: depth-1 REPO_ROOT bypassed the sweep; "
                    f"expected exit 6, got {rc}"
                )

        def _probe_single_repo_not_refused() -> None:
            # Asserts only that THIS gate does not fire. Deliberately not `== 0`:
            # a single-repo init continues to the KIT_STATUS check (:241), which
            # legitimately refuses whenever the kit has uncommitted changes — so
            # `== 0` would make this test hostage to the developer's worktree.
            rc = _init_gate_rc(["source/a"], "gate-single")
            if rc == 6:
                raise ValidationError(
                    "init multi-repo gate: fired on a single-repo register (false positive)"
                )

        check("init refuses a multi-repo register (exit 6, nothing created)",
              _probe_multi_repo_refused)
        check("init sweeps sibling-clone registers (exit 6)",
              _probe_sibling_register_refused)
        check("init sweeps a platform-root register (exit 6)",
              _probe_platform_root_register_refused)
        check("init sweeps from a depth-1 repo root (exit 6)",
              _probe_depth1_repo_root_refused)
        check("init multi-repo gate does not fire on a single-repo register",
              _probe_single_repo_not_refused)
        trivial_check = json.loads(json.dumps(implementation))
        trivial_check["checks"][0]["argv"] = ["true"]
        trivial_check["checks"][0]["command"] = "true"
        trivial_check["checks"][0]["evidence"]["command"] = "true"
        check("trivial success check refused", lambda: _validate_implementation_against_repo(
            trivial_check, state, state_path
        ), "trivial success")
        check("release bound to reviewed implementation and live refs", lambda: (
            validate_release_manifest(release, state, state_dir),
            _validate_release_against_implementation(release, implementation, state_path),
        ))
        wrong_source = json.loads(json.dumps(release))
        wrong_source["source_revisions"][0]["commit"] = base
        check("release from unreviewed source refused", lambda: _validate_release_against_implementation(
            wrong_source, implementation, state_path
        ), "exactly equal")

        # Capability B1 — a render whose provenance records NO artifact digest (empty
        # `artifacts`, as the platform renderer does), where the released digest is bound
        # at release time by resolving the chart-bump image tag from intermediate_revisions.
        b1_render = Path(td) / "b1-render"
        subprocess.run(["git", "init", "-q", str(b1_render)], check=True)
        subprocess.run(["git", "-C", str(b1_render), "config", "user.email", "t@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(b1_render), "config", "user.name", "Test"], check=True)
        b1_prov = b1_render / provenance_path
        b1_prov.parent.mkdir(parents=True)
        b1_prov.write_text(json.dumps({
            "source_repo": "repo", "source_commit": head, "target_ids": [target_id], "artifacts": [],
        }, sort_keys=True) + "\n", encoding="utf-8")
        (b1_render / "rendered.yaml").write_text("kind: Application\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(b1_render), "add", "."], check=True)
        subprocess.run(["git", "-C", str(b1_render), "commit", "-qm", "render"], check=True)
        b1_render_head = _git_output(b1_render, "rev-parse", "HEAD").decode().strip()
        b1_render_remote = Path(td) / "b1-render-remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(b1_render_remote)], check=True)
        subprocess.run(["git", "-C", str(b1_render), "remote", "add", "origin", str(b1_render_remote)], check=True)
        subprocess.run(["git", "-C", str(b1_render), "push", "-q", "origin", "HEAD:refs/heads/dev"], check=True)
        b1_release = json.loads(json.dumps(release))
        b1_release["rendered_revisions"] = [{
            "repo": "deploy", "remote": str(b1_render_remote), "branch": "dev", "commit": b1_render_head,
            "source_repo": "repo", "source_commit": head, "verified_at": stamp,
            "target_ids": [target_id], "provenance_path": provenance_path,
            "provenance_sha256": _file_sha(b1_prov), "evidence": evref("b1-render"),
        }]
        b1_release["artifacts"] = [{
            "kind": "container", "uri": f"registry/app@{b1_tag_digest}", "digest": b1_tag_digest,
            "target_ids": [target_id], "resolved_at": stamp, "evidence": evref("b1-image"),
        }]
        b1_release["intermediate_revisions"] = [{
            "repo": "charts/app", "remote": str(b1_render_remote), "branch": "main", "commit": "c" * 40,
            "role": "chart-bump", "binds_image_tag": head[:8], "verified_at": stamp,
            "evidence": evref("b1-chart"),
        }]
        check("tag-bound artifact binds via chart-bump tag resolution", lambda: (
            validate_release_manifest(b1_release, state, state_dir),
            _validate_release_against_implementation(b1_release, implementation, state_path),
        ))
        b1_no_intermediate = json.loads(json.dumps(b1_release))
        b1_no_intermediate["intermediate_revisions"] = []
        check("tag-bound artifact without a chart-bump tag is refused", lambda: (
            validate_release_manifest(b1_no_intermediate, state, state_dir),
            _validate_release_against_implementation(b1_no_intermediate, implementation, state_path),
        ), "no intermediate")

        state["phase"] = "publish-running"
        state["phase_history"].append({"phase": "publish-running", "at": phase_stamp})
        state["artifact_bindings"] = code_receipt["bindings"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        original_preflight = _preflight_publication_delivery_policy
        original_candidate = _publication_candidate
        candidate_calls = 0

        def _forced_preflight(*_args: Any, **_kwargs: Any) -> None:
            raise ValidationError("fixture policy preflight refusal")

        def _unexpected_candidate(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal candidate_calls
            candidate_calls += 1
            raise ValidationError("fixture publication candidate reached after preflight")

        globals()["_preflight_publication_delivery_policy"] = _forced_preflight
        globals()["_publication_candidate"] = _unexpected_candidate
        try:
            check("publication preview runs policy preflight before candidate", lambda: publication_preview(
                state_path, "publish", now
            ), "fixture policy preflight refusal")
            check("publication authorization runs policy preflight before candidate", lambda: publication_authorize(
                state_path, "Chris Dare", "0" * 64, "publish", now
            ), "fixture policy preflight refusal")
        finally:
            globals()["_preflight_publication_delivery_policy"] = original_preflight
            globals()["_publication_candidate"] = original_candidate
        check("policy preflight failure creates no candidate, observation, or intent", lambda: _expect(
            candidate_calls == 0
            and not (art / POINTERS["publication_intent"]).exists()
            and not (art / "publication").exists(),
            "policy preflight reached publication candidate state",
        ))
        subprocess.run(
            ["git", "-C", str(root), "push", "-q", "--delete", "origin", "dev"],
            check=True,
        )
        subprocess.run([
            "git", "-C", str(root), "config", "--local",
            "url.file:///tmp/unreviewed-endpoint.insteadOf", remote_url,
        ], check=True)
        check("publication preview rejects local URL rewrites", lambda: publication_preview(
            state_path, "publish", now
        ), "current canonical origin changed")
        subprocess.run([
            "git", "-C", str(root), "config", "--local", "--unset-all",
            "url.file:///tmp/unreviewed-endpoint.insteadOf",
        ], check=True)
        subprocess.run([
            "git", "-C", str(root), "config", "extensions.worktreeConfig", "true",
        ], check=True)
        subprocess.run([
            "git", "-C", str(root), "config", "--worktree",
            "url.file:///tmp/unreviewed-worktree.insteadOf", remote_url,
        ], check=True)
        check("publication preview rejects per-worktree URL rewrites", lambda: publication_preview(
            state_path, "publish", now
        ), "current canonical origin changed")
        worktree_config = Path(
            _git_output(root, "rev-parse", "--git-dir").decode().strip()
        )
        if not worktree_config.is_absolute():
            worktree_config = root / worktree_config
        (worktree_config / "config.worktree").unlink(missing_ok=True)
        subprocess.run([
            "git", "-C", str(root), "config", "--unset-all", "extensions.worktreeConfig",
        ], check=True)
        publication_preview_result = publication_preview(state_path, "publish", now)
        check("publication preview is read-only and exposes exact CAS", lambda: _expect(
            not (art / POINTERS["publication_intent"]).exists()
            and publication_preview_result["proposed_action"]
            == publication_preview_result["scope"]["push_argv"]
            and publication_preview_result["proposed_action"][-3:] == [
                "--", remote_url, f"{head}:refs/heads/dev"
            ],
            "publication preview mutated state or hid the exact action",
        ))
        check("publication authorization rejects unseen scope", lambda: publication_authorize(
            state_path, "Chris Dare", "f" * 64, "publish", now
        ), "preview scope changed")
        publication_result = publication_authorize(
            state_path, "Chris Dare", publication_preview_result["scope_hash"],
            "publish", now,
        )
        check("publication authorization binds previewed scope", lambda: _expect(
            publication_result["authorized_action"]
            == publication_preview_result["proposed_action"],
            "authorized action differs from the human-reviewed preview",
        ))
        check("publication intent cannot be replaced without supersession", lambda: publication_authorize(
            state_path, "Chris Dare", publication_preview_result["scope_hash"],
            "publish", now
        ), "same-scope reauthorization requires a receipted failed execution")
        original_apply_preflight = _preflight_publication_delivery_policy
        original_apply_observe = _publication_observe
        apply_observation_calls = 0

        def _forced_apply_preflight(*_args: Any, **_kwargs: Any) -> None:
            raise ValidationError("fixture apply policy preflight refusal")

        def _unexpected_apply_observe(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal apply_observation_calls
            apply_observation_calls += 1
            raise ValidationError("fixture publication apply observed remote after preflight")

        globals()["_preflight_publication_delivery_policy"] = _forced_apply_preflight
        globals()["_publication_observe"] = _unexpected_apply_observe
        try:
            check("publication apply runs policy preflight before remote observation", lambda: publication_apply(
                state_path, now
            ), "fixture apply policy preflight refusal")
        finally:
            globals()["_preflight_publication_delivery_policy"] = original_apply_preflight
            globals()["_publication_observe"] = original_apply_observe
        check("publication apply preflight failure performs no remote observation", lambda: _expect(
            apply_observation_calls == 0,
            "publication apply reached remote observation after preflight failure",
        ))
        subprocess.run(
            ["git", "-C", str(root), "push", "-q", "origin",
             f"{base}:refs/heads/dev"],
            check=True,
        )
        check("publication writer rejects remote CAS race", lambda: publication_apply(
            state_path, now
        ), "remote precondition changed")
        race_tree = _git_output(root, "rev-parse", f"{base}^{{tree}}").decode().strip()
        race_commit = subprocess.run(
            ["git", "-C", str(root), "commit-tree", race_tree, "-p", base],
            input=b"publication race\n", capture_output=True, check=True,
        ).stdout.decode().strip()
        subprocess.run(
            ["git", "-C", str(root), "push", "-q", "--force", "origin",
             f"{race_commit}:refs/heads/dev"],
            check=True,
        )
        check("publication preview rejects non-fast-forward race", lambda: publication_preview(
            state_path, "publish", now
        ), "normal publication must be a fast-forward")
        subprocess.run(
            ["git", "-C", str(root), "push", "-q", "--force", "origin",
             f"{base}:refs/heads/dev"],
            check=True,
        )
        supersession_preview = publication_preview(state_path, "publish", now)
        supersession_result = publication_authorize(
            state_path, "Chris Dare", supersession_preview["scope_hash"],
            "publish", now,
        )
        superseded_intent = _load_json(
            art / POINTERS["publication_intent"], "superseded publication intent"
        )
        check("CAS race is repaired by append-only intent supersession", lambda: _expect(
            supersession_preview["generation"] == 2
            and supersession_result["scope_hash"] == supersession_preview["scope_hash"]
            and superseded_intent["generation"] == 2
            and len(superseded_intent["superseded_intents"]) == 1,
            "publication supersession did not preserve the prior authorization",
        ))
        hook_proof = Path(td) / "source-pre-push-hook-fired"
        git_dir = Path(_git_output(root, "rev-parse", "--git-dir").decode().strip())
        if not git_dir.is_absolute():
            git_dir = root / git_dir
        pre_push = git_dir / "hooks" / "pre-push"
        pre_push.write_text(
            f"#!/bin/sh\nprintf fired > {shlex.quote(str(hook_proof))}\n",
            encoding="utf-8",
        )
        pre_push.chmod(0o755)
        publication_apply_result = publication_apply(state_path, now)
        check("isolated publication does not execute source hooks", lambda: _expect(
            not hook_proof.exists(), "source repository pre-push hook executed"
        ))
        release["published_revisions"][0]["verification"] = publication_apply_result[
            "verification"
        ]
        (art / POINTERS["release_manifest"]).write_text(
            json.dumps(release), encoding="utf-8"
        )
        state = _load_json(state_path, "state after publication intent")
        published_receipt = gate(state_path, "published", now)
        check("end-to-end published gate re-reads live remote", lambda: _expect(
            "release_manifest" in published_receipt["bindings"],
            "published gate did not bind release manifest",
        ))
        state["phase"] = "published"
        state["phase_history"].append({"phase": "published", "at": phase_stamp})
        state["artifact_bindings"].update(published_receipt["bindings"])
        state["implementation_status"] = "published"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        publisher = Path(td) / "publisher"
        subprocess.run(["git", "clone", "-q", "-b", "dev", str(bare_remote), str(publisher)], check=True)
        subprocess.run(["git", "-C", str(publisher), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(publisher), "config", "user.name", "Test"], check=True)
        (publisher / "unreviewed.txt").write_text("unreviewed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(publisher), "add", "."], check=True)
        subprocess.run(["git", "-C", str(publisher), "commit", "-qm", "unreviewed descendant"], check=True)
        subprocess.run(["git", "-C", str(publisher), "push", "-q", "origin", "dev"], check=True)
        unreviewed = _git_output(publisher, "rev-parse", "HEAD").decode().strip()
        descendant_release = json.loads(json.dumps(release))
        descendant_release["published_revisions"][0]["commit"] = unreviewed
        descendant_release["published_revisions"][0]["verification"]["observed_commit"] = unreviewed
        check("unreviewed published descendant refused", lambda: _validate_release_against_implementation(
            descendant_release, implementation, state_path
        ), "exact reviewed commit")
        subprocess.run(
            ["git", "-C", str(root), "push", "-q", "--force", "origin", f"{head}:refs/heads/dev"],
            check=True,
        )

        fail_smoke_flag = Path(td) / "fail-smoke"
        apply_count_file = Path(td) / "apply-count"
        operation_collector = Path(td) / "fixture-operation-collector"
        fail_apply_flag = Path(td) / "fail-apply"
        desired = {
            "source_commit": head, "render_commit": render_head,
            "image_digest": image_digest,
        }
        operation_collector.write_text(
            f"#!{sys.executable}\nimport json, pathlib, sys\n"
            f"desired = {desired!r}\nflag = pathlib.Path({str(fail_smoke_flag)!r})\n"
            f"fail_apply = pathlib.Path({str(fail_apply_flag)!r})\n"
            f"apply_count = pathlib.Path({str(apply_count_file)!r})\n"
            "if sys.argv[1] == 'apply':\n"
            "    count = int(apply_count.read_text()) if apply_count.exists() else 0\n"
            "    apply_count.write_text(str(count + 1))\n"
            "    if fail_apply.exists():\n"
            "        print('apply failed', file=sys.stderr); raise SystemExit(1)\n"
            "    print('applied')\n"
            "elif sys.argv[1] == 'observe':\n"
            "    print(json.dumps({'source_commit': desired['source_commit'], "
            "'render_commit': desired['render_commit'], 'image_digest': desired['image_digest'], "
            "'generation': 1}))\n"
            "elif sys.argv[1] == 'behavioral-smoke' and flag.exists():\n"
            "    print('smoke failed', file=sys.stderr); raise SystemExit(1)\n"
            "else:\n    print('ok')\n",
            encoding="utf-8",
        )
        operation_collector.chmod(0o755)
        operation_collector_sha = _file_sha(operation_collector)
        contract_kinds = [
            "argocd-synced", "deployment-observed-generation",
            "pod-image-digest", "service-selects-workload",
            "ingress-routes-service", "behavioral-smoke",
        ]
        target = {
            "id": target_id, "environment": "dev", "account": "123", "cluster": "core",
            "resource": "Application/app", "apply_method": "gitops-manual-sync", "desired": desired,
            "execution_environment": {
                "PATH": "/usr/bin:/bin",
                "MILESTONE_TARGET_ID": target_id,
                "MILESTONE_ENVIRONMENT": "dev",
                "MILESTONE_ACCOUNT": "123",
                "MILESTONE_CLUSTER": "core",
                "MILESTONE_RESOURCE": "Application/app",
            },
            "execution_contexts": {},
            "verification_profile": {
                "kind": "argocd-web-workload-v1",
                "argocd_application": "app",
                "argocd_application_uid": "fixture-app-uid",
                "argocd_project": "default",
                "source_repo_url": "https://git.example.invalid/workspace/app.git",
                "source_path": "deploy/app",
                "destination_server": "https://kubernetes.example.invalid",
                "destination_namespace": "app",
                "deployment_name": "app",
                "deployment_uid": "fixture-deployment-uid",
                "pod_selector": "app.kubernetes.io/name=app",
                "container_name": "app",
                "service_name": "app",
                "service_uid": "fixture-service-uid",
                "service_port": 443,
                "ingress_name": "app",
                "ingress_uid": "fixture-ingress-uid",
                "ingress_path": "/healthz",
                "behavioral_smoke_url": "https://app.example.invalid/healthz",
                "behavioral_smoke_status": 200,
            },
            "apply_command": [str(operation_collector), "apply"],
            "apply_executable_sha256": operation_collector_sha,
            "apply_timeout_seconds": 30,
            "observation_command": [str(operation_collector), "observe"],
            "observation_executable_sha256": operation_collector_sha,
            "observation_timeout_seconds": 30,
            "verification_contract": [
                {"kind": kind, "command": [str(operation_collector), kind],
                 "executable_sha256": operation_collector_sha, "timeout_seconds": 30}
                for kind in contract_kinds
            ],
            "rollback": "revert", "operations_owner": "app", "verification_owner": "sre",
        }
        plan = {
            "schema_version": 2, "milestone_id": "m1", "generation": 1, "created_at": stamp,
            "producer": producer(), "operations_required": True, "not_required_reason": None,
            "plan_hash": "", "max_evidence_age_seconds": 3600, "targets": [target],
        }
        plan["plan_hash"] = plan_hash(plan)
        (art / POINTERS["operations_plan"]).write_text(json.dumps(plan), encoding="utf-8")
        internal_target = json.loads(json.dumps(target))
        internal_target.update({
            "apply_method": "gitops-auto-sync-observe-v1",
            "apply_command": None, "apply_executable_sha256": None,
            "apply_timeout_seconds": None,
        })
        internal_target["verification_profile"] = {
            "kind": "argocd-istio-internal-http-v1",
            "argocd_application": "app", "argocd_application_uid": "fixture-app-uid",
            "argocd_project": "default",
            "source_repo_url": "https://git.example.invalid/deploy.git",
            "source_target_revision": "dev", "source_path": "deploy/app",
            "destination_server": "https://kubernetes.example.invalid",
            "destination_namespace": "app", "resource_namespace": "app",
            "deployment_name": "app", "deployment_uid": "fixture-deployment-uid",
            "pod_selector": "app.kubernetes.io/name=app", "container_name": "app",
            "service_name": "app", "service_uid": "fixture-service-uid",
            "service_port": 8080, "service_port_name": "http",
            "service_target_port": "http",
            "service_host": "app.app.svc.cluster.local", "readiness_path": "/readyz",
            "behavioral_smoke_url": "http://app.app.svc.cluster.local:8080/readyz",
            "behavioral_smoke_status": 200,
            "probe_origin": {
                "namespace": "app", "pod_name": "probe", "pod_uid": "probe-uid",
                "service_account_name": "probe", "container_name": "probe",
                "container_image_digest": "sha256:" + "c" * 64,
                "curl_path": "/usr/bin/curl", "istio_proxy_container": "istio-proxy",
            },
        }
        internal_kinds = sorted(_required_probe_kinds(
            "argocd-istio-internal-http-v1", True
        ))
        internal_target["verification_contract"] = [
            {"kind": kind, "command": [str(operation_collector), kind],
             "executable_sha256": operation_collector_sha, "timeout_seconds": 30}
            for kind in internal_kinds
        ]
        internal_target["auto_sync_binding"] = {
            "publication_intent_id": "m1-publication-auto", "publication_scope_hash": "d" * 64,
            "delivery_effect_sha256": "e" * 64, "target_id": target_id,
            "render_remote": internal_target["verification_profile"]["source_repo_url"],
            "render_branch": "dev", "argocd_application_uid": "fixture-app-uid",
            "verification_action_sha256": "0" * 64,
            "automated": {"enabled": True, "prune": True, "self_heal": True, "allow_empty": False},
        }
        internal_target["auto_sync_binding"]["verification_action_sha256"] = _value_sha(
            _auto_verification_action(internal_target)
        )
        internal_plan = {
            **{key: value for key, value in plan.items() if key not in {"targets", "plan_hash"}},
            "targets": [internal_target], "plan_hash": "",
        }
        internal_plan["plan_hash"] = plan_hash(internal_plan)
        check("typed same-cluster auto-sync plan validates", lambda: validate_operations_plan(
            internal_plan, {**state, "_state_path": str(state_path)}, now,
            verify_executables=False,
        ))
        auto_evidence = _initial_operations_evidence(
            {**state, "_state_path": str(state_path)}, internal_plan, now
        )
        auto_scope = target_scope_hash(internal_plan, internal_target)
        auto_observation_command = shlex.join(internal_target["observation_command"])
        auto_observation_ref = {
            "path": "artifacts/operations/fixture/auto-observation.json",
            "sha256": "f" * 64, "media_type": "application/json", "size_bytes": 2,
            "collector": "fixture", "command": auto_observation_command,
        }
        auto_evidence["targets"][0]["attempts"].append({
            "attempt_id": "auto-a0001", "sequence": 1,
            "previous_attempt_sha256": None, "recorded_at": stamp,
            "authorization": {
                "decision": "approved", "by": "Chris Dare",
                "method": "publication-effect", "at": stamp,
                "scope_hash": auto_scope,
                "publication_scope_hash": internal_target["auto_sync_binding"]["publication_scope_hash"],
                "delivery_effect_sha256": internal_target["auto_sync_binding"]["delivery_effect_sha256"],
                "target_id": target_id,
            },
            "apply": {
                "kind": "observed-auto-sync-v1", "status": "applied", "at": stamp,
                "actor": "argocd-auto-sync-observer", "idempotency_key": None,
                "intent_evidence": None, "observed": {**desired, "generation": 1},
                "evidence": None, "observation_evidence": auto_observation_ref,
                "failure_reason": None, "recovered_from_ambiguous": False,
            },
            "verification": {
                "status": "pending", "observed_at": None, "observed": None,
                "observation_evidence": None, "probes": [],
            },
        })
        auto_evidence["targets"][0]["status"] = "applied"
        check("publication-effect auto-sync evidence validates without apply receipt", lambda: (
            validate_operations_evidence(
                auto_evidence, {**state, "_state_path": str(state_path)}, internal_plan,
                now, None, enforce_latest_freshness=False, verify_executables=False,
            )
        ))
        auto_schema_path = art / "auto-sync-evidence-schema-fixture.json"
        auto_schema_path.write_text(json.dumps(auto_evidence), encoding="utf-8")
        auto_schema_result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("milestone-pipeline-schema-check.py")),
                "--instance", "milestone-operations-evidence-v2.schema.json",
                str(auto_schema_path),
            ], capture_output=True, text=True,
        )
        check("auto-sync evidence round-trips through Draft 2020-12 schema", lambda: _expect(
            auto_schema_result.returncode == 0,
            f"auto-sync evidence schema rejected: {auto_schema_result.stderr}",
        ))
        manual_receipt_launder = json.loads(json.dumps(auto_evidence))
        manual_receipt_launder["targets"][0]["attempts"][0]["apply"].pop("kind")
        manual_launder_path = art / "manual-receipt-launder-schema-fixture.json"
        manual_launder_path.write_text(
            json.dumps(manual_receipt_launder), encoding="utf-8"
        )
        manual_launder_result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("milestone-pipeline-schema-check.py")),
                "--instance", "milestone-operations-evidence-v2.schema.json",
                str(manual_launder_path),
            ], capture_output=True, text=True,
        )
        check("manual evidence cannot borrow null auto-sync apply receipts", lambda: _expect(
            manual_launder_result.returncode != 0,
            "manual evidence schema accepted null execution/idempotency receipts",
        ))
        wrong_internal = json.loads(json.dumps(internal_plan))
        wrong_internal["targets"][0]["verification_profile"]["service_host"] = (
            "app.app.svc.cluster-example.global"
        )
        wrong_internal["plan_hash"] = plan_hash(wrong_internal)
        check("same-cluster profile refuses .global alias", lambda: validate_operations_plan(
            wrong_internal, {**state, "_state_path": str(state_path)}, now,
            verify_executables=False,
        ), "exact Service FQDN")
        eastwest_profile = json.loads(json.dumps(internal_target["verification_profile"]))
        eastwest_profile["kind"] = "argocd-istio-eastwest-v1"
        eastwest_profile.pop("service_host")
        eastwest_profile["global_service_host"] = "app.app.svc.cluster-example.global"
        eastwest_profile["behavioral_smoke_url"] = (
            "http://app.app.svc.cluster-example.global:8080/readyz"
        )
        eastwest_profile["sender"] = {
            "namespace": "istio-system", "service_entry_name": "app-global",
            "service_entry_uid": "sender-se-uid", "destination_rule_name": "app-global",
            "destination_rule_uid": "sender-dr-uid",
            "eastwest_endpoint_host": "eastwest.example.invalid",
            "eastwest_endpoint_port": 18443, "proxy_pod": "probe",
            "proxy_uid": "probe-uid",
        }
        eastwest_profile["receiver"] = {
            "namespace": "istio-system", "service_entry_name": "app-global",
            "service_entry_uid": "receiver-se-uid", "destination_rule_name": "app-local",
            "destination_rule_uid": "receiver-dr-uid", "envoy_filter_name": "app-global",
            "envoy_filter_uid": "receiver-ef-uid", "envoy_cluster_name": "app-global",
            "local_service_host": "app.app.svc.cluster.local", "local_service_port": 8080,
            "gateway_proxy_pod": "eastwest-gateway", "gateway_proxy_uid": "gateway-uid",
        }
        check("typed cross-cluster .global profile validates independently", lambda: (
            _validate_verification_profile(
                eastwest_profile, internal_target, "fixture.eastwest_profile"
            )
        ))
        not_global = json.loads(json.dumps(eastwest_profile))
        not_global["global_service_host"] = "app.app.svc.cluster.local"
        not_global["behavioral_smoke_url"] = "http://app.app.svc.cluster.local:8080/readyz"
        check("east-west profile refuses same-cluster FQDN", lambda: (
            _validate_verification_profile(
                not_global, internal_target, "fixture.eastwest_profile"
            )
        ), "exact tenant .global host")
        generic_auto = json.loads(json.dumps(internal_plan))
        generic_auto["targets"][0]["apply_method"] = "gitops-auto-sync"
        generic_auto["plan_hash"] = plan_hash(generic_auto)
        check("generic auto-sync method is refused", lambda: validate_operations_plan(
            generic_auto, {**state, "_state_path": str(state_path)}, now,
            verify_executables=False,
        ), "generic or unknown auto-sync")
        automatic_target = {
            "id": target_id, "environment": "dev", "account": "123", "cluster": "core",
            "resource": "Application/app", "argocd_application": "app",
            "argocd_server": "https://argocd.example.invalid", "argocd_context": "dev",
            "argocd_config_path": "/config/argocd.json", "argocd_config_sha256": "a" * 64,
            "certificate_authority_sha256": "a" * 64, "argocd_project": "default",
            "source_repo_url": "https://git.example.invalid/deploy.git",
            "source_target_revision": "dev", "source_path": "deploy/app",
            "destination_server": "https://kubernetes.example.invalid",
            "destination_namespace": "app",
            "verification_action_sha256": internal_target["auto_sync_binding"]["verification_action_sha256"],
            "automated": {"enabled": True, "prune": True, "self_heal": True, "allow_empty": False},
        }
        target_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", target_id).strip("-")
        automatic = {
            "kind": "ci-render-argocd-auto-sync-v1",
            "render": {"remote": automatic_target["source_repo_url"], "branch": "dev",
                       "protected": True, "provenance_path": ".workspace/source-revision.json"},
            "ci_render": {"provider": "gitlab", "project": "platform/app",
                          "source_ref": "main", "pipeline_source": "push",
                          "config_sha256": "a" * 64, "deploy_job": "deploy:dev",
                          "protected_environment": "dev", "writes_only_render_target": True},
            "targets": [automatic_target],
            "cascade_steps": [
                {"id": "source-publication", "kind": "source-publication", "depends_on": [], "target_id": None},
                {"id": "ci-render", "kind": "ci-render", "depends_on": ["source-publication"], "target_id": None},
                {"id": "render-publication", "kind": "render-publication", "depends_on": ["ci-render"], "target_id": None},
                {"id": f"argocd-auto-sync-{target_slug}", "kind": "argocd-auto-sync", "depends_on": ["render-publication"], "target_id": target_id},
            ],
        }
        check("exact finite publication cascade validates", lambda: _validate_automatic_gitops_contract(
            automatic, [automatic_target["source_repo_url"]], "fixture.automatic_gitops"
        ))
        missing_step = json.loads(json.dumps(automatic)); missing_step["cascade_steps"].pop()
        check("omitted conditional cascade step is refused", lambda: _validate_automatic_gitops_contract(
            missing_step, [automatic_target["source_repo_url"]], "fixture.automatic_gitops"
        ), "every material cascade step")
        mr_trigger = json.loads(json.dumps(automatic))
        mr_trigger["ci_render"]["pipeline_source"] = "merge_request_event"
        check("non-push automatic render trigger is refused", lambda: (
            _validate_automatic_gitops_contract(
                mr_trigger, [automatic_target["source_repo_url"]],
                "fixture.automatic_gitops",
            )
        ), "exact push trigger")
        disabled_auto = json.loads(json.dumps(automatic))
        disabled_auto["targets"][0]["automated"]["enabled"] = False
        check("disabled Argo policy cannot claim an automatic effect", lambda: (
            _validate_automatic_gitops_contract(
                disabled_auto, [automatic_target["source_repo_url"]],
                "fixture.automatic_gitops",
            )
        ), "auto-sync must be explicitly enabled")

        # Fanout (multi-deploy-repo) cascade: source -> image -> chart-bump -> 2 legs -> 2 targets.
        fan_registry = "registry.example/app"
        fan_chart_remote = "https://git.example.invalid/chart.git"
        leg_a_remote = "https://git.example.invalid/deploy-a.git"
        leg_b_remote = "https://git.example.invalid/deploy-b.git"

        def fan_target(tid: str, leg_id: str, remote: str) -> dict[str, Any]:
            return {
                "id": tid, "render_leg_id": leg_id, "environment": "dev", "account": "123",
                "cluster": "core", "resource": "Application/app", "argocd_application": "app",
                "argocd_server": "https://argocd.example.invalid", "argocd_context": "dev",
                "argocd_config_path": "/config/argocd.json", "argocd_config_sha256": "a" * 64,
                "certificate_authority_sha256": "a" * 64, "argocd_project": "default",
                "source_repo_url": remote, "source_target_revision": "dev", "source_path": "deploy/app",
                "destination_server": "https://kubernetes.example.invalid",
                "destination_namespace": "app", "verification_action_sha256": "0" * 64,
                "automated": {"enabled": True, "prune": True, "self_heal": True, "allow_empty": False},
            }

        def fan_leg(leg_id: str, remote: str) -> dict[str, Any]:
            return {
                "id": leg_id, "remote": remote, "branch": "dev", "protected": True,
                "provenance_path": ".workspace/source-revisions/app.json",
                "ci_render": {"provider": "gitlab", "project": "platform/chart", "source_ref": "main",
                              "pipeline_source": "push", "config_sha256": "a" * 64, "deploy_job": "deploy",
                              "protected_environment": "dev", "writes_only_render_target": True},
            }

        fanout = {
            "kind": "ci-render-argocd-auto-sync-fanout-v1",
            "image_build": {"provider": "gitlab", "project": "platform/source",
                            "registry_repo": fan_registry, "tag_scheme": "source-short-sha"},
            "chart": {"remote": fan_chart_remote, "branch": "main", "bump_path": "base/kustomization.yaml"},
            "render_legs": [fan_leg("commercial", leg_a_remote), fan_leg("commercial-mono", leg_b_remote)],
            "targets": [fan_target("dev-commercial", "commercial", leg_a_remote),
                        fan_target("dev-mono", "commercial-mono", leg_b_remote)],
            "cascade_steps": [
                {"id": "source-publication", "kind": "source-publication", "depends_on": [], "target_id": None, "render_leg_id": None},
                {"id": "image-build", "kind": "image-build", "depends_on": ["source-publication"], "target_id": None, "render_leg_id": None},
                {"id": "chart-bump", "kind": "chart-bump", "depends_on": ["image-build"], "target_id": None, "render_leg_id": None},
                {"id": "ci-render-commercial", "kind": "ci-render", "depends_on": ["chart-bump"], "target_id": None, "render_leg_id": "commercial"},
                {"id": "render-publication-commercial", "kind": "render-publication", "depends_on": ["ci-render-commercial"], "target_id": None, "render_leg_id": "commercial"},
                {"id": "ci-render-commercial-mono", "kind": "ci-render", "depends_on": ["chart-bump"], "target_id": None, "render_leg_id": "commercial-mono"},
                {"id": "render-publication-commercial-mono", "kind": "render-publication", "depends_on": ["ci-render-commercial-mono"], "target_id": None, "render_leg_id": "commercial-mono"},
                {"id": "argocd-auto-sync-dev-commercial", "kind": "argocd-auto-sync", "depends_on": ["render-publication-commercial"], "target_id": "dev-commercial", "render_leg_id": "commercial"},
                {"id": "argocd-auto-sync-dev-mono", "kind": "argocd-auto-sync", "depends_on": ["render-publication-commercial-mono"], "target_id": "dev-mono", "render_leg_id": "commercial-mono"},
            ],
        }
        fan_render_prefixes = [leg_a_remote, leg_b_remote, fan_chart_remote]
        fan_artifact_prefixes = [fan_registry]
        check("fanout multi-deploy-repo cascade validates", lambda: _validate_automatic_gitops_contract(
            fanout, fan_render_prefixes, "fixture.fanout", artifact_prefixes=fan_artifact_prefixes))
        fan_missing_leg = json.loads(json.dumps(fanout))
        fan_missing_leg["cascade_steps"] = [s for s in fan_missing_leg["cascade_steps"]
                                            if s["id"] != "render-publication-commercial-mono"]
        check("fanout omitted render-publication step is refused", lambda: _validate_automatic_gitops_contract(
            fan_missing_leg, fan_render_prefixes, "fixture.fanout", artifact_prefixes=fan_artifact_prefixes),
            "every material cascade step")
        fan_cross_leg = json.loads(json.dumps(fanout))
        fan_cross_leg["targets"][1]["render_leg_id"] = "commercial"
        check("fanout cross-leg target source is refused", lambda: _validate_automatic_gitops_contract(
            fan_cross_leg, fan_render_prefixes, "fixture.fanout", artifact_prefixes=fan_artifact_prefixes),
            "Argo source must equal the protected render target")
        fan_bad_leg_ref = json.loads(json.dumps(fanout))
        fan_bad_leg_ref["targets"][0]["render_leg_id"] = "ghost"
        check("fanout unresolved render_leg_id is refused", lambda: _validate_automatic_gitops_contract(
            fan_bad_leg_ref, fan_render_prefixes, "fixture.fanout", artifact_prefixes=fan_artifact_prefixes),
            "does not resolve to a declared render leg")
        check("fanout image registry outside allowlist is refused", lambda: _validate_automatic_gitops_contract(
            fanout, fan_render_prefixes, "fixture.fanout", artifact_prefixes=["registry.example/other"]),
            "outside reviewed artifact registry allowlist")
        fan_leg_off_allowlist = json.loads(json.dumps(fanout))
        check("fanout render leg outside allowlist is refused", lambda: _validate_automatic_gitops_contract(
            fan_leg_off_allowlist, [leg_a_remote, fan_chart_remote], "fixture.fanout",
            artifact_prefixes=fan_artifact_prefixes),
            "outside reviewed render allowlist")

        # A target may address its cluster by registered name (destination_name) instead of a
        # server URL -- the platform's commercial ApplicationSets use `destination.name`.
        fan_dest_name = json.loads(json.dumps(fanout))
        del fan_dest_name["targets"][0]["destination_server"]
        fan_dest_name["targets"][0]["destination_name"] = "platform-core-services"
        check("fanout target may address a cluster by destination_name", lambda: _validate_automatic_gitops_contract(
            fan_dest_name, fan_render_prefixes, "fixture.fanout", artifact_prefixes=fan_artifact_prefixes))
        fan_dest_both = json.loads(json.dumps(fanout))
        fan_dest_both["targets"][0]["destination_name"] = "platform-core-services"
        check("fanout target with both server and name is refused", lambda: _validate_automatic_gitops_contract(
            fan_dest_both, fan_render_prefixes, "fixture.fanout", artifact_prefixes=fan_artifact_prefixes),
            "exactly one of destination_server / destination_name")
        fan_dest_neither = json.loads(json.dumps(fanout))
        del fan_dest_neither["targets"][0]["destination_server"]
        check("fanout target with no destination is refused", lambda: _validate_automatic_gitops_contract(
            fan_dest_neither, fan_render_prefixes, "fixture.fanout", artifact_prefixes=fan_artifact_prefixes),
            "exactly one of destination_server / destination_name")

        # Fanout delivery effect: reified legs (with expected_remote_head) and targets (with uid).
        fan_effect = {
            "kind": "ci-render-argocd-auto-sync-fanout-v1",
            "declaration_sha256": _value_sha(fanout),
            "argocd_executable_path": "/usr/bin/argocd", "argocd_executable_sha256": "a" * 64,
            "image_build": fanout["image_build"], "chart": fanout["chart"],
            "render_legs": [{**leg, "expected_remote_head": None} for leg in fanout["render_legs"]],
            "cascade_steps": fanout["cascade_steps"],
            "targets": [{**t, "argocd_application_uid": f"uid-{t['id']}"} for t in fanout["targets"]],
        }
        check("fanout delivery effect reifies the reviewed declaration",
              lambda: _validate_delivery_effect(fan_effect, "fixture.fan_effect"))
        fan_effect_drift = json.loads(json.dumps(fan_effect))
        fan_effect_drift["targets"][0]["source_path"] = "deploy/tampered"
        check("fanout delivery effect declaration drift is refused",
              lambda: _validate_delivery_effect(fan_effect_drift, "fixture.fan_effect"),
              "differs from reviewed declaration")
        fan_effect_name = json.loads(json.dumps(fan_effect))
        del fan_effect_name["targets"][0]["destination_server"]
        fan_effect_name["targets"][0]["destination_name"] = "platform-core-services"
        fan_effect_name["declaration_sha256"] = _value_sha(fan_dest_name)
        check("fanout delivery effect reifies a destination_name target",
              lambda: _validate_delivery_effect(fan_effect_name, "fixture.fan_effect_name"))

        ALLOW_TEST_OPERATION_EXECUTABLES = False
        check("unreviewed temporary operation executable refused", lambda: (
            _validate_operational_executable_trust(
                [str(operation_collector)], operation_collector_sha,
                {**state, "_state_path": str(state_path)},
                "fixture.operation", "gitops-manual-sync",
            )
        ), "source-backed operational wrappers are deferred")
        check("trusted mutating verification argv is refused", lambda: (
            _validate_verification_command_semantics(
                ["/opt/homebrew/bin/argocd", "app", "delete", "prod-app", "--yes"],
                "fixture.verification",
            )
        ), "machine-enforced read-only policy")
        check("secret-bearing kubectl query is refused", lambda: (
            _validate_verification_command_semantics(
                ["/usr/bin/kubectl", "get", "secret/prod-creds", "-o", "yaml"],
                "fixture.verification",
            )
        ), "secret/config payload resources are forbidden")
        check("Pulumi secret reveal flag is refused", lambda: (
            _validate_verification_command_semantics(
                ["/opt/homebrew/bin/pulumi", "stack", "output", "--show-secrets"],
                "fixture.verification",
            )
        ), "secret-revealing output flags are forbidden")
        check("HTTP token userinfo is refused before persistence", lambda: (
            _validated_remote_url(
                "https://PATSECRET@git.example.com/team/repo.git", "fixture remote"
            )
        ), "userinfo credentials are forbidden")
        check("URL credential in operation argv is refused", lambda: (
            _reject_secret_argv(
                ["/usr/bin/argocd", "--server", "https://PATSECRET@argocd.example"],
                "fixture argv",
            )
        ), "userinfo credentials are forbidden")
        check("kubectl raw secret API query is refused", lambda: (
            _validate_verification_command_semantics(
                ["/usr/bin/kubectl", "get", "--raw=/api/v1/namespaces/prod/secrets/db"],
                "fixture.verification",
            )
        ), "raw API queries are forbidden")
        check("kubectl mutable file selector is refused", lambda: (
            _validate_verification_command_semantics(
                ["/usr/bin/kubectl", "get", "-f", "/tmp/secret.yaml"],
                "fixture.verification",
            )
        ), "mutable file/kustomize selectors are forbidden")
        pinned_sync = [
            "/usr/bin/argocd", "app", "sync", "app", "--revision", render_head,
            "--server", "argo.prod",
        ]
        check("unpinned local kubectl apply is outside v2 mutation contract", lambda: (
            _validate_apply_command_semantics(
                ["/usr/bin/kubectl", "apply", "-f", "deploy.yaml"], target,
                "fixture.apply",
            )
        ), "v2 apply must use argocd")
        check("Argo credential override flag is refused", lambda: (
            _validate_apply_command_semantics(
                [*pinned_sync, "--auth-token", "opaque-super-secret"], target,
                "fixture.apply",
            )
        ), "unsupported or unsafe")
        check("Argo core context override is refused", lambda: (
            _validate_command_context(
                [*pinned_sync, "--core", "--kube-context", "other"],
                {"argocd": {"server": "argo.prod"}}, "fixture.apply",
            )
        ), "credential/context override flags are forbidden")
        check("kubectl server override is refused", lambda: (
            _validate_command_context(
                [
                    "/usr/bin/kubectl", "get", "pods", "--kubeconfig", "/tmp/frozen",
                    "--context", "prod", "--server", "https://other",
                ],
                {"kubernetes": {
                    "kubeconfig_path": "/tmp/frozen", "context": "prod",
                    "kubeconfig_sha256": "a" * 64,
                }}, "fixture.verification",
            )
        ), "credential/target override flags are forbidden")
        production_contexts = {"argocd": {
            "server": "https://argo.prod", "config_path": "/tmp/frozen-argocd.json",
            "config_sha256": "a" * 64, "context": "prod",
            "certificate_authority_sha256": "b" * 64,
        }}
        profile = target["verification_profile"]
        check("probe labels cannot relabel unrelated commands", lambda: (
            _validate_probe_command_semantics(
                "argocd-synced", ["/usr/bin/argocd", "version"], 30,
                production_contexts, profile, "fixture.typed-probe",
            )
        ), "exact target-bound")
        check("literal identity JSON cannot replace Argo object collection", lambda: (
            _project_observed_identity(
                json.dumps({
                    "source_commit": head, "render_commit": render_head,
                    "image_digest": image_digest, "generation": 1,
                }).encode(), target, "fixture.literal-observation",
            )
        ), "incomplete Argo Application")
        check("typed 2xx smoke command is canonical", lambda: (
            _validate_probe_command_semantics(
                "behavioral-smoke", [
                    "/usr/bin/curl", "--disable", "--silent", "--show-error",
                    "--max-time", "30", "--output", "/dev/null", "--write-out",
                    "%{http_code}", "--request", "GET",
                    profile["behavioral_smoke_url"],
                ], 30, production_contexts, profile, "fixture.typed-smoke",
            )
        ))
        query_profile = dict(profile)
        query_profile["behavioral_smoke_url"] += "?token=persisted-secret"
        check("smoke URL query credentials are refused", lambda: (
            _validate_verification_profile(query_profile, target, "fixture.profile")
        ), "query/fragment")
        redirect_profile = dict(profile)
        redirect_profile["behavioral_smoke_status"] = 302
        check("redirect/error statuses cannot satisfy mandatory smoke", lambda: (
            _validate_verification_profile(redirect_profile, target, "fixture.profile")
        ), "requires HTTP 2xx")
        wrong_selector_deployment = json.dumps({
            "metadata": {
                "name": profile["deployment_name"],
                "namespace": profile["destination_namespace"],
                "uid": profile["deployment_uid"], "generation": 1,
            },
            "spec": {"selector": {"matchLabels": {"other": "workload"}}},
            "status": {"observedGeneration": 1, "availableReplicas": 1},
        }).encode()
        check("Deployment selector must equal reviewed pod scope", lambda: _expect(
            _project_probe_fact(
                wrong_selector_deployment, target,
                "deployment-observed-generation", True, "fixture.deployment",
            )[1] is False, "wrong selector was accepted"
        ))
        ca_bytes = b"fixture-ca"
        kubeconfig_path = Path(td) / "frozen-kubeconfig.json"
        kubeconfig = {
            "apiVersion": "v1", "kind": "Config", "current-context": "core",
            "contexts": [{"name": "core", "context": {"cluster": "core", "user": "sre"}}],
            "clusters": [{"name": "core", "cluster": {
                "server": "https://kubernetes.example.invalid",
                "certificate-authority-data": base64.b64encode(ca_bytes).decode(),
            }}],
            "users": [{"name": "sre", "user": {"token": "fixture-token"}}],
        }
        kubeconfig_path.write_text(json.dumps(kubeconfig), encoding="utf-8")
        ca_sha = hashlib.sha256(ca_bytes).hexdigest()
        check("static JSON kubeconfig identity validates", lambda: (
            _validate_json_kubeconfig(
                kubeconfig_path, "core", "https://kubernetes.example.invalid",
                ca_sha, "fixture.kubeconfig",
            )
        ))
        exec_kubeconfig = json.loads(json.dumps(kubeconfig))
        exec_kubeconfig["users"][0]["user"]["exec"] = {
            "command": "/tmp/unreviewed-credential-plugin"
        }
        kubeconfig_path.write_text(json.dumps(exec_kubeconfig), encoding="utf-8")
        check("kubeconfig executable auth plugins are refused", lambda: (
            _validate_json_kubeconfig(
                kubeconfig_path, "core", "https://kubernetes.example.invalid",
                ca_sha, "fixture.kubeconfig",
            )
        ), "credential plugins are forbidden")
        proxy_kubeconfig = json.loads(json.dumps(kubeconfig))
        proxy_kubeconfig["clusters"][0]["cluster"]["proxy-url"] = "https://proxy.invalid"
        kubeconfig_path.write_text(json.dumps(proxy_kubeconfig), encoding="utf-8")
        check("kubeconfig transport proxy overrides are refused", lambda: (
            _validate_json_kubeconfig(
                kubeconfig_path, "core", "https://kubernetes.example.invalid",
                ca_sha, "fixture.kubeconfig",
            )
        ), "proxy/TLS identity overrides are forbidden")
        argo_ca = b"fixture-argo-ca"
        argocd_config_path = Path(td) / "frozen-argocd.json"
        argocd_config = {
            "current-context": "prod",
            "contexts": [{"name": "prod", "server": "https://argo.prod", "user": "sre"}],
            "servers": [{
                "server": "https://argo.prod", "insecure": False,
                "certificate-authority-data": base64.b64encode(argo_ca).decode(),
            }],
            "users": [{"name": "sre", "auth-token": "fixture-secret-token"}],
        }
        argocd_config_path.write_text(json.dumps(argocd_config), encoding="utf-8")
        argo_ca_sha = hashlib.sha256(argo_ca).hexdigest()
        check("static JSON Argo auth/TLS context validates", lambda: (
            _validate_json_argocd_config(
                argocd_config_path, "prod", "https://argo.prod", argo_ca_sha,
                "fixture.argocd",
            )
        ))
        insecure_argocd = json.loads(json.dumps(argocd_config))
        insecure_argocd["servers"][0]["insecure"] = True
        argocd_config_path.write_text(json.dumps(insecure_argocd), encoding="utf-8")
        check("Argo insecure TLS context is refused", lambda: (
            _validate_json_argocd_config(
                argocd_config_path, "prod", "https://argo.prod", argo_ca_sha,
                "fixture.argocd",
            )
        ), "insecure TLS is forbidden")
        ALLOW_TEST_OPERATION_EXECUTABLES = True

        operations_role = "milestone-operations-adversary"
        operations_body = kit / f"data/agents/{operations_role}.md"
        operations_snapshot = review_dir / f"{operations_role}-task-operations-agent.md"
        operations_snapshot.write_bytes(operations_body.read_bytes())
        operations_rel = ".claude/notes/milestones/m1/artifacts/reviews/m1-operations-review.md"
        operations_path = root / operations_rel
        operations_path.write_text(
            f"**Reviewed range:** {base}..{head}\n**Operations verdict:** PASS\n",
            encoding="utf-8",
        )
        operations_prompt = review_dir / f"{operations_role}-task-operations-prompt.md"
        plan_sha = _file_sha(art / POINTERS["operations_plan"])
        release_sha = _file_sha(art / POINTERS["release_manifest"])
        operations_plan_snapshot = (
            review_dir / f"{operations_role}-task-operations-operations-plan.json"
        )
        release_manifest_snapshot = (
            review_dir / f"{operations_role}-task-operations-release-manifest.json"
        )
        operations_plan_snapshot.write_bytes((art / POINTERS["operations_plan"]).read_bytes())
        release_manifest_snapshot.write_bytes((art / POINTERS["release_manifest"]).read_bytes())
        operations_header = (
            "MILESTONE_REVIEW_DISPATCH_V2\n"
            f"ROLE: {operations_role}\nSTAGE: operations\nID: m1\n"
            f"REPO_ROOT: {root.resolve()}\nWORKSPACE_ROOT: {workspace.resolve()}\n"
            f"BASE_COMMIT: {base}\nFINAL_HEAD: {head}\n"
            f"RELEASE_MANIFEST: {release_manifest_snapshot.resolve()}\n"
            f"RELEASE_MANIFEST_SHA256: {release_sha}\n"
            f"OPERATIONS_PLAN: {operations_plan_snapshot.resolve()}\n"
            f"OPERATIONS_PLAN_SHA256: {plan_sha}\n"
            f"DELIVERY_REQUIREMENTS: {delivery_requirements_json}\n"
            f"DELIVERY_REQUIREMENTS_SHA256: {delivery_requirements_sha}\n"
            f"OPERATIONS_REVIEW_PATH: {operations_path.resolve()}\n"
            f"AGENT_KIT_COMMIT: {kit_commit}\nSOURCE_REMOTE_URL: {remote_url}"
        )
        operations_prompt.write_bytes(
            operations_header.encode("utf-8")
            + b"\n--- CANONICAL AGENT BODY ---\n"
            + operations_snapshot.read_bytes()
        )
        operations_receipt = {
            "role": operations_role, "stage": "operations", "provider": "codex", "model": None,
            "agent_task_id": "task-operations", "agent_body_path": f"data/agents/{operations_role}.md",
            "agent_body_snapshot_path": f"artifacts/reviews/{operations_role}-task-operations-agent.md",
            "agent_kit_commit": kit_commit, "workspace_root": str(workspace.resolve()),
            "reviewed_remote_url": remote_url,
            "agent_body_sha256": _file_sha(operations_snapshot),
            "prompt_path": f"artifacts/reviews/{operations_prompt.name}",
            "prompt_sha256": _file_sha(operations_prompt),
            "critique_path": operations_rel,
            "critique_sha256": _file_sha(operations_path),
            "reviewed_base": base, "reviewed_head": head,
            "started_at": review_completed_at,
            "completed_at": review_completed_at, "verdict": "PASS",
            "check_evidence_refs": [], "check_attempt_refs": [],
            "findings_register_sha256": None,
            "assessment_manifest_sha256": None,
            "operations_plan_sha256": plan_sha, "release_manifest_sha256": release_sha,
            "delivery_requirements_sha256": delivery_requirements_sha,
            "findings_snapshot_path": None,
            "operations_plan_snapshot_path": (
                f"artifacts/reviews/{operations_role}-task-operations-operations-plan.json"
            ),
            "release_manifest_snapshot_path": (
                f"artifacts/reviews/{operations_role}-task-operations-release-manifest.json"
            ),
        }
        operations_receipt_file = (
            review_dir / f"{operations_role}-task-operations-receipt.json"
        )
        operations_receipt_file.write_text(json.dumps(operations_receipt), encoding="utf-8")
        operations_append_time = max(
            now, _iso(review_completed_at, "fixture check completion")
        )
        operations_append_stamp = _utc_text(operations_append_time)
        state["phase"] = "plan-review-running"
        state["phase_history"].append({
            "phase": "plan-review-running", "at": operations_append_stamp
        })
        state["updated_at"] = operations_append_stamp
        state_path.write_text(json.dumps(state), encoding="utf-8")
        check("locked operations attempt append", lambda: review_append(
            state_path, "operations", operations_receipt_file, operations_append_time
        ))
        review = _load_json(art / POINTERS["review_manifest"], "review after operations append")
        state = _load_json(state_path, "state after operations append")
        plan_review_receipt = gate(state_path, "plan-reviewed", operations_append_time)
        check("post-publication operations review gate", lambda: _expect(
            plan_review_receipt["derived"].get("review_status") == "closed",
            "plan-reviewed gate did not close review",
        ))
        state["phase"] = "plan-reviewed"
        state["phase_history"].append({"phase": "plan-reviewed", "at": operations_append_stamp})
        state["updated_at"] = operations_append_stamp
        state["artifact_bindings"].update(plan_review_receipt["bindings"])
        state_path.write_text(json.dumps(state), encoding="utf-8")

        scope = target_scope_hash(plan, target)
        probes = [{
            "kind": spec["kind"], "exit_code": 0, "observed_at": ops_stamp,
            "evidence": command_ref(
                spec["kind"], spec["command"], target["execution_environment"]
            ),
        } for spec in target["verification_contract"]]
        observation_ref = command_ref(
            "observation", target["observation_command"],
            target["execution_environment"],
        )
        apply_observation_ref = command_ref(
            "apply-observation", target["observation_command"],
            target["execution_environment"],
        )
        attempt = {
            "attempt_id": "a1", "sequence": 1, "previous_attempt_sha256": None, "recorded_at": ops_stamp,
            "authorization": {"decision": "approved", "by": "Chris Dare", "method": "human-explicit", "at": ops_stamp, "scope_hash": scope},
            "apply": {
                "status": "applied", "at": ops_stamp, "actor": "argocd",
                "idempotency_key": "a" * 64,
                "intent_evidence": evref(
                    "apply-intent", shlex.join(target["apply_command"])
                ),
                "observed": {"source_commit": head, "render_commit": render_head, "image_digest": desired["image_digest"], "generation": 1},
                "evidence": command_ref(
                    "apply", target["apply_command"],
                    {
                        **target["execution_environment"],
                        "MILESTONE_IDEMPOTENCY_KEY": "a" * 64,
                    },
                ),
                "observation_evidence": apply_observation_ref,
                "failure_reason": None,
                "recovered_from_ambiguous": False,
            },
            "verification": {
                "status": "verified", "observed_at": ops_stamp,
                "observed": {"source_commit": head, "render_commit": render_head, "image_digest": desired["image_digest"], "generation": 1},
                "observation_evidence": observation_ref, "probes": probes,
            },
        }
        evidence = {
            "schema_version": 2, "milestone_id": "m1", "generation": 1, "created_at": stamp,
            "producer": _producer(), "plan_hash": plan["plan_hash"],
            "targets": [{
                "id": target["id"], "status": "verified", "attempts": [attempt],
                "verification_refresh_intents": [],
                "verification_refreshes": [],
            }],
        }
        fixture_operations_path = art / "fixture-operations-evidence.json"
        fixture_operations_path.write_text(json.dumps(evidence), encoding="utf-8")
        waiver = {
            "schema_version": 2, "milestone_id": "m1", "generation": 1, "created_at": stamp,
            "producer": _producer(), "plan_hash": plan["plan_hash"], "waivers": [],
        }
        fixture_waivers_path = art / "fixture-waivers.json"
        fixture_waivers_path.write_text(json.dumps(waiver), encoding="utf-8")

        check("valid operations evidence", lambda: validate_operations_evidence(evidence, state, plan, now))
        original_state_bytes = state_path.read_bytes()
        cas_state = _load_state(state_path)
        cas_state["_state_path"] = str(state_path.resolve())
        state_path.write_bytes(original_state_bytes + b"\n")
        check("mutable artifact commit rejects concurrent state edits", lambda: (
            _commit_mutable_artifact(
                "operations_evidence", state_path, cas_state,
                art / POINTERS["operations_evidence"], evidence,
                validate_operations_evidence(evidence, state, plan, now), now,
            )
        ), "state changed concurrently before append")
        state_path.write_bytes(original_state_bytes)
        wrong_digest = json.loads(json.dumps(evidence))
        wrong_digest["targets"][0]["attempts"][0]["verification"]["observed"]["image_digest"] = "sha256:" + "c" * 64
        check("synced but pod digest differs", lambda: validate_operations_evidence(wrong_digest, state, plan, now), "does not match desired")
        no_smoke = json.loads(json.dumps(evidence))
        no_smoke["targets"][0]["attempts"][0]["verification"]["probes"] = [p for p in probes if p["kind"] != "behavioral-smoke"]
        check("correct digest but smoke missing", lambda: validate_operations_evidence(no_smoke, state, plan, now), "missing/failed contract probes")
        stale = json.loads(json.dumps(evidence)); stale["plan_hash"] = "d" * 64
        check("prior plan hash refused", lambda: validate_operations_evidence(stale, state, plan, now), "stale plan")
        replayed_auth = json.loads(json.dumps(evidence))
        replayed_auth["targets"][0]["attempts"][0]["authorization"]["scope_hash"] = "d" * 64
        check("cross-target authorization replay refused", lambda: validate_operations_evidence(replayed_auth, state, plan, now), "authorization replay")
        future = json.loads(json.dumps(evidence))
        future["targets"][0]["attempts"][0]["verification"]["observed_at"] = _utc_text(
            now + timedelta(minutes=5)
        )
        check("future-dated verification refused", lambda: validate_operations_evidence(future, state, plan, now), "not be future")
        partial_plan = json.loads(json.dumps(plan))
        t2 = json.loads(json.dumps(target)); t2["id"] = "prod/core/app"
        t2["execution_environment"]["MILESTONE_TARGET_ID"] = t2["id"]
        partial_plan["targets"].append(t2); partial_plan["plan_hash"] = plan_hash(partial_plan)
        partial = json.loads(json.dumps(evidence)); partial["plan_hash"] = partial_plan["plan_hash"]
        check("multi-target partial rollout refused", lambda: validate_operations_evidence(partial, state, partial_plan, now), "exactly match")
        duplicate_target = json.loads(json.dumps(evidence))
        duplicate_target["targets"].append(json.loads(json.dumps(duplicate_target["targets"][0])))
        duplicate_target["targets"][1]["attempts"][0]["attempt_id"] = "duplicate-target-attempt"
        check("duplicate operations target id refused", lambda: validate_operations_evidence(
            duplicate_target, state, plan, now
        ), "duplicate target id")
        expired = json.loads(json.dumps(waiver)); expired["waivers"] = [{
            "waiver_id": "w1", "target_id": target["id"], "scope_hash": scope,
            "missing_contract": ["behavioral-smoke"], "decision": "approved", "approved_by": "Chris Dare",
            "approval_method": "human-explicit", "approved_at": "2026-07-10T00:00:00Z",
            "reason": "temporary", "created_at": "2026-07-10T00:00:00Z", "expires_at": "2026-07-11T00:00:00Z",
            "compensating_control": "manual check", "follow_up_milestone": "m2",
        }]
        expired_meta = validate_waivers(expired, state, plan, now)
        check("expired waiver retained but inactive", lambda: _expect(
            expired_meta["active_waivers"] == {}, "expired waiver became active"
        ))
        nonhuman = json.loads(json.dumps(expired))
        nonhuman["waivers"][0]["expires_at"] = "2026-07-13T00:00:00Z"
        nonhuman["waivers"][0]["approval_method"] = "agent"
        check("nonhuman waiver refused", lambda: validate_waivers(nonhuman, state, plan, now), "human-explicit")
        forged_human = json.loads(json.dumps(nonhuman))
        forged_human["waivers"][0]["approval_method"] = "human-explicit"
        forged_human["waivers"][0]["approved_by"] = "codex"
        check("automation identity cannot self-approve waiver", lambda: validate_waivers(
            forged_human, state, plan, now
        ), "accountable human")
        long_waiver = json.loads(json.dumps(nonhuman))
        long_waiver["waivers"][0]["approval_method"] = "human-explicit"
        long_waiver["waivers"][0]["expires_at"] = "2026-08-20T00:00:00Z"
        check("overlong waiver refused at gate", lambda: validate_waivers(
            long_waiver, state, plan, now
        ), "cannot exceed 30 days")
        bad_plan = json.loads(json.dumps(plan)); bad_plan["plan_hash"] = "e" * 64
        check("stale plan self-hash refused", lambda: validate_operations_plan(bad_plan, state), "stale")
        escape_state = dict(state); escape_state["operations_plan"] = "artifacts/../../outside.json"
        outside = state_dir / "outside.json"; outside.write_text("{}")
        check("artifact traversal refused", lambda: _safe_artifact_path(state_path, escape_state, "operations_plan"), "escapes")
        if platform_compat.supports_symlinks():
            external = Path(td) / "external.json"; external.write_text("{}")
            link = art / "escape-link.json"; link.symlink_to(external)
            symlink_state = dict(state); symlink_state["operations_plan"] = "artifacts/escape-link.json"
            check("artifact symlink escape refused", lambda: _safe_artifact_path(state_path, symlink_state, "operations_plan"), "escapes")
            link.unlink()
        else:
            skip("artifact symlink escape refused", "REQUIRES:symlink-privilege")
        prior_meta = validate_operations_evidence(evidence, state, plan, now)
        prior_binding = _binding("operations_evidence", fixture_operations_path, evidence, "applied", prior_meta)
        pending = json.loads(json.dumps(evidence))
        pending_attempt = pending["targets"][0]["attempts"][0]
        pending_attempt["verification"] = {
            "status": "pending", "observed_at": None, "observed": None,
            "observation_evidence": None, "probes": [],
        }
        pending["targets"][0]["status"] = "applied"
        pending_meta = validate_operations_evidence(pending, state, plan, now)
        pending_path = art / "pending-evidence.json"; pending_path.write_text(json.dumps(pending))
        applied_binding = _binding("operations_evidence", pending_path, pending, "applied", pending_meta)
        verified_binding = _binding("operations_evidence", fixture_operations_path, evidence, "operationally-verified", prior_meta)
        check("verification may append only in deterministic writer", lambda: _check_prior_binding(
            "operations_evidence", verified_binding, applied_binding, allow_append=True
        ))
        first_failed = json.loads(json.dumps(attempt))
        first_failed["attempt_id"] = "failed-1"
        first_failed["apply"]["status"] = "failed"
        first_failed["apply"]["failure_reason"] = "fixture apply failure"
        first_failed["verification"] = {
            "status": "pending", "observed_at": None, "observed": None,
            "observation_evidence": None, "probes": [],
        }
        second_success = json.loads(json.dumps(attempt))
        second_success["attempt_id"] = "success-2"
        second_success["sequence"] = 2
        second_success["previous_attempt_sha256"] = _value_sha(first_failed)
        retry_evidence = json.loads(json.dumps(evidence))
        retry_evidence["targets"][0]["attempts"] = [first_failed, second_success]
        check("failed then successful attempts are preserved", lambda: validate_operations_evidence(retry_evidence, state, plan, now))
        changed = json.loads(json.dumps(evidence)); changed["targets"][0]["attempts"][0]["authorization"]["by"] = "other-human"
        changed_meta = validate_operations_evidence(changed, state, plan, now)
        changed_path = art / "changed-evidence.json"; changed_path.write_text(json.dumps(changed))
        current_binding = _binding("operations_evidence", changed_path, changed, "operationally-verified", changed_meta)
        check("append-only attempt mutation refused", lambda: _check_prior_binding(
            "operations_evidence", current_binding, prior_binding, allow_append=True
        ), "changed/vanished")

        # Exercise the only supported operation-attempt writers against real
        # evidence files, including a stale historical attempt, append-only
        # retry, failed verification, and a target-scoped waiver.
        operations_path = art / POINTERS["operations_evidence"]
        operations_path.unlink(missing_ok=True)
        waivers_path = art / POINTERS["waivers"]
        waivers_path.unlink(missing_ok=True)
        apply_gate = gate(state_path, "apply-running", now)
        state["phase"] = "apply-running"
        state["phase_history"].append({"phase": "apply-running", "at": phase_stamp})
        state["artifact_bindings"].update(apply_gate["bindings"])
        state["operational_status"] = "applying"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        t1 = now + timedelta(minutes=1)
        t2 = now + timedelta(minutes=2)
        t3 = now + timedelta(minutes=3)
        stale_time = now + timedelta(hours=2)
        stale_1 = stale_time + timedelta(minutes=1)
        stale_2 = stale_time + timedelta(minutes=2)
        stale_3 = stale_time + timedelta(minutes=3)
        stale_4 = stale_time + timedelta(minutes=4)
        stale_5 = stale_time + timedelta(minutes=5)
        TEST_FAIL_AFTER_ARTIFACT_WRITE = True
        check(
            "mutable writer crash is journaled",
            lambda: attempt_start(
                state_path, target["id"], "Chris Dare", scope,
                t1,
            ),
            "self-test crash after mutable artifact write",
        )
        TEST_FAIL_AFTER_ARTIFACT_WRITE = False
        first_start = attempt_start(
            state_path, target["id"], "Chris Dare", scope,
            t1,
        )
        check("mutable writer journal recovers on retry", lambda: _expect(
            not _transaction_path(state_path).exists(), "transaction journal was not cleared"
        ))
        check("deterministic attempt-start writer", lambda: _expect(
            bool(first_start["attempt_id"]), "missing generated attempt id"
        ))
        check("deterministic attempt-apply writer", lambda: attempt_apply(
            state_path, target["id"], first_start["attempt_id"], "argocd", "fixture",
            t2,
        ))
        state = _load_json(state_path, "state after deterministic apply")
        applied_gate = gate(
            state_path, "applied", t2
        )
        state["phase"] = "applied"
        state["phase_history"].append({"phase": "applied", "at": _utc_text(t2)})
        state["artifact_bindings"].update(applied_gate["bindings"])
        state["operational_status"] = "applied"
        state["updated_at"] = _utc_text(t2)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        verify_gate = gate(
            state_path, "verify-running", t2
        )
        state["phase"] = "verify-running"
        state["phase_history"].append({"phase": "verify-running", "at": _utc_text(t2)})
        state["updated_at"] = _utc_text(t2)
        state["artifact_bindings"].update(verify_gate["bindings"])
        state_path.write_text(json.dumps(state), encoding="utf-8")
        TEST_FAIL_AFTER_ARTIFACT_WRITE = True
        check("successful verification crash is journaled", lambda: attempt_verify(
            state_path, target["id"], first_start["attempt_id"], "fixture",
            t3,
        ), "self-test crash after mutable artifact write")
        TEST_FAIL_AFTER_ARTIFACT_WRITE = False
        with _state_lock(state_path):
            _recover_pending_transactions(state_path)
        check("successful verification journal recovers derived status", lambda: _expect(
            not _transaction_path(state_path).exists()
            and _load_json(state_path, "recovered verification state")["operational_status"]
            == "applied",
            "verification recovery did not reconstruct deterministic state",
        ))

        # At 14:00 the prior verification is stale. A new pending attempt must
        # still be appendable; stale history is retained but is no longer the
        # current delivery claim.
        first_written = _load_json(operations_path, "writer operations evidence")
        state = _load_json(state_path, "state after deterministic verification")
        first_meta = validate_operations_evidence(
            first_written, state, plan,
            t3, state_dir,
        )
        operational_gate = gate(
            state_path, "operationally-verified",
            t3,
        )
        state["phase"] = "operationally-verified"
        state["phase_history"].append({"phase": "operationally-verified", "at": _utc_text(t3)})
        state["updated_at"] = _utc_text(t3)
        state["artifact_bindings"].update(operational_gate["bindings"])
        state["operational_status"] = "verified"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        complete_gate = gate(
            state_path, "complete", t3
        )
        state["phase"] = "complete"
        state["phase_history"].append({"phase": "complete", "at": _utc_text(t3)})
        state["artifact_bindings"].update(complete_gate["bindings"])
        state["updated_at"] = _utc_text(t3)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        (root / "later-milestone.txt").write_text("later\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "later-milestone.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "later milestone"], check=True)
        subprocess.run(["git", "-C", str(root), "push", "-q", "origin", "dev"], check=True)
        check("completed snapshot survives later source branch advancement", lambda: gate(
            state_path, "complete", t3
        ))
        check(
            "fresh complete claim cannot enter verification refresh",
            lambda: gate(
                state_path, "verify-running",
                t3,
            ),
            "reserved for stale verification evidence",
        )
        retry_gate = gate(
            state_path, "verify-running",
            stale_time,
        )
        check("stale operational claim enters authorized verification refresh", lambda: _expect(
            "operations_evidence" in retry_gate["bindings"],
            "refresh gate did not preserve operations evidence",
        ))
        state["phase"] = "verify-running"
        state["phase_history"].append({"phase": "verify-running", "at": _utc_text(stale_time)})
        state["updated_at"] = _utc_text(stale_time)
        state["artifact_bindings"].update(retry_gate["bindings"])
        state_path.write_text(json.dumps(state), encoding="utf-8")
        original_verification_refs = [
            first_written["targets"][0]["attempts"][0]["verification"]["observation_evidence"],
            *[
                probe["evidence"]
                for probe in first_written["targets"][0]["attempts"][0]["verification"]["probes"]
            ],
        ]
        fail_smoke_flag.write_text("fail\n", encoding="utf-8")
        refresh_preview = attempt_preview(
            state_path, target["id"],
            stale_time,
            first_start["attempt_id"],
        )
        check("refresh preview binds exact verification-only action", lambda: _expect(
            refresh_preview["mode"] == "verification-refresh"
            and refresh_preview["approval_required"] is True
            and refresh_preview["scope"]["source_attempt_sha256"]
            == _value_sha(first_written["targets"][0]["attempts"][0]),
            "refresh preview omitted exact source/action scope",
        ))
        TEST_FAIL_AFTER_REFRESH_INTENT = True
        check("refresh authorization is durable before generic commands", lambda: attempt_verify(
            state_path, target["id"], first_start["attempt_id"], "fixture",
            stale_time,
            approved_by="Chris Dare", expected_scope_hash=refresh_preview["scope_hash"],
        ), "crash after durable verification refresh intent")
        TEST_FAIL_AFTER_REFRESH_INTENT = False
        unresolved = _load_json(operations_path, "unresolved refresh intent")
        unresolved_id = unresolved["targets"][0]["verification_refresh_intents"][-1][
            "refresh_id"
        ]
        check("unresolved refresh cannot replay commands", lambda: attempt_verify(
            state_path, target["id"], first_start["attempt_id"], "fixture",
            stale_time,
            approved_by="Chris Dare", expected_scope_hash=refresh_preview["scope_hash"],
        ), "unresolved")
        recovery = attempt_verify_recover(
            state_path, target["id"], unresolved_id,
            stale_time,
        )
        check("unresolved refresh recovers as ambiguous without replay", lambda: _expect(
            recovery["status"] == "ambiguous" and recovery["commands_replayed"] is False,
            "refresh recovery fabricated or replayed an observation",
        ))
        retry_refresh_preview = attempt_preview(
            state_path, target["id"],
            stale_time,
            first_start["attempt_id"],
        )
        refresh_result = attempt_verify(
            state_path, target["id"], first_start["attempt_id"], "fixture",
            stale_time,
            approved_by="Chris Dare",
            expected_scope_hash=retry_refresh_preview["scope_hash"],
        )
        check("stale verification appends an authorized refresh", lambda: _expect(
            bool(refresh_result["refresh_id"]) and refresh_result["status"] == "failed",
            "stale refresh did not append a failed observation result",
        ))
        schema_round_trip = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("milestone-pipeline-schema-check.py")),
                "--instance", "milestone-operations-evidence-v2.schema.json",
                str(operations_path),
            ],
            capture_output=True, text=True,
        )
        check("runtime refresh output round-trips through Draft 2020-12 schema", lambda: _expect(
            schema_round_trip.returncode == 0,
            schema_round_trip.stderr or schema_round_trip.stdout,
        ))
        check("refresh preserves original verification evidence", lambda: _expect(
            all(_file_sha(state_dir / ref["path"]) == ref["sha256"]
                for ref in original_verification_refs),
            "verification refresh overwrote append-only evidence",
        ))
        state = _load_json(state_path, "state after failed verification refresh")
        reapply_gate = gate(
            state_path, "apply-running",
            stale_time,
        )
        state["phase"] = "apply-running"
        state["phase_history"].append({"phase": "apply-running", "at": _utc_text(stale_time)})
        state["updated_at"] = _utc_text(stale_time)
        state["artifact_bindings"].update(reapply_gate["bindings"])
        state["operational_status"] = "applying"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        second_start = attempt_start(
            state_path, target["id"], "Chris Dare", scope,
            stale_time,
        )
        written = _load_json(operations_path, "writer operations evidence")
        check("stale historical evidence permits append-only retry", lambda: _expect(
            written["targets"][0]["attempts"][1]["previous_attempt_sha256"]
            == _value_sha(written["targets"][0]["attempts"][0]),
            "retry did not hash-chain to prior attempt",
        ))
        TEST_FAIL_AFTER_APPLY_INTENT = True
        check("durable apply intent precedes live mutation", lambda: attempt_apply(
            state_path, target["id"], second_start["attempt_id"], "argocd", "fixture",
            stale_1,
        ), "simulated failure after durable apply intent")
        TEST_FAIL_AFTER_APPLY_INTENT = False
        check("unresolved apply intent cannot be superseded", lambda: attempt_start(
            state_path, target["id"], "Chris Dare", scope,
            stale_1,
        ), "latest attempt is unresolved")
        attempt_apply(
            state_path, target["id"], second_start["attempt_id"], "argocd", "fixture",
            stale_1,
        )
        check("ambiguous recovery observes without replaying apply", lambda: _expect(
            apply_count_file.read_text(encoding="utf-8") == "1",
            "durable executing intent replayed the live apply command",
        ))
        state = _load_json(state_path, "state after retry apply")
        retry_applied_gate = gate(
            state_path, "applied", stale_1
        )
        state["phase"] = "applied"
        state["phase_history"].append({"phase": "applied", "at": _utc_text(stale_1)})
        state["updated_at"] = _utc_text(stale_1)
        state["artifact_bindings"].update(retry_applied_gate["bindings"])
        state["operational_status"] = "applied"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        retry_verify_gate = gate(
            state_path, "verify-running", stale_1
        )
        state["phase"] = "verify-running"
        state["phase_history"].append({"phase": "verify-running", "at": _utc_text(stale_1)})
        state["updated_at"] = _utc_text(stale_1)
        state["artifact_bindings"].update(retry_verify_gate["bindings"])
        state_path.write_text(json.dumps(state), encoding="utf-8")
        fail_smoke_flag.write_text("fail\n", encoding="utf-8")
        TEST_FAIL_AFTER_ARTIFACT_WRITE = True
        check("failed verification crash is journaled", lambda: attempt_verify(
            state_path, target["id"], second_start["attempt_id"], "fixture",
            stale_2,
        ), "self-test crash after mutable artifact write")
        TEST_FAIL_AFTER_ARTIFACT_WRITE = False
        with _state_lock(state_path):
            _recover_pending_transactions(state_path)
        check("failed verification journal recovers derived status", lambda: _expect(
            not _transaction_path(state_path).exists()
            and _load_json(state_path, "recovered failed verification state")[
                "operational_status"
            ] == "failed",
            "failed verification recovery did not reconstruct deterministic state",
        ))
        waiver_result = waiver_append(
            state_path, target["id"], "Chris Dare", ["behavioral-smoke"],
            "temporary smoke endpoint outage", _utc_text(stale_3 + timedelta(days=1)),
            "manual transaction trace reviewed", "m2", None,
            stale_3,
        )
        check("deterministic target-scoped waiver writer", lambda: _expect(
            bool(waiver_result["waiver_id"]), "missing generated waiver id"
        ))
        state = _load_json(state_path, "state before failed-apply fixture")
        failed_apply_gate = gate(
            state_path, "apply-running",
            stale_4,
        )
        state["phase"] = "apply-running"
        state["phase_history"].append({
            "phase": "apply-running", "at": _utc_text(stale_4),
        })
        state["updated_at"] = _utc_text(stale_4)
        state["artifact_bindings"].update(failed_apply_gate["bindings"])
        state["operational_status"] = "applying"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        third_start = attempt_start(
            state_path, target["id"], "Chris Dare", scope,
            stale_4,
        )
        fail_apply_flag.write_text("fail\n", encoding="utf-8")
        failed_apply = attempt_apply(
            state_path, target["id"], third_start["attempt_id"], "argocd", "fixture",
            stale_5,
        )
        check("nonzero apply cannot be laundered by matching observation", lambda: _expect(
            failed_apply["ok"] is False
            and failed_apply["status"] == "failed"
            and "exited 1" in (failed_apply["failure_reason"] or ""),
            "fresh nonzero apply was recorded as applied",
        ))
        check("failed apply updates top-level operational status", lambda: _expect(
            _load_json(state_path, "state after failed apply")["operational_status"]
            == "failed",
            "failed target remained hidden behind top-level applying status",
        ))
        check("multi-target failure dominates status projection", lambda: _expect(
            _operational_status_projection(
                {"statuses": {"a": "applied", "b": "failed"}}, "apply-running"
            ) == "failed",
            "partial rollout failure was not projected to the milestone state",
        ))

    with tempfile.TemporaryDirectory() as td:
        def reviewer_repo(
            name: str, changed_path: str, remote: str | None = None
        ) -> tuple[Path, str, str]:
            repo = Path(td) / name
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            if remote is not None:
                subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], check=True)
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
            base_commit = _git_output(repo, "rev-parse", "HEAD").decode().strip()
            changed = repo / changed_path
            changed.parent.mkdir(parents=True, exist_ok=True)
            changed.write_text("changed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
            head_commit = _git_output(repo, "rev-parse", "HEAD").decode().strip()
            return repo, base_commit, head_commit

        css_repo, css_base, css_head = reviewer_repo("ui-only", "styles/theme.scss")
        check("style-only diff selects frontend adversary", lambda: _expect(
            "milestone-frontend-ux" in _required_reviewers(css_repo, css_base, css_head),
            "frontend reviewer was omitted for SCSS-only change",
        ))
        infra_repo, infra_base, infra_head = reviewer_repo("crossplane", "values.yaml")
        check("Crossplane identity selects infra adversary", lambda: _expect(
            "milestone-infra-safety" in _required_reviewers(infra_repo, infra_base, infra_head),
            "infra reviewer was omitted for Crossplane repository",
        ))
        keycloak_repo, keycloak_base, keycloak_head = reviewer_repo(
            "keycloak", "dev/commercial/base-values.yaml"
        )
        check("Keycloak identity selects infra adversary for non-taxonomy path", lambda: _expect(
            "milestone-infra-safety" in _required_reviewers(
                keycloak_repo, keycloak_base, keycloak_head
            ), "infra reviewer was omitted for Keycloak repository identity",
        ))
        istio_repo, istio_base, istio_head = reviewer_repo(
            "istio-gateway", "README.md"
        )
        check("Istio identity selects infra adversary for documentation diff", lambda: _expect(
            "milestone-infra-safety" in _required_reviewers(
                istio_repo, istio_base, istio_head
            ), "infra reviewer was omitted for Istio repository identity",
        ))
        # V-L4: infra/ops-infra authors live Kargo/Pulumi control-plane CRs but is not
        # in infra_names, its remote does not tail-match a listed name, and its tracked
        # paths are repo-relative (stacks/…, docs/…) so none match ^infra/ — structurally
        # exempting every ops-infra milestone from the infra-safety lane. The two topology
        # signals (GitLab group path + local clone parent dir) must close that gap.
        opsinfra_remote_repo, opsinfra_remote_base, opsinfra_remote_head = reviewer_repo(
            "ops-infra", "stacks/kargo/main.go",
            remote="git@git.example.com:example-org/platform/infra/ops-infra.git",
        )
        check("ops-infra group-path topology selects infra adversary despite repo-relative diff", lambda: _expect(
            "milestone-infra-safety" in _required_reviewers(
                opsinfra_remote_repo, opsinfra_remote_base, opsinfra_remote_head
            ), "infra reviewer was omitted for ops-infra GitLab group-path topology",
        ))
        opsinfra_local_repo, opsinfra_local_base, opsinfra_local_head = reviewer_repo(
            "infra/ops-infra", "docs/runbook.md"
        )
        check("ops-infra clone-parent topology selects infra adversary without a remote", lambda: _expect(
            "milestone-infra-safety" in _required_reviewers(
                opsinfra_local_repo, opsinfra_local_base, opsinfra_local_head
            ), "infra reviewer was omitted for platform/infra/<name> clone-parent topology",
        ))
        source_repo, source_base, source_head = reviewer_repo(
            "some-app", "cmd/server/main.go",
            remote="git@git.example.com:example-org/platform/source/some-app.git",
        )
        check("non-infra source repo does not over-select infra adversary", lambda: _expect(
            "milestone-infra-safety" not in _required_reviewers(
                source_repo, source_base, source_head
            ), "infra reviewer fired for a non-infra source-group repository",
        ))
        check("delivery allowlist rejects prefix collision", lambda: _expect(
            _endpoint_prefix_match("registry.example/team/app", "registry.example/team")
            and not _endpoint_prefix_match(
                "registry.example/team-evil/app", "registry.example/team"
            ),
            "endpoint allowlist used a raw string prefix",
        ))

    verdict = "OK" if failures == 0 else f"{failures} failure(s)"
    # Skips are surfaced in the verdict, not swallowed: a green line that hid an
    # unrun security assertion is the exact defect M2 is closing.
    if skipped:
        verdict += f" ({skipped} skipped on {sys.platform})"
    print(f"milestone-pipeline-artifacts self-test: {verdict}")
    return 0 if failures == 0 else 1


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_validate = sub.add_parser("validate")
    p_validate.add_argument("kind", choices=sorted(POINTERS))
    p_validate.add_argument("path", type=Path)
    p_validate.add_argument("--state", type=Path, required=True)
    p_validate.add_argument("--at")
    p_gate = sub.add_parser("gate")
    p_gate.add_argument("--state", type=Path, required=True)
    p_gate.add_argument("--phase", required=True)
    p_gate.add_argument("--at")
    p_rec = sub.add_parser("reconcile")
    p_rec.add_argument("--state", type=Path, required=True)
    p_rec.add_argument("--at")
    p_recover = sub.add_parser("recover")
    p_recover.add_argument("--state", type=Path, required=True)
    p_kit_upgrade = sub.add_parser("kit-upgrade")
    p_kit_upgrade.add_argument("--state", type=Path, required=True)
    p_kit_upgrade.add_argument("--approved-by", required=True)
    p_kit_upgrade.add_argument("--scope-hash", required=True)
    p_kit_upgrade_preview = sub.add_parser("kit-upgrade-preview")
    p_kit_upgrade_preview.add_argument("--state", type=Path, required=True)
    p_plan = sub.add_parser("plan-hash")
    p_plan.add_argument("path", type=Path)
    p_scope = sub.add_parser("scope-hash")
    p_scope.add_argument("path", type=Path)
    p_scope.add_argument("target_id")
    p_check = sub.add_parser("check-run")
    p_check.add_argument("--state", type=Path, required=True)
    p_check.add_argument("--name", required=True)
    p_check.add_argument("--timeout", type=int, default=1800)
    p_check.add_argument("argv", nargs=argparse.REMAINDER)
    p_review = sub.add_parser("review-append")
    p_review.add_argument("--state", type=Path, required=True)
    p_review.add_argument("--stage", choices=("closure", "operations"), required=True)
    p_review.add_argument("--receipt", type=Path, required=True)
    p_publish_preview = sub.add_parser("publication-preview")
    p_publish_preview.add_argument("--state", type=Path, required=True)
    p_publish_preview.add_argument(
        "--mode", choices=("publish", "adopt-preexisting"), default="publish"
    )
    p_publish_authorize = sub.add_parser("publication-authorize")
    p_publish_authorize.add_argument("--state", type=Path, required=True)
    p_publish_authorize.add_argument("--approved-by", required=True)
    p_publish_authorize.add_argument("--scope-hash", required=True)
    p_publish_authorize.add_argument(
        "--mode", choices=("publish", "adopt-preexisting"), default="publish"
    )
    p_publish_apply = sub.add_parser("publication-apply")
    p_publish_apply.add_argument("--state", type=Path, required=True)
    p_attempt_preview = sub.add_parser("attempt-preview")
    p_attempt_preview.add_argument("--state", type=Path, required=True)
    p_attempt_preview.add_argument("--target", required=True)
    p_attempt_preview.add_argument("--attempt-id")
    p_start = sub.add_parser("attempt-start")
    p_start.add_argument("--state", type=Path, required=True)
    p_start.add_argument("--target", required=True)
    p_start.add_argument("--approved-by", required=True)
    p_start.add_argument("--scope-hash", required=True)
    p_apply = sub.add_parser("attempt-apply")
    p_apply.add_argument("--state", type=Path, required=True)
    p_apply.add_argument("--target", required=True)
    p_apply.add_argument("--attempt-id", required=True)
    p_apply.add_argument("--actor", required=True)
    p_apply.add_argument("--collector", default="milestone-operator")
    p_adopt_auto = sub.add_parser("attempt-adopt-auto-sync")
    p_adopt_auto.add_argument("--state", type=Path, required=True)
    p_adopt_auto.add_argument("--target", required=True)
    p_adopt_auto.add_argument("--collector", default="milestone-auto-sync-observer")
    p_verify = sub.add_parser("attempt-verify")
    p_verify.add_argument("--state", type=Path, required=True)
    p_verify.add_argument("--target", required=True)
    p_verify.add_argument("--attempt-id", required=True)
    p_verify.add_argument("--collector", default="milestone-verifier")
    p_verify.add_argument("--approved-by")
    p_verify.add_argument("--scope-hash")
    p_verify_recover = sub.add_parser("attempt-verify-recover")
    p_verify_recover.add_argument("--state", type=Path, required=True)
    p_verify_recover.add_argument("--target", required=True)
    p_verify_recover.add_argument("--refresh-id", required=True)
    p_waiver = sub.add_parser("waiver-append")
    p_waiver.add_argument("--state", type=Path, required=True)
    p_waiver.add_argument("--target", required=True)
    p_waiver.add_argument("--approved-by", required=True)
    p_waiver.add_argument("--missing-contract", nargs="+", required=True)
    p_waiver.add_argument("--reason", required=True)
    p_waiver.add_argument("--expires-at", required=True)
    p_waiver.add_argument("--compensating-control", required=True)
    p_waiver.add_argument("--follow-up-milestone", required=True)
    p_waiver.add_argument("--waiver-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            print(json.dumps(_validate_one(args.kind, args.path, args.state, _now(args.at)), indent=2))
        elif args.command in {"gate", "reconcile"}:
            if args.command == "reconcile":
                recover_state(args.state)
            phase = args.phase if args.command == "gate" else "complete"
            print(json.dumps(gate(args.state, phase, _now(args.at)), indent=2))
        elif args.command == "recover":
            print(json.dumps(recover_state(args.state), indent=2))
        elif args.command == "kit-upgrade":
            print(json.dumps(kit_upgrade_state(
                args.state, args.approved_by, args.scope_hash, _now(None)
            ), indent=2))
        elif args.command == "kit-upgrade-preview":
            print(json.dumps(kit_upgrade_preview(
                args.state, _now(None)
            ), indent=2))
        elif args.command == "plan-hash":
            print(plan_hash(_load_json(args.path, "operations_plan")))
        elif args.command == "scope-hash":
            data = _load_json(args.path, "operations_plan")
            matches = [t for t in data.get("targets", []) if isinstance(t, dict) and t.get("id") == args.target_id]
            _expect(len(matches) == 1, f"target id {args.target_id!r} not found exactly once")
            print(target_scope_hash(data, matches[0]))
        elif args.command == "check-run":
            argv_value = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
            result = run_check(args.state, args.name, argv_value, args.timeout)
            print(json.dumps(result, indent=2))
            return 0 if result["exit_code"] == 0 else 4
        elif args.command == "review-append":
            print(json.dumps(review_append(
                args.state, args.stage, args.receipt, _now(None)
            ), indent=2))
        elif args.command == "publication-preview":
            print(json.dumps(publication_preview(
                args.state, args.mode, _now(None)
            ), indent=2))
        elif args.command == "publication-authorize":
            print(json.dumps(publication_authorize(
                args.state, args.approved_by, args.scope_hash, args.mode, _now(None)
            ), indent=2))
        elif args.command == "publication-apply":
            result = publication_apply(args.state, _now(None))
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 5
        elif args.command == "attempt-preview":
            print(json.dumps(attempt_preview(
                args.state, args.target, _now(None), args.attempt_id
            ), indent=2))
        elif args.command == "attempt-start":
            print(json.dumps(attempt_start(
                args.state, args.target, args.approved_by, args.scope_hash, _now(None)
            ), indent=2))
        elif args.command == "attempt-apply":
            result = attempt_apply(
                args.state, args.target, args.attempt_id, args.actor,
                args.collector, _now(None),
            )
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 5
        elif args.command == "attempt-adopt-auto-sync":
            result = attempt_adopt_auto_sync(
                args.state, args.target, args.collector, _now(None)
            )
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 5
        elif args.command == "attempt-verify":
            result = attempt_verify(
                args.state, args.target, args.attempt_id, args.collector, _now(None),
                args.approved_by, args.scope_hash,
            )
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "verified" else 6
        elif args.command == "attempt-verify-recover":
            print(json.dumps(attempt_verify_recover(
                args.state, args.target, args.refresh_id, _now(None)
            ), indent=2))
        elif args.command == "waiver-append":
            print(json.dumps(waiver_append(
                args.state, args.target, args.approved_by, args.missing_contract,
                args.reason, args.expires_at, args.compensating_control,
                args.follow_up_milestone, args.waiver_id, _now(None),
            ), indent=2))
    except ValidationError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

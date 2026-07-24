#!/usr/bin/env python3
"""Locked state writer for milestone-pipeline delivery-state v2.

Usage:
  milestone-pipeline-checkpoint.py <ID> <new-phase>
  milestone-pipeline-checkpoint.py <ID> --get <field>
  milestone-pipeline-checkpoint.py <ID> --set <field>=<json>
  milestone-pipeline-checkpoint.py --self-test

This writer deliberately refuses v1 and mixed-shape state.  Run
milestone-pipeline-migrate.py explicitly; a legacy `complete` claim is never
silently treated as operational proof.

Agents may author artifact contents, but only milestone-pipeline-artifacts.py
can validate a delivery claim.  This writer calls that gate while holding the
state lock, re-hashes every returned receipt, persists the receipts, then
advances the phase atomically.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MILESTONE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STATE_SCHEMA_VERSION = 2

# Branching/short paths and operational retries make a numeric PHASE_ORDER
# incorrect.  Every legal edge is explicit and is tested below.
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
PHASES = set(PHASE_EDGES)

FIELD_TYPES: dict[str, type] = {
    "milestone_brief": str,
    "research_briefs": list,
    "research_mode": str,
    "research_synthesis": str,
    "implementation_path": str,
    "implementation_specialist": str,
    "implementation_base": str,
    "implementation_commit_range": str,
    "implementation_commits": list,
    "implementation_branch": str,
    "critique_path": str,
    "critics_run": list,
    "critique_files": list,
    "critique_finding_counts": dict,
    "findings_register": str,
    "rectification_commit": str,
    "rectification_not_required_reason": str,
    "fixed_findings": list,
    "deferred_findings": list,
    "invalidated_findings": list,
    "regression_tests_added": list,
    "publication_required": bool,
    "publication_not_required_reason": str,
    "operations_required": bool,
    "operations_not_required_reason": str,
}

FIELD_ENUMS: dict[str, set[str]] = {
    "research_mode": {"standard", "deep", "single"},
    "implementation_path": {"inline", "delegated", "specialist"},
}

KNOWN_FIELDS = {
    "schema_version", "id", "created_at", "updated_at", "phase", "phase_history", "agent_kit_commit", "kit_upgrade_history", "check_run_head", "check_run_hashes", "check_run_history", "check_run_attempts",
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
    "operations_plan", "operations_evidence", "waivers", "artifact_bindings",
    "migration",
}

MACHINE_FIELDS = {
    "schema_version", "id", "created_at", "updated_at", "phase", "phase_history", "agent_kit_commit", "kit_upgrade_history", "check_run_head", "check_run_hashes", "check_run_history", "check_run_attempts",
    "implementation_status", "operational_status", "review_status", "artifact_bindings",
    "review_manifest", "implementation_evidence", "publication_intent", "release_manifest",
    "operations_plan",
    "operations_evidence", "waivers", "migration",
}

# Human-authored fields are writable only while their owning phase is active.
# Once the corresponding phase closes, the state becomes an audit record; a
# correction must be represented by a new milestone/amendment rather than by
# rewriting history in place.
FIELD_WRITABLE_PHASES: dict[str, set[str]] = {
    "milestone_brief": {"init"},
    "research_briefs": {"research-running"},
    "research_mode": {"research-running"},
    "research_synthesis": {"research-running"},
    "implementation_path": {"implement-running"},
    "implementation_specialist": {"implement-running"},
    "implementation_base": {"implement-running"},
    "implementation_commit_range": {"implement-running"},
    "implementation_commits": {"implement-running"},
    "implementation_branch": {"implement-running"},
    "critique_path": {"critique-running"},
    "critics_run": {"critique-running"},
    "critique_files": {"critique-running"},
    "critique_finding_counts": {"critique-running"},
    "findings_register": {"critique-running"},
    "rectification_commit": {"rectify-running"},
    "rectification_not_required_reason": {"rectify-running"},
    "fixed_findings": {"rectify-running"},
    "deferred_findings": {"rectify-running"},
    "invalidated_findings": {"rectify-running"},
    "regression_tests_added": {"rectify-running"},
    "publication_required": {
        "init", "research-running", "research-complete", "implement-running",
        "implement-complete", "critique-running", "critique-complete", "rectify-running",
    },
    "publication_not_required_reason": {
        "init", "research-running", "research-complete", "implement-running",
        "implement-complete", "critique-running", "critique-complete", "rectify-running",
    },
    "operations_required": {
        "init", "research-running", "research-complete", "implement-running",
        "implement-complete", "critique-running", "critique-complete", "rectify-running",
    },
    "operations_not_required_reason": {
        "init", "research-running", "research-complete", "implement-running",
        "implement-complete", "critique-running", "critique-complete", "rectify-running",
    },
}

REQUIRED_FIELDS_BY_PHASE: dict[str, list[str]] = {
    "research-complete": ["research_briefs", "research_mode"],
    "implement-complete": [
        "implementation_base", "implementation_commit_range", "implementation_commits",
        "implementation_branch",
    ],
    "critique-complete": [
        "critique_path", "critics_run", "critique_files", "critique_finding_counts",
        "findings_register",
    ],
}

COUNT_KEYS = ("critical", "high", "medium", "low")
ALWAYS_REVIEWERS = {"milestone-adversary", "milestone-delivery-integrity-adversary"}
FINDINGS_SCRIPT = Path(__file__).resolve().parent / "milestone-pipeline-findings.py"
ARTIFACT_SCRIPT = Path(__file__).resolve().parent / "milestone-pipeline-artifacts.py"
AGENT_KIT_ROOT = Path(__file__).resolve().parents[2]


def _executing_kit_commit() -> str:
    try:
        value = subprocess.check_output(
            ["git", "-C", str(AGENT_KIT_ROOT), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip().lower()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        sys.exit(f"cannot identify executing milestone kit revision: {exc}")
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        sys.exit("executing milestone kit HEAD is not a full commit id")
    return value


def _find_repo_root() -> Path:
    env = os.environ.get("REPO_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        if out:
            return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / ".git").exists():
            return candidate
    sys.exit("could not determine repo root. Set REPO_ROOT or run inside the target git repo.")


def _state_path(mid: str) -> Path:
    if not MILESTONE_ID_RE.fullmatch(mid) or "/" in mid or "\\" in mid:
        sys.exit(
            f"invalid milestone id {mid!r} — ids are [A-Za-z0-9][A-Za-z0-9._-]* "
            "and become a state-directory segment"
        )
    return _find_repo_root() / ".claude" / "notes" / "milestones" / mid / "state.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        sys.exit(f"state.json not found at {path} — run milestone-pipeline-init-state.sh first")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"invalid state JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
    if not isinstance(state, dict):
        sys.exit("state.json root must be an object")
    version = state.get("schema_version")
    if version != STATE_SCHEMA_VERSION:
        sys.exit(
            f"state schema {version!r} is not supported by the v2 writer — run "
            "milestone-pipeline-migrate.py explicitly; mixed/implicit migration is forbidden"
        )
    unknown = sorted(set(state) - KNOWN_FIELDS)
    if unknown:
        sys.exit(f"state v2 contains unknown field(s): {', '.join(unknown)}")
    if state.get("id") != path.parent.name:
        sys.exit("state.id does not match its milestone directory (possible cross-run replay)")
    phase = state.get("phase")
    if phase not in PHASES:
        sys.exit(f"state.phase {phase!r} is not a v2 phase")
    history = state.get("phase_history")
    if not isinstance(history, list) or not history or history[-1].get("phase") != phase:
        sys.exit("state.phase_history must be non-empty and end at state.phase")
    frozen_kit = state.get("agent_kit_commit")
    executing_kit = _executing_kit_commit()
    if frozen_kit != executing_kit:
        sys.exit(
            "state.agent_kit_commit differs from the executing kit revision; "
            "inspect milestone-pipeline-artifacts.py kit-upgrade-preview and perform "
            "an explicit human-authorized kit-upgrade before any checkpoint read/write"
        )
    return state


def _save_atomic(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(state, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def _locked(path: Path):
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _refuse_pending_transactions(path: Path) -> None:
    pending = [
        candidate.name
        for candidate in (
            path.with_name(path.name + ".txn"),
            path.with_name(path.name + ".check-txn"),
        )
        if candidate.exists()
    ]
    if pending:
        sys.exit(
            "state has a pending artifact/check transaction; checkpoint refuses to "
            "read or mutate an ambiguous snapshot. Recover it first with "
            "milestone-pipeline-artifacts.py recover --state "
            f"{path}: {', '.join(pending)}"
        )


def _populated(state: dict[str, Any], field: str, problems: list[str]) -> None:
    value = state.get(field)
    if value is None or (isinstance(value, (str, list, dict)) and not value):
        problems.append(f"{field} not recorded")
        return
    expected = FIELD_TYPES.get(field)
    if expected and not isinstance(value, expected):
        problems.append(f"{field} has type {type(value).__name__}, expected {expected.__name__}")
        return
    allowed = FIELD_ENUMS.get(field)
    if allowed and value not in allowed:
        problems.append(f"{field} value {value!r} is not one of {sorted(allowed)}")
    if field == "critique_finding_counts":
        bad = [k for k in COUNT_KEYS if not isinstance(value.get(k), int) or isinstance(value.get(k), bool)]
        if bad:
            problems.append(f"critique_finding_counts missing/non-int keys: {', '.join(bad)}")


def _run_findings_gate(state: dict[str, Any], state_path: Path) -> None:
    marker = state.get("findings_register")
    if not isinstance(marker, str) or not marker.strip() or os.path.isabs(marker):
        sys.exit("refusing code-complete: state.findings_register must be a non-empty repo-relative path")
    repo = state_path.parents[4]
    register = (repo / marker).resolve()
    try:
        register.relative_to(repo.resolve())
    except ValueError:
        sys.exit("refusing code-complete: findings_register path traversal")
    if not register.is_file():
        sys.exit(f"refusing code-complete: findings register missing: {register}")
    if not FINDINGS_SCRIPT.is_file():
        sys.exit(f"refusing code-complete: findings gate script missing: {FINDINGS_SCRIPT}")
    # Bind the register to this state before asking the findings tool about open
    # severities. A clean register from another run must never satisfy the gate.
    try:
        payload = json.loads(register.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"refusing code-complete: invalid findings register: {exc}")
    if payload.get("milestone_id") != state["id"]:
        sys.exit("refusing code-complete: findings register milestone_id mismatch")
    if sorted(payload.get("critique_files") or []) != sorted(state.get("critique_files") or []):
        sys.exit("refusing code-complete: findings register critique set mismatch")
    proc = subprocess.run(
        [sys.executable, str(FINDINGS_SCRIPT), "gate", "--register", str(register)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit("refusing code-complete: findings gate refused:\n" + (proc.stderr or proc.stdout).rstrip())
    if "WARN" in proc.stdout:
        print(proc.stdout.rstrip())


def _artifact_gate(state_path: Path, phase: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not ARTIFACT_SCRIPT.is_file():
        sys.exit(f"refusing {phase}: artifact gate script missing: {ARTIFACT_SCRIPT}")
    proc = subprocess.run(
        [sys.executable, str(ARTIFACT_SCRIPT), "gate", "--state", str(state_path), "--phase", phase],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"refusing {phase}: artifact gate refused:\n{(proc.stderr or proc.stdout).rstrip()}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        sys.exit(f"refusing {phase}: artifact gate returned invalid JSON: {exc}")
    if result.get("ok") is not True or not isinstance(result.get("bindings"), dict):
        sys.exit(f"refusing {phase}: artifact gate did not return an ok bindings receipt")
    # Close the validate/read race: re-hash each path before persisting the
    # receipt. A later edit remains detectable from the persisted binding.
    bindings = result["bindings"]
    base = state_path.parent
    for kind, receipt in bindings.items():
        if not isinstance(receipt, dict):
            sys.exit(f"refusing {phase}: malformed binding for {kind}")
        rel = receipt.get("path")
        expected = receipt.get("sha256")
        if not isinstance(rel, str) or not isinstance(expected, str):
            sys.exit(f"refusing {phase}: incomplete binding for {kind}")
        path = (base / rel).resolve()
        try:
            path.relative_to((base / "artifacts").resolve())
        except ValueError:
            sys.exit(f"refusing {phase}: binding for {kind} escapes artifacts/")
        if not path.is_file():
            sys.exit(f"refusing {phase}: bound artifact vanished: {path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            sys.exit(f"refusing {phase}: {kind} changed between validation and binding")
    derived = result.get("derived") or {}
    if not isinstance(derived, dict):
        sys.exit(f"refusing {phase}: malformed derived-state receipt")
    return bindings, derived


def _check_transition_requirements(
    state: dict[str, Any], state_path: Path, new_phase: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    problems: list[str] = []
    for field in REQUIRED_FIELDS_BY_PHASE.get(new_phase, []):
        _populated(state, field, problems)
    if new_phase == "critique-complete":
        critics = state.get("critics_run") or []
        missing = sorted(ALWAYS_REVIEWERS - set(critics)) if isinstance(critics, list) else sorted(ALWAYS_REVIEWERS)
        if missing:
            problems.append(f"mandatory independent adversaries missing: {', '.join(missing)}")
        if isinstance(critics, list) and len(critics) != len(set(critics)):
            problems.append("critics_run contains duplicates")
        if isinstance(state.get("critique_files"), list) and len(state["critique_files"]) != len(critics):
            problems.append("critique_files must contain exactly one file per critic")
    if new_phase == "research-complete":
        briefs = state.get("research_briefs")
        if isinstance(briefs, list):
            repo = state_path.parents[4].resolve()
            for i, value in enumerate(briefs):
                if not isinstance(value, str) or not value.strip():
                    problems.append(f"research_briefs[{i}] must be a non-empty repo-relative path")
                    continue
                if os.path.isabs(value) or ".." in Path(value).parts:
                    problems.append(f"research_briefs[{i}] must be a safe repo-relative path")
                    continue
                path = (repo / value).resolve()
                try:
                    path.relative_to(repo)
                except ValueError:
                    problems.append(f"research_briefs[{i}] escapes the repository")
                    continue
                if not path.is_file():
                    problems.append(f"research_briefs[{i}] does not exist: {value}")
    if new_phase == "code-complete":
        has_commit = isinstance(state.get("rectification_commit"), str) and bool(state["rectification_commit"].strip())
        has_reason = isinstance(state.get("rectification_not_required_reason"), str) and bool(state["rectification_not_required_reason"].strip())
        if has_commit == has_reason:
            problems.append("exactly one of rectification_commit or rectification_not_required_reason is required")
        if state.get("publication_required") is False and state.get("operations_required") is True:
            problems.append("operations cannot be required when publication is explicitly not required")
    if new_phase == "publish-running" and state.get("publication_required") is not True:
        problems.append("publish-running requires publication_required=true")
    if new_phase in {"plan-review-running", "plan-reviewed"}:
        if state.get("publication_required") is not True:
            problems.append(f"{new_phase} requires publication_required=true")
        if state.get("operations_required") is not True:
            problems.append(f"{new_phase} requires operations_required=true")
    if new_phase == "apply-running" and state.get("operations_required") is not True:
        problems.append("apply-running requires operations_required=true")
    if new_phase == "complete":
        cur = state.get("phase")
        if cur == "code-complete":
            if state.get("publication_required") is not False or state.get("operations_required") is not False:
                problems.append("code-complete -> complete is only legal when publication and operations are not required")
        elif cur == "published" and state.get("operations_required") is not False:
            problems.append("published -> complete is only legal when operations are not required")
    if problems:
        sys.exit(
            f"refusing transition to {new_phase}:\n  - " + "\n  - ".join(problems)
            + "\nRecord valid evidence first; do not edit state.json directly."
        )
    if new_phase == "code-complete":
        _run_findings_gate(state, state_path)
    if new_phase in {
        "critique-complete", "code-complete", "publish-running", "published",
        "plan-reviewed", "apply-running", "applied", "verify-running",
        "operationally-verified", "complete",
    }:
        return _artifact_gate(state_path, new_phase)
    return {}, {}


def _phase_derived(cur: str, new: str) -> dict[str, str]:
    derived: dict[str, str] = {}
    if new in {"research-running", "research-complete", "implement-running", "implement-complete", "critique-running", "critique-complete", "rectify-running"}:
        derived["implementation_status"] = "in_progress"
    if new == "implement-complete":
        derived["implementation_status"] = "committed"
    if new == "publish-running":
        derived["implementation_status"] = "committed"
    if new == "verify-running":
        derived["operational_status"] = "applied"
    if new == "apply-running":
        derived["operational_status"] = "applying"
    return derived


def advance(mid: str, new_phase: str) -> None:
    if new_phase not in PHASES:
        sys.exit(f"unknown phase {new_phase!r}. Valid: {', '.join(sorted(PHASES))}")
    path = _state_path(mid)
    with _locked(path):
        _refuse_pending_transactions(path)
        initial_bytes = path.read_bytes()
        state = _load(path)
        cur = state["phase"]
        if new_phase not in PHASE_EDGES[cur]:
            legal = ", ".join(sorted(PHASE_EDGES[cur])) or "(terminal)"
            sys.exit(f"refusing illegal transition: {cur} -> {new_phase}. Legal next: {legal}")
        bindings, derived = _check_transition_requirements(state, path, new_phase)
        # Refresh after the artifact subprocess. The lock is advisory across
        # unrelated editors, so compare the complete byte snapshot, not just
        # phase/updated_at fields that an editor could leave unchanged.
        _current_bytes = path.read_bytes()
        if _current_bytes != initial_bytes:
            sys.exit("state changed concurrently outside the checkpoint lock; retry after inspection")
        latest = _load(path)
        state = latest
        state["artifact_bindings"].update(bindings)
        for key, value in {**_phase_derived(cur, new_phase), **derived}.items():
            if key not in {"implementation_status", "operational_status", "review_status"}:
                sys.exit(f"artifact gate attempted to set unauthorized derived field {key!r}")
            state[key] = value
        now = _now()
        state["phase"] = new_phase
        state["updated_at"] = now
        state["phase_history"].append({"phase": new_phase, "at": now})
        _save_atomic(path, state)
    print(f"{mid}: {cur} -> {new_phase} @ {now}")


def get_field(mid: str, field: str) -> None:
    path = _state_path(mid)
    with _locked(path):
        _refuse_pending_transactions(path)
        state = _load(path)
    if field not in state:
        sys.exit(f"unknown field: {field}. Valid: {', '.join(sorted(state))}")
    value = state[field]
    if isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2))
    elif value is None:
        print("")
    else:
        print(value)


def set_field(mid: str, expr: str) -> None:
    if "=" not in expr:
        sys.exit("--set value must be field=<json>")
    field, raw = expr.split("=", 1)
    field = field.strip()
    if field not in KNOWN_FIELDS:
        sys.exit(f"unknown field {field!r}; add it to the v2 schema first")
    if field in MACHINE_FIELDS:
        sys.exit(f"--set {field}: machine-owned field; only the init/checkpoint/artifact tools may write it")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    expected = FIELD_TYPES.get(field)
    if expected and not isinstance(value, expected):
        # Nullable optional fields are cleared with JSON null.
        if value is not None:
            sys.exit(f"--set {field}: expected {expected.__name__} or null, got {type(value).__name__}")
    allowed = FIELD_ENUMS.get(field)
    if allowed and value is not None and value not in allowed:
        sys.exit(f"--set {field}: {value!r} is not one of {sorted(allowed)}")
    path = _state_path(mid)
    with _locked(path):
        _refuse_pending_transactions(path)
        state = _load(path)
        if state["phase"] == "complete":
            sys.exit(f"--set {field}: complete is terminal and immutable")
        writable = FIELD_WRITABLE_PHASES.get(field)
        if writable is None:
            sys.exit(f"--set {field}: no owning phase is declared; schema/tool update required")
        if state["phase"] not in writable:
            allowed = ", ".join(sorted(writable))
            sys.exit(
                f"--set {field}: field is frozen in phase {state['phase']!r}; "
                f"writable only in: {allowed}"
            )
        state[field] = value
        state["updated_at"] = _now()
        _save_atomic(path, state)
    print(f"{mid}: set {field} = {json.dumps(value)[:100]}")


def self_test() -> int:
    import contextlib
    import io
    import tempfile

    global ARTIFACT_SCRIPT, FINDINGS_SCRIPT
    failures = 0

    def expect(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {name}: {'ok' if ok else 'FAIL ' + detail[:140]}")
        failures += 0 if ok else 1

    def run(args: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                main(["checkpoint.py", *args])
                rc = 0
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
                if isinstance(exc.code, str):
                    print(exc.code)
        return rc, output.getvalue()

    expect(
        "operational evidence is reverified before any re-apply",
        PHASE_EDGES["operationally-verified"] == {"complete", "verify-running"},
        str(PHASE_EDGES["operationally-verified"]),
    )
    expect(
        "stale completed delivery re-enters read-only verification",
        PHASE_EDGES["complete"] == {"verify-running"},
        str(PHASE_EDGES["complete"]),
    )

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        os.environ["REPO_ROOT"] = str(root)
        state_dir = root / ".claude" / "notes" / "milestones" / "m1"
        (state_dir / "artifacts").mkdir(parents=True)
        register = state_dir / "findings.json"
        register.write_text(json.dumps({
            "schema_version": 1, "milestone_id": "m1", "critique_files": ["a.md", "b.md"], "findings": []
        }))
        now = "2026-07-12T00:00:00Z"
        state = {
            "schema_version": 2, "id": "m1", "created_at": now, "updated_at": now,
            "phase": "init", "phase_history": [{"phase": "init", "at": now}],
            "milestone_brief": "test", "research_mode": None, "research_briefs": [], "research_synthesis": None,
            "implementation_path": None, "implementation_specialist": None, "implementation_base": None,
            "implementation_commit_range": None, "implementation_commits": [], "implementation_branch": None,
            "critique_path": None, "critics_run": [], "critique_files": [], "critique_finding_counts": None,
            "findings_register": None, "rectification_commit": None, "rectification_not_required_reason": None,
            "fixed_findings": [], "deferred_findings": [], "invalidated_findings": [], "regression_tests_added": [],
            "publication_required": True, "publication_not_required_reason": None,
            "operations_required": True, "operations_not_required_reason": None,
            "implementation_status": "pending", "operational_status": "pending", "review_status": "pending",
            "review_manifest": "artifacts/review-manifest.json", "implementation_evidence": "artifacts/implementation-evidence.json",
            "publication_intent": "artifacts/publication-intent.json",
            "release_manifest": "artifacts/release-manifest.json", "operations_plan": "artifacts/operations-plan.json",
            "operations_evidence": "artifacts/operations-evidence.json", "waivers": "artifacts/waivers.json",
            "artifact_bindings": {}, "migration": None,
            "check_run_head": None, "check_run_hashes": {}, "check_run_history": {},
            "check_run_attempts": [], "agent_kit_commit": _executing_kit_commit(),
            "kit_upgrade_history": [],
        }
        path = state_dir / "state.json"
        path.write_text(json.dumps(state))
        fake_art = root / "artifact-gate.py"
        fake_art.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps({'ok':True,'bindings':{},'derived':{}}))\n")
        fake_find = root / "findings-gate.py"
        fake_find.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n")
        old_art, old_find = ARTIFACT_SCRIPT, FINDINGS_SCRIPT
        ARTIFACT_SCRIPT, FINDINGS_SCRIPT = fake_art, fake_find
        try:
            rc, out = run(["m1", "research-complete"])
            expect("illegal skipped transition", rc != 0 and "illegal transition" in out, out)
            rc, _ = run(["m1", "research-running"]); expect("init -> research-running", rc == 0)
            before_journal = path.read_bytes()
            pending_check = path.with_name(path.name + ".check-txn")
            pending_check.write_text("{}", encoding="utf-8")
            rc, out = run(["m1", "--set", 'research_mode="standard"'])
            expect(
                "checkpoint refuses pending check journal",
                rc != 0 and "pending artifact/check transaction" in out
                and path.read_bytes() == before_journal,
                out,
            )
            pending_check.unlink()
            pending_mutable = path.with_name(path.name + ".txn")
            pending_mutable.write_text("{}", encoding="utf-8")
            rc, out = run(["m1", "--get", "phase"])
            expect(
                "checkpoint read refuses pending mutable journal",
                rc != 0 and "pending artifact/check transaction" in out,
                out,
            )
            pending_mutable.unlink()
            rc, out = run(["m1", "research-complete"]); expect("research evidence required", rc != 0 and "research_briefs" in out, out)
            run(["m1", "--set", "research_briefs=[true]"])
            rc, out = run(["m1", "research-complete"])
            expect("research brief items are paths", rc != 0 and "repo-relative path" in out, out)
            frozen = json.loads(path.read_text(encoding="utf-8"))
            frozen["agent_kit_commit"] = "0" * 40
            path.write_text(json.dumps(frozen), encoding="utf-8")
            rc, out = run(["m1", "--get", "phase"])
            expect("checkpoint refuses unreviewed kit drift", rc != 0 and "kit-upgrade" in out, out)
            frozen["agent_kit_commit"] = _executing_kit_commit()
            path.write_text(json.dumps(frozen), encoding="utf-8")
            (root / "a.md").write_text("research\n", encoding="utf-8")
            run(["m1", "--set", 'research_briefs=["a.md"]'])
            run(["m1", "--set", 'research_mode="standard"'])
            rc, _ = run(["m1", "research-complete"]); expect("research complete", rc == 0)
            rc, out = run(["m1", "--set", 'research_synthesis="rewritten"'])
            expect("research fields freeze after research-complete", rc != 0 and "frozen" in out, out)
            run(["m1", "implement-running"])
            for expr in ('implementation_base="abcdef1"', 'implementation_commit_range="abcdef1..abcdef2"',
                         'implementation_commits=["abcdef2"]', 'implementation_branch="dev"'):
                run(["m1", "--set", expr])
            rc, _ = run(["m1", "implement-complete"]); expect("implement complete", rc == 0)
            rc, out = run(["m1", "--set", 'implementation_base="fffffff"'])
            expect("implementation fields freeze after implement-complete", rc != 0 and "frozen" in out, out)
            run(["m1", "critique-running"])
            for expr in ('critique_path="a.md"', 'critics_run=["milestone-adversary"]',
                         'critique_files=["a.md"]',
                         'critique_finding_counts={"critical":0,"high":0,"medium":0,"low":0}',
                         'findings_register=".claude/notes/milestones/m1/findings.json"'):
                run(["m1", "--set", expr])
            rc, out = run(["m1", "critique-complete"])
            expect("two independent adversaries required", rc != 0 and "delivery-integrity" in out, out)
            run(["m1", "--set", 'critics_run=["milestone-adversary","milestone-delivery-integrity-adversary"]'])
            run(["m1", "--set", 'critique_files=["a.md","b.md"]'])
            rc, _ = run(["m1", "critique-complete"]); expect("critique complete", rc == 0)
            rc, out = run(["m1", "--set", 'critique_path="rewritten.md"'])
            expect("critique fields freeze after critique-complete", rc != 0 and "frozen" in out, out)
            run(["m1", "rectify-running"])
            rc, out = run(["m1", "code-complete"])
            expect("rectification evidence required", rc != 0 and "exactly one" in out, out)
            run(["m1", "--set", 'rectification_not_required_reason="no findings"'])
            rc, _ = run(["m1", "code-complete"]); expect("code complete", rc == 0)
            rc, out = run(["m1", "complete"])
            expect("short complete refuses required delivery", rc != 0 and "only legal" in out, out)
            rc, out = run(["m1", "--set", "implementation_status=published"])
            expect("machine fields refuse --set", rc != 0 and "machine-owned" in out, out)

            complete_state = _load(path)
            complete_state["phase"] = "complete"
            complete_state["phase_history"].append({"phase": "complete", "at": now})
            _save_atomic(path, complete_state)
            rc, out = run(["m1", "--set", 'milestone_brief="rewritten after completion"'])
            expect("complete state refuses all --set", rc != 0 and "terminal and immutable" in out, out)
        finally:
            ARTIFACT_SCRIPT, FINDINGS_SCRIPT = old_art, old_find
            os.environ.pop("REPO_ROOT", None)

        legacy_dir = root / ".claude" / "notes" / "milestones" / "legacy"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "state.json").write_text(json.dumps({"id": "legacy", "phase": "complete", "phase_history": [{"phase": "complete"}]}))
        os.environ["REPO_ROOT"] = str(root)
        rc, out = run(["legacy", "--get", "phase"])
        expect("v1 state refuses implicit migration", rc != 0 and "migrate" in out, out)
        os.environ.pop("REPO_ROOT", None)

    print(f"milestone-pipeline-checkpoint self-test: {'OK' if failures == 0 else f'{failures} failure(s)'}")
    return 0 if failures == 0 else 1


def main(argv: list[str]) -> None:
    if len(argv) == 2 and argv[1] == "--self-test":
        raise SystemExit(self_test())
    if len(argv) < 3:
        sys.exit(__doc__)
    mid = argv[1]
    if argv[2] == "--get":
        if len(argv) != 4:
            sys.exit("usage: checkpoint.py <ID> --get <field>")
        get_field(mid, argv[3])
    elif argv[2] == "--set":
        if len(argv) != 4:
            sys.exit("usage: checkpoint.py <ID> --set <field>=<json>")
        set_field(mid, argv[3])
    else:
        if len(argv) != 3:
            sys.exit("usage: checkpoint.py <ID> <new-phase>")
        advance(mid, argv[2])


if __name__ == "__main__":
    main(sys.argv)

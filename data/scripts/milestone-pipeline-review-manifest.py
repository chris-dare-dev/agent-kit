#!/usr/bin/env python3
"""Build `artifacts/review-manifest.json` from a prepared + dispatched review wave.

The third of the three scripts in the /milestone-pipeline Workflow port:

    milestone-pipeline-review-prepare.py   persist + emit the prompt bytes (and their
                                           length+checksum tripwire values)
    milestone-pipeline-workflow.mjs        re-measure the args value against those
                                           tripwires, then dispatch it, parallel + blind
    milestone-pipeline-review-manifest.py  <- verify the wave, then bind the receipts

WHAT THIS SCRIPT CAN AND CANNOT SEE
-----------------------------------
It hashes FILES. It has no visibility into what was actually dispatched: the
prompt reaches the Workflow tool over a model-composed args hop that is not
byte-attested by the harness, and the dispatched bytes are retained nowhere. A
run that persisted a compliant prompt and dispatched something else would pass
every check in this file.

That hop is covered upstream, in the .mjs, by a length+checksum tripwire that
throws before dispatch — not here, and not by the harness. What this file adds is
that the tripwire VALUES themselves are re-derived from the persisted prompt
(`_verify_prompt_artifacts`), so a stale or edited prepare result cannot quietly
hand the .mjs the wrong expectation to compare against.

WHY A SCRIPT AND NOT AN AGENT
-----------------------------
Every claim in a review manifest is deterministic: a hash, a range, a file's
existence, a parsed count. The platform's rule is that "agents may author
artifact contents, but only milestone-pipeline-artifacts.py can validate a
delivery claim" — deterministic claims live in scripts. The Workflow `.mjs`
additionally CANNOT do this work at all: it has no filesystem and `Date.now()`
throws.

WHAT IT REFUSES (each refusal is a real failure this pipeline has hit)
---------------------------------------------------------------------
* **wave invalidated** — HEAD moved or the worktree's tracked/untracked state
  changed during the wave. The reviewers are read-only by contract; a mutation
  means something wrote to the tree under review, and every receipt would bind a
  range that no longer describes what was reviewed.
* **zero findings parsed** — a critique that passes the format lint but yields no
  findings leaves the closure loop verifying an empty set. `extract --check`
  cannot catch this: a header block with `Severity counts: C0 H0 M0 L0` and no
  finding blocks is *well-formed*. A dead or truncated critic looks exactly like
  a perfect diff, and this pipeline has already been burned by a dead critic
  returning a clean count.
* **any hash/marker drift** — checked BEFORE the manifest is written, because
  after `critique-complete` binds it there is deliberately NO in-machine repair
  (`immutable_root_hash` + forward-only `PHASE_EDGES` + `review-append`'s
  hash-bound critique set). A refusal here is cheap; the same defect discovered
  after the binding costs the whole run.

It never writes a manifest it cannot validate: the document is built in memory,
validated against `milestone-review-manifest-v2.schema.json` via
`milestone-pipeline-schema-check.py --instance`, and only then written.

Usage:

    python3 milestone-pipeline-review-manifest.py --id ID --repo-root PATH \\
        --prepare-result prepare.json [--prepare-result wave2.json] \\
        --workflow-result workflow.json [--provider claude] [--generation 1]
    python3 milestone-pipeline-review-manifest.py --self-test

Exit 0 ok | 2 usage | 3 REFUSED.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ARTIFACTS_SCRIPT = SCRIPT_DIR / "milestone-pipeline-artifacts.py"
FINDINGS_SCRIPT = SCRIPT_DIR / "milestone-pipeline-findings.py"
SCHEMA_CHECK_SCRIPT = SCRIPT_DIR / "milestone-pipeline-schema-check.py"
PREPARE_SCRIPT = SCRIPT_DIR / "milestone-pipeline-review-prepare.py"

MANIFEST_SCHEMA = "milestone-review-manifest-v2.schema.json"
FRONTMATTER_MODEL_RE = re.compile(r"^model:\s*(\S+)\s*$", re.MULTILINE)


class Refused(Exception):
    """Deterministic refusal. Never downgraded to a warning."""


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Refused(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _git(repo: Path, *a: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    if proc.returncode != 0:
        raise Refused(f"git {' '.join(a)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def _git_bytes(repo: Path, *a: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *a], capture_output=True)
    if proc.returncode != 0:
        raise Refused(
            f"git {' '.join(a)} failed in {repo}: "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout


def _iso(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise Refused(f"{label}: expected a non-empty RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refused(f"{label}: invalid ISO-8601 timestamp {value!r}: {exc}") from exc
    if parsed.tzinfo is None:
        raise Refused(f"{label}: timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


# ------------------------------------------------------- the wave invariant


def verify_wave_invariant(
    *, repo_root: Path, state_path: Path, pre_wave: dict[str, Any], artifacts: Any
) -> None:
    """HEAD + worktree status must be byte-identical to the pre-wave snapshot.

    The command's rule: "Any reviewer-created tracked mutation or ref move
    invalidates the wave and stops the pipeline." `_worktree_status` is imported
    from the frozen writer rather than reimplemented: it reports tracked changes
    plus untracked files OUTSIDE the milestone state dir, so the reviewers'
    own critique files are correctly not treated as drift while a reviewer that
    touched source is.
    """
    head_now = _git(repo_root, "rev-parse", "HEAD").strip()
    if head_now != pre_wave.get("head"):
        raise Refused(
            f"WAVE INVALIDATED: HEAD moved during the review wave "
            f"({pre_wave.get('head')} -> {head_now}). Reviewers are read-only; a ref move "
            "means the tree under review changed. Discard this wave, re-run prepare "
            "against the correct head, and re-dispatch."
        )
    try:
        porcelain_now = artifacts._worktree_status(repo_root, state_path)
    except Exception as exc:
        raise Refused(f"cannot read worktree status: {exc}") from exc
    sha_now = _sha256_bytes(porcelain_now.encode("utf-8"))
    if sha_now != pre_wave.get("porcelain_sha256"):
        raise Refused(
            "WAVE INVALIDATED: the worktree changed during the review wave "
            f"(status sha256 {pre_wave.get('porcelain_sha256')} -> {sha_now}). A reviewer "
            "created a tracked mutation, or a concurrent session wrote to the repo. "
            "Every receipt would bind a range that no longer describes what was reviewed. "
            f"Current status:\n{porcelain_now.strip() or '(clean)'}"
        )


# ------------------------------------------------------------- the critiques


def _findings_check(findings_mod: Any, critique_paths: list[Path]) -> dict[str, int]:
    """Lint every critique in ONE call, then REFUSE any that parses zero findings.

    Two distinct gates, deliberately not merged:
      * `extract --check` is the FORMAT authority (one parser, one format).
      * the zero-count gate is a SUBSTANCE floor the lint cannot express.
    """
    proc = subprocess.run(
        [sys.executable, str(FINDINGS_SCRIPT), "extract", "--check",
         *[str(p) for p in critique_paths]],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise Refused(
            "critique format lint failed (milestone-pipeline-findings.py extract --check):\n"
            f"{(proc.stderr or proc.stdout).strip()}"
        )

    counts: dict[str, int] = {}
    for path in critique_paths:
        text = path.read_text(encoding="utf-8")
        findings, problems = findings_mod.parse_critique(text, str(path))
        if problems:
            raise Refused(
                f"{path}: critique parse problems the lint did not surface: {problems}"
            )
        counts[str(path)] = len(findings)

    empty = sorted(p for p, n in counts.items() if n == 0)
    if empty:
        raise Refused(
            "ZERO FINDINGS parsed from: " + ", ".join(empty) + ".\n"
            "A critique that lints clean but yields no findings leaves the closure loop "
            "verifying an EMPTY SET — a dead, truncated, or misdispatched critic is "
            "indistinguishable from a perfect diff at that point. Refusing to bind it.\n"
            "Investigate the critic's run (did it read the diff? did it die mid-write?) and "
            "re-dispatch that reviewer with a fresh task id. Do NOT hand-write findings into "
            "the file to get past this gate: the closure verifier re-opens the code."
        )
    return counts


def _assert_critique_markers(
    *, path: Path, role: str, verdict: str, base: str, head: str, prepare_mod: Any
) -> None:
    """Pre-check the markers artifacts.py will demand at critique-complete.

    Same rules as `_validate_review_receipt`, applied while the run is still
    repairable. After critique-complete the manifest is immutable and the only
    remedy is re-initializing the milestone.
    """
    text = path.read_text(encoding="utf-8")
    expected_critic = prepare_mod.critic_marker(role)
    markers = re.findall(r"(?m)^\*\*Critic(?:\(s\))?:\*\*\s*(\S+)\s*$", text)
    if markers != [expected_critic]:
        raise Refused(
            f"{path}: **Critic:** must identify exactly {expected_critic!r}, found {markers!r}"
        )
    if f"**Diff range:** {base}..{head}" not in text:
        raise Refused(
            f"{path}: report does not declare the exact reviewed range "
            f"'**Diff range:** {base}..{head}'"
        )
    verdicts = re.findall(
        r"(?m)^(?:- )?\*\*Overall verdict:\*\*\s*(SHIP-WITH-FIXES|DO-NOT-SHIP|SHIP)\s*$", text
    )
    if len(verdicts) != 1:
        raise Refused(
            f"{path}: expected exactly one '**Overall verdict:**' marker, found {len(verdicts)}"
        )
    if verdicts[0] != verdict:
        raise Refused(
            f"{path}: reviewer returned verdict {verdict!r} but the report declares "
            f"{verdicts[0]!r} — the receipt would contradict its own evidence"
        )


def _verify_prompt_artifacts(
    *, role: str, prompt_path: Path, spec: dict[str, Any], prepare_mod: Any
) -> None:
    """Re-derive the .mjs's tripwire values from the persisted prompt.

    This does NOT verify what was dispatched — nothing here can; the dispatched
    bytes are retained nowhere. It verifies that the values the .mjs was told to
    compare against actually describe the prompt file this manifest binds. A
    stale, hand-edited, or wrong-wave prepare result would otherwise hand the
    .mjs an expectation unrelated to the persisted prompt, silently turning the
    leg-3 tripwire into a check of a string against its own bad twin.

    The functions come from prepare_mod so there is exactly one Python
    implementation, agreed with the .mjs by prepare.py's --self-test.
    """
    text = prompt_path.read_text(encoding="utf-8")
    expected_len = prepare_mod.prompt_len_utf16(text)
    expected_checksum = prepare_mod.prompt_checksum(text)
    if spec["prompt_len_utf16"] != expected_len:
        raise Refused(
            f"{role}: prepare result claims prompt_len_utf16={spec['prompt_len_utf16']} but "
            f"{prompt_path} measures {expected_len}. The prepare result does not describe this "
            "prompt; the .mjs's dispatch tripwire was comparing against the wrong expectation."
        )
    if spec["prompt_checksum"] != expected_checksum:
        raise Refused(
            f"{role}: prepare result claims prompt_checksum={spec['prompt_checksum']!r} but "
            f"{prompt_path} measures {expected_checksum!r}. The prepare result does not describe "
            "this prompt; the .mjs's dispatch tripwire was comparing against the wrong expectation."
        )


def _stamped_model(body_text: str) -> str | None:
    """The model the agent inherits from its stamped frontmatter.

    The .mjs passes `model: undefined` (no override), so the stamped value in the
    canonical body snapshot IS what ran. Reading it from the snapshot rather than
    accepting a caller-supplied string keeps the receipt's `model` bound to the
    same bytes the rest of the receipt binds.
    """
    head = body_text.split("\n---", 2)
    match = FRONTMATTER_MODEL_RE.search(head[0] if head else body_text)
    return match.group(1) if match else None


# ---------------------------------------------------------------- the build


PREPARE_TOP_FIELDS = (
    "id", "state_path", "reviewed_base", "reviewed_head", "agent_kit_commit",
    "remote_url", "workspace_root", "required_reviewers", "reviewers", "pre_wave",
    "reviews_census",
)
PREPARE_REVIEWER_FIELDS = (
    "role", "task_id", "prompt_path", "prompt_sha256", "prompt_len_utf16",
    "prompt_checksum", "agent_body_path", "agent_body_snapshot_path",
    "agent_body_sha256", "critique_path", "critique_abs",
)


def _require_prepare_shape(result: Any, label: str) -> None:
    """Refuse a hand-authored or truncated prepare result explicitly.

    Without this, a missing key surfaced as a KeyError traceback and exit 1 —
    indistinguishable from a crash, and NOT the documented exit-3 refusal. The
    only supported producer is milestone-pipeline-review-prepare.py; say so.
    """
    if not isinstance(result, dict):
        raise Refused(f"{label}: expected a JSON object from milestone-pipeline-review-prepare.py")
    missing = [f for f in PREPARE_TOP_FIELDS if f not in result]
    if missing:
        raise Refused(
            f"{label}: missing {missing} — this must be the unmodified stdout of "
            "milestone-pipeline-review-prepare.py, not a hand-authored object"
        )
    if not isinstance(result["reviewers"], list) or not result["reviewers"]:
        raise Refused(f"{label}.reviewers: expected a non-empty array")
    for i, reviewer in enumerate(result["reviewers"]):
        if not isinstance(reviewer, dict):
            raise Refused(f"{label}.reviewers[{i}]: expected an object")
        gaps = [f for f in PREPARE_REVIEWER_FIELDS if f not in reviewer]
        if gaps:
            raise Refused(f"{label}.reviewers[{i}]: missing {gaps}")
    pre_wave = result["pre_wave"]
    if not isinstance(pre_wave, dict) or "head" not in pre_wave \
            or "porcelain_sha256" not in pre_wave:
        raise Refused(
            f"{label}.pre_wave: expected {{head, porcelain_sha256}} — without it the wave "
            "invariant cannot be verified and the wave cannot be bound"
        )
    if not isinstance(result["reviews_census"], dict):
        raise Refused(
            f"{label}.reviews_census: expected an object mapping reviews-dir paths to sha256 — "
            "without it a reviewer's stray or overwriting write under artifacts/reviews/ is "
            "invisible (_worktree_status ignores that directory entirely)"
        )


def verify_reviews_census(
    *, reviews_dir: Path, prepare_results: list[dict[str, Any]]
) -> None:
    """Refuse unbound or mutated artifacts under `artifacts/reviews/`.

    WHY: `artifacts.py:_worktree_status` deliberately ignores untracked files
    under the milestone state dir — otherwise every reviewer writing its own
    critique would trip the wave invariant. The side effect is that the reviews
    directory, where all of this wave's provenance lives, is the one place NOT
    covered by the invariant. A reviewer could overwrite a sibling's prompt or
    critique and no check would notice.

    WHAT THIS CLOSES:
      * mutation or deletion of any file that existed at prepare time — that
        includes every prompt + agent-body snapshot, and any EARLIER wave's
        critique;
      * any file appearing under reviews/ that no prepare result bound.

    WHAT IT DOES NOT CLOSE (accepted residual, stated so nobody assumes more):
      * SAME-WAVE critique cross-authoring. Two critiques of one wave are both
        legitimately new, so neither the census nor the invariant can tell "each
        reviewer wrote its own" from "one agent wrote both". The active defence
        there is `_assert_critique_markers`, which pins each critique's
        `**Critic:**` to the role it is bound to, plus the duplicate-sha256
        backstop; together they bound the gap to a single agent fabricating a
        correctly-marked critique for a role it is not. Detecting THAT needs
        per-file authorship attestation the harness does not provide.
    """
    expected: set[str] = set()
    for result in prepare_results:
        for reviewer in result["reviewers"]:
            for field in ("prompt_path", "agent_body_snapshot_path"):
                expected.add(Path(reviewer[field]).name)
            expected.add(Path(reviewer["critique_abs"]).name)

    # Union of every wave's census: a later wave's census legitimately contains an
    # earlier wave's artifacts, and each must still hash to what SOME wave saw.
    censused: dict[str, str] = {}
    for result in prepare_results:
        for rel, sha in result["reviews_census"].items():
            censused.setdefault(rel, sha)

    present: dict[str, str] = {}
    for path in sorted(reviews_dir.rglob("*")):
        if path.is_file():
            present[str(path.relative_to(reviews_dir))] = _file_sha(path)

    for rel, sha in sorted(censused.items()):
        if rel not in present:
            raise Refused(
                f"WAVE INVALIDATED: {reviews_dir / rel} existed when the wave was prepared and is "
                "now GONE. A reviewer deleted bound provenance; the receipts cannot be trusted."
            )
        if present[rel] != sha:
            raise Refused(
                f"WAVE INVALIDATED: {reviews_dir / rel} changed during the review wave "
                f"(sha256 {sha} -> {present[rel]}). A reviewer overwrote another reviewer's "
                "artifact — the receipt would bind bytes that are no longer there. "
                "Discard this wave and re-dispatch with fresh task ids."
            )

    stray = sorted(rel for rel in present if rel not in censused and rel not in expected)
    if stray:
        raise Refused(
            "WAVE INVALIDATED: unbound file(s) appeared under "
            f"{reviews_dir}: {stray}. Every artifact in the reviews directory must be one a "
            "prepare result bound (a prompt, an agent-body snapshot, or a critique). A reviewer "
            "writing anything else there is out of contract, and the directory is invisible to "
            "the worktree invariant, so this is the only place it can be caught."
        )


def build_manifest(
    *,
    milestone_id: str,
    repo_root: Path,
    prepare_results: list[dict[str, Any]],
    workflow_result: dict[str, Any],
    provider: str,
    generation: int,
    artifacts: Any,
    findings_mod: Any,
    prepare_mod: Any,
    kit_root: Path,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not prepare_results:
        raise Refused("at least one --prepare-result is required")
    for i, result in enumerate(prepare_results):
        _require_prepare_shape(result, f"--prepare-result[{i}]")
        # --id is retyped by a human; the prepare result carries the id the
        # artifacts were actually built for. Binding a manifest for milestone A
        # from milestone B's wave would file a real review against the wrong
        # milestone and pass every other check here (the paths all resolve, the
        # hashes all match — they are simply the wrong milestone's).
        if result["id"] != milestone_id:
            raise Refused(
                f"--prepare-result[{i}] was prepared for milestone {result['id']!r} but --id says "
                f"{milestone_id!r}. Refusing to bind one milestone's review wave to another's "
                "manifest; re-run with --id " + repr(result["id"]) + " or pass that milestone's "
                "prepare result."
            )
    first = prepare_results[0]
    state_path = Path(first["state_path"])
    state_dir = state_path.parent

    base = first["reviewed_base"]
    head = first["reviewed_head"]
    kit_commit = first["agent_kit_commit"]
    remote_url = first["remote_url"]
    workspace_root = first["workspace_root"]
    required = sorted(first["required_reviewers"])

    for extra in prepare_results[1:]:
        for field in ("reviewed_base", "reviewed_head", "agent_kit_commit",
                      "remote_url", "workspace_root"):
            if extra.get(field) != first.get(field):
                raise Refused(
                    f"prepare results disagree on {field}: "
                    f"{first.get(field)!r} vs {extra.get(field)!r} — waves of one manifest "
                    "must share one frozen scope"
                )
        if sorted(extra.get("required_reviewers", [])) != required:
            raise Refused("prepare results disagree on the deterministic reviewer set")

    # Every wave's invariant is checked: a later wave cannot excuse an earlier
    # wave's drift.
    for result in prepare_results:
        verify_wave_invariant(
            repo_root=repo_root, state_path=state_path,
            pre_wave=result["pre_wave"], artifacts=artifacts,
        )
    # ...and the invariant's own blind spot (artifacts/reviews/) is covered here.
    verify_reviews_census(
        reviews_dir=state_dir / "artifacts" / "reviews",
        prepare_results=prepare_results,
    )

    prepared: dict[str, dict[str, Any]] = {}
    for result in prepare_results:
        for reviewer in result["reviewers"]:
            if reviewer["task_id"] in prepared:
                raise Refused(f"duplicate task id across waves: {reviewer['task_id']}")
            prepared[reviewer["task_id"]] = reviewer

    prepared_roles = sorted({r["role"] for r in prepared.values()})
    if prepared_roles != required:
        missing = sorted(set(required) - set(prepared_roles))
        raise Refused(
            f"prepared reviewers {prepared_roles} do not cover the deterministic selection "
            f"{required} (missing {missing}). The validator recomputes this selection from "
            "the live diff; a manifest cannot omit a selected reviewer."
        )

    dispatched = workflow_result.get("reviews")
    if not isinstance(dispatched, list) or not dispatched:
        raise Refused("--workflow-result carries no reviews[]")
    if len(dispatched) != len(prepared):
        raise Refused(
            f"workflow returned {len(dispatched)} review(s) for {len(prepared)} prepared "
            "reviewer(s) — a dropped reviewer is never a clean pass"
        )

    reviews: list[dict[str, Any]] = []
    critique_paths: list[Path] = []

    for item in dispatched:
        task_id = item.get("task_id")
        spec = prepared.get(task_id)
        if spec is None:
            raise Refused(
                f"workflow returned task id {task_id!r} that prepare never persisted — "
                "the receipt cannot be bound to a persisted prompt"
            )
        role = spec["role"]
        if item.get("role") != role:
            raise Refused(f"task {task_id}: role mismatch {item.get('role')!r} != {role!r}")

        # Re-verify the persisted artifacts still hash to what prepare emitted.
        prompt_path = state_dir / spec["prompt_path"]
        if not prompt_path.is_file():
            raise Refused(f"{role}: persisted prompt is missing: {prompt_path}")
        if _file_sha(prompt_path) != spec["prompt_sha256"]:
            raise Refused(
                f"{role}: the persisted prompt changed after dispatch ({prompt_path}). "
                "The manifest would bind bytes that were never sent."
            )
        _verify_prompt_artifacts(
            role=role, prompt_path=prompt_path, spec=spec, prepare_mod=prepare_mod
        )
        body_snapshot = state_dir / spec["agent_body_snapshot_path"]
        if not body_snapshot.is_file():
            raise Refused(f"{role}: agent body snapshot is missing: {body_snapshot}")
        if _file_sha(body_snapshot) != spec["agent_body_sha256"]:
            raise Refused(f"{role}: the agent body snapshot changed after dispatch")
        canonical = _git_bytes(kit_root, "show", f"{kit_commit}:{spec['agent_body_path']}")
        if body_snapshot.read_bytes() != canonical:
            raise Refused(
                f"{role}: body snapshot is not {spec['agent_body_path']} at {kit_commit} — "
                "the receipt would misattribute the role"
            )

        critique_abs = Path(spec["critique_abs"])
        claimed = item.get("critique_path")
        if claimed and Path(claimed).resolve() != critique_abs.resolve():
            raise Refused(
                f"{role}: reviewer wrote {claimed!r} but was bound to {str(critique_abs)!r}"
            )
        if not critique_abs.is_file():
            raise Refused(
                f"{role}: critique is missing at {critique_abs} — the reviewer returned a "
                "result without producing its artifact"
            )

        verdict = item.get("verdict")
        if verdict not in {"SHIP", "SHIP-WITH-FIXES", "DO-NOT-SHIP"}:
            raise Refused(f"{role}: invalid assessment verdict {verdict!r}")
        _assert_critique_markers(
            path=critique_abs, role=role, verdict=verdict, base=base, head=head,
            prepare_mod=prepare_mod,
        )

        started = _iso(item.get("started_at"), f"{role}.started_at")
        completed = _iso(item.get("completed_at"), f"{role}.completed_at")
        if completed < started:
            raise Refused(f"{role}: completed_at precedes started_at")
        if completed > now:
            raise Refused(
                f"{role}: future-dated review ({completed.isoformat()} > {now.isoformat()})"
            )

        critique_paths.append(critique_abs)
        reviews.append({
            "role": role,
            "stage": "assessment",
            "provider": provider,
            "model": _stamped_model(body_snapshot.read_text(encoding="utf-8")),
            "agent_task_id": task_id,
            "agent_body_path": spec["agent_body_path"],
            "agent_body_snapshot_path": spec["agent_body_snapshot_path"],
            "agent_body_sha256": spec["agent_body_sha256"],
            "agent_kit_commit": kit_commit,
            "workspace_root": workspace_root,
            "prompt_path": spec["prompt_path"],
            "prompt_sha256": spec["prompt_sha256"],
            "critique_path": spec["critique_path"],
            "critique_sha256": _file_sha(critique_abs),
            "reviewed_base": base,
            "reviewed_head": head,
            "reviewed_remote_url": remote_url,
            "started_at": _utc_text(started),
            "completed_at": _utc_text(completed),
            "verdict": verdict,
            # Assessment receipts are blind: no checks, no register, no plan.
            "check_evidence_refs": [],
            "check_attempt_refs": [],
            "findings_register_sha256": None,
            "assessment_manifest_sha256": None,
            "operations_plan_sha256": None,
            "release_manifest_sha256": None,
            "delivery_requirements_sha256": None,
            "findings_snapshot_path": None,
            "operations_plan_snapshot_path": None,
            "release_manifest_snapshot_path": None,
        })

    finding_counts = _findings_check(findings_mod, critique_paths)

    critique_shas = [r["critique_sha256"] for r in reviews]
    if len(set(critique_shas)) != len(critique_shas):
        raise Refused(
            "two reviewers produced byte-identical critiques — blind independent review "
            "did not happen (a copied critique, or one agent serving two roles)"
        )

    live_remote = _git(repo_root, "remote", "get-url", "origin").strip()
    if live_remote != remote_url:
        raise Refused(
            f"origin changed during the wave: {remote_url!r} -> {live_remote!r}"
        )

    diff = _git_bytes(repo_root, "diff", "--binary", "--full-index", f"{base}..{head}")
    manifest = {
        "schema_version": 2,
        "milestone_id": milestone_id,
        "generation": generation,
        "created_at": _utc_text(now),
        "producer": {
            "kind": "deterministic-tool",
            "name": "milestone-pipeline-review-manifest.py",
            "provider": "local",
            "version": _file_sha(Path(__file__).resolve()),
        },
        "reviewed": {
            "repo": repo_root.name,
            "base_commit": base,
            "head_commit": head,
            "diff_sha256": _sha256_bytes(diff),
            "remote_url": remote_url,
        },
        "required_reviewers": required,
        "reviews": sorted(reviews, key=lambda r: r["role"]),
        "closure_reviews": [],
        "operations_reviews": [],
    }

    summary = {
        "id": milestone_id,
        "manifest_path": str(state_dir / "artifacts" / "review-manifest.json"),
        "required_reviewers": required,
        "reviewed_base": base,
        "reviewed_head": head,
        "findings_per_critique": finding_counts,
        "verdicts": {r["role"]: r["verdict"] for r in reviews},
        # Exactly what the main session must --set before `critique-complete`;
        # emitted so the orchestrator never has to retype (and mistype) them.
        "checkpoint_values": {
            "critics_run": required,
            "critique_files": sorted(r["critique_path"] for r in reviews),
        },
    }
    return manifest, summary


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------ validate-before-write


def validate_manifest_document(manifest: dict[str, Any]) -> None:
    """Schema-validate BEFORE writing. Fail loudly; never write the unvalidated."""
    if not SCHEMA_CHECK_SCRIPT.is_file():
        raise Refused(
            f"{SCHEMA_CHECK_SCRIPT.name} is missing — refusing to write an unvalidated "
            "manifest. The schema is the authority; a manifest written without it is a "
            "claim nobody checked."
        )
    with tempfile.TemporaryDirectory() as td:
        candidate = Path(td) / "review-manifest.json"
        candidate.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCHEMA_CHECK_SCRIPT), "--instance",
             MANIFEST_SCHEMA, str(candidate)],
            capture_output=True, text=True,
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if "jsonschema" in detail and "are required" in detail:
            raise Refused(
                "jsonschema/referencing are not installed, so the manifest cannot be "
                "validated. Refusing to write it unvalidated — install them "
                "(`python3 -m pip install jsonschema referencing`) and re-run."
            )
        raise Refused(f"manifest failed schema validation; not written:\n{detail}")


def write_manifest(manifest: dict[str, Any], path: Path) -> None:
    validate_manifest_document(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ self-test


def _read_json_arg(raw: str, label: str) -> dict[str, Any]:
    """Accept a path or inline JSON — the Workflow result arrives as a value."""
    candidate = Path(raw)
    try:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    except OSError:
        pass
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refused(f"{label}: neither a readable file nor inline JSON: {exc}") from exc


CRITIQUE_TEMPLATE = """# {title} — milestone {mid}

**Diff range:** {base}..{head}
**Critic:** {critic}
**Generated:** 2026-07-16T12:00:00Z
**Critique format version:** 1.0

## Executive summary

- **Overall verdict:** {verdict}
- **Severity counts:** {prefix}C0 {prefix}H0 {prefix}M1 {prefix}L0
- **Headline finding:** demo

**{prefix}M1 — Doc drift on convention** (MEDIUM)

File: `src/a.py:1`

**What:** Stale doc.

**Why it matters:** Convention is load-bearing.

**Proposed fix:** Update it.

**Regression-guard:** (none feasible)

**Source critic:** {critic}
"""

EMPTY_CRITIQUE = """# Adversary critique — milestone {mid}

**Diff range:** {base}..{head}
**Critic:** adversary
**Generated:** 2026-07-16T12:00:00Z
**Critique format version:** 1.0

## Executive summary

- **Overall verdict:** SHIP
- **Severity counts:** C0 H0 M0 L0
- **Headline finding:** none
"""


def self_test() -> int:
    failures = 0

    def expect(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {name}: {'ok' if ok else f'FAIL {detail}'}")
        if not ok:
            failures += 1

    print("self-test: milestone-pipeline-review-manifest.py")
    artifacts = _load_module(ARTIFACTS_SCRIPT, "milestone_pipeline_artifacts")
    findings_mod = _load_module(FINDINGS_SCRIPT, "milestone_pipeline_findings")
    prepare_mod = _load_module(PREPARE_SCRIPT, "milestone_pipeline_review_prepare")
    now = datetime(2026, 7, 16, 13, 0, 0, tzinfo=timezone.utc)
    mid = "demo-m1"
    bodies = {
        "milestone-adversary": "---\nname: milestone-adversary\nmodel: fable\n---\n# body\n",
        "milestone-delivery-integrity-adversary":
            "---\nname: milestone-delivery-integrity-adversary\nmodel: fable\n---\n# body2\n",
    }

    def scenario(td: str):
        root = Path(td)
        kit, kit_commit = prepare_mod._fixture_kit(root, bodies)
        repo, base, head = prepare_mod._fixture_repo(root, kit_commit, mid)
        ws = root / "workspace"
        ws.mkdir()
        prep = prepare_mod.prepare_wave(
            milestone_id=mid, repo_root=repo, stage="assessment", roles_override=None,
            artifacts=artifacts, kit_root=kit, workspace_root=ws,
            now=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc),
        )
        wf = {"id": mid, "repo_root": str(repo), "stage": "assessment", "reviews": []}
        for r in prep["reviewers"]:
            critic = prepare_mod.critic_marker(r["role"])
            prefix = "" if r["role"] == "milestone-adversary" else "V-"
            Path(r["critique_abs"]).write_text(CRITIQUE_TEMPLATE.format(
                title=critic, mid=mid, base=base, head=head, critic=critic,
                verdict="SHIP-WITH-FIXES", prefix=prefix,
            ), encoding="utf-8")
            wf["reviews"].append({
                "role": r["role"], "task_id": r["task_id"],
                "verdict": "SHIP-WITH-FIXES", "critique_path": r["critique_abs"],
                "started_at": "2026-07-16T12:10:00Z", "completed_at": "2026-07-16T12:40:00Z",
            })
        return root, kit, repo, base, head, prep, wf

    def build(prep, wf, repo, kit):
        return build_manifest(
            milestone_id=mid, repo_root=repo, prepare_results=[prep],
            workflow_result=wf, provider="claude", generation=1,
            artifacts=artifacts, findings_mod=findings_mod, prepare_mod=prepare_mod,
            kit_root=kit, now=now,
        )

    # 1. Happy path builds and schema-validates.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        manifest, summary = build(prep, wf, repo, kit)
        expect("build: required_reviewers is the deterministic pair",
               manifest["required_reviewers"] == sorted(artifacts.ALWAYS_REVIEWERS),
               str(manifest["required_reviewers"]))
        expect("build: two assessment receipts", len(manifest["reviews"]) == 2)
        expect("build: closure/operations start empty",
               manifest["closure_reviews"] == [] and manifest["operations_reviews"] == [])
        expect("build: reviewed binds base/head/diff",
               manifest["reviewed"]["base_commit"] == base
               and manifest["reviewed"]["head_commit"] == head
               and len(manifest["reviewed"]["diff_sha256"]) == 64)
        expect("build: model read from the stamped frontmatter snapshot",
               all(r["model"] == "fable" for r in manifest["reviews"]),
               str([r["model"] for r in manifest["reviews"]]))
        expect("build: assessment receipts are blind (null inputs)",
               all(r["findings_register_sha256"] is None
                   and r["assessment_manifest_sha256"] is None
                   and r["check_evidence_refs"] == [] for r in manifest["reviews"]))
        expect("build: checkpoint values emitted for the orchestrator",
               summary["checkpoint_values"]["critics_run"] == sorted(artifacts.ALWAYS_REVIEWERS)
               and len(summary["checkpoint_values"]["critique_files"]) == 2)
        try:
            validate_manifest_document(manifest)
            expect("schema: manifest validates against the v2 schema", True)
        except Refused as exc:
            expect("schema: manifest validates against the v2 schema", False, str(exc))

        # 2. Never writes what it cannot validate.
        broken = json.loads(json.dumps(manifest))
        broken["reviews"][0]["verdict"] = "MAYBE"
        target = Path(td) / "out" / "review-manifest.json"
        try:
            write_manifest(broken, target)
            expect("schema: invalid manifest refused before write", False, "no refusal")
        except Refused:
            expect("schema: invalid manifest refused before write", not target.exists())

    # 3. ZERO-FINDINGS refusal.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        adv = next(r for r in prep["reviewers"] if r["role"] == "milestone-adversary")
        Path(adv["critique_abs"]).write_text(
            EMPTY_CRITIQUE.format(mid=mid, base=base, head=head), encoding="utf-8"
        )
        for entry in wf["reviews"]:
            if entry["task_id"] == adv["task_id"]:
                entry["verdict"] = "SHIP"
        try:
            build(prep, wf, repo, kit)
            expect("zero-findings: refused", False, "no refusal")
        except Refused as exc:
            expect("zero-findings: refused",
                   "ZERO FINDINGS" in str(exc) and "EMPTY SET" in str(exc), str(exc)[:120])

    # 3b. The empty critique passes the FORMAT lint — proving the gate is not redundant.
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "empty.md"
        p.write_text(EMPTY_CRITIQUE.format(mid=mid, base="a" * 40, head="b" * 40),
                     encoding="utf-8")
        lint = subprocess.run(
            [sys.executable, str(FINDINGS_SCRIPT), "extract", "--check", str(p)],
            capture_output=True, text=True,
        )
        expect("zero-findings: extract --check alone would have PASSED it",
               lint.returncode == 0, f"rc={lint.returncode}")

    # 4. HEAD-drift refusal.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        (repo / "src" / "b.py").write_text("y = 1\n", encoding="utf-8")
        prepare_mod._run(["git", "add", "-A"], repo)
        prepare_mod._run(["git", "commit", "-q", "-m", "reviewer moved HEAD"], repo)
        try:
            build(prep, wf, repo, kit)
            expect("wave: HEAD move refused", False, "no refusal")
        except Refused as exc:
            expect("wave: HEAD move refused",
                   "WAVE INVALIDATED" in str(exc) and "HEAD moved" in str(exc), str(exc)[:120])

    # 5. Porcelain-drift refusal (tracked mutation, HEAD unchanged).
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        (repo / "src" / "a.py").write_text("x = 999  # reviewer wrote here\n", encoding="utf-8")
        try:
            build(prep, wf, repo, kit)
            expect("wave: tracked worktree mutation refused", False, "no refusal")
        except Refused as exc:
            expect("wave: tracked worktree mutation refused",
                   "WAVE INVALIDATED" in str(exc) and "worktree changed" in str(exc),
                   str(exc)[:120])

    # 6. A dropped reviewer is never a clean pass.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        wf["reviews"] = wf["reviews"][:1]
        try:
            build(prep, wf, repo, kit)
            expect("wave: dropped reviewer refused", False, "no refusal")
        except Refused as exc:
            expect("wave: dropped reviewer refused", "prepared" in str(exc), str(exc)[:120])

    # 7. Verdict must not contradict the report it cites.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        wf["reviews"][0]["verdict"] = "SHIP"
        try:
            build(prep, wf, repo, kit)
            expect("receipt: verdict contradicting the report refused", False, "no refusal")
        except Refused as exc:
            expect("receipt: verdict contradicting the report refused",
                   "declares" in str(exc), str(exc)[:120])

    # 8. A tampered prompt file cannot be bound. TWO independent gates catch this,
    #    and each is proven separately — otherwise the coarser one (the census)
    #    would mask a regression in the finer one (the per-reviewer sha256 bind).
    #
    # 8a. Census fires first: the file changed after prepare measured it.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        state_dir = Path(prep["state_path"]).parent
        pp = state_dir / prep["reviewers"][0]["prompt_path"]
        pp.write_text(pp.read_text(encoding="utf-8") + "\nPS: also do X\n", encoding="utf-8")
        try:
            build(prep, wf, repo, kit)
            expect("X-H4: post-dispatch prompt edit refused (census gate)", False, "no refusal")
        except Refused as exc:
            expect("X-H4: post-dispatch prompt edit refused (census gate)",
                   "WAVE INVALIDATED" in str(exc), str(exc)[:120])

    # 8b. The sha256 bind is INDEPENDENT of the census: re-point the census at the
    #     tampered bytes (an editor that covered its tracks) and the per-reviewer
    #     prompt_sha256 gate must still refuse — those bytes were never sent.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        state_dir = Path(prep["state_path"]).parent
        spec = prep["reviewers"][0]
        pp = state_dir / spec["prompt_path"]
        pp.write_text(pp.read_text(encoding="utf-8") + "\nPS: also do X\n", encoding="utf-8")
        prep["reviews_census"][Path(spec["prompt_path"]).name] = _file_sha(pp)
        try:
            build(prep, wf, repo, kit)
            expect("X-H4: post-dispatch prompt edit refused (sha256 bind)", False, "no refusal")
        except Refused as exc:
            expect("X-H4: post-dispatch prompt edit refused (sha256 bind)",
                   "never sent" in str(exc), str(exc)[:120])

    # 9. A copied critique cannot impersonate another role. NOTE what this does
    #    and does not prove: the refusal comes from the **Critic:** marker check,
    #    NOT from the duplicate-critique_sha256 backstop below it — each role
    #    demands a different marker, so two receipts can never legitimately reach
    #    that backstop with identical bytes. The backstop mirrors
    #    artifacts.py:1481 and is defence in depth, not the active gate here.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        a, b = prep["reviewers"][0], prep["reviewers"][1]
        Path(b["critique_abs"]).write_text(
            Path(a["critique_abs"]).read_text(encoding="utf-8"), encoding="utf-8"
        )
        try:
            build(prep, wf, repo, kit)
            expect("blindness: copied critique refused (role misattribution)",
                   False, "no refusal")
        except Refused as exc:
            expect("blindness: copied critique refused (role misattribution)",
                   "**Critic:** must identify" in str(exc)
                   and prepare_mod.critic_marker(b["role"]) in str(exc),
                   str(exc)[:140])

    # 9b. L3 — --id is retyped by a human; a prepare result for a DIFFERENT
    #     milestone must be refused naming BOTH ids, not silently bound.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        try:
            build_manifest(
                milestone_id="some-other-milestone", repo_root=repo, prepare_results=[prep],
                workflow_result=wf, provider="claude", generation=1, artifacts=artifacts,
                findings_mod=findings_mod, prepare_mod=prepare_mod, kit_root=kit, now=now,
            )
            expect("L3: --id disagreeing with the prepare result refused", False, "no refusal")
        except Refused as exc:
            expect("L3: --id disagreeing with the prepare result refused",
                   "demo-m1" in str(exc) and "some-other-milestone" in str(exc), str(exc)[:140])

    # 9c. L3 — the matching id still builds (the guard is not just always-refuse).
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        try:
            build(prep, wf, repo, kit)
            expect("L3: the matching --id still builds", True)
        except Refused as exc:
            expect("L3: the matching --id still builds", False, str(exc)[:140])

    # 9d. L4 — a reviewer overwriting a SIBLING's persisted prompt is invisible to
    #     _worktree_status (it ignores the state dir). The census must catch it.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        state_dir = Path(prep["state_path"]).parent
        victim = state_dir / prep["reviewers"][1]["agent_body_snapshot_path"]
        victim.write_text("# OVERWRITTEN BY A SIBLING\n", encoding="utf-8")
        try:
            build(prep, wf, repo, kit)
            expect("L4: sibling artifact overwrite refused", False, "no refusal")
        except Refused as exc:
            expect("L4: sibling artifact overwrite refused",
                   "WAVE INVALIDATED" in str(exc) and "overwrote" in str(exc), str(exc)[:140])

    # 9e. L4 — proves the gate is NOT redundant: the tampering above leaves
    #     _worktree_status completely clean, so the wave invariant passes it.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        state_dir = Path(prep["state_path"]).parent
        (state_dir / prep["reviewers"][1]["agent_body_snapshot_path"]).write_text(
            "# OVERWRITTEN BY A SIBLING\n", encoding="utf-8"
        )
        try:
            verify_wave_invariant(
                repo_root=repo, state_path=Path(prep["state_path"]),
                pre_wave=prep["pre_wave"], artifacts=artifacts,
            )
            expect("L4: the wave invariant alone would have MISSED it", True)
        except Refused as exc:
            expect("L4: the wave invariant alone would have MISSED it", False, str(exc)[:120])

    # 9f. L4 — an unbound stray file under reviews/ is refused.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        reviews_dir = Path(prep["state_path"]).parent / "artifacts" / "reviews"
        (reviews_dir / "scratch-notes.md").write_text("stray\n", encoding="utf-8")
        try:
            build(prep, wf, repo, kit)
            expect("L4: unbound stray file under reviews/ refused", False, "no refusal")
        except Refused as exc:
            expect("L4: unbound stray file under reviews/ refused",
                   "unbound file" in str(exc) and "scratch-notes.md" in str(exc), str(exc)[:140])

    # 9g. A tampered prepare result whose tripwire values no longer describe the
    #     persisted prompt is refused — the .mjs would have compared the dispatch
    #     against an expectation unrelated to the file this manifest binds.
    with tempfile.TemporaryDirectory() as td:
        _root, kit, repo, base, head, prep, wf = scenario(td)
        prep["reviewers"][0]["prompt_checksum"] = "deadbeef"
        try:
            build(prep, wf, repo, kit)
            expect("leg 3: prepare result whose checksum misdescribes the prompt refused",
                   False, "no refusal")
        except Refused as exc:
            expect("leg 3: prepare result whose checksum misdescribes the prompt refused",
                   "wrong expectation" in str(exc), str(exc)[:140])

    # 10. A hand-authored / truncated prepare result is an explicit refusal,
    #     never a KeyError traceback (which would exit 1, not the documented 3).
    for bad, why in (
        ({}, "empty object"),
        ({"id": "demo-m1", "state_path": "/x", "reviewed_base": "a", "reviewed_head": "b",
          "agent_kit_commit": "c", "remote_url": "r", "workspace_root": "/w",
          "required_reviewers": [], "reviewers": [{"role": "x"}],
          "pre_wave": {"head": "h", "porcelain_sha256": "s"},
          "reviews_census": {}}, "reviewer missing fields"),
        ({"id": "demo-m1", "state_path": "/x", "reviewed_base": "a", "reviewed_head": "b",
          "agent_kit_commit": "c", "remote_url": "r", "workspace_root": "/w",
          "required_reviewers": [], "reviewers": [{"role": "x"}],
          "pre_wave": {"head": "h", "porcelain_sha256": "s"}},
         "reviews_census absent"),
    ):
        rc = main([
            "--id", "demo-m1", "--repo-root", ".",
            "--prepare-result", json.dumps(bad), "--workflow-result", "{}",
        ])
        expect(f"input: {why} refused with exit 3", rc == 3, f"rc={rc}")

    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


# ---------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    parser = argparse.ArgumentParser(
        description="Verify a dispatched review wave and bind review-manifest.json."
    )
    parser.add_argument("--id", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--prepare-result", action="append", required=True,
                        help="prepare JSON (path or inline); repeat once per wave")
    parser.add_argument("--workflow-result", required=True,
                        help="milestone-pipeline-workflow.mjs result (path or inline JSON)")
    parser.add_argument("--provider", default="claude")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    try:
        artifacts = _load_module(ARTIFACTS_SCRIPT, "milestone_pipeline_artifacts")
        findings_mod = _load_module(FINDINGS_SCRIPT, "milestone_pipeline_findings")
        prepare_mod = _load_module(PREPARE_SCRIPT, "milestone_pipeline_review_prepare")
        repo_root = Path(args.repo_root).resolve()
        prepare_results = [
            _read_json_arg(raw, f"--prepare-result[{i}]")
            for i, raw in enumerate(args.prepare_result)
        ]
        workflow_result = _read_json_arg(args.workflow_result, "--workflow-result")
        manifest, summary = build_manifest(
            milestone_id=args.id,
            repo_root=repo_root,
            prepare_results=prepare_results,
            workflow_result=workflow_result,
            provider=args.provider,
            generation=args.generation,
            artifacts=artifacts,
            findings_mod=findings_mod,
            prepare_mod=prepare_mod,
            kit_root=artifacts.AGENT_KIT_ROOT,
            now=datetime.now(timezone.utc),
        )
        write_manifest(manifest, Path(summary["manifest_path"]))
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

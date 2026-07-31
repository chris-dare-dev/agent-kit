#!/usr/bin/env python3
"""Persist the review wave's dispatch artifacts AND emit their exact bytes — the
X-H4 work for the /milestone-pipeline Workflow port.

WHY THIS SCRIPT EXISTS (read before changing anything here)
----------------------------------------------------------
`data/references/milestone-pipeline-artifacts-v2.md` states the one link no
deterministic check can hold:

    "The orchestrator MUST dispatch the persisted prompt's exact bytes. The
     validator hashes the prompt FILE; it cannot see what was actually sent, so
     a run that persists a compliant prompt and then dispatches something else
     PASSES validation while producing void provenance, and that void is
     unrepairable."

A human orchestrator persists a file and then SEPARATELY composes a dispatch —
two sources, hence the void (observed 2026-07-15, dna-rem-m6: prompts were built
correctly and dispatched as reference notes; the validator passed them and the
whole assessment lane's provenance was void and unrecoverable).

WHAT IS CLOSED, AND BY WHAT — BE PRECISE HERE
---------------------------------------------
There are three legs between "the bytes this script composed" and "the bytes a
reviewer received". Two are closed by construction; one is not. Do not describe
this pipeline as though all three were.

  1. composed str -> persisted file        CLOSED BY CONSTRUCTION. `build_prompt`
     returns one Python `str`; it is written to `prompt_path` and placed in
     `reviewers[].prompt` with no second composition step. They cannot disagree.
  2. composed str -> emitted stdout JSON   CLOSED BY CONSTRUCTION. Same `str`.
  3. emitted JSON -> `Workflow({ args })`  **NOT** closed, and NOT byte-attested
     by the harness. There is no file-based args mechanism: the orchestrating
     MODEL re-emits the whole 20-43KB blob inside a tool call. A context
     compaction that truncates the body tail would slip past the .mjs's envelope
     tripwires (they inspect only the head, before the
     `--- CANONICAL AGENT BODY ---` delimiter), and the manifest hashes the
     pristine FILE — so nothing downstream can see the divergence.

Leg 3 is COVERED, not closed: this script emits `prompt_len_utf16` and
`prompt_checksum` alongside each prompt, and `milestone-pipeline-workflow.mjs`
recomputes both over `args.reviewers[i].prompt` immediately before `agent(...)`
and THROWS on any mismatch. That converts a silent, unrepairable void into a
loud pre-dispatch refusal. It does NOT prove the reviewer received the bytes, and
it does not bind anything a harness might do to the string after the tripwire
runs. A harness that mutates the string between the tripwire and the model call
remains outside this closure.

Do NOT "helpfully" reformat, re-read, or rebuild the prompt anywhere downstream.
The value of this script is that `prompt` and `prompt_path`'s content have a
single origin.

WHAT IT DOES
------------
For each deterministically selected reviewer role:

  1. picks a unique task leaf matching `[A-Za-z0-9][A-Za-z0-9._-]*`;
  2. snapshots the canonical agent body **at `state.agent_kit_commit`** (NOT the
     working tree — `milestone-pipeline-artifacts.py:1049` compares the snapshot
     against `git show <kit_commit>:data/agents/<role>.md`, so a working-tree
     read silently fails validation whenever the kit has moved or is dirty)
     -> `artifacts/reviews/<role>-<task>-agent.md`;
  3. builds the `MILESTONE_REVIEW_DISPATCH_V2` prompt (exact header map +
     `--- CANONICAL AGENT BODY ---` + exact body bytes, NO trailing instructions)
     -> `artifacts/reviews/<role>-<task>-prompt.md`;
  4. sha256s both, and measures the prompt's UTF-16 length + checksum for the
     .mjs's leg-3 tripwire.

It also captures the pre-wave `HEAD` + worktree status so
`milestone-pipeline-review-manifest.py` can verify the wave invariant afterwards
(a reviewer-created tracked mutation or ref move invalidates the wave).

REVIEWER SELECTION IS NOT A CHOICE
---------------------------------
`_required_reviewers()` is imported from `milestone-pipeline-artifacts.py`
rather than reimplemented. The validator recomputes the selection from the live
diff and repo identity, so a manifest cannot omit a selected reviewer; a local
copy of that rule would drift and produce runs that die at `critique-complete`.
`--roles` therefore only narrows a *wave* (subset), never the requirement.

Usage:

    python3 milestone-pipeline-review-prepare.py --id ID --repo-root PATH \\
        --stage assessment [--roles a,b] [--pretty]
    python3 milestone-pipeline-review-prepare.py --self-test
    python3 milestone-pipeline-review-prepare.py --self-test-agreement   # needs node

TWO self-tests, deliberately: `--self-test` is node-free so it can run in CI's
data-lint job (image `python:3.12-slim`), while `--self-test-agreement` executes
the .mjs's own checksum block in node and therefore runs in `typecheck-and-test`
(image `node:20-bookworm`) — the same split, for the same reason, as
frontend-uplift-mjs-lint.sh. `--self-test` asserts that CI still invokes the
agreement gate, so the split cannot decay into a silent skip.

stdout is ONE JSON object (see `main`). Exit 0 ok | 2 usage | 3 REFUSED.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ARTIFACTS_SCRIPT = SCRIPT_DIR / "milestone-pipeline-artifacts.py"
WORKFLOW_MJS = SCRIPT_DIR / "milestone-pipeline-workflow.mjs"

TASK_LEAF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MILESTONE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The one envelope. Header order mirrors the artifact contract's literal block;
# the validator compares a parsed dict (order-insensitive) but humans diffing a
# persisted prompt against the reference should see the same shape.
DISPATCH_HEADER = "MILESTONE_REVIEW_DISPATCH_V2"
BODY_MARKER = "\n--- CANONICAL AGENT BODY ---\n"


class Refused(Exception):
    """Deterministic refusal — never a warning, never a partial wave."""


# --------------------------------------------------------------- shared rules


def _load_artifacts_module() -> Any:
    """Import the frozen writer so selection/worktree rules have ONE source.

    Module import is side-effect free (constants + defs; `main()` is guarded),
    and `_fail` raises ValidationError rather than exiting.
    """
    spec = importlib.util.spec_from_file_location(
        "milestone_pipeline_artifacts", ARTIFACTS_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise Refused(f"cannot import {ARTIFACTS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise Refused(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}"
        )
    return proc.stdout


def _git_bytes(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if proc.returncode != 0:
        raise Refused(
            f"git {' '.join(args)} failed in {repo}: "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def critic_marker(role: str) -> str:
    """The **Critic:** token the validator demands in the critique.

    Mirrors milestone-pipeline-artifacts.py:1357 exactly.
    """
    if role == "milestone-adversary":
        return "adversary"
    return role.removeprefix("milestone-").removesuffix("-adversary")


# ------------------------------------------------- the leg-3 tripwire values
#
# These two functions are the Python HALF of an agreement with the JS block
# delimited by `--- BEGIN/END PORTABLE CHECKSUM ---` in
# milestone-pipeline-workflow.mjs. `self_test` extracts that exact JS text and
# runs it in node against these functions, so the two cannot silently drift.
#
# The unit of agreement is the UTF-16 CODE UNIT, because that is what JS's
# `String.prototype.length` and `.charCodeAt(i)` natively expose and the .mjs has
# nothing else available to it (no TextEncoder, no Buffer, no crypto — the
# Workflow sandbox cannot be assumed to have any Node API).


def _utf16_code_units(text: str) -> list[int]:
    """The exact sequence JS's `charCodeAt(0..length-1)` would return.

    Encoding to `utf-16-le` and pairing the bytes is what makes astral characters
    agree: JS sees a surrogate PAIR (two code units) where Python's `ord()` sees
    ONE codepoint. Hashing `ord()` over the string therefore diverges from JS on
    any emoji — in both the length and the checksum — which is precisely the bug
    this mirror exists to avoid. `utf-16-le` is explicit and BOM-free, so the byte
    stream is exactly the code units and nothing else.
    """
    data = text.encode("utf-16-le")
    return [data[i] | (data[i + 1] << 8) for i in range(0, len(data), 2)]


def prompt_len_utf16(text: str) -> int:
    """The value JS's `prompt.length` will report."""
    return len(text.encode("utf-16-le")) // 2


def prompt_checksum(text: str) -> str:
    """FNV-1a, 32-bit, over UTF-16 code units -> 8 lowercase hex chars.

    FNV-1a is chosen for what it must survive rather than for strength: it is ~10
    lines of arithmetic that the Workflow sandbox can definitely run. This is a
    TRUNCATION/MANGLING tripwire on a model-composed hop, NOT a security
    primitive — a party that can rewrite the prompt can also rewrite the checksum
    beside it. It is not, and must not be described as, tamper-proofing.
    """
    h = 0x811C9DC5
    for unit in _utf16_code_units(text):
        h ^= unit
        h = (h * 0x01000193) & 0xFFFFFFFF
    return format(h, "08x")


# ------------------------------------------------------------- the ONE prompt


def build_prompt(
    *,
    role: str,
    stage: str,
    milestone_id: str,
    repo_root: Path,
    workspace_root: Path,
    kit_commit: str,
    remote_url: str,
    base: str,
    head: str,
    critique_abs: str,
    body_text: str,
) -> str:
    """Build the exact MILESTONE_REVIEW_DISPATCH_V2 prompt.

    Pure function: same inputs -> same bytes. The persisted file and the emitted
    `reviewers[].prompt` are BOTH this return value; that identity closes X-H4
    legs 1+2 and is the reason this is a function rather than two templates.
    (Leg 3 — the model-composed hop into `Workflow({args})` — is not closed here
    and cannot be; see the module docstring.)

    NO trailing instructions after the body — the artifact contract forbids them
    and `artifacts.py:1353` asserts `prompt_body == body_bytes` exactly.
    """
    if stage != "assessment":
        raise Refused(f"stage {stage!r}: only 'assessment' is ported")
    header_lines = [
        DISPATCH_HEADER,
        f"ROLE: {role}",
        f"STAGE: {stage}",
        f"ID: {milestone_id}",
        f"REPO_ROOT: {repo_root}",
        f"WORKSPACE_ROOT: {workspace_root}",
        f"COMMIT_RANGE: {base}..{head}",
        f"CRITIQUE_PATH: {critique_abs}",
        f"AGENT_KIT_COMMIT: {kit_commit}",
        f"SOURCE_REMOTE_URL: {remote_url}",
    ]
    return "\n".join(header_lines) + BODY_MARKER + body_text


def _census_reviews_dir(reviews_dir: Path) -> dict[str, str]:
    """`{path relative to reviews_dir: sha256}` for every file currently there.

    Recursive on purpose: a reviewer dropping `notes/scratch.md` under the reviews
    dir is exactly as unbound as one dropping `scratch.md`, and neither is visible
    to `_worktree_status`. Sorted for a stable, diffable JSON payload.
    """
    census: dict[str, str] = {}
    for path in sorted(reviews_dir.rglob("*")):
        if path.is_file():
            census[str(path.relative_to(reviews_dir))] = _sha256_bytes(path.read_bytes())
    return census


def _unique_task_leaf(reviews_dir: Path, role: str, now: datetime) -> str:
    """A globally unique, path-safe leaf chosen BEFORE dispatch.

    The artifact contract requires the orchestrator to choose the leaf up front
    and never sanitize or synthesize a different one afterwards.
    """
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for _ in range(64):
        leaf = f"a{stamp}-{secrets.token_hex(3)}"
        if not TASK_LEAF_RE.fullmatch(leaf):
            continue
        collision = any(
            (reviews_dir / f"{role}-{leaf}-{suffix}").exists()
            for suffix in ("agent.md", "prompt.md", "critique.md")
        )
        if not collision:
            return leaf
    raise Refused("could not allocate a unique task leaf")


# ------------------------------------------------------------------ the state


def _load_state(repo_root: Path, milestone_id: str) -> tuple[Path, dict[str, Any]]:
    if not MILESTONE_ID_RE.fullmatch(milestone_id):
        raise Refused(f"--id {milestone_id!r}: unsafe milestone identifier")
    state_path = (
        repo_root / ".claude" / "notes" / "milestones" / milestone_id / "state.json"
    )
    if not state_path.is_file():
        raise Refused(f"state is missing: {state_path}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Refused(f"state is not JSON: {exc}") from exc
    if state.get("id") != milestone_id:
        raise Refused(
            f"state.id {state.get('id')!r} does not match --id {milestone_id!r}"
        )
    return state_path, state


def _reviewed_range(state: dict[str, Any]) -> tuple[str, str]:
    """base/head come from STATE, not from a live rev-parse.

    `artifacts.py:1439` requires the assessment head to equal
    `state.implementation_commits[-1]`. A live `git rev-parse HEAD` agrees only
    while nothing has been committed since `implement-complete`; deriving from
    state removes that silent divergence.
    """
    base = state.get("implementation_base")
    if not isinstance(base, str) or not base:
        raise Refused("state.implementation_base is missing (run implement-complete first)")
    commits = state.get("implementation_commits")
    if not isinstance(commits, list) or not commits:
        raise Refused("state.implementation_commits is missing or empty")
    head = commits[-1]
    if not isinstance(head, str) or not head:
        raise Refused("state.implementation_commits[-1] is not a commit")
    return base.lower(), head.lower()


# ------------------------------------------------------------------ the wave


def prepare_wave(
    *,
    milestone_id: str,
    repo_root: Path,
    stage: str,
    roles_override: list[str] | None,
    artifacts: Any,
    kit_root: Path,
    workspace_root: Path,
    now: datetime,
) -> dict[str, Any]:
    state_path, state = _load_state(repo_root, milestone_id)
    state_dir = state_path.parent
    reviews_dir = state_dir / "artifacts" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)

    base, head = _reviewed_range(state)
    kit_commit = state.get("agent_kit_commit")
    if not isinstance(kit_commit, str) or not kit_commit:
        raise Refused("state.agent_kit_commit is missing")

    remote_url = _git(repo_root, "remote", "get-url", "origin").strip()
    if not remote_url:
        raise Refused("origin remote has no URL")

    # ONE source for the selection rule — see the module docstring.
    try:
        required = sorted(artifacts._required_reviewers(repo_root, base, head))
    except Exception as exc:  # ValidationError et al.
        raise Refused(f"reviewer selection failed: {exc}") from exc

    if roles_override is None:
        wave_roles = list(required)
    else:
        wave_roles = list(dict.fromkeys(roles_override))
        unknown = [r for r in wave_roles if r not in required]
        if unknown:
            raise Refused(
                f"--roles names reviewers that are NOT deterministically selected: "
                f"{unknown}; the validator recomputes the selection from the live "
                f"diff and would refuse the manifest. Selected set: {required}"
            )
    if not wave_roles:
        raise Refused("no reviewer roles to dispatch")

    reviewers: list[dict[str, Any]] = []
    for role in wave_roles:
        body_rel = f"data/agents/{role}.md"
        # The bytes the validator will compare against — from the FROZEN kit
        # commit, never the working tree.
        body_bytes = _git_bytes(kit_root, "show", f"{kit_commit}:{body_rel}")
        try:
            body_text = body_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Refused(f"{body_rel} at {kit_commit} is not UTF-8: {exc}") from exc

        task_id = _unique_task_leaf(reviews_dir, role, now)
        agent_snapshot_rel = f"artifacts/reviews/{role}-{task_id}-agent.md"
        prompt_rel = f"artifacts/reviews/{role}-{task_id}-prompt.md"
        critique_rel_repo = os.path.relpath(
            reviews_dir / f"{role}-{task_id}-critique.md", repo_root
        )
        critique_abs = str((reviews_dir / f"{role}-{task_id}-critique.md").resolve())

        (state_dir / agent_snapshot_rel).write_bytes(body_bytes)

        prompt = build_prompt(
            role=role,
            stage=stage,
            milestone_id=milestone_id,
            repo_root=repo_root.resolve(),
            workspace_root=workspace_root,
            kit_commit=kit_commit,
            remote_url=remote_url,
            base=base,
            head=head,
            critique_abs=critique_abs,
            body_text=body_text,
        )
        prompt_bytes = prompt.encode("utf-8")
        (state_dir / prompt_rel).write_bytes(prompt_bytes)

        reviewers.append({
            "role": role,
            "task_id": task_id,
            "agent_type": role,
            # X-H4 legs 1+2: this is the same `str` just written to prompt_path,
            # so persisted == emitted by construction. The .mjs dispatches it
            # verbatim. Leg 3 (this JSON -> Workflow args) is model-composed and
            # is covered by the two tripwire values below, NOT by construction.
            "prompt": prompt,
            "prompt_path": prompt_rel,
            "prompt_sha256": _sha256_bytes(prompt_bytes),
            # Leg-3 tripwire. sha256 is unusable here: the .mjs cannot compute it
            # (no crypto in the Workflow sandbox). These two are what it CAN
            # recompute over the args value immediately before dispatch.
            "prompt_len_utf16": prompt_len_utf16(prompt),
            "prompt_checksum": prompt_checksum(prompt),
            "agent_body_path": body_rel,
            "agent_body_snapshot_path": agent_snapshot_rel,
            "agent_body_sha256": _sha256_bytes(body_bytes),
            "critique_path": critique_rel_repo,
            "critique_abs": critique_abs,
            "critic_marker": critic_marker(role),
        })

    # Snapshot AFTER the artifact writes: this is the tree the reviewers start
    # from. `_worktree_status` (imported, not reimplemented) reports tracked
    # changes plus untracked files OUTSIDE the milestone state dir — so a
    # reviewer writing its own critique is not drift, but a reviewer touching
    # source is.
    pre_head = _git(repo_root, "rev-parse", "HEAD").strip()
    porcelain = artifacts._worktree_status(repo_root, state_path)

    return {
        "id": milestone_id,
        "stage": stage,
        "repo_root": str(repo_root.resolve()),
        "workspace_root": str(workspace_root),
        "state_path": str(state_path),
        "agent_kit_commit": kit_commit,
        "remote_url": remote_url,
        "reviewed_base": base,
        "reviewed_head": head,
        "required_reviewers": required,
        "wave_roles": wave_roles,
        "reviewers": reviewers,
        "pre_wave": {
            "head": pre_head,
            "porcelain_sha256": _sha256_bytes(porcelain.encode("utf-8")),
        },
        # `_worktree_status` deliberately ignores untracked files under the
        # milestone state dir (a reviewer writing its own critique is not drift),
        # which leaves `artifacts/reviews/` itself completely unwatched. This
        # census re-covers part of that blind spot: every file present at prepare
        # time, hashed, so the manifest builder can refuse a wave in which a
        # reviewer mutated or deleted a sibling's prompt/body snapshot, tampered
        # with an EARLIER wave's critique, or dropped a stray file nobody bound.
        # It does NOT close same-wave critique cross-authoring — see
        # `verify_reviews_census` in milestone-pipeline-review-manifest.py.
        "reviews_census": _census_reviews_dir(reviews_dir),
        "prepared_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ------------------------------------------------------------------ self-test


def _run(cmd: list[str], cwd: Path) -> None:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr}")


CHECKSUM_BLOCK_RE = re.compile(
    r"^// --- BEGIN PORTABLE CHECKSUM.*?$\n(?P<js>.*?)^// --- END PORTABLE CHECKSUM ---$",
    re.DOTALL | re.MULTILINE,
)

# ASCII, CRLF, a BMP non-ASCII char, an ASTRAL char (surrogate pair), a trailing
# newline, code-unit boundary values, and a body large enough to exercise the
# 32-bit multiply's wraparound. Every entry exists because it could break the
# Python<->JS agreement in a way the others would not catch.
CHECKSUM_CORPUS = [
    "",
    "a",
    "hello world\n",
    "ASCII only, no trailing newline",
    "crlf\r\nlines\r\nkept\r\n",
    "BMP non-ascii: café — naïve ünïcode ✓ £€¥",
    "astral surrogate pairs: \U0001f600 \U0001f680 \U0001d11e",
    "mixed\r\nwith \U0001f600 astral and café BMP\nand a trailing newline\n",
    "\x00\x7f\x80߿ࠀ￿",
    "codepoint edges: \U00010000 \U0010ffff",
    "MILESTONE_REVIEW_DISPATCH_V2\nROLE: milestone-adversary\n"
    "\n--- CANONICAL AGENT BODY ---\n# body\n",
    "x" * 5000 + "\U0001f600" + "y" * 5000 + "\n",
]


def _extract_portable_checksum_js() -> str:
    """Pull the .mjs's OWN checksum text out of the .mjs.

    The self-test must not re-implement the JS: a hand-copied twin would let the
    real .mjs drift while the test kept passing — which is the same class of
    two-sources defect this whole port exists to remove.
    """
    if not WORKFLOW_MJS.is_file():
        raise Refused(f"{WORKFLOW_MJS} is missing; cannot verify the checksum agreement")
    match = CHECKSUM_BLOCK_RE.search(WORKFLOW_MJS.read_text(encoding="utf-8"))
    if match is None:
        raise Refused(
            f"{WORKFLOW_MJS.name} has no '--- BEGIN/END PORTABLE CHECKSUM ---' block. "
            "The Python<->JS agreement is unverifiable; do not ship."
        )
    return match.group("js")


def _js_checksums(corpus: list[str]) -> list[dict[str, Any]]:
    """Run the .mjs's checksum block in node over `corpus`.

    FAIL LOUD when node is absent — same posture as frontend-uplift-mjs-lint.sh.
    A skipped agreement check is indistinguishable from a passing one, and this
    check is the only thing pinning the tripwire's two halves together.
    """
    if not shutil.which("node"):
        raise Refused(
            "node is unavailable, so the Python<->JS checksum agreement cannot be proven. "
            "Refusing to report a PASS that was never executed."
        )
    driver = _extract_portable_checksum_js() + (
        "\nconst __input = JSON.parse(process.argv[1])\n"
        "console.log(JSON.stringify(__input.map(function (s) {\n"
        "  return { len: s.length, cs: _promptChecksum(s) }\n"
        "})))\n"
    )
    proc = subprocess.run(
        ["node", "-e", driver, json.dumps(corpus)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise Refused(f"node could not run the extracted checksum block: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _fixture_kit(root: Path, role_bodies: dict[str, str]) -> tuple[Path, str]:
    """A throwaway agent-kit repo so the self-test never touches the real one."""
    kit = root / "kit"
    (kit / "data" / "agents").mkdir(parents=True)
    for role, body in role_bodies.items():
        (kit / "data" / "agents" / f"{role}.md").write_text(body, encoding="utf-8")
    _run(["git", "init", "-q", "-b", "main"], kit)
    _run(["git", "config", "user.email", "t@example.invalid"], kit)
    _run(["git", "config", "user.name", "T"], kit)
    _run(["git", "config", "commit.gpgsign", "false"], kit)
    _run(["git", "add", "-A"], kit)
    _run(["git", "commit", "-q", "-m", "kit"], kit)
    commit = subprocess.run(
        ["git", "-C", str(kit), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    return kit, commit


def _fixture_repo(root: Path, kit_commit: str, milestone_id: str) -> tuple[Path, str, str]:
    repo = root / "target"
    (repo / "src").mkdir(parents=True)
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@example.invalid"], repo)
    _run(["git", "config", "user.name", "T"], repo)
    _run(["git", "config", "commit.gpgsign", "false"], repo)
    _run(["git", "remote", "add", "origin", "ssh://git@example.invalid/t/target.git"], repo)
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".claude/\n", encoding="utf-8")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "base"], repo)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    (repo / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "impl"], repo)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    state_dir = repo / ".claude" / "notes" / "milestones" / milestone_id
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(json.dumps({
        "schema_version": 2, "id": milestone_id, "phase": "critique-running",
        "agent_kit_commit": kit_commit,
        "implementation_base": base, "implementation_commits": [head],
    }), encoding="utf-8")
    return repo, base, head


def self_test() -> int:
    import tempfile

    failures = 0

    def expect(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {name}: {'ok' if ok else f'FAIL {detail}'}")
        if not ok:
            failures += 1

    print("self-test: milestone-pipeline-review-prepare.py")
    artifacts = _load_artifacts_module()
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    mid = "demo-m1"
    bodies = {
        "milestone-adversary": "# adversary body\n\nLine two.\n",
        "milestone-delivery-integrity-adversary": "# integrity body\n\nOther.\n",
    }

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        kit, kit_commit = _fixture_kit(root, bodies)
        repo, base, head = _fixture_repo(root, kit_commit, mid)
        ws = root / "workspace"
        ws.mkdir()

        result = prepare_wave(
            milestone_id=mid, repo_root=repo, stage="assessment",
            roles_override=None, artifacts=artifacts, kit_root=kit,
            workspace_root=ws, now=now,
        )

        # 1. deterministic selection = the two always-on adversaries (a src/*.py
        #    diff triggers neither conditional critic).
        expect(
            "selection: always-on pair for a plain source diff",
            result["required_reviewers"] == sorted(artifacts.ALWAYS_REVIEWERS),
            str(result["required_reviewers"]),
        )
        expect("selection: range comes from state",
               result["reviewed_base"] == base and result["reviewed_head"] == head,
               f"{result['reviewed_base']}..{result['reviewed_head']}")

        r0 = next(r for r in result["reviewers"] if r["role"] == "milestone-adversary")

        # 2. ENVELOPE SHAPE EXACTNESS — parsed the way artifacts.py parses it.
        prompt = r0["prompt"]
        expect("envelope: exactly one body delimiter",
               prompt.count(BODY_MARKER) == 1, str(prompt.count(BODY_MARKER)))
        header_text, prompt_body = prompt.split(BODY_MARKER, 1)
        header_lines = header_text.splitlines()
        expect("envelope: first line is the v2 dispatch header",
               header_lines[0] == DISPATCH_HEADER, header_lines[0])
        header: dict[str, str] = {}
        malformed = [ln for ln in header_lines[1:] if ": " not in ln]
        expect("envelope: no malformed header line", not malformed, str(malformed))
        for line in header_lines[1:]:
            k, v = line.split(": ", 1)
            header[k] = v
        expected_header = {
            "ROLE": "milestone-adversary",
            "STAGE": "assessment",
            "ID": mid,
            "REPO_ROOT": str(repo.resolve()),
            "WORKSPACE_ROOT": str(ws),
            "COMMIT_RANGE": f"{base}..{head}",
            "CRITIQUE_PATH": r0["critique_abs"],
            "AGENT_KIT_COMMIT": kit_commit,
            "SOURCE_REMOTE_URL": "ssh://git@example.invalid/t/target.git",
        }
        expect("envelope: header map is exactly the stage contract",
               header == expected_header,
               f"got {sorted(header)} want {sorted(expected_header)}")

        # 3. NO trailing instructions — body is the complete remainder.
        expect("envelope: body snapshot is the complete prompt body",
               prompt_body == bodies["milestone-adversary"],
               repr(prompt_body[-40:]))

        # 4. PROMPT-BYTES ROUND TRIP — persisted == emitted. X-H4 legs 1+2.
        state_dir = repo / ".claude" / "notes" / "milestones" / mid
        persisted = (state_dir / r0["prompt_path"]).read_bytes()
        expect("X-H4 legs 1+2: persisted prompt bytes == emitted prompt bytes",
               persisted == r0["prompt"].encode("utf-8"),
               f"{len(persisted)} vs {len(r0['prompt'].encode('utf-8'))}")
        expect("X-H4 legs 1+2: prompt_sha256 binds those same bytes",
               _sha256_bytes(persisted) == r0["prompt_sha256"])

        # 4b. LEG-3 TRIPWIRE VALUES describe the persisted file, not some other
        #     string — the .mjs compares against these, so they must be bound to
        #     the same bytes prompt_sha256 binds.
        persisted_text = persisted.decode("utf-8")
        expect("leg 3: prompt_len_utf16 measures the persisted prompt",
               r0["prompt_len_utf16"] == prompt_len_utf16(persisted_text),
               str(r0["prompt_len_utf16"]))
        expect("leg 3: prompt_checksum measures the persisted prompt",
               r0["prompt_checksum"] == prompt_checksum(persisted_text),
               str(r0["prompt_checksum"]))
        expect("leg 3: every reviewer carries both tripwire values",
               all(isinstance(r.get("prompt_len_utf16"), int)
                   and isinstance(r.get("prompt_checksum"), str)
                   and len(r["prompt_checksum"]) == 8
                   for r in result["reviewers"]))

        # 4c. The census sees this wave's own artifacts (so the manifest builder
        #     can refuse a reviewer that later mutates one).
        expect("census: reviews_census hashes this wave's prompt + body snapshots",
               result["reviews_census"].get(r0["prompt_path"].split("/")[-1])
               == r0["prompt_sha256"],
               str(sorted(result["reviews_census"])[:4]))

        # 5. Body snapshot comes from the kit COMMIT, not the working tree.
        (kit / "data" / "agents" / "milestone-adversary.md").write_text(
            "# TAMPERED\n", encoding="utf-8"
        )
        result2 = prepare_wave(
            milestone_id=mid, repo_root=repo, stage="assessment",
            roles_override=["milestone-adversary"], artifacts=artifacts,
            kit_root=kit, workspace_root=ws, now=now,
        )
        snap = (state_dir / result2["reviewers"][0]["agent_body_snapshot_path"]).read_text(encoding="utf-8")
        expect("kit-commit: snapshot ignores a dirty working-tree body",
               snap == bodies["milestone-adversary"], repr(snap))

        # 6. Task leaves are unique + path-safe across waves.
        leaves = [r["task_id"] for r in result["reviewers"]] + [
            r["task_id"] for r in result2["reviewers"]
        ]
        expect("task ids: unique across reviewers and waves",
               len(leaves) == len(set(leaves)), str(leaves))
        expect("task ids: match the safe leaf grammar",
               all(TASK_LEAF_RE.fullmatch(x) for x in leaves), str(leaves))

        # 7. --roles cannot invent a reviewer the validator did not select.
        try:
            prepare_wave(
                milestone_id=mid, repo_root=repo, stage="assessment",
                roles_override=["milestone-frontend-ux"], artifacts=artifacts,
                kit_root=kit, workspace_root=ws, now=now,
            )
            expect("--roles: unselected reviewer refused", False, "no refusal")
        except Refused as exc:
            expect("--roles: unselected reviewer refused",
                   "NOT deterministically selected" in str(exc), str(exc))

        # 8. pre-wave snapshot is present and stable for an unchanged tree.
        expect("pre-wave: HEAD captured",
               result["pre_wave"]["head"] == head, result["pre_wave"]["head"])
        again = artifacts._worktree_status(repo, state_dir / "state.json")
        expect("pre-wave: porcelain unchanged by our own artifact writes",
               _sha256_bytes(again.encode("utf-8")) == result["pre_wave"]["porcelain_sha256"])

    # 9. Conditional selection fires on a frontend path.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        kit, kit_commit = _fixture_kit(root, dict(
            bodies, **{"milestone-frontend-ux": "# fe\n"}
        ))
        repo, base, _ = _fixture_repo(root, kit_commit, mid)
        (repo / "app.tsx").write_text("export const A = 1\n", encoding="utf-8")
        _run(["git", "add", "-A"], repo)
        _run(["git", "commit", "-q", "-m", "fe"], repo)
        head2 = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        sp = repo / ".claude" / "notes" / "milestones" / mid / "state.json"
        doc = json.loads(sp.read_text(encoding="utf-8"))
        doc["implementation_commits"] = [head2]
        sp.write_text(json.dumps(doc), encoding="utf-8")
        ws = root / "workspace"
        ws.mkdir()
        res = prepare_wave(
            milestone_id=mid, repo_root=repo, stage="assessment",
            roles_override=None, artifacts=artifacts, kit_root=kit,
            workspace_root=ws, now=now,
        )
        expect("selection: frontend path adds milestone-frontend-ux",
               "milestone-frontend-ux" in res["required_reviewers"],
               str(res["required_reviewers"]))

    # 10. The cross-language agreement needs node, which the data-lint image
    #     (python:3.12-slim) does not ship — so it lives in `--self-test-agreement`
    #     and runs in the node job, exactly like frontend-uplift-mjs-lint.sh.
    #     That split is only safe while CI actually invokes it; assert that it
    #     does, rather than trusting it. A gate nobody runs is this pipeline's
    #     recurring defect, and "clean" must never mean "never ran".
    failures += _assert_agreement_gate_registered()

    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    print("  note: the Python<->JS checksum agreement is NOT covered by this "
          "invocation — it needs node. Run --self-test-agreement (CI does, in "
          "typecheck-and-test).")
    return 0 if failures == 0 else 1


CI_CONFIG = SCRIPT_DIR.parent.parent / ".gitlab-ci.yml"
AGREEMENT_FLAG = "--self-test-agreement"


def _assert_agreement_gate_registered() -> int:
    """Fail if CI stopped running the agreement gate.

    `--self-test` cannot run the agreement itself (no node in the data-lint
    image), so the agreement's only enforcement is a separate CI line. If that
    line is ever dropped, both self-tests would still report PASS while the
    tripwire's two halves silently drifted — the precise "the check never ran"
    failure this pipeline keeps re-learning. Cheap insurance: assert the line
    exists. Skipped only when .gitlab-ci.yml is genuinely absent (a partial
    checkout), never when it is present but missing the gate.
    """
    if not CI_CONFIG.is_file():
        return 0
    text = CI_CONFIG.read_text(encoding="utf-8")
    if AGREEMENT_FLAG in text:
        print("  ci: the --self-test-agreement gate is registered in .gitlab-ci.yml: ok")
        return 0
    print(
        f"  ci: the --self-test-agreement gate is registered in .gitlab-ci.yml: FAIL "
        f"{CI_CONFIG} never runs `{Path(__file__).name} {AGREEMENT_FLAG}`, so the "
        "Python<->JS checksum agreement is unenforced and the two halves of the "
        "dispatch tripwire can drift apart undetected. Re-add it to the node job."
    )
    return 1


def self_test_agreement() -> int:
    """The Python<->JS checksum agreement. Requires node; never skips."""
    failures = 0

    def expect(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {name}: {'ok' if ok else f'FAIL {detail}'}")
        if not ok:
            failures += 1

    print("self-test: milestone-pipeline-review-prepare.py (Python<->JS agreement)")

    # 1. THE AGREEMENT — the extracted .mjs checksum block must return exactly
    #    what this module computes, for every corpus entry. Executed in node,
    #    not eyeballed: a checksum the two sides disagree on is worse than none,
    #    because it would throw on every healthy wave and teach the next engineer
    #    to delete the tripwire.
    try:
        js_results = _js_checksums(CHECKSUM_CORPUS)
        expect("agreement: node returned one result per corpus entry",
               len(js_results) == len(CHECKSUM_CORPUS),
               f"{len(js_results)} vs {len(CHECKSUM_CORPUS)}")
        mismatches = []
        for sample, js in zip(CHECKSUM_CORPUS, js_results):
            py = {"len": prompt_len_utf16(sample), "cs": prompt_checksum(sample)}
            if py != js:
                mismatches.append(f"{sample[:24]!r}: py={py} js={js}")
        expect("agreement: python == extracted-.mjs JS over ASCII/CRLF/BMP/astral/trailing-NL",
               not mismatches, "; ".join(mismatches[:3]))
    except Refused as exc:
        expect("agreement: python == extracted-.mjs JS", False, str(exc))

    # 2. The corpus is only meaningful if it actually contains the hard cases; a
    #    future edit that drops the astral entry must fail HERE, loudly, rather
    #    than quietly reducing the agreement to an ASCII test.
    joined = "".join(CHECKSUM_CORPUS)
    expect("agreement: corpus covers astral (surrogate pair)",
           any(ord(ch) > 0xFFFF for ch in joined))
    expect("agreement: corpus covers BMP non-ASCII",
           any(0x7F < ord(ch) <= 0xFFFF for ch in joined))
    expect("agreement: corpus covers CRLF", any("\r\n" in s for s in CHECKSUM_CORPUS))
    expect("agreement: corpus covers a trailing newline",
           any(s.endswith("\n") for s in CHECKSUM_CORPUS))
    expect("agreement: corpus covers the empty string", "" in CHECKSUM_CORPUS)

    # 3. ord()-over-codepoints is the trap the mirror exists to avoid: prove it
    #    really would diverge, so nobody "simplifies" _utf16_code_units away.
    astral = "emoji \U0001f600 tail"
    naive = 0x811C9DC5
    for cp in astral:
        naive ^= ord(cp)
        naive = (naive * 0x01000193) & 0xFFFFFFFF
    expect("agreement: ord()-over-codepoints WOULD diverge on astral input",
           format(naive, "08x") != prompt_checksum(astral)
           and len(astral) != prompt_len_utf16(astral))

    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


# ---------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    # Checked before --self-test: `in` would otherwise match the prefix and run
    # the wrong suite.
    if AGREEMENT_FLAG in argv:
        return self_test_agreement()
    if "--self-test" in argv:
        return self_test()
    parser = argparse.ArgumentParser(
        description="Persist + emit the milestone review wave's dispatch bytes."
    )
    parser.add_argument("--id", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--stage", default="assessment", choices=["assessment"])
    parser.add_argument(
        "--roles",
        help="comma-list narrowing this WAVE to a subset of the deterministic "
             "selection; it can never add or omit a required reviewer",
    )
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--self-test-agreement", action="store_true",
        help="verify the Python<->JS checksum agreement (needs node; run in the "
             "node CI job, not data-lint)",
    )
    args = parser.parse_args(argv)

    try:
        artifacts = _load_artifacts_module()
        repo_root = Path(args.repo_root).resolve()
        if not (repo_root / ".git").exists():
            raise Refused(f"--repo-root is not a git repo: {repo_root}")
        try:
            workspace_root = artifacts._canonical_workspace_root()
        except Exception as exc:
            raise Refused(
                f"canonical workspace root is unresolvable ({exc}); a receipt "
                "carrying any other workspace cannot validate"
            ) from exc
        result = prepare_wave(
            milestone_id=args.id,
            repo_root=repo_root,
            stage=args.stage,
            roles_override=(
                [r.strip() for r in args.roles.split(",") if r.strip()]
                if args.roles else None
            ),
            artifacts=artifacts,
            kit_root=artifacts.AGENT_KIT_ROOT,
            workspace_root=workspace_root,
            now=datetime.now(timezone.utc),
        )
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    json.dump(result, sys.stdout, indent=2 if args.pretty else None, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

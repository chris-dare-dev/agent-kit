#!/usr/bin/env python3
"""model-policy-apply.py — stamp per-agent model+effort from data/model-policy.json.

The policy registry (data/model-policy.json) is the single source of truth for
which model and effort level every agent runs at. Agents carry a semantic
`model-class:` frontmatter key; this script materializes the class resolution
into the `model:` and `effort:` keys the Claude Code runtime actually reads.
Command files may embed marker spans that get the resolved text stamped in:

    <!-- {{model-policy:deep-reasoning-high}} -->fable (effort: high)<!-- /model-policy -->

To re-point the fleet (e.g. a model alias becomes unavailable): edit the
class resolution in data/model-policy.json, run this script, review the diff,
commit. NEVER hand-edit model:/effort:/model-class: in data/agents/*.md.

Modes:
    (default)   apply changes in place, print summary
    --diff      print would-be changes, write nothing
    --check     exit 2 if any file would change (drift gate), exit 3 on lint
                errors, write nothing
    --local     merge WORKSPACE_ROOT/.claude/model-policy.local.json class
                overrides on top of the canonical policy and stamp
                WORKSPACE_ROOT/.claude/agents/ instead of data/agents/.
                Use WORKSPACE_ROOT env var or --workspace to locate the
                workspace. Does not touch shared command/reference files.
                Combine with --diff or --check for dry-run / drift gate.

Exit codes: 0 clean | 2 drift (--check) | 3 lint violation (--check) |
            4 registry/file-set mismatch | 5 malformed input
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # data/scripts/ -> repo root
POLICY_PATH = REPO_ROOT / "data" / "model-policy.json"
AGENTS_DIR = REPO_ROOT / "data" / "agents"
LOCAL_POLICY_FILENAME = "model-policy.local.json"

# Frontmatter keys owned by this script, in stamp order.
OWNED_KEYS = ("model-class", "model", "effort")

MARKER_RE = re.compile(
    r"<!--\s*\{\{model-policy:([a-z0-9-]+)\}\}\s*-->(.*?)<!--\s*/model-policy\s*-->",
    re.DOTALL,
)


def fail(msg: str, code: int = 5) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_policy() -> dict:
    try:
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"policy registry not found: {POLICY_PATH}")
    except json.JSONDecodeError as e:
        fail(f"policy registry is not valid JSON: {e}")
    for cls, res in policy["classes"].items():
        if "model" not in res:
            fail(f"class {cls!r} has no model resolution")
    for agent, cls in policy["assignments"].items():
        if cls not in policy["classes"]:
            fail(f"agent {agent!r} assigned to unknown class {cls!r}")
    return policy


def load_local_override(workspace: Path) -> dict:
    """Load and validate WORKSPACE/.claude/model-policy.local.json."""
    path = workspace / ".claude" / LOCAL_POLICY_FILENAME
    if not path.exists():
        fail(
            f"--local: override file not found at {path}\n"
            f"  Create it with at minimum: {{\"classes\": {{}}}}",
            5,
        )
    try:
        override = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"--local: {path.name} is not valid JSON: {e}")
    if "classes" not in override:
        fail(f"--local: {path.name} must have a top-level 'classes' key")
    return override


def merge_local_override(policy: dict, override: dict) -> dict:
    """Return a shallow-merged copy: override.classes win over policy.classes."""
    import copy
    merged = copy.deepcopy(policy)
    for cls, res in override["classes"].items():
        if "model" not in res:
            fail(f"--local override: class {cls!r} has no 'model' key")
        merged["classes"][cls] = res
    return merged


def resolution_text(res: dict) -> str:
    """Human/orchestrator-readable resolved form stamped into marker spans."""
    if res.get("effort"):
        return f"{res['model']} (effort: {res['effort']})"
    return str(res["model"])


def stamp_agent(path: Path, cls: str, res: dict) -> tuple[str, str]:
    """Return (original_text, stamped_text) for one agent file."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        fail(f"{path.name}: no frontmatter block")
    try:
        close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        fail(f"{path.name}: unterminated frontmatter")

    def key_of(ln: str) -> str:
        """Top-level YAML key of a frontmatter line, or '' for continuation/indented lines."""
        if not ln or ln[0] in (" ", "\t", "-", "#") or ":" not in ln:
            return ""
        return ln.split(":", 1)[0].strip()

    fm = lines[1:close]
    # Drop owned keys (zero-indent only — never touch indented continuation lines).
    kept: list[str] = []
    for ln in fm:
        if key_of(ln) in OWNED_KEYS:
            continue
        kept.append(ln)

    desired = [f"model-class: {cls}\n", f"model: {res['model']}\n"]
    if res.get("effort"):
        desired.append(f"effort: {res['effort']}\n")

    # Insert after the tools: block if present (keeps name/description/tools at
    # top), else after the description: block, else at end of frontmatter.
    anchor = -1
    for i, ln in enumerate(kept):
        k = key_of(ln)
        if k == "tools":
            anchor = i
            break
        if k == "description" and anchor < 0:
            anchor = i
    if anchor >= 0:
        insert_at = anchor + 1
        # Skip the anchored key's indented continuation lines (block lists / folded scalars).
        while insert_at < len(kept) and key_of(kept[insert_at]) == "":
            insert_at += 1
    else:
        insert_at = len(kept)
    new_fm = kept[:insert_at] + desired + kept[insert_at:]

    stamped = "".join(["---\n", *new_fm, *lines[close:]])
    return text, stamped


def stamp_markers(text: str, policy: dict, path: Path, problems: list[str]) -> str:
    opens = text.count("<!-- {{model-policy:")
    closes = len(re.findall(r"<!--\s*/model-policy\s*-->", text))
    if opens != closes:
        fail(
            f"{path.name}: unbalanced model-policy markers ({opens} open / {closes} close) — "
            "an orphan marker makes the rewrite swallow intervening prose; fix the file first",
            5,
        )

    def sub(m: re.Match) -> str:
        cls = m.group(1)
        res = policy["classes"].get(cls)
        if res is None:
            problems.append(f"{path.name}: marker references unknown class {cls!r}")
            return m.group(0)
        return (
            f"<!-- {{{{model-policy:{cls}}}}} -->"
            f"{resolution_text(res)}"
            f"<!-- /model-policy -->"
        )

    return MARKER_RE.sub(sub, text)


def lint_commands(policy: dict, files: list[Path]) -> list[str]:
    lint = policy.get("lint", {})
    patterns = [re.compile(p, re.IGNORECASE) for p in lint.get("forbidden_patterns", [])]
    suppress = lint.get("suppress_marker", "model-policy:ok")
    violations: list[str] = []
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if suppress in line or "{{model-policy:" in line:
                continue
            for pat in patterns:
                if pat.search(line):
                    violations.append(f"{path.name}:{n}: hardcoded model literal ({pat.pattern}): {line.strip()[:100]}")
                    break
    return violations


def lint_workflows(policy: dict, files: list[Path]) -> list[str]:
    """Lint the Gen-2 `*-workflow.mjs` files for a raw model alias used as a VALUE
    (e.g. `model: 'opus'`) — the S3.8b regression gate. The patterns target QUOTED
    alias literals: a model alias only *functions* as a value when quoted in JS, so
    this catches the actual hardcode while leaving prose that names a policy class
    (in a comment or a `detail:` string) alone. The runtime has no filesystem
    access — a `--mode deep` tier bump must live in a per-mode agent file
    (mode-variant-agents.py), never a literal here."""
    lint = policy.get("lint", {})
    patterns = [re.compile(p, re.IGNORECASE) for p in lint.get("workflow_forbidden_patterns", [])]
    if not patterns:
        return []
    suppress = lint.get("suppress_marker", "model-policy:ok")
    violations: list[str] = []
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if suppress in line:
                continue
            for pat in patterns:
                if pat.search(line):
                    violations.append(
                        f"{path.name}:{n}: raw model alias literal in a workflow — resolve the "
                        f"tier via a per-mode agent file (mode-variant-agents.py), not a literal: "
                        f"{line.strip()[:100]}"
                    )
                    break
    return violations


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 2 on drift, 3 on lint; write nothing")
    mode.add_argument("--diff", action="store_true", help="print would-be changes; write nothing")
    ap.add_argument(
        "--local",
        action="store_true",
        help=(
            "merge WORKSPACE_ROOT/.claude/model-policy.local.json class overrides and "
            "stamp WORKSPACE_ROOT/.claude/agents/ instead of data/agents/. "
            "Does not touch shared command/reference files or run lint."
        ),
    )
    ap.add_argument(
        "--workspace",
        metavar="PATH",
        help="workspace root path (overrides WORKSPACE_ROOT env var, used with --local)",
    )
    args = ap.parse_args()

    policy = load_policy()
    if "classes" not in policy or "assignments" not in policy:
        fail("policy registry missing top-level 'classes' or 'assignments' key")

    # ── Local-override mode ────────────────────────────────────────────────────
    if args.local:
        workspace_str = args.workspace or os.environ.get("WORKSPACE_ROOT", "")
        if not workspace_str:
            fail(
                "--local requires WORKSPACE_ROOT env var or --workspace PATH.\n"
                "  Example: WORKSPACE_ROOT=/Users/you/Work/workspace python3 model-policy-apply.py --local"
            )
        workspace = Path(workspace_str).resolve()
        local_override = load_local_override(workspace)
        policy = merge_local_override(policy, local_override)

        agents_dir = workspace / ".claude" / "agents"
        if not agents_dir.is_dir():
            fail(f"--local: local agents directory not found: {agents_dir}")

        print(f"[local] workspace:    {workspace}")
        print(f"[local] agents dir:   {agents_dir}")
        print(f"[local] overrides:    {list(local_override['classes'].keys())}")

        assignments: dict[str, str] = policy["assignments"]
        on_disk = {p.stem: p for p in sorted(agents_dir.glob("*.md"))}

        # In local mode, orphaned assignments are tolerated (e.g. the local agents/
        # dir may be populated differently); only agents present on disk are stamped.
        unassigned = sorted(set(on_disk) - set(assignments))
        if unassigned:
            print(f"WARN: local agents with no policy assignment (skipped): {', '.join(unassigned)}", file=sys.stderr)

        problems: list[str] = []
        changed: list[str] = []
        per_class: dict[str, int] = {}

        for name, path in on_disk.items():
            cls = assignments.get(name)
            if cls is None:
                continue  # unassigned — already warned above
            res = policy["classes"][cls]
            per_class[cls] = per_class.get(cls, 0) + 1
            original, stamped = stamp_agent(path, cls, res)
            if original != stamped:
                changed.append(path.name)
                if args.diff:
                    print(f"--- {path.name}: would stamp model-class={cls} model={res['model']}"
                          f"{' effort=' + res['effort'] if res.get('effort') else ''}")
                if not (args.check or args.diff):
                    path.write_text(stamped, encoding="utf-8")

        print(f"policy (merged): canonical + local override")
        print(f"agents: {len(on_disk)} | per class: "
              + ", ".join(f"{c}={n}" for c, n in sorted(per_class.items())))
        if changed:
            verb = "drift in" if args.check else ("would change" if args.diff else "stamped")
            print(f"{verb} {len(changed)} file(s):")
            for c in changed:
                print(f"  .claude/agents/{c}")
        else:
            print("all local agent files already match merged policy (idempotent no-op)")

        if args.check and changed:
            sys.exit(2)
        return

    # ── Canonical mode (default) ───────────────────────────────────────────────
    assignments = policy["assignments"]
    on_disk = {p.stem: p for p in sorted(AGENTS_DIR.glob("*.md"))}
    unassigned = sorted(set(on_disk) - set(assignments))
    orphaned = sorted(set(assignments) - set(on_disk))
    if unassigned or orphaned:
        if unassigned:
            print(f"ERROR: agents on disk with no policy assignment: {', '.join(unassigned)}", file=sys.stderr)
        if orphaned:
            print(f"ERROR: policy assignments with no agent file: {', '.join(orphaned)}", file=sys.stderr)
        sys.exit(4)

    problems: list[str] = []
    changed: list[str] = []
    per_class: dict[str, int] = {}

    # Agent frontmatter stamping.
    for name, path in on_disk.items():
        cls = assignments[name]
        res = policy["classes"][cls]
        per_class[cls] = per_class.get(cls, 0) + 1
        original, stamped = stamp_agent(path, cls, res)
        if original != stamped:
            changed.append(f"agents/{path.name}")
            if args.diff:
                print(f"--- {path.name}: would stamp model-class={cls} model={res['model']}"
                      f"{' effort=' + res['effort'] if res.get('effort') else ''}")
            if not (args.check or args.diff):
                path.write_text(stamped, encoding="utf-8")

    # Marker spans in command + reference files (both are orchestrator-read surfaces).
    lint_cfg = policy.get("lint", {})
    cmd_files = sorted(REPO_ROOT.glob(lint_cfg.get("command_glob", "data/commands/*.md")))
    ref_files = sorted(REPO_ROOT.glob(lint_cfg.get("reference_glob", "data/references/*.md")))
    for path in cmd_files + ref_files:
        original = path.read_text(encoding="utf-8")
        stamped = stamp_markers(original, policy, path, problems)
        if original != stamped:
            changed.append(f"commands/{path.name}")
            if args.diff:
                print(f"--- {path.name}: would re-stamp model-policy marker span(s)")
            if not (args.check or args.diff):
                path.write_text(stamped, encoding="utf-8")

    exempt = set(lint_cfg.get("exempt_files", []))
    violations = lint_commands(policy, [p for p in cmd_files + ref_files if p.name not in exempt])
    # S3.8b — raw model-alias lint over the Gen-2 workflow files (stdlib regex,
    # runs in the same data-lint --check pass; no node needed).
    wf_glob = lint_cfg.get("workflow_glob")
    if wf_glob:
        violations += lint_workflows(policy, sorted(REPO_ROOT.glob(wf_glob)))
    for v in violations:
        print(f"LINT: {v}", file=sys.stderr)
    for p in problems:
        print(f"WARN: {p}", file=sys.stderr)

    print(f"policy: {POLICY_PATH.relative_to(REPO_ROOT)} (updated {policy.get('updated', '?')})")
    print(f"agents: {len(on_disk)} | per class: "
          + ", ".join(f"{c}={n}" for c, n in sorted(per_class.items())))
    if changed:
        verb = "drift in" if args.check else ("would change" if args.diff else "stamped")
        print(f"{verb} {len(changed)} file(s):")
        for c in changed:
            print(f"  {c}")
    else:
        print("all files already match policy (idempotent no-op)")

    if args.check:
        if violations or problems:
            sys.exit(3)
        if changed:
            sys.exit(2)
    elif violations or problems:
        # Apply mode writes its stamps but exits nonzero so `apply && commit` cannot
        # green-light hardcoded literals or dead (unknown-class) marker spans.
        print("NOTE: lint violations / marker problems above must be fixed — replace "
              "hardcoded model literals with {{model-policy:<class>}} marker spans.", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()

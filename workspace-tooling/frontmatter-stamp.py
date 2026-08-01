#!/usr/bin/env python3
"""
frontmatter-stamp.py — normalize + authorship-stamp the scattered agent .md.

For every .md in the agent regions it:
  1. FIXES malformed YAML frontmatter (the mixed-indent `tags:` list bug — a `- x` at
     column 0 mixed with `  - x` at column 2, which makes the whole block un-parseable).
  2. ENSURES an authorship tag (`authorship/agent-generated`) + an `authorship:` property.
  3. PREPENDS a minimal frontmatter block to files that have none.

Idempotent — re-running stamps nothing new (safe for the PostToolUse hook).
Conservative — anything with an unusual shape (no closing fence, inline `tags: [..]`) is
REPORTED and SKIPPED rather than risk corrupting it. stdlib-only (no PyYAML).

Usage:
  python3 scripts/frontmatter-stamp.py --dry-run     # report only, touch nothing
  python3 scripts/frontmatter-stamp.py               # apply
  python3 scripts/frontmatter-stamp.py --file <p.md> # one file (hook mode)
  --authorship human-generated   # override (default agent-generated, region-derived)
"""
import argparse
import glob
import os
import sys

from path_contract import default_manifest_path, load_project_manifest


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VAULT = load_project_manifest(default_manifest_path(SCRIPT_DIR))["vault_root"]
AGENT_REGIONS = [
    "plans",
    "docs",
    "<workspace>/<project>/plans",
    "<workspace>/<project>/docs",
]


def normalize_frontmatter(fm_lines, authorship):
    """Return (new_fm_lines, why[]) or (None, ['skip:reason']) for unusual shapes."""
    why = []
    result = []
    tag_items = []
    saw_tags = False
    has_auth_field = False
    malformed = False
    j, n = 0, len(fm_lines)
    while j < n:
        ln = fm_lines[j]
        stripped = ln.strip()
        if stripped.startswith("authorship:"):
            has_auth_field = True
            result.append(ln)
            j += 1
            continue
        if stripped == "tags:":  # block-list form only
            saw_tags = True
            k = j + 1
            indents = set()
            while k < n and fm_lines[k].lstrip().startswith("- "):
                raw = fm_lines[k]
                indents.add(2 if raw.startswith("  - ") else (0 if raw.startswith("- ") else -1))
                tag_items.append(raw.lstrip()[2:].strip())
                k += 1
            # TRUE malformation = mixed indentation in one list (breaks YAML parse).
            # A uniformly 0-indent OR uniformly 2-indent list is valid; we normalize to
            # 2-space regardless (harmless) but only LABEL the genuinely-broken ones.
            if len(indents) > 1:
                malformed = True
            result.append("__TAGS__")
            j = k
            continue
        if stripped.startswith("tags:") and stripped != "tags:":
            # inline form `tags: [a, b]` or `tags: a` — too risky to rewrite blind
            return None, ["skip:inline-tags"]
        result.append(ln)
        j += 1

    authtag = f"authorship/{authorship}"
    if authtag not in tag_items:
        tag_items.append(authtag)
        why.append("added-authorship-tag")
    if malformed:
        why.append("fixed-malformed-tags")
    seen, deduped = set(), []
    for t in tag_items:
        if t and t not in seen:
            seen.add(t)
            deduped.append(t)
    tags_block = ["tags:"] + [f"  - {t}" for t in deduped]

    final = []
    for ln in result:
        final += tags_block if ln == "__TAGS__" else [ln]
    if not saw_tags:
        final += tags_block
        why.append("added-tags-key")
    if not has_auth_field:
        final = [f"authorship: {authorship}"] + final
        why.append("added-authorship-field")
    return final, why


def process_file(path, authorship, dry):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = text.split("\n")

    if lines and lines[0].strip() == "---":
        try:
            end = lines.index("---", 1)
        except ValueError:
            return "skip:no-closing-fence"
        fm = lines[1:end]
        body = lines[end + 1:]
        new_fm, why = normalize_frontmatter(fm, authorship)
        if new_fm is None:
            return why[0]  # skip:reason
        if not why:
            return "unchanged"
        out = ["---"] + new_fm + ["---"] + body
    else:
        out = ["---", f"authorship: {authorship}", "tags:",
               f"  - authorship/{authorship}", "---", ""] + lines
        why = ["added-frontmatter"]

    if not dry:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
    return ", ".join(why)


def region_authorship(path, override):
    if override:
        return override
    return "agent-generated"  # all AGENT_REGIONS are agent


def iter_targets(one_file):
    if one_file:
        yield os.path.abspath(one_file)
        return
    for r in AGENT_REGIONS:
        for p in sorted(glob.glob(os.path.join(VAULT, r, "*.md"))):
            yield p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--authorship", help="override authorship value")
    ap.add_argument("--show-sample", type=int, default=0, help="print N before/after diffs (dry-run)")
    args = ap.parse_args()

    if args.file:
        if not args.file.lower().endswith(".md"):
            return 0
        # HOOK GUARD: only stamp files that live directly in an agent region.
        # Never touch human notes (Notes/), config, or anything outside the agent regions.
        parent = os.path.abspath(os.path.dirname(args.file))
        agent_dirs = {os.path.abspath(os.path.join(VAULT, r)) for r in AGENT_REGIONS}
        if parent not in agent_dirs:
            return 0

    from collections import Counter
    tally = Counter()
    samples = []
    for p in iter_targets(args.file):
        if not os.path.isfile(p):
            continue
        if args.show_sample and len(samples) < args.show_sample and args.dry_run:
            before = open(p, encoding="utf-8", errors="replace").read().split("\n")[:9]
            samples.append((p, before))
        res = process_file(p, region_authorship(p, args.authorship), args.dry_run)
        key = res if res in ("unchanged",) or res.startswith("skip:") else "changed:" + res
        tally[key] += 1

    if args.file:
        return 0  # hook mode: silent

    print(f"{'DRY-RUN — no files written' if args.dry_run else 'APPLIED'}")
    for k in sorted(tally):
        print(f"  {tally[k]:4d}  {k}")
    print(f"  ----  total {sum(tally.values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

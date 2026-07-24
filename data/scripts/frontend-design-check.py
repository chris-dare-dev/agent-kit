#!/usr/bin/env python3
"""frontend-design-check.py — deterministic ship gate for the /frontend-design skill (scorecard v2).

v1 (2026-07-09, critique rounds 1-2) enforced the ANTI-pattern rubric: per-tell evidence,
derived totals, an independent scorer, band gates. Round 3 (external critique) proved that
measuring only the ABSENCE of named clichés lets weak-but-uncliched surfaces ship (a sparse,
ornamental page scored 1/12 and "passed"). v2 keeps the anti-pattern half and adds the
DIRECTED QUALITY half — eight 0-4 dimensions that measure the PRESENCE of design:

  1 Task clarity        can the operator state posture + next action in five seconds?
  2 Priority fidelity   does visual weight match risk / cost / urgency?
  3 Decision integrity  labels, units, scope, freshness, thresholds, "so what" without hover?
  4 Composition         each region answers a distinct question; no dead-space theater/crowding
  5 Typography          type roles encode hierarchy; legible at real operating density
  6 Semantic depth      layers mean nesting/selection/ownership/state — not decoration
  7 Interaction & state craft   focus/selection/drill/loading/empty/error/stale coherent
  8 Product signature   logo removed: still recognizably WORKSPACE, coherent with adjacent routes

Ship rule (canon §14): anti-pattern total ≤ 2 AND quality mean ≥ 3.0 AND no dimension < 2 AND
dimensions 1/3/8 each ≥ 3 — on BOTH the self and independent scores. Conservative posture: a
false "not done" is possible; a false ship requires deliberately gamed inputs, and every gaming
lane the round-4 review found is now either refused (fabricated `✓ live` artifact paths are
existence-checked; ANY UNSCORABLE row refuses without an audited --allow-unscored waiver) or
flagged loudly (zero-variance "everything is a 3" columns). Sections with no verifiable
`✓ live` artifact are UNVERIFIED and refuse to gate without an audited waiver.

Subcommands
  template [--slug S]              print an empty v2 scorecard skeleton
  check <scorecard.md> [--repo-root P] [--allow-missing-independent "<reason>"]
                                   [--allow-no-live "<reason>"]
  diff-scan [--repo-root P] [--base REF] [--list-only]
  --self-test                      fixture-based regression suite (CI-wired)

Exit codes
  0 pass · 1 structural/parse failure (incl. UNSCORABLE abuse) · 2 usage
  3 gate failed (anti band ≥3, or a quality threshold missed)
  4 independence/evidence problem (missing independent without waiver, Δ too large,
    UNVERIFIED live-evidence without waiver) · 5 diff-scan violations · 6 persistence unmet

Trust boundary (round-2 L-6, unchanged): this validates the ARTIFACT, not provenance — it
cannot prove the Independent sections came from a fresh-context agent. That guarantee is
procedural (SKILL.md Step 4 + session transcript).
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

# ---- anti-pattern half (13 tells; 13 = BAN-15 same-silhouette, round 3) ----------------
RUBRIC_TELLS = [
    "navy/near-navy dark shell + 2+ neon accents (BAN-1)",
    "6+ equal rounded cards as primary layout (BAN-2)",
    "icon-in-rounded-square decoration tiles (BAN-3)",
    "default Inter + Lucide + shadcn look untouched (BAN-4)",
    "no focal element / equal panel weight (BAN-5)",
    "decorative, unannotated charts (BAN-6)",
    "badge soup — >5 colored chips per view (BAN-7)",
    "glow/gradient/glass without a layering reason (BAN-8)",
    "multiple primary CTAs per viewport (BAN-9)",
    "generic or cosplay copy (BAN-10)",
    "semantic colors used decoratively (BAN-11)",
    "uniform density, no authored modes (BAN-14)",
    "same-silhouette syndrome — another surface/run's shell reused as identity (BAN-15)",
]
TELL_BAN = {1: "BAN-1", 2: "BAN-2", 3: "BAN-3", 4: "BAN-4", 5: "BAN-5", 6: "BAN-6",
            7: "BAN-7", 8: "BAN-8", 9: "BAN-9", 10: "BAN-10", 11: "BAN-11", 12: "BAN-14",
            13: "BAN-15"}
N_TELLS = 13
ANTI_FAIL_AT = 3          # §14 band edge: 0-2 ship, ≥3 not done
MAX_ANTI_DELTA = 1
MAX_UNSCORABLE_ANTI = 4   # > this per section = scored nothing, refuse (round-2 H-3)

# ---- directed-quality half (8 dimensions, 0-4) ------------------------------------------
QUALITY_DIMS = [
    "Task clarity — operator states posture + next action in 5s",
    "Priority fidelity — visual weight matches risk/cost/urgency",
    "Decision integrity — labels/units/scope/freshness/thresholds without hover",
    "Composition — regions answer distinct questions; no dead-space theater",
    "Typography — roles encode hierarchy; legible at operating density",
    "Semantic depth — layers mean nesting/selection/state, not decoration",
    "Interaction & state craft — focus/selection/loading/empty/error/stale coherent",
    "Product signature — logo removed, still recognizably WORKSPACE + route-coherent",
]
# each quality row must name its dimension (keyword match — round-2 M-8 discipline)
DIM_KEY = {1: "clarity", 2: "priority", 3: "decision", 4: "composition",
           5: "typograph", 6: "semantic", 7: "state", 8: "signature"}
N_DIMS = 8
QUALITY_MEAN_MIN = 3.0
QUALITY_DIM_MIN = 2
QUALITY_CORE_DIMS = (1, 3, 8)   # task clarity, decision integrity, product signature
QUALITY_CORE_MIN = 3
MAX_QUALITY_DELTA = 0.75        # |self mean − independent mean|
MAX_UNSCORABLE_QUALITY = 2      # 8 dims; >2 unscored = insufficient basis

TIER_TOKENS = ("✓", "~")
LIVE_TOKEN = "✓ live"
# Round-4 F-A3: '✓ live' must cite a checkable artifact, not be a 7-character incantation.
ARTIFACT_RE = re.compile(r"[\w][\w./-]*\.(?:png|jpe?g|webp|gif|mp4|webm)\b")

# ---- diff-scan (unchanged from v1 round-2 state) -----------------------------------------
ALLOWED_MS = {0.0, 100.0, 150.0, 200.0, 300.0, 500.0}
HEX_RE = re.compile(r"#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3,4}\b")
MS_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)ms\b")
S_RE = re.compile(r"(?<![\w.])(\d*\.\d+|\d+)s\b")
COLOR_FN_RE = re.compile(r"\b(?:rgba?|hsla?|oklch|oklab|color-mix)\(")
WAIVER_MARK = "design-ok:"
SCAN_EXTS = (".tsx", ".jsx", ".ts", ".js", ".css", ".scss", ".vue", ".svelte")
SCAN_EXEMPT_SUBSTRINGS = (
    "tailwind.config", "globals.css", "token", "fonts", "theme.css", ".stories.",
    "node_modules/", ".md", ".svg",
)

ANTI_ROW_RE = re.compile(r"^\|\s*(\d{1,2})\s*\|([^|]*)\|\s*(0|1|UNSCORABLE)\s*\|(.*)\|\s*$")
QUAL_ROW_RE = re.compile(r"^\|\s*(\d)\s*\|([^|]*)\|\s*([0-4]|UNSCORABLE)\s*\|(.*)\|\s*$")
ANTI_TOTAL_RE = re.compile(r"^\*\*(Self|Independent) anti total:\*\*\s*(\d{1,2})/13\s*$")
QUAL_MEAN_RE = re.compile(r"^\*\*(Self|Independent) quality mean:\*\*\s*(\d(?:\.\d{1,2})?)/4\s*$")
FIELD_RE = re.compile(r"^\*\*(Surface|Scope|Thesis|Direction|Persisted|Scorecard format version):\*\*\s*(.*\S)\s*$")

SECTION_HEADERS = {
    "## Self score — anti-pattern tells": ("Self", "anti"),
    "## Self score — directed quality": ("Self", "quality"),
    "## Independent score — anti-pattern tells": ("Independent", "anti"),
    "## Independent score — directed quality": ("Independent", "quality"),
}


def _strip_fences(lines):
    """Blank fenced code blocks (keep line count) so quoted examples never parse."""
    out, in_fence, tok = [], False, None
    for line in lines:
        s = line.lstrip()
        if s.startswith("```") or s.startswith("~~~"):
            t = s[:3]
            if not in_fence:
                in_fence, tok = True, t
            elif t == tok:
                in_fence, tok = False, None
            out.append("")
            continue
        out.append("" if in_fence else line)
    return out


def _parse_rows(sec, kind, i, line, errors):
    rx = ANTI_ROW_RE if kind == "anti" else QUAL_ROW_RE
    m = rx.match(line)
    if not m:
        return
    n, label_cell, verdict, evidence = int(m.group(1)), m.group(2), m.group(3), m.group(4).strip()
    limit = N_TELLS if kind == "anti" else N_DIMS
    if not 1 <= n <= limit:
        errors.append(f"line {i}: {kind} row number {n} out of range 1..{limit}")
        return
    if n in sec["rows"]:
        errors.append(f"line {i}: duplicate {kind} row {n} in this section")
        return
    if kind == "anti":
        if not re.search(rf"{TELL_BAN[n]}\b", label_cell):
            errors.append(f"line {i}: tell {n} row does not name its token {TELL_BAN[n]} — use the template")
    else:
        if DIM_KEY[n] not in label_cell.lower():
            errors.append(f"line {i}: quality row {n} does not name its dimension ('{DIM_KEY[n]}') — use the template")
    if len(evidence) < 8:
        errors.append(f"line {i}: {kind} row {n} evidence too thin ('{evidence}') — cite an artifact")
    elif not any(tok in evidence for tok in TIER_TOKENS):
        errors.append(f"line {i}: {kind} row {n} evidence lacks a §14 tier marker (✓ live / ✓ code / ~ inferred)")
    sec["rows"][n] = (verdict, evidence)


def parse_scorecard(path):
    errors = []
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().splitlines()
    except OSError as exc:
        # Round-4 F-A4: the caller unpacks four values — a 3-tuple here crashed with a raw
        # traceback on any unreadable path instead of the crafted message.
        return {}, {}, [f"cannot read scorecard: {exc}"], False
    lines = _strip_fences(raw)

    fields = {}
    for i, line in enumerate(lines, 1):
        m = FIELD_RE.match(line)
        if m:
            key = m.group(1)
            if key in fields:
                errors.append(f"line {i}: duplicate header field '{key}'")
            fields[key] = m.group(2).strip()
    for req in ("Surface", "Scope", "Thesis", "Direction", "Persisted", "Scorecard format version"):
        if req not in fields:
            errors.append(f"missing required header field '**{req}:**'")
    ver = fields.get("Scorecard format version")
    if ver is not None and ver != "2.0":
        errors.append(
            f"unsupported scorecard format version '{ver}' — v2.0 adds the directed-quality "
            "half (round 3); regenerate via `frontend-design-check.py template`"
        )
    if fields.get("Scope") not in (None, "small", "program"):
        errors.append(f"Scope must be 'small' or 'program', got '{fields.get('Scope')}'")
    thesis = fields.get("Thesis", "")
    if thesis and (thesis.startswith("<") or len(thesis) < 15):
        errors.append("Thesis looks like an unfilled placeholder")

    # sections keyed (scorer, kind)
    sections = {}
    current = None
    for i, line in enumerate(lines, 1):
        header_hit = None
        for h, key in SECTION_HEADERS.items():
            if line.startswith(h):
                header_hit = key
                break
        if header_hit:
            if header_hit in sections:
                errors.append(f"line {i}: duplicate section '{line.strip()}'")
            current = sections.setdefault(header_hit, {"rows": {}, "declared": None})
            continue
        if line.startswith("## "):
            current = None
            continue
        m = ANTI_TOTAL_RE.match(line)
        if m:
            key = (m.group(1), "anti")
            if key not in sections:
                errors.append(f"line {i}: '{m.group(1)} anti total' before its section")
            elif sections[key]["declared"] is not None:
                errors.append(f"line {i}: duplicate '{m.group(1)} anti total' line")
            else:
                sections[key]["declared"] = int(m.group(2))
            continue
        m = QUAL_MEAN_RE.match(line)
        if m:
            key = (m.group(1), "quality")
            if key not in sections:
                errors.append(f"line {i}: '{m.group(1)} quality mean' before its section")
            elif sections[key]["declared"] is not None:
                errors.append(f"line {i}: duplicate '{m.group(1)} quality mean' line")
            else:
                sections[key]["declared"] = float(m.group(2))
            continue
        if current is not None:
            kind = [k for (s, k), sec in sections.items() if sec is current][0]
            _parse_rows(current, kind, i, line, errors)

    # per-section derivations + integrity
    for (scorer, kind), sec in sections.items():
        limit = N_TELLS if kind == "anti" else N_DIMS
        missing = [str(n) for n in range(1, limit + 1) if n not in sec["rows"]]
        if missing:
            errors.append(f"{scorer} {kind}: missing rows: {', '.join(missing)}")
        unscorable = sum(1 for v, _ in sec["rows"].values() if v == "UNSCORABLE")
        sec["unscorable"] = unscorable
        # live_rows is provisional here — cmd_check re-derives it after existence-checking
        # each cited artifact (round-4 F-A3); parse has no --repo-root to resolve against.
        sec["live_rows"] = sum(1 for _, ev in sec["rows"].values() if LIVE_TOKEN in ev)
        if kind == "anti":
            derived = sum(1 for v, _ in sec["rows"].values() if v == "1")
            sec["derived"] = derived
            if unscorable > MAX_UNSCORABLE_ANTI:
                errors.append(
                    f"{scorer} anti: {unscorable} UNSCORABLE (> {MAX_UNSCORABLE_ANTI}) — a scorecard "
                    "that scored almost nothing cannot gate anything (round-2 H-3)"
                )
            if sec["declared"] is None:
                errors.append(f"{scorer} anti: missing '**{scorer} anti total:** N/13' line")
            elif sec["declared"] != derived:
                errors.append(f"{scorer} anti: declared {sec['declared']} != derived {derived} (totals are DERIVED)")
        else:
            scored = [(n, int(v)) for n, (v, _) in sec["rows"].items() if v != "UNSCORABLE"]
            sec["scores"] = dict(scored)
            sec["mean"] = (sum(v for _, v in scored) / len(scored)) if scored else 0.0
            if unscorable > MAX_UNSCORABLE_QUALITY:
                errors.append(
                    f"{scorer} quality: {unscorable} UNSCORABLE (> {MAX_UNSCORABLE_QUALITY} of {N_DIMS}) — "
                    "insufficient basis to gate; capture the evidence and re-score"
                )
            if sec["declared"] is None:
                errors.append(f"{scorer} quality: missing '**{scorer} quality mean:** N.N/4' line")
            elif abs(sec["declared"] - sec["mean"]) > 0.051:
                errors.append(
                    f"{scorer} quality: declared mean {sec['declared']} != derived {sec['mean']:.2f} "
                    "(means are DERIVED from scored rows; UNSCORABLE excluded)"
                )

    if ("Self", "anti") not in sections or ("Self", "quality") not in sections:
        errors.append("missing Self sections (need '## Self score — anti-pattern tells' AND '## Self score — directed quality')")
    has_indep = ("Independent", "anti") in sections and ("Independent", "quality") in sections
    if (("Independent", "anti") in sections) != (("Independent", "quality") in sections):
        errors.append("Independent sections must include BOTH anti-pattern and directed-quality tables")
    return fields, sections, errors, has_indep


def _quality_threshold_failures(scorer, sec):
    fails = []
    if sec["mean"] < QUALITY_MEAN_MIN:
        fails.append(f"{scorer} quality mean {sec['mean']:.2f} < {QUALITY_MEAN_MIN}")
    for n, v in sec["scores"].items():
        if v < QUALITY_DIM_MIN:
            fails.append(f"{scorer} quality dim {n} ({DIM_KEY[n]}) = {v} < {QUALITY_DIM_MIN}")
    for n in QUALITY_CORE_DIMS:
        v = sec["scores"].get(n)
        if v is not None and v < QUALITY_CORE_MIN:
            fails.append(f"{scorer} core dim {n} ({DIM_KEY[n]}) = {v} < {QUALITY_CORE_MIN} (task clarity / decision integrity / product signature must each be ≥3)")
        if v is None:
            fails.append(f"{scorer} core dim {n} ({DIM_KEY[n]}) is UNSCORABLE — core dimensions must be scored")
    return fails


def cmd_check(args):
    fields, sections, errors, has_indep = parse_scorecard(args.scorecard)
    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1

    warnings = []
    repo_root = args.repo_root or os.getcwd()
    scorecard_dir = os.path.dirname(os.path.abspath(args.scorecard))

    # Round-4 F-A3: existence-check every '✓ live' citation; re-derive live_rows from
    # VERIFIED artifacts only. A fabricated path is a structural failure; a '✓ live' with no
    # checkable path does not count as live evidence.
    artifact_fails = []
    for (scorer, kind), sec in sections.items():
        verified_live = 0
        for n, (v, ev) in sec["rows"].items():
            if LIVE_TOKEN not in ev:
                continue
            m = ARTIFACT_RE.search(ev)
            if not m:
                warnings.append(
                    f"{scorer} {kind} row {n}: '✓ live' cites no checkable artifact path — "
                    "not counted as live evidence"
                )
                continue
            p = m.group(0)
            candidates = [p, os.path.join(repo_root, p), os.path.join(scorecard_dir, p)]
            if any(os.path.isfile(c) for c in candidates):
                verified_live += 1
            else:
                artifact_fails.append(
                    f"{scorer} {kind} row {n}: cited ✓ live artifact '{p}' not found "
                    "(checked cwd, --repo-root, scorecard dir) — a live claim must be checkable"
                )
        sec["live_rows"] = verified_live
    if artifact_fails:
        for a in artifact_fails:
            print(f"FAIL: {a}", file=sys.stderr)
        return 1

    if not has_indep:
        if not args.allow_missing_independent:
            print(
                "FAIL: no Independent sections. The implementer must not be the only scorer "
                '(critique R-4). Dispatch a fresh-context scorer, or use '
                '--allow-missing-independent "<reason>" (audited).',
                file=sys.stderr,
            )
            return 4
        if len(args.allow_missing_independent.strip()) < 10:
            print("FAIL: --allow-missing-independent reason too thin to audit", file=sys.stderr)
            return 2
        warnings.append(f"AUDITED WAIVER: independent score missing — {args.allow_missing_independent!r}. Self-only gating; weaker evidence.")

    # UNVERIFIED rule (round 3): a section with zero ✓ live artifacts cannot silently gate.
    unverified = [f"{s} {k}" for (s, k), sec in sections.items() if sec["live_rows"] == 0]
    if unverified:
        if not args.allow_no_live:
            print(
                f"FAIL: UNVERIFIED — no '✓ live' artifact in: {', '.join(sorted(unverified))}. "
                "Code-only/inferred evidence may produce UNVERIFIED, never PASS (round 3). Capture "
                'live screenshots/computed styles, or waive with --allow-no-live "<reason>" (audited).',
                file=sys.stderr,
            )
            return 4
        if len(args.allow_no_live.strip()) < 10:
            print("FAIL: --allow-no-live reason too thin to audit", file=sys.stderr)
            return 2
        warnings.append(f"AUDITED WAIVER: no live artifacts in {', '.join(sorted(unverified))} — {args.allow_no_live!r}. Result is UNVERIFIED-grade evidence.")

    # Round-4 F-A1/F-A2: UNSCORABLE launders exactly the tells/dims that would fail (fixture-
    # proven fail→pass flip). ANY UNSCORABLE row now refuses to gate without an audited waiver;
    # waived rows stay excluded from totals but the trail is loud. Structural ceilings
    # (anti > 4, quality > 2) remain hard parse failures regardless of waiver.
    unscored = [
        f"{s} {k} ({sec['unscorable']} row(s))"
        for (s, k), sec in sections.items() if sec["unscorable"] > 0
    ]
    if unscored:
        if not args.allow_unscored:
            print(
                f"FAIL: UNSCORABLE rows present in: {', '.join(sorted(unscored))}. UNSCORABLE "
                "hides exactly the rows that would fail the gate (round-4 F-A1) — capture the "
                'evidence and re-score, or waive with --allow-unscored "<reason>" (audited).',
                file=sys.stderr,
            )
            return 4
        if len(args.allow_unscored.strip()) < 10:
            print("FAIL: --allow-unscored reason too thin to audit", file=sys.stderr)
            return 2
        warnings.append(f"AUDITED WAIVER: UNSCORABLE rows in {', '.join(sorted(unscored))} — {args.allow_unscored!r}. Waived rows are excluded from totals; the gate is weaker for it.")

    # Round-4 F-F1: a uniform quality column (all dims identical) is the LLM central-tendency
    # rubber stamp — warn loudly; the per-dim evidence rows are the audit surface.
    for (scorer, kind), sec in sections.items():
        if kind == "quality" and len(sec["scores"]) == N_DIMS and len(set(sec["scores"].values())) == 1:
            warnings.append(
                f"{scorer} quality column is zero-variance (all dims = "
                f"{next(iter(sec['scores'].values()))}) — central-tendency rubber-stamp risk; "
                "verify the per-dim evidence genuinely differs."
            )

    scorers = ["Self"] + (["Independent"] if has_indep else [])
    if has_indep:
        d_anti = abs(sections[("Self", "anti")]["derived"] - sections[("Independent", "anti")]["derived"])
        if d_anti > MAX_ANTI_DELTA:
            print(f"FAIL: anti-pattern Δ {d_anti} > {MAX_ANTI_DELTA} — arbitrate per-tell from the evidence rows.", file=sys.stderr)
            return 4
        d_q = abs(sections[("Self", "quality")]["mean"] - sections[("Independent", "quality")]["mean"])
        if d_q > MAX_QUALITY_DELTA:
            print(f"FAIL: quality-mean Δ {d_q:.2f} > {MAX_QUALITY_DELTA} — arbitrate per-dimension from the evidence rows.", file=sys.stderr)
            return 4

    gate_fails = []
    for s in scorers:
        anti = sections[(s, "anti")]
        if anti["derived"] >= ANTI_FAIL_AT:
            gate_fails.append(f"{s} anti-pattern score {anti['derived']}/13 ≥ {ANTI_FAIL_AT} (§14: not done)")
        gate_fails.extend(_quality_threshold_failures(s, sections[(s, "quality")]))
    if gate_fails:
        for g in gate_fails:
            print(f"FAIL: {g}", file=sys.stderr)
        print("FAIL: gate NOT passed — return to the contract; do not ship 'improved but generic' (or clean-but-empty).", file=sys.stderr)
        return 3

    persisted = fields.get("Persisted", "")
    scope = fields.get("Scope", "small")
    if persisted in ("(pending)", ""):
        print("FAIL: Persisted is '(pending)' — decide + write the persistence artifact before gating.", file=sys.stderr)
        return 6
    if persisted != "commit-body":
        base = os.path.basename(persisted)
        if scope == "program" and not (base.startswith("design-direction-") and base.endswith(".md")):
            print(f"FAIL: Scope=program persistence must be docs/design-direction-<slug>.md, got '{persisted}' (round-2 M-4).", file=sys.stderr)
            return 6
        p = persisted if os.path.isabs(persisted) else os.path.join(repo_root, persisted)
        if not os.path.isfile(p):
            print(f"FAIL: Persisted names '{persisted}' but no file exists at {p}", file=sys.stderr)
            return 6
    elif scope == "program":
        print("FAIL: Scope=program requires a docs/design-direction-<slug>.md doc (commit-body is small-scope only).", file=sys.stderr)
        return 6

    for w in warnings:
        print(f"WARN: {w}")
    parts = []
    for s in scorers:
        parts.append(f"{s.lower()}: anti={sections[(s,'anti')]['derived']}/13 quality-mean={sections[(s,'quality')]['mean']:.2f}/4")
    print("PASS: " + " | ".join(parts) + f" scope={scope} persisted={persisted}")
    return 0


# ------------------------------------------------------------------ diff-scan (v1, unchanged)
def scan_added_lines(pairs):
    hits = []
    for path, lineno, line in pairs:
        if any(s in path.lower() for s in SCAN_EXEMPT_SUBSTRINGS):
            continue
        if not path.endswith(SCAN_EXTS):
            continue
        if WAIVER_MARK in line:
            continue
        for m in HEX_RE.finditer(line):
            hits.append((path, lineno, f"hard-coded color {m.group(0)}"))
        if COLOR_FN_RE.search(line) and "var(--" not in line:
            hits.append((path, lineno, "hard-coded color function (rgb/hsl/oklch/oklab/color-mix without var(--…))"))
        for m in MS_RE.finditer(line):
            if float(m.group(1)) not in ALLOWED_MS:
                hits.append((path, lineno, f"raw duration {m.group(0)} (motion-vocabulary §9 tokens only)"))
        for m in S_RE.finditer(line):
            if float(m.group(1)) * 1000.0 not in ALLOWED_MS:
                hits.append((path, lineno, f"raw duration {m.group(0)} (motion-vocabulary §9 tokens only)"))
    return hits


def _added_lines_from_git(repo_root, base):
    pairs = []
    diff = subprocess.run(
        ["git", "-C", repo_root, "diff", "--unified=0", base],
        capture_output=True, text=True, check=True,
    ).stdout
    path, new_ln = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_ln = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            if path:
                pairs.append((path, new_ln, line[1:]))
            new_ln += 1
        elif not line.startswith("-"):
            new_ln += 1
    untracked = subprocess.run(
        ["git", "-C", repo_root, "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    for rel in untracked:
        if not rel.endswith(SCAN_EXTS):
            continue
        try:
            with open(os.path.join(repo_root, rel), encoding="utf-8", errors="replace") as f:
                for i, l in enumerate(f, 1):
                    pairs.append((rel, i, l.rstrip("\n")))
        except OSError:
            continue
    return pairs


def cmd_diff_scan(args):
    repo_root = args.repo_root or os.getcwd()
    try:
        pairs = _added_lines_from_git(repo_root, args.base)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"FAIL: git diff unavailable ({exc}) — diff-scan cannot run (do not skip it silently)", file=sys.stderr)
        return 1
    hits = scan_added_lines(pairs)
    for path, ln, msg in hits:
        print(f"{path}:{ln}: {msg}")
    if hits and not args.list_only:
        print(f"FAIL: {len(hits)} token-discipline violation(s) in added lines "
              f"(waive a deliberate literal with '{WAIVER_MARK} <why>')", file=sys.stderr)
        return 5
    if not pairs:
        print(f"WARN: 0 added lines scanned vs base '{args.base}' — if the work is already committed "
              "this scan is vacuous; re-run with --base <ref-before-the-work>.")
    print(f"diff-scan: {len(hits)} hit(s) across {len(pairs)} added lines")
    return 0


# ------------------------------------------------------------------ template
def cmd_template(slug):
    anti = "\n".join(f"| {i} | {t} | ? | ? |" for i, t in enumerate(RUBRIC_TELLS, 1))
    qual = "\n".join(f"| {i} | {d} | ? | ? |" for i, d in enumerate(QUALITY_DIMS, 1))
    body = f"""# Design scorecard — {slug}

**Surface:** <route / view name>
**Scope:** small
**Thesis:** <the one-sentence product-specific thesis>
**Direction:** <chosen direction name>
**Persisted:** commit-body
**Scorecard format version:** 2.0

Anti-pattern verdicts: 1 (tell present) / 0 (absent) / UNSCORABLE. Quality verdicts: 0-4 /
UNSCORABLE. Evidence: one artifact per row with a §14 tier — `✓ live <screenshot/computed
style>` · `✓ code <file:line>` · `~ inferred <why>`. UNSCORABLE never counts; a section with
no `✓ live` artifact is UNVERIFIED and will not gate without an audited waiver.

## Self score — anti-pattern tells

| # | Tell | Verdict | Evidence |
|---|---|---|---|
{anti}

**Self anti total:** 0/13

## Self score — directed quality

| # | Dimension | Score (0-4) | Evidence |
|---|---|---|---|
{qual}

**Self quality mean:** 0.0/4

## Independent score — anti-pattern tells

| # | Tell | Verdict | Evidence |
|---|---|---|---|
{anti}

**Independent anti total:** 0/13

## Independent score — directed quality

| # | Dimension | Score (0-4) | Evidence |
|---|---|---|---|
{qual}

**Independent quality mean:** 0.0/4
"""
    print(body)
    return 0


# ------------------------------------------------------------------ self-test
def _anti_rows(ones=0, unscorable=0, live_row=True):
    rows = []
    for i in range(1, N_TELLS + 1):
        if i <= ones:
            v = "1"
        elif i <= ones + unscorable:
            v = "UNSCORABLE"
        else:
            v = "0"
        tier = "✓ live shot-01.png route=/x" if (live_row and i == 1) else f"✓ code fixture.tsx:{i} clean"
        rows.append(f"| {i} | tell {i} ({TELL_BAN[i]}) | {v} | {tier} |")
    return "\n".join(rows)


def _qual_rows(scores=None, unscorable=0, live_row=True):
    scores = scores or {}
    rows = []
    for i in range(1, N_DIMS + 1):
        if i <= unscorable:
            v = "UNSCORABLE"
        else:
            v = str(scores.get(i, 3))
        tier = "✓ live shot-02.png route=/x" if (live_row and i == 1) else f"✓ code fixture.tsx:{i} basis"
        rows.append(f"| {i} | dim {i} {DIM_KEY[i]} | {v} | {tier} |")
    return "\n".join(rows)


def _mean(scores=None, unscorable=0):
    scores = scores or {}
    vals = [scores.get(i, 3) for i in range(unscorable + 1, N_DIMS + 1)]
    return f"{(sum(vals)/len(vals)) if vals else 0.0:.2f}"


def _fixture(scope="small", persisted="commit-body", version="2.0",
             s_anti=None, s_anti_total=0, s_qual=None, s_mean=None,
             indep=True, i_anti=None, i_anti_total=0, i_qual=None, i_mean=None):
    s_anti = s_anti if s_anti is not None else _anti_rows()
    s_qual = s_qual if s_qual is not None else _qual_rows()
    s_mean = s_mean if s_mean is not None else _mean()
    i_anti = i_anti if i_anti is not None else _anti_rows()
    i_qual = i_qual if i_qual is not None else _qual_rows()
    i_mean = i_mean if i_mean is not None else _mean()
    doc = f"""# Design scorecard — fixture

**Surface:** Fixture view
**Scope:** {scope}
**Thesis:** A calm, exact fixture that feels machined and specific.
**Direction:** D-A fixture
**Persisted:** {persisted}
**Scorecard format version:** {version}

## Self score — anti-pattern tells

| # | Tell | Verdict | Evidence |
|---|---|---|---|
{s_anti}

**Self anti total:** {s_anti_total}/13

## Self score — directed quality

| # | Dimension | Score (0-4) | Evidence |
|---|---|---|---|
{s_qual}

**Self quality mean:** {s_mean}/4
"""
    if indep:
        doc += f"""
## Independent score — anti-pattern tells

| # | Tell | Verdict | Evidence |
|---|---|---|---|
{i_anti}

**Independent anti total:** {i_anti_total}/13

## Independent score — directed quality

| # | Dimension | Score (0-4) | Evidence |
|---|---|---|---|
{i_qual}

**Independent quality mean:** {i_mean}/4
"""
    return doc


def _run_check(tmpdir, content, extra=None):
    p = os.path.join(tmpdir, "scorecard.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    # The fixtures cite '✓ live shot-0N.png' — those artifacts must EXIST now (round-4 F-A3).
    for shot in ("shot-01.png", "shot-02.png"):
        sp = os.path.join(tmpdir, shot)
        if not os.path.exists(sp):
            with open(sp, "wb") as f:
                f.write(b"\x89PNG-fixture")
    extra = list(extra or [])
    if "--repo-root" not in extra:
        extra += ["--repo-root", tmpdir]
    return main(["check", p] + extra)


def self_test():
    failures = []

    def expect(name, got, want):
        if got != want:
            failures.append(f"{name}: exit {got}, wanted {want}")

    with tempfile.TemporaryDirectory() as td:
        expect("clean v2 pass", _run_check(td, _fixture()), 0)
        expect("v1 version refused", _run_check(td, _fixture(version="1.0")), 1)
        expect("anti band fail (self 3)", _run_check(td, _fixture(
            s_anti=_anti_rows(ones=3), s_anti_total=3,
            i_anti=_anti_rows(ones=2), i_anti_total=2)), 3)
        expect("quality mean fail", _run_check(td, _fixture(
            s_qual=_qual_rows({i: 2 for i in range(1, 9)}), s_mean="2.00",
            i_qual=_qual_rows({i: 2 for i in range(1, 9)}), i_mean="2.00")), 3)
        expect("quality min-dim fail", _run_check(td, _fixture(
            s_qual=_qual_rows({2: 1, 4: 4, 5: 4}), s_mean=_mean({2: 1, 4: 4, 5: 4}),
            i_qual=_qual_rows({2: 1, 4: 4, 5: 4}), i_mean=_mean({2: 1, 4: 4, 5: 4}))), 3)
        expect("core-dim <3 fails", _run_check(td, _fixture(
            s_qual=_qual_rows({1: 2, 4: 4, 5: 4, 6: 4}), s_mean=_mean({1: 2, 4: 4, 5: 4, 6: 4}),
            i_qual=_qual_rows({1: 2, 4: 4, 5: 4, 6: 4}), i_mean=_mean({1: 2, 4: 4, 5: 4, 6: 4}))), 3)
        expect("anti declared!=derived", _run_check(td, _fixture(s_anti_total=5)), 1)
        expect("quality mean mismatch", _run_check(td, _fixture(s_mean="3.75")), 1)
        expect("missing independent", _run_check(td, _fixture(indep=False)), 4)
        expect("waived independent", _run_check(td, _fixture(indep=False),
               ["--allow-missing-independent", "no Agent tool in this session"]), 0)
        # Round-4 F-A1 (the g1/g2 laundering pair): two weak NON-CORE dims honestly scored 1 →
        # fail; the SAME dims marked UNSCORABLE must NOT flip to PASS — refuse without a waiver
        # (and even the waiver never rescues a CORE dim — separate assertion above).
        weak = {6: 1, 7: 1}
        expect("g1: weak dims scored honestly fail", _run_check(td, _fixture(
            s_qual=_qual_rows(weak), s_mean=_mean(weak),
            i_qual=_qual_rows(weak), i_mean=_mean(weak))), 3)

        def _qual_rows_laundered(unscored_dims):
            rows = []
            for i in range(1, N_DIMS + 1):
                v = "UNSCORABLE" if i in unscored_dims else "3"
                tier = "✓ live shot-02.png route=/x" if i == 1 else f"✓ code fixture.tsx:{i} basis"
                rows.append(f"| {i} | dim {i} {DIM_KEY[i]} | {v} | {tier} |")
            return "\n".join(rows)
        laundered = _qual_rows_laundered({6, 7})
        expect("g2: laundering via UNSCORABLE refused", _run_check(td, _fixture(
            s_qual=laundered, s_mean="3.00",
            i_qual=laundered, i_mean="3.00")), 4)
        expect("g2 waived: audited laundering passes loudly", _run_check(td, _fixture(
            s_qual=laundered, s_mean="3.00",
            i_qual=laundered, i_mean="3.00"),
            ["--allow-unscored", "states not exercisable without seeded data"]), 0)
        # Round-4 F-A2: the same rule on the anti half — one hidden tell refuses unwaived.
        one_unscorable_anti = _fixture(s_anti=_anti_rows().replace(
            f"| 2 | tell 2 ({TELL_BAN[2]}) | 0 |", f"| 2 | tell 2 ({TELL_BAN[2]}) | UNSCORABLE |"))
        expect("anti UNSCORABLE refuses without waiver", _run_check(td, one_unscorable_anti), 4)
        expect("anti UNSCORABLE with waiver passes loudly", _run_check(td, one_unscorable_anti,
               ["--allow-unscored", "tell 2 needs a viewport this run lacked"]), 0)
        # Round-4 F-A3: a fabricated '✓ live' artifact path is a structural refusal.
        fabricated = _fixture(s_anti=_anti_rows().replace(
            "✓ live shot-01.png route=/x", "✓ live never-captured.png route=/nowhere"))
        expect("g4: fabricated live artifact refused", _run_check(td, fabricated), 1)
        # Round-4 F-A4: unreadable scorecard path fails cleanly, not with a traceback.
        expect("missing scorecard file fails cleanly",
               main(["check", os.path.join(td, "no-such-file.md")]), 1)
        expect("anti delta > 1", _run_check(td, _fixture(
            s_anti=_anti_rows(ones=0), s_anti_total=0,
            i_anti=_anti_rows(ones=2), i_anti_total=2)), 4)
        expect("quality delta > 0.75", _run_check(td, _fixture(
            s_qual=_qual_rows({i: 4 for i in range(1, 9)}), s_mean="4.00",
            i_qual=_qual_rows({i: 3 for i in range(1, 9)}), i_mean="3.00")), 4)
        expect("all-UNSCORABLE anti refused", _run_check(td, _fixture(
            s_anti=_anti_rows(unscorable=13), s_anti_total=0)), 1)
        expect(">2 UNSCORABLE quality refused", _run_check(td, _fixture(
            s_qual=_qual_rows(unscorable=3), s_mean=_mean(unscorable=3))), 1)
        no_live = _fixture(
            s_anti=_anti_rows(live_row=False), s_qual=_qual_rows(live_row=False),
            i_anti=_anti_rows(live_row=False), i_qual=_qual_rows(live_row=False))
        expect("UNVERIFIED (no live) refused", _run_check(td, no_live), 4)
        expect("UNVERIFIED waived", _run_check(td, no_live,
               ["--allow-no-live", "no dev server on this machine"]), 0)
        expect("fabricated tell refused", _run_check(td, _fixture(
            s_anti=_anti_rows().replace("| 3 | tell 3 (BAN-3) |", "| 3 | made-up-easy-tell |"))), 1)
        expect("wrong dim keyword refused", _run_check(td, _fixture(
            s_qual=_qual_rows().replace("| 4 | dim 4 composition |", "| 4 | dim 4 vibes |"))), 1)
        expect("program scope rejects arbitrary file", _run_check(td, _fixture(
            scope="program", persisted="package.json")), 6)
        doc_path = os.path.join(td, "docs", "design-direction-fixture.md")
        os.makedirs(os.path.dirname(doc_path), exist_ok=True)
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write("# direction\nThesis: fixture\n")
        expect("program scope with doc", _run_check(td, _fixture(
            scope="program", persisted="docs/design-direction-fixture.md"),
            ["--repo-root", td]), 0)
        fenced = _fixture() + "\n```\n| 1 | quoted (BAN-1) | 1 | fake |\n**Self anti total:** 9/13\n```\n"
        expect("fenced example inert", _run_check(td, fenced), 0)
        expect("pending persistence", _run_check(td, _fixture(persisted="(pending)")), 6)

    hits = scan_added_lines([
        ("src/App.tsx", 1, "const c = '#0B1220'"),
        ("src/App.tsx", 2, "transition-duration: 367ms"),
        ("src/App.tsx", 3, "transition-duration: 200ms"),
        ("src/App.tsx", 4, "const ok = '#ff0000' /* design-ok: brand literal */"),
        ("src/tokens.css", 5, "--accent: #ff0000;"),
        ("README.md", 6, "#ffffff"),
        ("src/App.tsx", 7, "color: '#fff8'"),
        ("src/App.tsx", 8, "background: oklch(0.55 0.12 250)"),
        ("src/App.tsx", 9, "color: color-mix(in oklch, var(--a), var(--b))"),
        ("src/App.tsx", 10, "transition: all 0.367s ease"),
        ("src/App.tsx", 11, "transition: opacity .3s"),
    ])
    got = {(h[0], h[1]) for h in hits}
    want = {("src/App.tsx", 1), ("src/App.tsx", 2), ("src/App.tsx", 7),
            ("src/App.tsx", 8), ("src/App.tsx", 10)}
    if got != want:
        failures.append(f"scan_added_lines: got {sorted(got)}, want {sorted(want)}")

    if failures:
        for f in failures:
            print(f"SELF-TEST FAIL: {f}", file=sys.stderr)
        return 1
    print("frontend-design-check.py self-test (v2): all assertions passed")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="frontend-design-check.py", add_help=True)
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    t = sub.add_parser("template")
    t.add_argument("--slug", default="<slug>")
    c = sub.add_parser("check")
    c.add_argument("scorecard")
    c.add_argument("--repo-root")
    c.add_argument("--allow-missing-independent", metavar="REASON")
    c.add_argument("--allow-no-live", metavar="REASON")
    c.add_argument("--allow-unscored", metavar="REASON")
    d = sub.add_parser("diff-scan")
    d.add_argument("--repo-root")
    d.add_argument("--base", default="HEAD")
    d.add_argument("--list-only", action="store_true")

    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.cmd == "template":
        return cmd_template(args.slug)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "diff-scan":
        return cmd_diff_scan(args)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

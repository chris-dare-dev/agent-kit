---
type: reference
project: milestone-pipeline
status: active
tags:
  - type/reference
  - project/milestone-pipeline
  - status/active
---
# Canonical critique format

Every Phase 3 critic (adversary, delivery-integrity, frontend-ux, infra-safety, oss-scout) emits findings in this format. The Rectifier walks the file top-to-bottom, and `data/scripts/milestone-pipeline-findings.py` (invoked via `python3`) is the ONE parser for it: `extract --check` is the format lint every critic self-runs before returning; `extract` materializes the findings register (`findings.json` — schema: `milestone-pipeline-findings-schema.md`); `dedupe` clusters cross-critic agreement. **The lint is fail-loud: a malformed finding block exits 1 listing the problem — findings are never silently skipped** (the pre-2026-07-09 dedupe script's silent `continue` is retired; its filename remains as a deprecation stub).

## File header

```markdown
# Adversary critique — milestone {ID}

**Diff range:** {BASE_SHA}..{HEAD_SHA}
**Critic:** adversary
**Generated:** {ISO8601 UTC}
**Critique format version:** 1.0
```

All four header lines are REQUIRED (the lint checks them). `**Critic:**` is the canonical spelling — every shipped critique and agent body uses it; the lint also tolerates the older `**Critic(s):**`.

## Executive summary (≤8 bullets)

```markdown
## Executive summary

- **Overall verdict:** SHIP / SHIP-WITH-FIXES / DO-NOT-SHIP
- **Severity counts:** C0 H2 M4 L5
- **Headline finding:** <one sentence — the single most important thing>
- **Hot spots:** <files where multiple findings cluster>
- **External writes flagged:** <count of external-write authorization findings>
```

`SHIP` = zero CRITICAL + zero HIGH. `SHIP-WITH-FIXES` = ≥1 HIGH but no CRITICAL. `DO-NOT-SHIP` = ≥1 CRITICAL.

The `Severity counts:` line is REQUIRED and **must equal the per-severity tally of the finding blocks in this file** — the lint refuses a mismatch. Conditional critics emit their prefixed form (`F-C0 F-H1 F-M1 F-L2` / `I-C0 …`); the parser accepts both.

## Findings (grouped by severity, in order: CRITICAL → HIGH → MEDIUM → LOW)

Each finding is a single block:

```markdown
**C1 — Short imperative title** (CRITICAL)

File: `path/to/file.yaml:42`

**What:** One sentence describing the issue.

**Why it matters:** One paragraph (≤120 words) on the downstream impact — what breaks, who's affected, what the worst case looks like. Cite memory references or prior incidents when applicable.

**Proposed fix:** Concrete — name the function/file/line/value to change. If the fix is non-obvious, include a 3-line code snippet.

**Regression-guard:** The test/assert/snapshot the Rectifier should add to prevent reintroduction. Format: `<file path or assertion type>`. Use `(none feasible)` if no artifact applies; the Rectifier will note this in the commit body.

**Source critic:** adversary  (or `delivery-integrity`, `frontend-ux`, `infra-safety`, `oss-scout`)
```

ID conventions:
- Adversary: `C1, C2, H1, H2, M1, M2, L1, L2, ...`
- Infra-safety: `I-C1, I-H1, I-M1, I-L1, ...`
- Frontend-UX: `F-C1, F-H1, F-M1, F-L1, ...` (the **Source critic** field additionally disambiguates)
- Delivery-integrity: `V-C1, V-H1, V-M1, V-L1, ...`
- OSS-scout: `O-M1, O-L1, ...` (never CRITICAL)

Per-finding requirements the lint enforces (`extract --check`):
- The header line parses (`**<id> — <title>** (<SEVERITY>)`), severity ∈ CRITICAL/HIGH/MEDIUM/LOW, and the id's letter matches the severity word (`H2` must be HIGH). A bold line that LOOKS like a finding header but does not parse canonically (e.g. a `:` delimiter) is refused, not skipped.
- Ids are unique across ALL of the run's critique files.
- A `File:` line exists. Canonical form is `` `path:line` ``; tolerated citations: line ranges (`` `path:33-35` ``), comma lists, file-only (`` `path` `` + prose), and `File: n/a (<why>)` when no file genuinely applies. The machine `file`/`line` fields are parsed best-effort from the first backticked token; the prose stays the Rectifier's read surface.
- The remediation-contract body fields each exist: `**What:**`, `**Why it matters:**`, `**Proposed fix:**`, `**Regression-guard:**` (corpus-calibrated 2026-07-09 — every shipped v1.0 critique already carries all four; round-4 F9).
- A `**Source critic:**` line exists.
- Exactly ONE `Severity counts:` line exists in the file (a stale duplicate misleads readers).

Fenced code blocks (``` or ~~~) are blanked before parsing — quoting a finding-shaped example or a `Severity counts:` string inside a fence is safe (it neither phantom-extracts nor trips the counts check). Keep quoted examples fenced; unfenced finding-shaped prose WILL parse.

## Severity calibration

| Severity | Bar |
|---|---|
| **CRITICAL** | Production breaks, data loss, secret leak, auth bypass, hardcoded external-write auto-execution, or a violation of an absolute workspace rule (`deploy/` edit, `--no-verify`, force-push to main). |
| **HIGH** | Feature broken on a supported environment; IRSA/PCA/Istio misconfiguration; ArgoCD won't sync; bitnami/* image reference; missing user-authorization step on an external write that's gated but not blocking. |
| **MEDIUM** | Degraded UX, missing test/assert for new code, doc drift on a load-bearing convention, deprecation warning that will become an error within 1 release. |
| **LOW** | Cosmetic, microcopy, deferrable cleanup, minor doc nits, redundant config. |

**Don't inflate severity to "earn" the pipeline.** A clean diff with 2 MEDIUMs and 4 LOWs is a legitimate result.

## What was done well (REQUIRED — 5–10 bullets)

```markdown
## What was done well

- Bullet about a good design decision in the diff
- Bullet about a non-obvious thing the implementer got right
- Bullet about a convention correctly followed
- ...
```

This section calibrates the rest of the critique. An empty section makes the critique read as adversarial-for-its-own-sake.

## Recommended rectification order

```markdown
## Recommended rectification order

1. C1 — fix first; blocks ship
2. H1 — fix this commit; rolling-restart impact
3. H2 — fix this commit; same area as H1, batch the change
4. M1, M2 — fix if ≤30 LOC each
5. M3, M4 — defer (test surface too large)
6. L1–L5 — defer (cosmetic)
7. Invalidated: H3 (if any — Phase 4 confirms)
```

## Cross-critic agreement (auto-emitted by `milestone-pipeline-findings.py dedupe`)

```markdown
## Cross-critic agreement

The following findings cluster within 5 lines of each other in the same file. Multiple critics flagged the same area — these are the strongest signals to fix first.

- **H1, I-H1** at `charts/kiali/templates/secret.yaml:12-15` (HIGH): Missing namespace selector; SG rule scope incorrect
- **M2, I-M1** at `infra/pulumi/observability/sg.py:88-92` (MEDIUM): IAM action over-broad; missing inline policy comment
```

This section is auto-inserted by `milestone-pipeline-findings.py dedupe` BEFORE `## Recommended rectification order`. Don't write it by hand — the script writes it after parsing. Findings whose `File:` citation has no line number are extractable but not clusterable (no window to measure).

## Rectification status (filled in Phase 4)

```markdown
---

## Rectification status (filled in Phase 4)

- **Commit:** {sha}
- **Fixed:** C1, H1, H2, M1
- **Invalidated on re-verification:** H3 (reason: <one line>)
- **Deferred to next milestone:** M2, L1, L2, L3
- **Test additions:** {file:line list}
- **External writes completed:** [...]
- **External writes skipped (user choice):** [...]
```

This footer is the only post-creation modification allowed. It is the human-readable **view**; the machine canon for per-finding status is the findings register (`findings.json`), written via `milestone-pipeline-findings.py set` — the footer and the register must tell the same story (`pipeline-reconcile.py` cross-checks the register against the state arrays and per-file counts).

## Anti-patterns (don't do these)

- **Architectural rewrites as findings.** "Rewrite this in Rust" is not a finding. Critique scope is the diff.
- **Severity inflation.** Don't promote a LOW to a MEDIUM to make the critique look productive.
- **Empty "What was done well".** Always include 5–10 bullets — calibration matters.
- **Bare-line citations.** Every finding has `file:line`. "There's a bug somewhere in the chart" is not a finding.
- **Fix prescriptions outside the diff scope.** Findings address what was changed, not pre-existing issues unless the change made them load-bearing.

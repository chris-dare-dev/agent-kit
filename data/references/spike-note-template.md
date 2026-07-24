---
type: reference
status: active
tags:
  - type/reference
  - status/active
---
# Spike Note Template

Copy this skeleton verbatim into `note.md`. Replace every `<placeholder>` with actual content.
Do NOT preserve placeholder text in the final note. Length cap: 200 lines. Numbers beat prose.

**The verdict is DERIVED, not chosen.** `spike-decide.py` already computed
`decision.json:verdict` from the criteria + measurements. Copy that value
verbatim into the `## Decision` line — do not re-adjudicate it. The writer's job
is the **implications**, not the verdict.

---

```markdown
# Spike: <spike-id>

**Assumption tested:** <one sentence — copy verbatim from design.md>
**Result:** YES | NO | UNCERTAIN  <!-- header summary only; the canonical Verdict line is in ## Decision below -->
**Cost:** ~$<X.XX> (estimated from token counts)
**Downstream milestone:** <milestone-id or "none">

---

## Method

**Experiment design:** <2–3 sentences describing what was built and run>

**Success criteria** (from design.md):

| Criterion | Threshold | Measured | Pass? |
|---|---|---|---|
| <name> | <value> | <measurements.json field> | YES / NO |
| <name> | <value> | <measurements.json field> | YES / NO |

**Sample size:** N = <integer> (<justification from design.md — e.g., "90% power at 0.05 α">)

**POC:** `poc/<script>.py` — <one-line description>

---

## Result

<!-- Cite measurements.json fields by name. Do not paraphrase — use the numbers. -->

| Metric | Value | Source field |
|---|---|---|
| <metric> | <value> | `measurements.json:<field>` |
| <metric> | <value> | `measurements.json:<field>` |

<2–4 sentences of context only if the table is insufficient. Numbers first.>

---

## Decision

Verdict: YES | NO | UNCERTAIN

<!-- Copy this verbatim from decision.json:verdict — it is DERIVED, do not choose it. -->
<!-- Keep the line bare — no markdown bold, no ** wrapping. -->
<!-- If UNCERTAIN, name the precise methodology gap the executor declared: -->
<!-- "UNCERTAIN because <X> was measured as unmeasured (reason: <Y>). Resolution: <Z>." -->

---

## Implications for Downstream Milestone

<!-- IMPERATIVE TONE REQUIRED. Use "Do X." / "Implement Y with Z." / "Do not use W." -->
<!-- Never use hedging language: "Consider", "Maybe", "Could explore", "Seems to". -->
<!-- The downstream milestone implementer reads this section and acts on it directly. -->
<!-- YES: "Proceed with <approach>. Design constraint: <X>." -->
<!-- NO: "Do not use <approach>. Alternative: <Y>." -->
<!-- UNCERTAIN: "Do not proceed until <gap> is resolved. Suggested next spike: <Z>." -->

- <implication 1>
- <implication 2>

---

## Spike Artifacts

| File | Description |
|---|---|
| `design.json` / `design.md` | Typed success criteria + human design, confounds |
| `design-deviation.md` | (only if executor flagged a flawed design — historical record) |
| `poc/<script>.py` | POC implementation (throwaway) |
| `measurements.json` | Raw measured values |
| `decision.json` | DERIVED verdict + per-criterion pass/fail/unmeasured |
| `review.json` / `review.md` | Reviewer verdict + axis findings |

---

## Confounds and Caveats

<!-- List ≥3 confounds from design.md and whether each was controlled. -->

| Confound | Controlled? | How |
|---|---|---|
| <confound 1> | YES / NO | <method or "not controlled — see caveats"> |
| <confound 2> | YES / NO | <method or "not controlled — see caveats"> |
| <confound 3> | YES / NO | <method or "not controlled — see caveats"> |

**Uncontrolled caveats:** <list any confounds marked NO above and their potential impact on the verdict>
```

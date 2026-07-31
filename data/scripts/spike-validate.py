#!/usr/bin/env python3
"""Validate a spike artifact against its frozen schema (fail-loud, never fail-open).

Usage:
  spike-validate.py design       <path>   # validate design.json
  spike-validate.py measurements <path>   # validate measurements.json
  spike-validate.py decision     <path>   # validate decision.json (+ internal verdict consistency)
  spike-validate.py review       <path>   # validate review.json
  spike-validate.py --self-test           # tempdir/in-memory fixtures for every refusal path

Exit 0 = valid; exit 1 = invalid (problems printed to stderr) or unreadable.

This is the ARTIFACT-LAYER authority for the /spike pipeline. The reference
pattern (milestone-pipeline-findings.py `_load_register`) is: a malformed or
hand-mangled artifact is REFUSED at every consumer path, never silently read as
a weaker-but-valid object. spike-checkpoint.py subprocesses this before every
evidence-gated transition; the four agents self-run it before returning.

Only schema_version 1 is understood — any other value is refused rather than
guessed (mirrors roadmap-milestones-status.py's version guard).

Scope: this validates the INTRINSIC shape of ONE artifact. Cross-artifact
provenance (does measurements.design_hash match the current design.json? does
every design criterion have a measured value?) is the checkpoint's job — it is
the only actor holding all the hashes at once. Keep that split: shape here,
provenance in spike-checkpoint.py.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1

# Comparison operators a criterion may use. Numeric operators require numeric
# operands (enforced by spike-decide.py at derivation time); ==/!= permit
# string or numeric equality.
OPERATORS = {"<", "<=", ">", ">=", "==", "!="}

NOTE_VERDICTS = {"YES", "NO", "UNCERTAIN"}
REVIEW_VERDICTS = {"ACCEPT", "RE-RUN", "RECONSIDER-DECISION"}
CRITERION_RESULTS = {"pass", "fail", "unmeasured"}

# The six reviewer axes — review.json must carry a disposition for each. Keeping
# the axis keys machine-checkable is what lets the reviewer's prose drift from
# its structured verdict be caught (audit defect: prose-only contracts).
REVIEW_AXES = (
    "design_validity",
    "sample_size",
    "confound",
    "methodology",
    "decision_validity",
    "implications",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_sha256(v: object) -> bool:
    return isinstance(v, str) and bool(_SHA256_RE.match(v))


def _is_number(v: object) -> bool:
    # bool is a subclass of int — a threshold of `true` is a mistake, not a 1.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _require_keys(obj: dict, keys: tuple[str, ...], problems: list[str]) -> None:
    for k in keys:
        if k not in obj:
            problems.append(f"missing required key '{k}'")


def _check_common(obj: object, kind: str, problems: list[str]) -> bool:
    """Shared top-level checks. Returns True if obj is a dict of the right version."""
    if not isinstance(obj, dict):
        problems.append(f"{kind} is not a JSON object")
        return False
    ver = obj.get("schema_version")
    if ver != SCHEMA_VERSION:
        problems.append(
            f"unsupported schema_version {ver!r} — this validator only accepts "
            f"version {SCHEMA_VERSION} (see spike-artifact-schema.md: Versioning)"
        )
        return False
    if not isinstance(obj.get("spike_id"), str) or not obj["spike_id"].strip():
        problems.append("spike_id must be a non-empty string")
    return True


# ---------------------------------------------------------------- design.json


def validate_design(obj: object) -> list[str]:
    problems: list[str] = []
    if not _check_common(obj, "design", problems):
        return problems
    assert isinstance(obj, dict)

    _require_keys(
        obj,
        (
            "assumption",
            "criteria",
            "sample_size",
            "sample_justification",
            "confounds",
            "measurement_fields",
            "poc_constraints",
        ),
        problems,
    )

    if not isinstance(obj.get("assumption"), str) or not obj.get("assumption", "").strip():
        problems.append("assumption must be a non-empty string")

    fields = obj.get("measurement_fields")
    field_set: set[str] = set()
    if not isinstance(fields, list) or not fields:
        problems.append("measurement_fields must be a non-empty list")
    else:
        for i, f in enumerate(fields):
            if not isinstance(f, str) or not f.strip():
                problems.append(f"measurement_fields[{i}] is not a non-empty string")
            else:
                field_set.add(f)

    criteria = obj.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        problems.append("criteria must be a non-empty list (a spike tests >=1 criterion)")
    else:
        seen_names: set[str] = set()
        for i, c in enumerate(criteria):
            if not isinstance(c, dict):
                problems.append(f"criteria[{i}] is not an object")
                continue
            name = c.get("name")
            if not isinstance(name, str) or not name.strip():
                problems.append(f"criteria[{i}].name must be a non-empty string")
            elif name in seen_names:
                problems.append(f"criteria[{i}].name {name!r} is duplicated")
            else:
                seen_names.add(name)
            fld = c.get("field")
            if not isinstance(fld, str) or not fld.strip():
                problems.append(f"criteria[{i}].field must be a non-empty string")
            elif field_set and fld not in field_set:
                problems.append(
                    f"criteria[{i}].field {fld!r} is not in measurement_fields "
                    "(the executor would have no value to compare against)"
                )
            op = c.get("operator")
            if op not in OPERATORS:
                problems.append(
                    f"criteria[{i}].operator {op!r} is not one of {sorted(OPERATORS)}"
                )
            thr = c.get("threshold")
            if op in {"<", "<=", ">", ">="} and not _is_number(thr):
                problems.append(
                    f"criteria[{i}].threshold must be a number for operator {op!r} "
                    f"(got {thr!r})"
                )
            elif op in {"==", "!="} and not (
                _is_number(thr) or isinstance(thr, (str, bool))
            ):
                problems.append(
                    f"criteria[{i}].threshold must be a number, string, or boolean "
                    f"for operator {op!r} (got {thr!r})"
                )
            if not isinstance(c.get("unit"), str):
                problems.append(f"criteria[{i}].unit must be a string (use \"\" if unitless)")

    ss = obj.get("sample_size")
    if not isinstance(ss, int) or isinstance(ss, bool) or ss <= 0:
        problems.append("sample_size must be a positive integer")
    if not isinstance(obj.get("sample_justification"), str) or not obj.get(
        "sample_justification", ""
    ).strip():
        problems.append("sample_justification must be a non-empty string")

    confounds = obj.get("confounds")
    if not isinstance(confounds, list) or len(confounds) < 3:
        problems.append("confounds must be a list of >=3 entries (spike-protocol rule)")
    else:
        for i, cf in enumerate(confounds):
            if not isinstance(cf, dict) or not isinstance(cf.get("confound"), str) or not isinstance(
                cf.get("control"), str
            ):
                problems.append(
                    f"confounds[{i}] must be an object with string 'confound' and 'control'"
                )

    pc = obj.get("poc_constraints")
    if not isinstance(pc, dict):
        problems.append("poc_constraints must be an object")
    else:
        if "dependencies" in pc and not isinstance(pc["dependencies"], list):
            problems.append("poc_constraints.dependencies must be a list when present")

    return problems


# ------------------------------------------------------------ measurements.json


def _valid_value(v: object) -> bool:
    """A measurement value is a scalar, null, or an explicit unmeasured sentinel."""
    if v is None or isinstance(v, (int, float, str, bool)):
        return True
    if isinstance(v, dict) and v.get("unmeasured") is True and isinstance(v.get("reason"), str):
        return True
    return False


def validate_measurements(obj: object) -> list[str]:
    problems: list[str] = []
    if not _check_common(obj, "measurements", problems):
        return problems
    assert isinstance(obj, dict)

    _require_keys(obj, ("design_hash", "executed_at", "values"), problems)

    if not _is_sha256(obj.get("design_hash")):
        problems.append(
            "design_hash must be a 64-char hex sha256 (provenance link to the "
            "design.json this measures)"
        )
    if not isinstance(obj.get("executed_at"), str) or not obj.get("executed_at", "").strip():
        problems.append("executed_at must be a non-empty string")

    values = obj.get("values")
    if not isinstance(values, dict) or not values:
        problems.append("values must be a non-empty object of {field: measurement}")
    else:
        for k, v in values.items():
            if not _valid_value(v):
                problems.append(
                    f"values[{k!r}] must be a scalar, null, or "
                    '{"unmeasured": true, "reason": "..."} — got '
                    f"{type(v).__name__}"
                )

    for opt, typ, label in (
        ("iterations", int, "an integer"),
        ("sample_count", int, "an integer"),
        ("poc_command", str, "a string"),
    ):
        if opt in obj and (not isinstance(obj[opt], typ) or isinstance(obj[opt], bool)):
            problems.append(f"{opt} must be {label} when present")
    if "poc_hash" in obj and not _is_sha256(obj["poc_hash"]):
        problems.append("poc_hash must be a 64-char hex sha256 when present")

    return problems


# ---------------------------------------------------------------- decision.json


def _derive_verdict(per_criterion: list[dict]) -> str:
    """Same rule the writer applied by hand (spike-writer Step 2), now mechanical:
    any fail -> NO; else any unmeasured -> UNCERTAIN; else YES."""
    results = [c.get("result") for c in per_criterion]
    if any(r == "fail" for r in results):
        return "NO"
    if any(r == "unmeasured" for r in results):
        return "UNCERTAIN"
    return "YES"


def validate_decision(obj: object) -> list[str]:
    problems: list[str] = []
    if not _check_common(obj, "decision", problems):
        return problems
    assert isinstance(obj, dict)

    _require_keys(obj, ("measurements_hash", "design_hash", "verdict", "per_criterion"), problems)

    for hkey in ("measurements_hash", "design_hash"):
        if not _is_sha256(obj.get(hkey)):
            problems.append(f"{hkey} must be a 64-char hex sha256 (provenance link)")

    verdict = obj.get("verdict")
    if verdict not in NOTE_VERDICTS:
        problems.append(f"verdict {verdict!r} is not one of {sorted(NOTE_VERDICTS)}")

    pc = obj.get("per_criterion")
    if not isinstance(pc, list) or not pc:
        problems.append("per_criterion must be a non-empty list")
        return problems

    for i, c in enumerate(pc):
        if not isinstance(c, dict):
            problems.append(f"per_criterion[{i}] is not an object")
            continue
        if not isinstance(c.get("name"), str):
            problems.append(f"per_criterion[{i}].name must be a string")
        if c.get("operator") not in OPERATORS:
            problems.append(f"per_criterion[{i}].operator invalid")
        if c.get("result") not in CRITERION_RESULTS:
            problems.append(
                f"per_criterion[{i}].result {c.get('result')!r} is not one of "
                f"{sorted(CRITERION_RESULTS)}"
            )

    # Internal consistency: the stored verdict must equal the verdict its own
    # per-criterion results derive. A decision.json whose verdict was tampered
    # to YES while a criterion says 'fail' is refused (fail-loud). Only checked
    # when every result is in-vocabulary (else the earlier error already fired).
    if verdict in NOTE_VERDICTS and all(
        isinstance(c, dict) and c.get("result") in CRITERION_RESULTS for c in pc
    ):
        expected = _derive_verdict(pc)
        if expected != verdict:
            problems.append(
                f"verdict {verdict!r} is inconsistent with per_criterion results "
                f"(they derive {expected!r}) — decision.json must be the honest "
                "derivation, not a hand-set value"
            )

    return problems


# ------------------------------------------------------------------ review.json


def validate_review(obj: object) -> list[str]:
    problems: list[str] = []
    if not _check_common(obj, "review", problems):
        return problems
    assert isinstance(obj, dict)

    _require_keys(
        obj,
        ("decision_hash", "note_hash", "reviewer_independent_verdict", "axes", "verdict"),
        problems,
    )

    for hkey in ("decision_hash", "note_hash"):
        if not _is_sha256(obj.get(hkey)):
            problems.append(f"{hkey} must be a 64-char hex sha256 (provenance link)")

    if obj.get("reviewer_independent_verdict") not in NOTE_VERDICTS:
        problems.append(
            "reviewer_independent_verdict must be one of "
            f"{sorted(NOTE_VERDICTS)} (the reviewer's own read of the raw data)"
        )
    if obj.get("verdict") not in REVIEW_VERDICTS:
        problems.append(f"verdict {obj.get('verdict')!r} is not one of {sorted(REVIEW_VERDICTS)}")

    axes = obj.get("axes")
    if not isinstance(axes, dict):
        problems.append("axes must be an object keyed by the six review axes")
    else:
        for ax in REVIEW_AXES:
            v = axes.get(ax)
            if not isinstance(v, str) or not v.strip():
                problems.append(f"axes.{ax} must be a non-empty string ('sound' or 'finding: ...')")
        extra = set(axes) - set(REVIEW_AXES)
        if extra:
            problems.append(f"axes has unknown keys: {sorted(extra)}")

    return problems


VALIDATORS = {
    "design": validate_design,
    "measurements": validate_measurements,
    "decision": validate_decision,
    "review": validate_review,
}


def validate_file(kind: str, path: Path) -> list[str]:
    if kind not in VALIDATORS:
        return [f"unknown artifact kind {kind!r}; valid: {', '.join(VALIDATORS)}"]
    if not path.is_file():
        return [f"file not found: {path}"]
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"not valid JSON: {exc}"]
    return VALIDATORS[kind](obj)


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    failures = 0

    def expect(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {name}: {'ok' if ok else f'FAIL {detail}'}")
        if not ok:
            failures += 1

    print("self-test: spike-validate.py")

    # ---- design ----
    good_design = {
        "schema_version": 1,
        "spike_id": "x-spike-1",
        "assumption": "lib X hits p95 <= 20ms",
        "criteria": [
            {"name": "p95", "field": "p95_ms", "operator": "<=", "threshold": 20, "unit": "ms"},
            {"name": "ok", "field": "success", "operator": "==", "threshold": True, "unit": ""},
        ],
        "sample_size": 1000,
        "sample_justification": "stable p95",
        "confounds": [
            {"confound": "warmup", "control": "discard first 100"},
            {"confound": "gc", "control": "gc.disable"},
            {"confound": "io", "control": "in-memory"},
        ],
        "measurement_fields": ["p95_ms", "success"],
        "poc_constraints": {"language": "python3-stdlib", "max_loc": 200, "dependencies": []},
    }
    expect("valid design passes", validate_design(good_design) == [])

    d = json.loads(json.dumps(good_design)); d["schema_version"] = 2
    expect("design wrong version refused", any("schema_version" in p for p in validate_design(d)))
    d = json.loads(json.dumps(good_design)); d["criteria"] = []
    expect("design empty criteria refused", any("criteria" in p for p in validate_design(d)))
    d = json.loads(json.dumps(good_design)); d["criteria"][0]["operator"] = "=~"
    expect("design bad operator refused", any("operator" in p for p in validate_design(d)))
    d = json.loads(json.dumps(good_design)); d["criteria"][0]["threshold"] = "twenty"
    expect("design non-numeric threshold for <= refused",
           any("threshold must be a number" in p for p in validate_design(d)))
    d = json.loads(json.dumps(good_design)); d["criteria"][0]["field"] = "not_declared"
    expect("design criterion field not in measurement_fields refused",
           any("measurement_fields" in p for p in validate_design(d)))
    d = json.loads(json.dumps(good_design)); d["confounds"] = d["confounds"][:2]
    expect("design <3 confounds refused", any("confounds" in p for p in validate_design(d)))
    d = json.loads(json.dumps(good_design)); d["sample_size"] = 0
    expect("design non-positive sample_size refused", any("sample_size" in p for p in validate_design(d)))
    d = json.loads(json.dumps(good_design)); d["sample_size"] = True
    expect("design bool sample_size refused (bool is not int)",
           any("sample_size" in p for p in validate_design(d)))
    d = json.loads(json.dumps(good_design))
    d["criteria"].append({"name": "p95", "field": "p95_ms", "operator": "<", "threshold": 5, "unit": "ms"})
    expect("design duplicate criterion name refused", any("duplicated" in p for p in validate_design(d)))

    # ---- measurements ----
    sha = "a" * 64
    good_meas = {
        "schema_version": 1,
        "spike_id": "x-spike-1",
        "design_hash": sha,
        "executed_at": "2026-07-10T00:00:00Z",
        "poc_command": "python3 poc/bench.py",
        "poc_hash": "b" * 64,
        "iterations": 1,
        "sample_count": 1000,
        "values": {"p95_ms": 14.2, "success": True},
    }
    expect("valid measurements passes", validate_measurements(good_meas) == [])
    m = json.loads(json.dumps(good_meas)); m["design_hash"] = "short"
    expect("measurements bad design_hash refused", any("design_hash" in p for p in validate_measurements(m)))
    m = json.loads(json.dumps(good_meas)); m["values"] = {}
    expect("measurements empty values refused", any("values" in p for p in validate_measurements(m)))
    m = json.loads(json.dumps(good_meas))
    m["values"] = {"p95_ms": {"unmeasured": True, "reason": "needs full cluster"}}
    expect("measurements unmeasured sentinel accepted", validate_measurements(m) == [])
    m = json.loads(json.dumps(good_meas)); m["values"] = {"p95_ms": {"unmeasured": True}}
    expect("measurements unmeasured w/o reason refused",
           any("unmeasured" in p for p in validate_measurements(m)))
    m = json.loads(json.dumps(good_meas)); m["sample_count"] = "lots"
    expect("measurements non-int sample_count refused",
           any("sample_count" in p for p in validate_measurements(m)))

    # ---- decision ----
    good_dec = {
        "schema_version": 1,
        "spike_id": "x-spike-1",
        "measurements_hash": sha,
        "design_hash": "c" * 64,
        "verdict": "YES",
        "per_criterion": [
            {"name": "p95", "field": "p95_ms", "operator": "<=", "threshold": 20, "unit": "ms",
             "measured": 14.2, "result": "pass"},
        ],
        "derived_at": "2026-07-10T00:00:00Z",
    }
    expect("valid decision passes", validate_decision(good_dec) == [])
    dc = json.loads(json.dumps(good_dec)); dc["verdict"] = "MAYBE"
    expect("decision bad verdict enum refused", any("verdict" in p for p in validate_decision(dc)))
    dc = json.loads(json.dumps(good_dec)); dc["verdict"] = ""  # the corpus empty-string case
    expect("decision empty-string verdict refused", any("verdict" in p for p in validate_decision(dc)))
    dc = json.loads(json.dumps(good_dec)); dc["verdict"] = "GO-ephemeral"  # the corpus off-vocab case
    expect("decision off-vocabulary verdict refused (GO-ephemeral)",
           any("verdict" in p for p in validate_decision(dc)))
    dc = json.loads(json.dumps(good_dec)); dc["per_criterion"][0]["result"] = "fail"
    expect("decision verdict/result inconsistency refused (YES over a fail)",
           any("inconsistent" in p for p in validate_decision(dc)))
    dc = json.loads(json.dumps(good_dec))
    dc["per_criterion"][0]["result"] = "unmeasured"; dc["verdict"] = "UNCERTAIN"
    expect("decision UNCERTAIN over an unmeasured criterion accepted", validate_decision(dc) == [])

    # ---- review ----
    good_rev = {
        "schema_version": 1,
        "spike_id": "x-spike-1",
        "decision_hash": sha,
        "note_hash": "d" * 64,
        "reviewer_independent_verdict": "YES",
        "axes": {ax: "sound" for ax in REVIEW_AXES},
        "verdict": "ACCEPT",
        "reviewed_at": "2026-07-10T00:00:00Z",
    }
    expect("valid review passes", validate_review(good_rev) == [])
    rv = json.loads(json.dumps(good_rev)); rv["verdict"] = "APPROVE"
    expect("review bad verdict enum refused", any("verdict" in p for p in validate_review(rv)))
    rv = json.loads(json.dumps(good_rev)); del rv["axes"]["confound"]
    expect("review missing axis refused", any("confound" in p for p in validate_review(rv)))
    rv = json.loads(json.dumps(good_rev)); rv["axes"]["extra_axis"] = "sound"
    expect("review unknown axis refused", any("unknown keys" in p for p in validate_review(rv)))
    rv = json.loads(json.dumps(good_rev)); rv["note_hash"] = "nope"
    expect("review bad note_hash refused", any("note_hash" in p for p in validate_review(rv)))

    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


def main(argv: list[str]) -> None:
    if "--self-test" in argv:
        raise SystemExit(self_test())
    if len(argv) < 3:
        sys.exit(__doc__)
    kind, path = argv[1], Path(argv[2])
    problems = validate_file(kind, path)
    if problems:
        print(f"INVALID {kind} artifact {path}:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    print(f"valid {kind}: {path}")


if __name__ == "__main__":
    main(sys.argv)

#!/usr/bin/env python3
"""Derive a spike's YES/NO/UNCERTAIN verdict deterministically from measured data.

Usage:
  spike-decide.py <spike-id>     # read design.json + measurements.json, write decision.json
  spike-decide.py --self-test    # in-memory fixtures for the derivation rule + boundaries

The verdict is a PURE FUNCTION of (design criteria, measured values) — never an
agent's free choice. This is the structural fix for the sealed-corpus defect
where 4 of 16 ACCEPTed spikes carried a non-canonical verdict ("GO-ephemeral",
three empty strings): a value nothing computed and nothing validated. Here the
writer explains implications; this script owns the verdict.

Derivation rule (identical to the writer's old Step-2 prose, now mechanical):
  - each criterion: apply operator(measured, threshold) -> pass | fail
  - a null / {"unmeasured": true} value          -> unmeasured
  - verdict = NO if any fail; else UNCERTAIN if any unmeasured; else YES

A criterion whose operator needs a number but whose measured value is a
non-numeric scalar is a schema violation, NOT an UNCERTAIN — the script exits
non-zero and writes nothing (the executor must fix measurements.json).

decision.json self-records the design_hash + measurements_hash it derived from
(this script CAN compute hashes reliably, unlike an LLM agent). spike-checkpoint
cross-checks those against its own stored hashes before accepting the transition.

Repo-root detection: $REPO_ROOT, else `git rev-parse --show-toplevel`, else
walk up from this script to the nearest .git/ (same order as the sibling scripts).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
NUMERIC_OPS = {"<", "<=", ">", ">="}
SPIKE_ID_RE = re.compile(r"^[a-z][a-z0-9-]*-spike-[0-9]+$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


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
    for cand in [here, *here.parents]:
        if (cand / ".git").exists():
            return cand
    sys.exit("could not determine repo root. Set REPO_ROOT or run inside a git repo.")


def _spike_dir(sid: str) -> Path:
    if not SPIKE_ID_RE.match(sid) or "/" in sid or "\\" in sid:
        sys.exit(f"invalid spike id {sid!r} — must match {SPIKE_ID_RE.pattern}")
    return _find_repo_root() / ".claude" / "notes" / "spikes" / sid


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _save_atomic(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, encoding="utf-8"))
    os.replace(tmp, path)


def _evaluate(crit: dict, values: dict) -> tuple[str, object, str | None]:
    """Return (result, measured, error). result in pass|fail|unmeasured."""
    field = crit["field"]
    op = crit["operator"]
    thr = crit["threshold"]
    if field not in values:
        return "unmeasured", None, f"criterion {crit.get('name')!r}: no value for field {field!r}"
    v = values[field]
    if v is None or (isinstance(v, dict) and v.get("unmeasured") is True):
        return "unmeasured", (v.get("reason") if isinstance(v, dict) else None), None
    if op in NUMERIC_OPS:
        if not _is_number(v):
            return (
                "unmeasured",
                v,
                f"criterion {crit.get('name')!r}: operator {op!r} needs a numeric "
                f"measurement but values[{field!r}]={v!r} is {type(v).__name__} "
                "(fix measurements.json; do not launder a type error into UNCERTAIN)",
            )
        ok = {
            "<": v < thr,
            "<=": v <= thr,
            ">": v > thr,
            ">=": v >= thr,
        }[op]
    elif op == "==":
        ok = v == thr
    elif op == "!=":
        ok = v != thr
    else:
        return "unmeasured", v, f"criterion {crit.get('name')!r}: unknown operator {op!r}"
    return ("pass" if ok else "fail"), v, None


def derive_decision(design: dict, measurements: dict) -> tuple[list[dict], str, list[str]]:
    """Pure derivation. Returns (per_criterion, verdict, errors)."""
    errors: list[str] = []
    values = measurements.get("values", {})
    per_criterion: list[dict] = []
    for crit in design.get("criteria", []):
        result, measured, err = _evaluate(crit, values)
        if err:
            errors.append(err)
        per_criterion.append(
            {
                "name": crit.get("name"),
                "field": crit.get("field"),
                "operator": crit.get("operator"),
                "threshold": crit.get("threshold"),
                "unit": crit.get("unit", ""),
                "measured": measured,
                "result": result,
            }
        )
    results = [c["result"] for c in per_criterion]
    if any(r == "fail" for r in results):
        verdict = "NO"
    elif any(r == "unmeasured" for r in results):
        verdict = "UNCERTAIN"
    else:
        verdict = "YES"
    return per_criterion, verdict, errors


def decide(sid: str) -> None:
    sdir = _spike_dir(sid)
    design_path = sdir / "design.json"
    meas_path = sdir / "measurements.json"
    for p in (design_path, meas_path):
        if not p.is_file():
            sys.exit(f"required artifact missing: {p}")
    design = json.loads(design_path.read_text(encoding="utf-8"))
    measurements = json.loads(meas_path.read_text(encoding="utf-8"))

    per_criterion, verdict, errors = derive_decision(design, measurements)
    if errors:
        sys.exit(
            "cannot derive a verdict — measurement/criterion inconsistencies:\n  - "
            + "\n  - ".join(errors)
        )

    decision = {
        "schema_version": SCHEMA_VERSION,
        "spike_id": sid,
        "design_hash": _sha256_file(design_path),
        "measurements_hash": _sha256_file(meas_path),
        "verdict": verdict,
        "per_criterion": per_criterion,
        "derived_at": _now(),
    }
    _save_atomic(sdir / "decision.json", decision)
    passed = sum(1 for c in per_criterion if c["result"] == "pass")
    print(
        f"{sid}: verdict={verdict} "
        f"({passed}/{len(per_criterion)} criteria pass) -> decision.json"
    )


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    failures = 0

    def expect(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {name}: {'ok' if ok else f'FAIL {detail}'}")
        if not ok:
            failures += 1

    print("self-test: spike-decide.py")

    design = {
        "criteria": [
            {"name": "p95", "field": "p95_ms", "operator": "<=", "threshold": 20, "unit": "ms"},
            {"name": "ok", "field": "success", "operator": "==", "threshold": True, "unit": ""},
        ]
    }

    # all pass -> YES  (boundary: measured exactly equals a <= threshold)
    pc, v, e = derive_decision(design, {"values": {"p95_ms": 20, "success": True}})
    expect("all pass -> YES (<= boundary inclusive)", v == "YES" and e == [], f"{v} {e}")

    # one fail -> NO
    pc, v, e = derive_decision(design, {"values": {"p95_ms": 21, "success": True}})
    expect("over threshold -> NO", v == "NO" and e == [])

    # equality fail -> NO
    pc, v, e = derive_decision(design, {"values": {"p95_ms": 10, "success": False}})
    expect("== mismatch -> NO", v == "NO")

    # unmeasured sentinel -> UNCERTAIN
    pc, v, e = derive_decision(
        design, {"values": {"p95_ms": {"unmeasured": True, "reason": "no cluster"}, "success": True}}
    )
    expect("unmeasured sentinel -> UNCERTAIN", v == "UNCERTAIN" and e == [])

    # null -> UNCERTAIN
    pc, v, e = derive_decision(design, {"values": {"p95_ms": None, "success": True}})
    expect("null value -> UNCERTAIN", v == "UNCERTAIN")

    # fail dominates unmeasured (any fail -> NO even if another is unmeasured)
    pc, v, e = derive_decision(design, {"values": {"p95_ms": 99, "success": None}})
    expect("fail dominates unmeasured -> NO", v == "NO")

    # type error on a numeric op -> error, NOT UNCERTAIN
    pc, v, e = derive_decision(design, {"values": {"p95_ms": "fast", "success": True}})
    expect("non-numeric measured for <= is an ERROR, not UNCERTAIN", len(e) == 1, str(e))

    # missing field -> unmeasured + error surfaced
    pc, v, e = derive_decision(design, {"values": {"success": True}})
    expect("missing field surfaces an error", len(e) == 1 and pc[0]["result"] == "unmeasured")

    # strictly-greater boundary
    d2 = {"criteria": [{"name": "t", "field": "x", "operator": ">", "threshold": 5, "unit": ""}]}
    pc, v, e = derive_decision(d2, {"values": {"x": 5}})
    expect("> boundary exclusive -> NO at equality", v == "NO")
    pc, v, e = derive_decision(d2, {"values": {"x": 6}})
    expect("> above -> YES", v == "YES")

    # the corpus regression: a spike with a real criterion can NEVER emit an
    # empty-string or off-vocabulary verdict — derive_decision only ever returns
    # one of the three canonical tokens.
    expect("verdict is always canonical", v in {"YES", "NO", "UNCERTAIN"})

    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


def main(argv: list[str]) -> None:
    if "--self-test" in argv:
        raise SystemExit(self_test())
    if len(argv) < 2:
        sys.exit(__doc__)
    decide(argv[1])


if __name__ == "__main__":
    main(sys.argv)

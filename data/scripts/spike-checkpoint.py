#!/usr/bin/env python3
"""Own the /spike run state machine — the machine authority for phase, attempt,
loop budgets, artifact provenance hashes, terminal status, and exactly-once
outcome emission. Deterministic tooling owns state transitions and terminal
claims; the agents author prose and typed artifacts.

Usage:
  spike-checkpoint.py <id> --init [--roadmap-path P] [--brief-source S]
  spike-checkpoint.py <id> <phase>              # advance one step (evidence-gated)
  spike-checkpoint.py <id> --get <field>
  spike-checkpoint.py <id> --set <field>=<json> # only roadmap_path / brief_source
  spike-checkpoint.py <id> --rerun              # RE-RUN: new attempt, back to design
  spike-checkpoint.py <id> --reconsider         # RECONSIDER-DECISION: re-write note only
  spike-checkpoint.py <id> --deviation          # design-deviation: re-design
  spike-checkpoint.py <id> --terminal <status>  # non-accept terminal + once-only emit
  spike-checkpoint.py --self-test

Phase machine (forward-only WITHIN an attempt; loops reset the phase explicitly):
  init -> designed -> executed -> decided -> written -> reviewed -> complete

'complete' is the ACCEPT terminal; the non-accept terminals are recorded via
--terminal. Every terminal emits exactly one outcome record (guarded by
outcome_emitted under the state lock).

Evidence gates (the drift-guard — a phase cannot be entered until its artifact
exists, VALIDATES against spike-validate.py, and its provenance hashes line up):
  designed  design.json valid + design.md present            -> store design_hash
  executed  design unchanged; measurements.json valid; its
            design_hash echoes the stored one; every criterion
            field has a measured value                        -> store measurements_hash
  decided   design+measurements unchanged; decision.json valid;
            its hashes echo the stored ones                   -> store decision_hash + verdict
  written   design+measurements+decision unchanged; note.md
            present + non-empty                               -> store note_hash
  reviewed  all upstream unchanged; review.json valid; its
            decision_hash+note_hash echo the stored ones      -> store review_hash + review_verdict
  complete  phase==reviewed; review_verdict==ACCEPT; verdict
            in {YES,NO,UNCERTAIN}; all upstream unchanged      -> terminal accept + emit

Provenance model: SCRIPTS compute hashes (this file stores state hashes;
spike-decide.py records decision.json's hashes); AGENTS echo the hash the
orchestrator hands them (executor -> measurements.design_hash; reviewer ->
review.decision_hash / review.note_hash). This file re-hashes every upstream
artifact at every advance, so an out-of-band edit to design.json after 'designed'
makes 'executed' refuse — the stale generation is caught, not silently resumed.

Concurrency + durability mirror milestone-pipeline-checkpoint.py: an fcntl lock
on <state.json>.lock serializes the read-modify-write; the write is atomic
(temp+rename). Only schema_version 1 is mutated.
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

SCHEMA_VERSION = 1

# Spike-id containment: the id is a path segment under .claude/notes/spikes/.
# Same regex as validate-spike-id.sh; keep in sync.
SPIKE_ID_RE = re.compile(r"^[a-z][a-z0-9-]*-spike-[0-9]+$")

PHASE_ORDER = ["init", "designed", "executed", "decided", "written", "reviewed", "complete"]

NOTE_VERDICTS = {"YES", "NO", "UNCERTAIN"}

# Non-accept terminals recorded via --terminal (accept is reached by advancing
# to the 'complete' phase). 'skip-review' additionally requires a canonical
# verdict (the writer ran); the rest can fire at any phase.
TERMINAL_STATUSES = {
    "skip-review",
    "rerun-cap",
    "reconsider-cap",
    "design-deviation-cap",
    "brief-inadequate",
    "aborted-scope",
    "reviewer-malformed",
    "unexpected",
}

RERUN_CAP = 2
RECONSIDER_CAP = 2
DEVIATION_CAP = 2

# --set is deliberately narrow: everything else (phase, hashes, verdicts,
# counters, terminal_status) is script-computed and must not be hand-set.
SETTABLE_FIELDS = {"roadmap_path", "brief_source"}

# Upstream artifact -> its stored hash field, in phase order. Used by the
# re-hash-on-advance provenance check.
ARTIFACT_HASH = [
    ("design.json", "design_hash"),
    ("measurements.json", "measurements_hash"),
    ("decision.json", "decision_hash"),
    ("note.md", "note_hash"),
    ("review.json", "review_hash"),
]

_VALIDATE = Path(__file__).resolve().parent / "spike-validate.py"
_OUTCOME_LOG = Path(__file__).resolve().parent / "pipeline-outcome-log.py"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_repo_root() -> Path:
    env = os.environ.get("REPO_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    plat = os.environ.get("PLATFORM_ROOT")
    if plat and Path(plat).is_dir():
        return Path(plat)
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
        sys.exit(
            f"invalid spike id {sid!r} — ids are {SPIKE_ID_RE.pattern} "
            "(no path separators; the id is a directory segment under "
            ".claude/notes/spikes/)"
        )
    return _find_repo_root() / ".claude" / "notes" / "spikes" / sid


def _state_path(sid: str) -> Path:
    return _spike_dir(sid) / "state.json"


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _load(sp: Path) -> dict:
    if not sp.exists():
        sys.exit(f"state.json not found at {sp} — run spike-checkpoint.py <id> --init first")
    state = json.loads(sp.read_text())
    if state.get("schema_version") != SCHEMA_VERSION:
        sys.exit(
            f"unsupported schema_version {state.get('schema_version')!r} in {sp} — "
            "this writer only mutates v1 (see spike-state-schema.md: Versioning)"
        )
    return state


def _save_atomic(sp: Path, state: dict) -> None:
    tmp = sp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, sp)


@contextmanager
def _locked(sp: Path):
    lock_path = sp.with_name(sp.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _skeleton(sid: str, roadmap_path: str | None, brief_source: str | None) -> dict:
    now = _now()
    return {
        "schema_version": SCHEMA_VERSION,
        "spike_id": sid,
        "created_at": now,
        "updated_at": now,
        "phase": "init",
        "attempt": 1,
        "rerun_count": 0,
        "reconsider_count": 0,
        "deviation_count": 0,
        "design_hash": None,
        "measurements_hash": None,
        "decision_hash": None,
        "note_hash": None,
        "review_hash": None,
        "verdict": None,
        "review_verdict": None,
        "skipped_review": False,
        "terminal_status": None,
        "outcome_emitted": False,
        "roadmap_path": roadmap_path,
        "brief_source": brief_source,
        "phase_history": [{"phase": "init", "at": now}],
        "attempt_history": [],
    }


def _validate_artifact(kind: str, path: Path) -> str | None:
    """Return None if valid, else the validator's message. Fail-loud if the
    validator script is missing (never skip a present artifact)."""
    if not _VALIDATE.is_file():
        return f"validator missing at {_VALIDATE} — cannot verify {kind} (restore it; do not skip)"
    proc = subprocess.run(
        [sys.executable, str(_VALIDATE), kind, str(path)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout).strip()
    return None


def _verify_upstream(state: dict, sdir: Path, upto_field: str, problems: list[str]) -> None:
    """Re-hash every upstream artifact through `upto_field` and confirm it still
    matches the stored hash — catches out-of-band edits / stale generations."""
    for fname, hfield in ARTIFACT_HASH:
        stored = state.get(hfield)
        if stored is None:
            # Not yet produced in this attempt — nothing to verify (and if it is
            # required for this transition, the caller's own gate catches it).
            if hfield == upto_field:
                break
            continue
        fpath = sdir / fname
        if not fpath.is_file():
            problems.append(f"{fname} recorded a hash but the file is gone ({fpath})")
        elif _sha256_file(fpath) != stored:
            problems.append(
                f"{fname} changed on disk since it was checkpointed "
                f"(stored {stored[:12]}…, now differs) — an out-of-band edit or a "
                "stale generation; re-run the phase that owns it via --rerun/--reconsider"
            )
        if hfield == upto_field:
            break


# --------------------------------------------------------------- gates


def _gate_designed(sid: str, sdir: str, state: dict, problems: list[str]) -> dict:
    sd = Path(sdir)
    dj, dm = sd / "design.json", sd / "design.md"
    if not dj.is_file():
        problems.append("design.json not written")
        return {}
    msg = _validate_artifact("design", dj)
    if msg:
        problems.append(f"design.json invalid:\n    {msg.replace(chr(10), chr(10)+'    ')}")
        return {}
    if not dm.is_file() or not dm.read_text().strip():
        problems.append("design.md not written (or empty)")
        return {}
    return {"design_hash": _sha256_file(dj), "brief_source": json.loads(dj.read_text()).get("brief_source")}


def _gate_executed(sid: str, sdir: str, state: dict, problems: list[str]) -> dict:
    sd = Path(sdir)
    _verify_upstream(state, sd, "design_hash", problems)
    mj = sd / "measurements.json"
    if not mj.is_file():
        problems.append("measurements.json not written")
        return {}
    msg = _validate_artifact("measurements", mj)
    if msg:
        problems.append(f"measurements.json invalid:\n    {msg.replace(chr(10), chr(10)+'    ')}")
        return {}
    meas = json.loads(mj.read_text())
    if meas.get("design_hash") != state.get("design_hash"):
        problems.append(
            "measurements.design_hash does not echo the checkpointed design_hash "
            f"(got {meas.get('design_hash')!r}) — the executor ran against a different "
            "design; re-dispatch it with the current design_hash"
        )
    # Coverage: every design criterion field must have a measured value.
    design = json.loads((sd / "design.json").read_text())
    values = meas.get("values", {})
    missing = [c.get("field") for c in design.get("criteria", []) if c.get("field") not in values]
    if missing:
        problems.append(
            "measurements.values is missing a value for criterion field(s): "
            + ", ".join(repr(m) for m in missing)
        )
    if problems:
        return {}
    return {"measurements_hash": _sha256_file(mj)}


def _gate_decided(sid: str, sdir: str, state: dict, problems: list[str]) -> dict:
    sd = Path(sdir)
    _verify_upstream(state, sd, "measurements_hash", problems)
    cj = sd / "decision.json"
    if not cj.is_file():
        problems.append("decision.json not written (run spike-decide.py <id> first)")
        return {}
    msg = _validate_artifact("decision", cj)
    if msg:
        problems.append(f"decision.json invalid:\n    {msg.replace(chr(10), chr(10)+'    ')}")
        return {}
    dec = json.loads(cj.read_text())
    if dec.get("design_hash") != state.get("design_hash"):
        problems.append("decision.design_hash does not echo the checkpointed design_hash")
    if dec.get("measurements_hash") != state.get("measurements_hash"):
        problems.append("decision.measurements_hash does not echo the checkpointed measurements_hash")
    if problems:
        return {}
    return {"decision_hash": _sha256_file(cj), "verdict": dec.get("verdict")}


def _gate_written(sid: str, sdir: str, state: dict, problems: list[str]) -> dict:
    sd = Path(sdir)
    _verify_upstream(state, sd, "decision_hash", problems)
    nt = sd / "note.md"
    if not nt.is_file() or not nt.read_text().strip():
        problems.append("note.md not written (or empty)")
    if problems:
        return {}
    return {"note_hash": _sha256_file(nt)}


def _gate_reviewed(sid: str, sdir: str, state: dict, problems: list[str]) -> dict:
    sd = Path(sdir)
    _verify_upstream(state, sd, "note_hash", problems)
    rj = sd / "review.json"
    if not rj.is_file():
        problems.append("review.json not written")
        return {}
    msg = _validate_artifact("review", rj)
    if msg:
        problems.append(f"review.json invalid:\n    {msg.replace(chr(10), chr(10)+'    ')}")
        return {}
    rev = json.loads(rj.read_text())
    if rev.get("decision_hash") != state.get("decision_hash"):
        problems.append(
            "review.decision_hash does not echo the checkpointed decision_hash — the "
            "review is of a stale decision; re-review the current one"
        )
    if rev.get("note_hash") != state.get("note_hash"):
        problems.append(
            "review.note_hash does not echo the checkpointed note_hash — the review is "
            "of a stale note (the classic RE-RUN-skipped-the-writer bug); regenerate the note"
        )
    if problems:
        return {}
    return {"review_hash": _sha256_file(rj), "review_verdict": rev.get("verdict")}


def _gate_complete(sid: str, sdir: str, state: dict, problems: list[str]) -> dict:
    sd = Path(sdir)
    _verify_upstream(state, sd, "review_hash", problems)
    if state.get("review_verdict") != "ACCEPT":
        problems.append(
            f"reviewer verdict is {state.get('review_verdict')!r}, not ACCEPT — a spike "
            "reaches 'complete' only on ACCEPT; use --rerun / --reconsider / --terminal"
        )
    if state.get("verdict") not in NOTE_VERDICTS:
        # This is the sealed-corpus fix: an ACCEPT can never carry a null /
        # empty / off-vocabulary verdict, because the verdict is the validated
        # enum spike-decide.py derived.
        problems.append(
            f"spike verdict is {state.get('verdict')!r}, not one of {sorted(NOTE_VERDICTS)} "
            "— cannot accept a spike without a canonical derived verdict"
        )
    if problems:
        return {}
    return {"terminal_status": "accept", "verdict": state["verdict"]}


GATES = {
    "designed": _gate_designed,
    "executed": _gate_executed,
    "decided": _gate_decided,
    "written": _gate_written,
    "reviewed": _gate_reviewed,
    "complete": _gate_complete,
}


# --------------------------------------------------------------- operations


def do_init(sid: str, roadmap_path: str | None, brief_source: str | None) -> None:
    sp = _state_path(sid)
    sp.parent.mkdir(parents=True, exist_ok=True)
    with _locked(sp):
        if sp.exists():
            state = _load(sp)
            print(f"{sid}: state.json already exists (phase={state['phase']}, attempt={state['attempt']})")
            return
        state = _skeleton(sid, roadmap_path, brief_source)
        _save_atomic(sp, state)
    print(f"{sid}: initialized state.json (phase=init, attempt=1)")


def advance(sid: str, new_phase: str) -> None:
    if new_phase not in PHASE_ORDER:
        sys.exit(f"unknown phase: {new_phase}. Valid: {', '.join(PHASE_ORDER)}")
    if new_phase == "init":
        sys.exit("cannot advance to 'init' — use --init to create, --rerun to restart")
    sp = _state_path(sid)
    sdir = str(sp.parent)
    emit_after: dict | None = None
    with _locked(sp):
        state = _load(sp)
        if state.get("terminal_status"):
            sys.exit(f"{sid} is already terminal ({state['terminal_status']}) — no further advance")
        cur = state["phase"]
        cur_idx, new_idx = PHASE_ORDER.index(cur), PHASE_ORDER.index(new_phase)
        if new_idx <= cur_idx:
            sys.exit(f"refusing backward/same transition: {cur} -> {new_phase}")
        if new_idx - cur_idx > 1:
            sys.exit(
                f"refusing skipped transition: {cur} -> {new_phase} "
                "(advance one step at a time)"
            )
        problems: list[str] = []
        updates = GATES[new_phase](sid, sdir, state, problems)
        if problems:
            sys.exit(
                f"refusing transition to {new_phase}:\n  - "
                + "\n  - ".join(problems)
                + "\n(this is the evidence gate — fix the artifact, do not edit state.json)"
            )
        now = _now()
        state.update(updates)
        state["phase"] = new_phase
        state["updated_at"] = now
        state["phase_history"].append({"phase": new_phase, "at": now})
        _save_atomic(sp, state)
        if new_phase == "complete" and not state.get("outcome_emitted"):
            emit_after = _mark_emitted(sp, state)
    if emit_after is not None:
        _emit_outcome(emit_after)
    print(f"{sid}: {cur} -> {new_phase} @ {_now()}")


def _reset_fields() -> dict:
    return {
        "design_hash": None,
        "measurements_hash": None,
        "decision_hash": None,
        "note_hash": None,
        "review_hash": None,
        "verdict": None,
        "review_verdict": None,
    }


def do_loop(sid: str, kind: str) -> None:
    """kind in rerun|reconsider|deviation."""
    sp = _state_path(sid)
    with _locked(sp):
        state = _load(sp)
        if state.get("terminal_status"):
            sys.exit(f"{sid} is already terminal ({state['terminal_status']}) — cannot loop")
        now = _now()
        if kind == "reconsider":
            if state["reconsider_count"] >= RECONSIDER_CAP:
                sys.exit(f"RECONSIDER cap ({RECONSIDER_CAP}) reached — surface to user (--terminal reconsider-cap)")
            state["reconsider_count"] += 1
            # keep design/measurements/decision + verdict; drop note + review only
            state["note_hash"] = None
            state["review_hash"] = None
            state["review_verdict"] = None
            state["phase"] = "decided"
            event = {"kind": "reconsider", "attempt": state["attempt"], "at": now}
        else:
            cap = RERUN_CAP if kind == "rerun" else DEVIATION_CAP
            counter = "rerun_count" if kind == "rerun" else "deviation_count"
            if state[counter] >= cap:
                short = "rerun-cap" if kind == "rerun" else "design-deviation-cap"
                sys.exit(f"{kind.upper()} cap ({cap}) reached — surface to user (--terminal {short})")
            state[counter] += 1
            state["attempt"] += 1
            state.update(_reset_fields())
            state["phase"] = "init"
            event = {"kind": kind, "attempt": state["attempt"], "at": now}
        state["updated_at"] = now
        state["attempt_history"].append(event)
        _save_atomic(sp, state)
    print(f"{sid}: {kind} -> phase={state['phase']} attempt={state['attempt']} "
          f"(rerun={state['rerun_count']} reconsider={state['reconsider_count']} deviation={state['deviation_count']})")


def _mark_emitted(sp: Path, state: dict) -> dict:
    """Flip outcome_emitted under the held lock; return the fields to emit.
    Caller emits AFTER releasing the lock (never hold the lock across a subprocess)."""
    state["outcome_emitted"] = True
    state["updated_at"] = _now()
    _save_atomic(sp, state)
    return {
        "id": state["spike_id"],
        "verdict": state.get("verdict") or "",
        "review_verdict": state.get("review_verdict") or "",
        "status": state.get("terminal_status") or "",
    }


def _emit_outcome(fields: dict) -> None:
    """Best-effort, at-most-once outcome capture. Never raises; a test hook skips
    the real subprocess so self-tests don't touch the shared log."""
    if os.environ.get("SPIKE_CHECKPOINT_NO_EMIT") == "1":
        return
    if not _OUTCOME_LOG.is_file():
        return
    try:
        subprocess.run(
            [
                sys.executable, str(_OUTCOME_LOG), "emit", "--pipeline", "spike",
                "--id", fields["id"],
                "--field", f"spike_review_verdict={fields['review_verdict']}",
                "--field", f"verdict={fields['verdict']}",
                "--field", f"status={fields['status']}",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # best-effort capture — never break the terminal
        print(f"WARN: outcome emit failed (non-fatal): {exc}", file=sys.stderr)


def do_terminal(sid: str, status: str) -> None:
    if status not in TERMINAL_STATUSES:
        sys.exit(f"unknown terminal status {status!r}. Valid: {', '.join(sorted(TERMINAL_STATUSES))}")
    sp = _state_path(sid)
    emit_after: dict | None = None
    with _locked(sp):
        state = _load(sp)
        prior = state.get("terminal_status")
        if prior and prior != status:
            sys.exit(
                f"{sid} is already terminal as {prior!r}; refusing to overwrite with {status!r}"
            )
        if status == "skip-review":
            if state.get("verdict") not in NOTE_VERDICTS:
                sys.exit(
                    "skip-review requires a canonical derived verdict (run the writer path "
                    "through 'decided'/'written' first) — got "
                    f"{state.get('verdict')!r}"
                )
            state["skipped_review"] = True
        state["terminal_status"] = status
        state["updated_at"] = _now()
        _save_atomic(sp, state)
        if not state.get("outcome_emitted"):
            emit_after = _mark_emitted(sp, state)
    if emit_after is not None:
        _emit_outcome(emit_after)
    print(f"{sid}: terminal={status}")


def get_field(sid: str, field: str) -> None:
    state = _load(_state_path(sid))
    if field not in state:
        sys.exit(f"unknown field: {field}. Valid: {', '.join(state.keys())}")
    val = state[field]
    if isinstance(val, (dict, list)):
        print(json.dumps(val, indent=2))
    elif val is None:
        print("")
    else:
        print(val)


def set_field(sid: str, expr: str) -> None:
    if "=" not in expr:
        sys.exit("--set value must be field=<json-or-string>")
    field, raw = expr.split("=", 1)
    field = field.strip()
    if field not in SETTABLE_FIELDS:
        sys.exit(
            f"refusing --set {field}: only {sorted(SETTABLE_FIELDS)} are hand-settable "
            "(phase, hashes, verdicts, counters and terminal_status are script-owned)"
        )
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        val = raw
    if not isinstance(val, str) and val is not None:
        sys.exit(f"--set {field}: expected a string, got {type(val).__name__}")
    sp = _state_path(sid)
    with _locked(sp):
        state = _load(sp)
        state[field] = val
        state["updated_at"] = _now()
        _save_atomic(sp, state)
    print(f"{sid}: set {field} = {json.dumps(val)}")


# --------------------------------------------------------------- self-test


def self_test() -> int:
    import contextlib
    import io
    import tempfile

    failures = 0
    os.environ["SPIKE_CHECKPOINT_NO_EMIT"] = "1"

    def expect(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {name}: {'ok' if ok else f'FAIL {detail}'}")
        if not ok:
            failures += 1

    def run(argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                main(["spike-checkpoint.py", *argv])
                rc = 0
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                if not isinstance(exc.code, int) and exc.code is not None:
                    out.write(str(exc.code) + "\n")
        return rc, out.getvalue()

    SID = "demo-spike-1"

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def write_valid_design(sd: Path) -> None:
        (sd / "design.json").write_text(json.dumps({
            "schema_version": 1, "spike_id": SID, "assumption": "p95 <= 20ms",
            "brief_source": "roadmap bullet",
            "criteria": [{"name": "p95", "field": "p95_ms", "operator": "<=", "threshold": 20, "unit": "ms"}],
            "sample_size": 1000, "sample_justification": "stable p95",
            "confounds": [{"confound": "a", "control": "x"}, {"confound": "b", "control": "y"},
                          {"confound": "c", "control": "z"}],
            "measurement_fields": ["p95_ms"],
            "poc_constraints": {"language": "python3-stdlib", "max_loc": 200, "dependencies": []},
        }))
        (sd / "design.md").write_text("# Design\nassumption etc.\n")

    def write_measurements(sd: Path, design_hash: str, val: float = 14.2) -> None:
        (sd / "measurements.json").write_text(json.dumps({
            "schema_version": 1, "spike_id": SID, "design_hash": design_hash,
            "executed_at": "2026-07-10T00:00:00Z", "poc_command": "python3 poc/b.py",
            "iterations": 1, "sample_count": 1000, "values": {"p95_ms": val},
        }))

    def write_decision(sd: Path, dh: str, mh: str, verdict: str, result: str) -> None:
        (sd / "decision.json").write_text(json.dumps({
            "schema_version": 1, "spike_id": SID, "design_hash": dh, "measurements_hash": mh,
            "verdict": verdict, "derived_at": "t",
            "per_criterion": [{"name": "p95", "field": "p95_ms", "operator": "<=",
                               "threshold": 20, "unit": "ms", "measured": 14.2, "result": result}],
        }))

    def write_review(sd: Path, dh: str, nh: str, verdict: str) -> None:
        (sd / "review.json").write_text(json.dumps({
            "schema_version": 1, "spike_id": SID, "decision_hash": dh, "note_hash": nh,
            "reviewer_independent_verdict": "YES",
            "axes": {"design_validity": "sound", "sample_size": "sound", "confound": "sound",
                     "methodology": "sound", "decision_validity": "sound", "implications": "sound"},
            "verdict": verdict, "reviewed_at": "t",
        }))

    print("self-test: spike-checkpoint.py")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        os.environ["REPO_ROOT"] = str(root)
        sd = root / ".claude" / "notes" / "spikes" / SID
        sd.mkdir(parents=True)

        rc, out = run([SID, "--init"])
        expect("init creates state.json", rc == 0 and (sd / "state.json").is_file())
        rc, out = run([SID, "designed"])
        expect("designed refused w/o design.json", rc != 0 and "design.json not written" in out)

        write_valid_design(sd)
        rc, out = run([SID, "executed"])
        expect("skipped transition refused (init->executed)", rc != 0 and "skipped" in out)
        rc, out = run([SID, "designed"])
        expect("designed passes with valid design", rc == 0, out.strip()[:120])
        rc, out = run([SID, "designed"])
        expect("same transition refused", rc != 0 and "backward/same" in out)

        # out-of-band edit to design.json is caught at the next advance
        dh = sha(sd / "design.json")
        write_measurements(sd, dh)
        (sd / "design.json").write_text((sd / "design.json").read_text() + "\n")  # tamper
        rc, out = run([SID, "executed"])
        expect("out-of-band design edit refuses executed", rc != 0 and "changed on disk" in out)
        write_valid_design(sd)  # restore -> deterministic identical bytes -> hash re-matches stored

        # wrong echoed design_hash in measurements
        write_measurements(sd, "0" * 64)
        rc, out = run([SID, "executed"])
        expect("executed refused on mismatched echoed design_hash", rc != 0 and "does not echo" in out)
        write_measurements(sd, dh)
        rc, out = run([SID, "executed"])
        expect("executed passes", rc == 0, out.strip()[:160])
        mh = sha(sd / "measurements.json")

        # decided: decision.json with valid hashes
        write_decision(sd, dh, mh, "YES", "pass")
        rc, out = run([SID, "decided"])
        expect("decided passes; verdict stored", rc == 0)
        rc, out = run([SID, "--get", "verdict"])
        expect("verdict is YES", out.strip() == "YES")
        dch = sha(sd / "decision.json")

        # written
        (sd / "note.md").write_text("# Note\nVerdict cited from decision.json\n")
        rc, out = run([SID, "written"])
        expect("written passes", rc == 0)
        nh = sha(sd / "note.md")

        # reviewed with a stale note_hash echo (the RE-RUN-skipped-writer bug)
        write_review(sd, dch, "0" * 64, "ACCEPT")
        rc, out = run([SID, "reviewed"])
        expect("reviewed refused on stale note_hash echo", rc != 0 and "stale note" in out)
        write_review(sd, dch, nh, "RE-RUN")
        rc, out = run([SID, "reviewed"])
        expect("reviewed passes; review_verdict stored", rc == 0)

        # complete refused on non-ACCEPT review
        rc, out = run([SID, "complete"])
        expect("complete refused when review_verdict != ACCEPT", rc != 0 and "not ACCEPT" in out)

        # RE-RUN resets to init and clears downstream hashes + verdict
        rc, out = run([SID, "--rerun"])
        expect("rerun resets to init, attempt=2", rc == 0 and "phase=init" in out and "attempt=2" in out)
        rc, out = run([SID, "--get", "verdict"])
        expect("rerun cleared verdict", out.strip() == "")

        # walk attempt 2 to ACCEPT
        write_valid_design(sd); run([SID, "designed"]); dh = sha(sd / "design.json")
        write_measurements(sd, dh); run([SID, "executed"]); mh = sha(sd / "measurements.json")
        write_decision(sd, dh, mh, "YES", "pass"); run([SID, "decided"]); dch = sha(sd / "decision.json")
        (sd / "note.md").write_text("# Note v2\n"); run([SID, "written"]); nh = sha(sd / "note.md")
        write_review(sd, dch, nh, "ACCEPT"); run([SID, "reviewed"])
        rc, out = run([SID, "complete"])
        expect("complete passes on ACCEPT + canonical verdict", rc == 0, out.strip()[:160])
        rc, out = run([SID, "--get", "terminal_status"])
        expect("terminal_status = accept", out.strip() == "accept")
        rc, out = run([SID, "--get", "outcome_emitted"])
        expect("outcome marked emitted exactly once", out.strip() == "True")
        rc, out = run([SID, "designed"])
        expect("no advance after terminal", rc != 0 and "already terminal" in out)

        # --- fresh spike: canonical-verdict gate can't be bypassed ---
        SID2 = "gap-spike-1"
        sd2 = root / ".claude" / "notes" / "spikes" / SID2
        sd2.mkdir(parents=True)

        def wd(sd, sid):
            (sd / "design.json").write_text(json.dumps({
                "schema_version": 1, "spike_id": sid, "assumption": "a",
                "criteria": [{"name": "p95", "field": "p95_ms", "operator": "<=", "threshold": 20, "unit": "ms"}],
                "sample_size": 10, "sample_justification": "j",
                "confounds": [{"confound": "a", "control": "x"}, {"confound": "b", "control": "y"},
                              {"confound": "c", "control": "z"}],
                "measurement_fields": ["p95_ms"],
                "poc_constraints": {"language": "python3-stdlib", "max_loc": 200},
            }))
            (sd / "design.md").write_text("# d\n")

        # rewrite fixtures for SID2
        orig_write_design = write_valid_design
        run([SID2, "--init"]); wd(sd2, SID2); run([SID2, "designed"]); dh2 = sha(sd2 / "design.json")
        (sd2 / "measurements.json").write_text(json.dumps({
            "schema_version": 1, "spike_id": SID2, "design_hash": dh2, "executed_at": "t",
            "values": {"p95_ms": 14.0}}))
        run([SID2, "executed"]); mh2 = sha(sd2 / "measurements.json")
        # hand-crafted decision with a tampered non-canonical verdict must fail validation at 'decided'
        (sd2 / "decision.json").write_text(json.dumps({
            "schema_version": 1, "spike_id": SID2, "design_hash": dh2, "measurements_hash": mh2,
            "verdict": "GO-ephemeral", "derived_at": "t",
            "per_criterion": [{"name": "p95", "field": "p95_ms", "operator": "<=", "threshold": 20,
                               "unit": "ms", "measured": 14.0, "result": "pass"}]}))
        rc, out = run([SID2, "decided"])
        expect("decided refuses non-canonical decision verdict (GO-ephemeral)",
               rc != 0 and "invalid" in out)

        # reconsider cap
        SID3 = "recon-spike-1"
        sd3 = root / ".claude" / "notes" / "spikes" / SID3
        sd3.mkdir(parents=True)
        run([SID3, "--init"]); wd(sd3, SID3); run([SID3, "designed"]); dh3 = sha(sd3 / "design.json")
        (sd3 / "measurements.json").write_text(json.dumps({
            "schema_version": 1, "spike_id": SID3, "design_hash": dh3, "executed_at": "t",
            "values": {"p95_ms": 14.0}}))
        run([SID3, "executed"]); mh3 = sha(sd3 / "measurements.json")
        (sd3 / "decision.json").write_text(json.dumps({
            "schema_version": 1, "spike_id": SID3, "design_hash": dh3, "measurements_hash": mh3,
            "verdict": "YES", "derived_at": "t",
            "per_criterion": [{"name": "p95", "field": "p95_ms", "operator": "<=", "threshold": 20,
                               "unit": "ms", "measured": 14.0, "result": "pass"}]}))
        run([SID3, "decided"])
        rc, o1 = run([SID3, "--reconsider"]); rc, o2 = run([SID3, "--reconsider"])
        rc, o3 = run([SID3, "--reconsider"])
        expect("reconsider capped at 2", "cap (2) reached" in o3)
        rc, out = run([SID3, "--get", "phase"])
        expect("reconsider left phase at decided", out.strip() == "decided")

        # --set is restricted; path-traversal id refused
        rc, out = run([SID3, "--set", "phase=complete"])
        expect("--set refuses script-owned field", rc != 0 and "script-owned" in out)
        rc, out = run(["../evil-spike-1", "--get", "phase"])
        expect("path-traversal id refused", rc != 0 and "invalid spike id" in out)

    del os.environ["REPO_ROOT"]
    del os.environ["SPIKE_CHECKPOINT_NO_EMIT"]
    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


def main(argv: list[str]) -> None:
    if "--self-test" in argv:
        raise SystemExit(self_test())
    if len(argv) < 3:
        sys.exit(__doc__)
    sid, second = argv[1], argv[2]
    if second == "--init":
        roadmap_path = brief_source = None
        i = 3
        while i < len(argv):
            if argv[i] == "--roadmap-path" and i + 1 < len(argv):
                roadmap_path = argv[i + 1]; i += 2
            elif argv[i] == "--brief-source" and i + 1 < len(argv):
                brief_source = argv[i + 1]; i += 2
            else:
                sys.exit(f"unknown --init arg: {argv[i]}")
        do_init(sid, roadmap_path, brief_source)
    elif second == "--get":
        if len(argv) < 4:
            sys.exit("--get requires a field name")
        get_field(sid, argv[3])
    elif second == "--set":
        if len(argv) < 4:
            sys.exit("--set requires field=value")
        set_field(sid, argv[3])
    elif second == "--rerun":
        do_loop(sid, "rerun")
    elif second == "--reconsider":
        do_loop(sid, "reconsider")
    elif second == "--deviation":
        do_loop(sid, "deviation")
    elif second == "--terminal":
        if len(argv) < 4:
            sys.exit("--terminal requires a status")
        do_terminal(sid, argv[3])
    else:
        advance(sid, second)


if __name__ == "__main__":
    main(sys.argv)

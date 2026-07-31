#!/usr/bin/env python3
"""pipeline-outcome-log — capture per-run pipeline outcomes as an append-only JSONL log.

This is the FOUNDATIONAL capture layer for the self-improving-tooling loop (E1 / KR1).
It is CAPTURE ONLY: it appends one labelled outcome record per pipeline run at that
pipeline's terminal/`complete` transition. It does NOT learn, feed back, or mutate any
pipeline behavior. The JSONL log it writes is the dataset later milestones (E3 learning,
E5 sealed-eval) read.

Hard guarantees (each test-asserted in pipeline-outcome-log-test.py):
  (a) CONCURRENCY  — two runs reaching `complete` at once produce two intact lines, never
                     torn/interleaved. Mechanism: O_APPEND + a single os.write() of one
                     compact (< PIPE_BUF) line. POSIX guarantees a <= PIPE_BUF write to an
                     O_APPEND fd is atomic w.r.t. concurrent appenders.
  (b) NON-BLOCKING — a malformed/partial/absent state.json emits a record with missing
                     fields nulled and NEVER aborts the host pipeline's terminal transition.
                     The entire emit body is wrapped: ANY internal error is swallowed, logged
                     to stderr, and the process exits 0 (best-effort capture).
  (c) APPEND-ONLY  — N runs -> exactly N records, never clobbered (no read-modify-write).

Usage:
  # Families with a state.json (milestone, discovery x6): read the state file + map keys.
  pipeline-outcome-log.py emit --pipeline milestone --id <ID> --state <path/to/state.json>
  pipeline-outcome-log.py emit --pipeline capability-scout --id <ID> --state <path>

  # Families with no state.json (spike, roadmap, argoops): pass scalars via --field.
  pipeline-outcome-log.py emit --pipeline spike --id <ID> \
      --field spike_review_verdict=ACCEPT --field verdict=yes --field status=accept

  # --state and --field compose: state.json fills what it has, --field overrides/fills rest.

  # Read/summary entrypoint (the surface E3/E5 read later):
  pipeline-outcome-log.py summary [--pipeline <fam>] [--last N] [--json] [--log <path>]

Log-path resolution (first match wins):
  1. --log <path>
  2. $PIPELINE_OUTCOME_LOG env var
  3. <repo-root>/.claude/notes/pipeline-outcomes/outcomes.jsonl
     (.claude/notes/ is gitignored by ensure-claude-gitignore.sh in every target repo,
      so the log is local-only by construction — satisfies KR1 "never pushed".)

Repo-root detection mirrors milestone-pipeline-checkpoint.py:
  1. $REPO_ROOT  2. $PLATFORM_ROOT  3. `git rev-parse --show-toplevel`  4. walk up to .git/
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Schema version of the emitted record. Bump rule:
#   additive nullable field   -> NO bump (readers use dict.get)
#   renamed/removed/retyped    -> bump + summary() switches on schema_version
SCHEMA_VERSION = 1

# POSIX guarantees a write() <= PIPE_BUF to an O_APPEND fd is atomic w.r.t. concurrent
# appenders. PIPE_BUF is >= 512 by POSIX and 4096 on Linux/macOS. We keep the compact
# JSONL line well under this and assert it before the single-write append.
PIPE_BUF = 4096

# The full, stable column set. Every record carries every key; a family that lacks a field
# emits it as null (NOT omitted) so downstream readers (jq / pandas / dict.get) see a stable
# schema. Order here is the emission order (schema_version first for forward-compat).
RECORD_FIELDS = [
    "schema_version",
    "pipeline",
    "id",
    "run_id",
    "status",
    "verdict",
    "rectification_count",
    "rectification_commit",
    "critique_critical",
    "critique_high",
    "critique_medium",
    "critique_low",
    "candidate_count",
    "spike_review_verdict",
    "token_cost",
    "started_at",
    "completed_at",
    "emitted_at",
    "source_state_path",
]

# Identifier/string-typed columns that must NEVER be numerically coerced from a --field.
# A hex commit SHA like `10365e3` is valid JSON scientific-notation and would silently
# become the float 10365000.0, corrupting the value (caught by sealed-eval-spike-1).
# These keys keep their raw --field string verbatim; only the numeric/structured columns
# (critique_*, rectification_count, candidate_count, token_cost, schema_version) JSON-parse.
STRING_FIELDS = frozenset({
    "id",
    "run_id",
    "status",
    "verdict",
    "rectification_commit",
    "spike_review_verdict",
    "started_at",
    "completed_at",
    "emitted_at",
    "source_state_path",
})


# --------------------------------------------------------------------------- #
# Repo-root + log-path resolution
# --------------------------------------------------------------------------- #


def _find_repo_root():
    # type: () -> Path
    for env_key in ("REPO_ROOT", "PLATFORM_ROOT"):
        val = os.environ.get(env_key)
        if val:
            p = Path(val)
            if p.is_dir():
                return p
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / ".git").exists():
            return candidate
    # Last resort: cwd. emit() swallows any downstream failure, so this never aborts a host.
    return Path.cwd()


def _resolve_log_path(explicit):
    # type: (str | None) -> Path
    if explicit:
        return Path(explicit)
    env = os.environ.get("PIPELINE_OUTCOME_LOG")
    if env:
        return Path(env)
    return (
        _find_repo_root() / ".claude" / "notes" / "pipeline-outcomes" / "outcomes.jsonl"
    )


def _now():
    # type: () -> str
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Field mapping
# --------------------------------------------------------------------------- #


def _safe_load_state(state_path):
    # type: (str | None) -> dict
    """Best-effort load of a pipeline state.json. Returns {} on absent/empty/malformed.

    NEVER raises — a malformed state file must not abort the host pipeline's terminal
    transition. The caller fills what it could read and nulls the rest.
    """
    if not state_path:
        return {}
    try:
        p = Path(state_path)
        if not p.exists():
            sys.stderr.write(
                "pipeline-outcome-log: state file not found: %s (emitting nulled record)\n"
                % state_path
            )
            return {}
        text = p.read_text(encoding="utf-8")
        if not text.strip():
            sys.stderr.write(
                "pipeline-outcome-log: state file empty: %s (emitting nulled record)\n"
                % state_path
            )
            return {}
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            sys.stderr.write(
                "pipeline-outcome-log: state file is not a JSON object: %s\n"
                % state_path
            )
            return {}
        return loaded
    except Exception as exc:  # noqa: BLE001 — capture must never raise on a bad state file
        sys.stderr.write(
            "pipeline-outcome-log: could not parse state file %s: %s (emitting nulled record)\n"
            % (state_path, exc)
        )
        return {}


def _counts(state, key):
    # type: (dict, str) -> dict
    """Safely pull a {critical,high,medium,low} sub-dict; tolerate absence/wrong type."""
    sub = state.get(key)
    return sub if isinstance(sub, dict) else {}


def _coerce_field_value(raw):
    # type: (str) -> object
    """Parse a --field value as JSON, falling back to the plain string.

    Same convention as milestone-pipeline-checkpoint.py --set. An empty string stays "".
    """
    if raw == "":
        return ""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def build_record(pipeline, mid, state_path, overrides):
    # type: (str, str, str | None, dict) -> dict
    """Assemble one outcome record from (pipeline, id, optional state.json, --field overrides).

    Field -> source map (see data/references/pipeline-outcome-log-schema.md):
      milestone (A):   critique_* from critique_finding_counts; rectification_count from
                       len(fixed_findings); rectification_commit; started/completed from
                       created_at/updated_at.
      discovery (B):   critique_* from challenge_finding_counts; candidate_count.
      spike/roadmap/argoops (C): no state.json -> scalars come from --field overrides.
    """
    state = _safe_load_state(state_path)

    critique = _counts(state, "critique_finding_counts")
    challenge = _counts(state, "challenge_finding_counts")

    # Severity counts: milestone uses critique_finding_counts; discovery uses
    # challenge_finding_counts. Prefer whichever is present (they are mutually exclusive
    # across the two families); fall back to None.
    def sev(level):
        if level in critique:
            return critique.get(level)
        if level in challenge:
            return challenge.get(level)
        return None

    # rectification_count: milestone state.json has no integer count. LOCKED DECISION
    # (asserted in a test): derive len(fixed_findings). Only milestone carries it.
    fixed = state.get("fixed_findings")
    rectification_count = len(fixed) if isinstance(fixed, list) else None

    record = {
        "schema_version": SCHEMA_VERSION,
        "pipeline": pipeline,
        "id": mid,
        "run_id": None,
        # status defaults to the live phase if the state file carries one, else "complete".
        "status": state.get("phase") or "complete",
        "verdict": None,
        "rectification_count": rectification_count,
        "rectification_commit": state.get("rectification_commit"),
        "critique_critical": sev("critical"),
        "critique_high": sev("high"),
        "critique_medium": sev("medium"),
        "critique_low": sev("low"),
        "candidate_count": state.get("candidate_count"),
        "spike_review_verdict": None,
        # token_cost is intentionally null in M1 (E4's OTel job); the key exists so E4 can
        # backfill without a schema bump. If state.json ever carries it, read it.
        "token_cost": state.get("token_cost"),
        "started_at": state.get("created_at"),
        "completed_at": state.get("updated_at"),
        "emitted_at": _now(),
        "source_state_path": state_path,
    }

    # --field overrides win over state-derived values (and supply the family-C scalars
    # that have no state.json at all). Identifier/string columns keep their raw string
    # verbatim (a numeric-looking SHA must not JSON-coerce to a float — see STRING_FIELDS).
    for key, raw in overrides.items():
        record[key] = raw if key in STRING_FIELDS else _coerce_field_value(raw)

    # Keep only known columns in canonical order (drop any stray --field key, stay stable).
    return {k: record.get(k) for k in RECORD_FIELDS}


# --------------------------------------------------------------------------- #
# Atomic, non-blocking append
# --------------------------------------------------------------------------- #


def _append_line(log_path, line_bytes):
    # type: (Path, bytes) -> None
    """Append one line via O_APPEND single write(). Atomic for lines < PIPE_BUF.

    Fallback for a pathological oversized record: an advisory flock around the write so the
    atomicity guarantee never silently lapses (a torn line would corrupt the dataset).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        if len(line_bytes) < PIPE_BUF:
            os.write(fd, line_bytes)  # single atomic append (no buffering, one syscall)
        else:
            # Oversized line: O_APPEND atomicity is not guaranteed above PIPE_BUF. Take an
            # advisory exclusive lock so concurrent writers can't interleave the chunks.
            sys.stderr.write(
                "pipeline-outcome-log: record >= PIPE_BUF (%d bytes); using flock fallback\n"
                % len(line_bytes)
            )
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    os.write(fd, line_bytes)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                # No flock available (rare): best-effort single write; still one syscall.
                os.write(fd, line_bytes)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# Best-effort metric emit (E4 — self-improving-tooling-m4)
# --------------------------------------------------------------------------- #

# Bucket width for the run_id cardinality bound. 4 hex chars = sha256(run_id)[:4]
# = 65536 buckets max. Chosen over an unbounded run_id label (which explodes
# Prometheus series at production run-volume) and over OpenMetrics exemplars
# (Thanos does NOT federate exemplars — confirmed in the m4 cardinality brief).
# The JSONL record remains the AUTHORITATIVE exact run_id join key; this bounded
# label is for Grafana/Thanos trend exploration only. Locked decision (m4).
_RUN_BUCKET_HEX = 4


def emit(pipeline, mid, state_path, overrides, log_path):
    # type: (str, str, str | None, dict, str | None) -> int
    """Build + append one outcome record. NON-BLOCKING: returns 0 even on internal error.

    The entire body is wrapped so that capture is best-effort and a writer failure can never
    be the reason a host pipeline's terminal transition fails.
    """
    try:
        record = build_record(pipeline, mid, state_path, overrides)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        path = _resolve_log_path(log_path)
        _append_line(path, data)
        return 0
    except Exception as exc:  # noqa: BLE001 — capture is best-effort; never abort the host
        sys.stderr.write("pipeline-outcome-log: emit failed (swallowed): %s\n" % exc)
        return 0


# --------------------------------------------------------------------------- #
# Read / summary
# --------------------------------------------------------------------------- #


def read_records(log_path):
    # type: (Path) -> tuple[list, int]
    """Read all parseable records; tolerate (and count) malformed lines."""
    records = []
    malformed = 0
    if not log_path.exists():
        return records, malformed
    with open(str(log_path), "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    records.append(obj)
                else:
                    malformed += 1
            except (json.JSONDecodeError, ValueError):
                malformed += 1
    return records, malformed


def summary(pipeline, last, as_json, log_path):
    # type: (str | None, int | None, bool, str | None) -> int
    path = _resolve_log_path(log_path)
    records, malformed = read_records(path)
    if pipeline:
        records = [r for r in records if r.get("pipeline") == pipeline]

    if as_json:
        out = {"records": records, "malformed_lines": malformed, "total": len(records)}
        print(json.dumps(out, indent=2))
        return 0

    print("pipeline-outcome-log summary  (%s)" % path)
    print("  total records:   %d" % len(records))
    print("  malformed lines: %d" % malformed)

    by_pipeline = {}
    by_status = {}
    by_verdict = {}
    for r in records:
        by_pipeline[r.get("pipeline")] = by_pipeline.get(r.get("pipeline"), 0) + 1
        by_status[r.get("status")] = by_status.get(r.get("status"), 0) + 1
        # "by verdict" groups on the pipeline's OWN verdict (spike YES/NO/UNCERTAIN, etc.),
        # NOT the reviewer gate (spike_review_verdict). A spike with verdict=NO +
        # spike_review_verdict=ACCEPT must count under NO.
        v = r.get("verdict")
        if v is not None:
            by_verdict[v] = by_verdict.get(v, 0) + 1

    print("  by pipeline:")
    for k in sorted(by_pipeline, key=lambda x: str(x)):
        print("    %-20s %d" % (k, by_pipeline[k]))
    print("  by status:")
    for k in sorted(by_status, key=lambda x: str(x)):
        print("    %-20s %d" % (k, by_status[k]))
    if by_verdict:
        print("  by verdict:")
        for k in sorted(by_verdict, key=lambda x: str(x)):
            print("    %-20s %d" % (k, by_verdict[k]))

    if last:
        print("  last %d:" % last)
        for r in records[-last:]:
            print(
                "    %s  %-18s %-12s rect=%s cand=%s sev[c/h/m/l]=%s/%s/%s/%s"
                % (
                    r.get("emitted_at"),
                    r.get("pipeline"),
                    r.get("id"),
                    r.get("rectification_count"),
                    r.get("candidate_count"),
                    r.get("critique_critical"),
                    r.get("critique_high"),
                    r.get("critique_medium"),
                    r.get("critique_low"),
                )
            )
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_overrides(field_args):
    # type: (list[str]) -> dict
    overrides = {}
    for item in field_args or []:
        if "=" not in item:
            sys.stderr.write(
                "pipeline-outcome-log: ignoring malformed --field %r\n" % item
            )
            continue
        key, raw = item.split("=", 1)
        overrides[key.strip()] = raw
    return overrides


def main(argv):
    # type: (list[str]) -> int
    parser = argparse.ArgumentParser(prog="pipeline-outcome-log", add_help=True)
    sub = parser.add_subparsers(dest="command")

    p_emit = sub.add_parser(
        "emit", help="append one outcome record (best-effort, exit 0)"
    )
    p_emit.add_argument("--pipeline", required=True)
    p_emit.add_argument("--id", required=True, dest="id")
    p_emit.add_argument(
        "--state", default=None, help="path to the pipeline's state.json"
    )
    p_emit.add_argument(
        "--field", action="append", default=[], help="key=value override"
    )
    p_emit.add_argument("--log", default=None, help="override log path")

    p_sum = sub.add_parser("summary", help="read/summarize the outcome log")
    p_sum.add_argument("--pipeline", default=None)
    p_sum.add_argument("--last", type=int, default=10)
    p_sum.add_argument("--json", action="store_true", dest="as_json")
    p_sum.add_argument("--log", default=None)

    # `emit` must be exit-0-on-internal-error. Guard argparse too: a bad emit invocation
    # should still not abort a host pipeline. `summary` may exit non-zero on bad args
    # (diagnostic, off the critical path).
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        # argparse already wrote usage. If this was an emit call, swallow to exit 0.
        if len(argv) > 1 and argv[1] == "emit":
            sys.stderr.write("pipeline-outcome-log: bad emit args (swallowed)\n")
            return 0
        raise

    if args.command == "emit":
        return emit(
            args.pipeline,
            args.id,
            args.state,
            _parse_overrides(args.field),
            args.log,
        )
    if args.command == "summary":
        return summary(args.pipeline, args.last, args.as_json, args.log)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Tests for pipeline-outcome-log.py — the E1/KR1 outcome-capture writer.

Stdlib only (unittest). Run:  python3 data/scripts/pipeline-outcome-log-test.py

Covers the three hard guarantees + a per-pipeline emit smoke for all families:
  4.1 CONCURRENCY  — torn-line tests: a thread pool AND a multiprocessing fork pool (the
                     realistic cross-process O_APPEND case, no GIL) -> all lines parseable, none
                     torn; plus an oversized-line (>= PIPE_BUF) flock-fallback test.
  4.2 NON-BLOCKING — malformed/absent/partial state.json -> exit 0, record with fields nulled.
  4.3 APPEND-ONLY  — N emits -> exactly N records, order preserved, summary round-trips;
                     a hand-corrupted line is counted malformed and the good records survive.
  4.4 PER-PIPELINE — one emit per family at its ACTUAL LIVE-WIRED shape (discovery + milestone
                     counts arrive via --field, NOT a hand-written state.json the live path never
                     produces; spike forwards distinct note-verdict + reviewer-verdict + status);
                     a coverage assert that all distinct pipeline values are present.
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "pipeline-outcome-log.py"

# Import the script as a module (its filename has a dash, so use spec-from-file).
_spec = importlib.util.spec_from_file_location("pipeline_outcome_log", str(SCRIPT))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _mp_emit_worker(args):
    """Module-level (picklable) worker for the multiprocessing concurrency test.

    Each forked process re-imports nothing — it inherits the already-loaded `mod` via fork —
    and emits one record. Must be top-level so `multiprocessing` can pickle the target on
    spawn-based platforms too.
    """
    log_path, mid = args
    # Re-import inside the worker so this is robust under the 'spawn' start method (macOS
    # default on 3.8+), where the child does NOT inherit module globals.
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location("pipeline_outcome_log_w", str(SCRIPT))
    m = _ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.emit("milestone", mid, None, {}, str(log_path))
    return mid


def run_emit_subprocess(args, log_path):
    """Invoke the script as a real subprocess (proves the CLI exit-0 contract end-to-end)."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "emit", "--log", str(log_path)] + args,
        capture_output=True,
        text=True,
    )


class TestConcurrency(unittest.TestCase):
    """4.1 — concurrent appenders must produce intact, non-interleaved lines."""

    def test_append_serialises_through_the_lock_on_every_platform(self):
        """Pin the mechanism, not just the outcome.

        Both platforms serialise through a lock file so `seq` is globally
        ordered; the DATA write stays a single os.write() to an O_APPEND fd,
        which is what makes a line un-tearable. Windows is the reason the lock
        exists at all — it promises no O_APPEND atomicity and silently lost
        15-18% of records without one.
        """
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "branch.jsonl"
            mod.emit("milestone", "run-branch", None, {}, str(log))
            self.assertTrue(
                mod._lock_path(log).exists(),
                "the append must serialise through a lock file on %s" % sys.platform,
            )
            self.assertNotIn(
                "lock", log.read_text(encoding="utf-8"),
                "the lock must live in its own file, never in the data",
            )

    def test_seq_is_monotonic_and_verify_finds_a_deleted_line(self):
        """An append-only log with no ordering key cannot be checked for loss.

        `seq` gives it one: delete a line and `verify` must name the number that
        went missing, rather than reporting a shorter file as healthy.
        """
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "seq.jsonl"
            for n in range(6):
                mod.emit("milestone", "run-%d" % n, None, {}, str(log))

            seqs = [json.loads(l)["seq"] for l in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(seqs, list(range(6)), "seq must be monotonic from 0")
            self.assertEqual(mod.verify(str(log)), 0, "an intact log must verify clean")

            lines = log.read_text(encoding="utf-8").splitlines(True)
            del lines[3]
            log.write_text("".join(lines), encoding="utf-8")
            self.assertEqual(
                mod.verify(str(log)), 1, "a deleted line must fail verification"
            )

    def test_concurrent_writes_no_torn_lines(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            writers = 50
            iterations = 20
            expected_ids = set()

            def one_emit(n):
                mid = "run-%05d" % n
                # Call the in-process emit() directly across threads — it uses O_APPEND +
                # single os.write, which is the atomicity-bearing path.
                mod.emit("milestone", mid, None, {}, str(log))
                return mid

            for it in range(iterations):
                batch = [it * writers + i for i in range(writers)]
                with ThreadPoolExecutor(max_workers=writers) as pool:
                    for mid in pool.map(one_emit, batch):
                        expected_ids.add(mid)

            total_expected = writers * iterations
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                len(lines), total_expected, "line count must equal number of emits"
            )
            got_ids = set()
            for ln in lines:
                # EVERY line must independently parse — a torn/interleaved write fails here.
                obj = json.loads(ln)
                got_ids.add(obj["id"])
            self.assertEqual(
                got_ids,
                expected_ids,
                "every emitted id present exactly once, none torn",
            )

    def test_concurrent_multiprocess_writes_no_torn_lines(self):
        # The realistic case: separate /spike, /argoops, /milestone runs are separate PROCESSES,
        # not threads. Threads serialize os.write under the GIL, so the thread test above
        # under-exercises the cross-process O_APPEND atomicity the design depends on. This forks
        # a real process pool (no GIL) so N procs x M emits must yield N*M intact unique lines.
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            procs = 16
            iterations = 25
            total_expected = procs * iterations
            jobs = [(str(log), "mp-%05d" % n) for n in range(total_expected)]

            # Use an explicit fork/spawn-agnostic context; cap workers at `procs`.
            ctx = multiprocessing.get_context()
            with ctx.Pool(processes=procs) as pool:
                returned = pool.map(_mp_emit_worker, jobs)

            expected_ids = {mid for _, mid in jobs}
            self.assertEqual(len(returned), total_expected)

            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                len(lines),
                total_expected,
                "multiprocess line count must equal number of emits (no lost/torn writes)",
            )
            got_ids = set()
            for ln in lines:
                # EVERY line must independently parse — a torn cross-process write fails here.
                obj = json.loads(ln)
                got_ids.add(obj["id"])
            self.assertEqual(
                got_ids,
                expected_ids,
                "every multiprocess-emitted id present exactly once, none torn/interleaved",
            )

    def test_oversized_record_uses_lock_fallback_not_torn(self):
        # An id larger than PIPE_BUF forces the >= PIPE_BUF branch; the line must still be
        # a single intact, parseable record (flock fallback), not a corrupt partial write.
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            big_id = "x" * (mod.PIPE_BUF + 200)
            rc = mod.emit("milestone", big_id, None, {}, str(log))
            self.assertEqual(rc, 0)
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            obj = json.loads(lines[0])  # still one intact parseable line
            self.assertEqual(obj["id"], big_id)


class TestNonBlocking(unittest.TestCase):
    """4.2 — malformed/partial/absent state.json must NOT abort; exit 0, fields nulled."""

    def _assert_record_nulled_severity(self, log):
        records, malformed = mod.read_records(log)
        self.assertEqual(malformed, 0)
        self.assertEqual(len(records), 1)
        rec = records[0]
        # Unreadable severity fields are null (not crash, not 0-by-accident-of-default).
        self.assertIsNone(rec["critique_high"])
        self.assertIsNone(rec["rectification_commit"])
        return rec

    def test_absent_state_file(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            res = run_emit_subprocess(
                [
                    "--pipeline",
                    "milestone",
                    "--id",
                    "m1",
                    "--state",
                    str(Path(d) / "nope.json"),
                ],
                log,
            )
            self.assertEqual(res.returncode, 0, "absent state must not abort the host")
            self._assert_record_nulled_severity(log)

    def test_empty_state_file(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            sp = Path(d) / "state.json"
            sp.write_text("", encoding="utf-8")
            res = run_emit_subprocess(
                ["--pipeline", "milestone", "--id", "m1", "--state", str(sp)], log
            )
            self.assertEqual(res.returncode, 0)
            self._assert_record_nulled_severity(log)

    def test_invalid_json_state_file(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            sp = Path(d) / "state.json"
            sp.write_text(
                '{"id": "m1", "critique_finding_counts": {trunc',
                encoding="utf-8",
            )  # broken JSON
            res = run_emit_subprocess(
                ["--pipeline", "milestone", "--id", "m1", "--state", str(sp)], log
            )
            self.assertEqual(
                res.returncode, 0, "malformed JSON must not abort the host"
            )
            self._assert_record_nulled_severity(log)

    def test_valid_json_missing_optional_keys(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            sp = Path(d) / "state.json"
            # Valid JSON, but missing critique_finding_counts AND rectification_commit.
            sp.write_text(json.dumps({"id": "m1", "phase": "complete"}), encoding="utf-8")
            res = run_emit_subprocess(
                ["--pipeline", "milestone", "--id", "m1", "--state", str(sp)], log
            )
            self.assertEqual(res.returncode, 0)
            rec = self._assert_record_nulled_severity(log)
            self.assertEqual(
                rec["status"], "complete"
            )  # the readable field IS populated
            # rectification_count nulled when fixed_findings absent (not 0).
            self.assertIsNone(rec["rectification_count"])

    def test_bad_emit_args_still_exit_zero(self):
        # A missing required arg on `emit` must still swallow to exit 0 (never abort host).
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            res = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "emit",
                    "--log",
                    str(log),
                    "--pipeline",
                    "milestone",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, "bad emit args must not abort the host")


class TestRectificationCount(unittest.TestCase):
    """Lock the derived rectification_count semantics (LOCKED: len(fixed_findings))."""

    def test_rectification_count_is_len_fixed_findings(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            sp = Path(d) / "state.json"
            sp.write_text(
                json.dumps({"id": "m1", "fixed_findings": ["C1", "H1", "H2", "M1"]}),
                encoding="utf-8",
            )
            mod.emit("milestone", "m1", str(sp), {}, str(log))
            records, _ = mod.read_records(log)
            self.assertEqual(records[0]["rectification_count"], 4)


class TestAppendOnly(unittest.TestCase):
    """4.3 — N emits -> exactly N records; corrupt line counted, good records survive."""

    def test_n_emits_n_records_order_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            n = 12
            families = [
                "milestone",
                "spike",
                "roadmap",
                "argoops",
                "capability-scout",
                "mesh-as-code",
            ]
            for i in range(n):
                fam = families[i % len(families)]
                mod.emit(fam, "id-%02d" % i, None, {"status": "complete"}, str(log))
            records, malformed = mod.read_records(log)
            self.assertEqual(len(records), n)
            self.assertEqual(malformed, 0)
            # Order preserved (append-only, no clobber/reorder).
            self.assertEqual(
                [r["id"] for r in records], ["id-%02d" % i for i in range(n)]
            )

    def test_summary_json_roundtrips_and_counts_malformed(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            for i in range(5):
                mod.emit("milestone", "id-%d" % i, None, {}, str(log))
            # Hand-append a deliberately corrupt line.
            with open(log, "a", encoding="utf-8") as f:
                f.write("{not valid json at all\n")
            res = subprocess.run(
                [sys.executable, str(SCRIPT), "summary", "--log", str(log), "--json"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            out = json.loads(res.stdout)
            self.assertEqual(out["total"], 5, "5 good records survive the corrupt line")
            self.assertEqual(
                out["malformed_lines"], 1, "corrupt line counted, not fatal"
            )


class TestPerPipelineEmitSmoke(unittest.TestCase):
    """4.4 — one emit per family at its ACTUAL LIVE-WIRED terminal shape; signature populated.

    IMPORTANT (regression-guard intent): the live slash-command wiring does NOT hand-write a
    populated state.json for discovery/milestone counts — the discovery `*-workflow.mjs` returns
    `candidate_count`/`challenge_counts` only in its JS return object, and the milestone
    orchestrator never `--set`s `critique_finding_counts`/`fixed_findings`/`rectification_commit`.
    So the slash command passes those via `--field` (which win over `--state`). This test
    exercises THAT shape (state.json carries only what the live orchestrator persists — id,
    timestamps, phase — and the counts arrive via --field overrides), so the divergence between
    "what the emit reads from state.json" and "what the orchestrator actually writes" is caught.
    """

    def test_all_families_emit_at_live_wired_shape(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"

            # --- A. milestone: state.json carries ONLY what the live orchestrator persists
            #     (id, timestamps, phase). critique_* / rectification_* arrive via --field,
            #     mirroring milestone-pipeline.md's emit block. NOTE: deliberately NO
            #     critique_finding_counts / fixed_findings / rectification_commit in state.json.
            ms = Path(d) / "milestone-state.json"
            ms.write_text(
                json.dumps(
                    {
                        "id": "sit-m1",
                        "created_at": "2026-06-17T14:00:00Z",
                        "updated_at": "2026-06-17T15:00:00Z",
                        "phase": "complete",
                    }
                ),
                encoding="utf-8",
            )
            # --field values arrive as STRINGS from the CLI (key=value); the writer JSON-coerces
            # them. Pass strings here to faithfully exercise the live `--field` invocation shape.
            mod.emit(
                "milestone",
                "sit-m1",
                str(ms),
                {
                    "critique_critical": "0",
                    "critique_high": "3",
                    "critique_medium": "2",
                    "critique_low": "1",
                    "rectification_count": "2",
                    "rectification_commit": "deadbee",
                },
                str(log),
            )

            # --- B. discovery x6: state.json is the SEED file (candidate_count: 0, all-zero
            #     challenge counts) the live workflow never updates. The real counts arrive via
            #     --field from the workflow return object — exactly the discovery emit-block shape.
            disc_seed = Path(d) / "disc-seed-state.json"
            disc_seed.write_text(
                json.dumps(
                    {
                        "id": "disc-1",
                        "kind": "capability-scout",
                        "created_at": "2026-06-17T14:00:00Z",
                        "updated_at": "2026-06-17T14:00:00Z",
                        "phase": "init",
                        "candidate_count": 0,
                        "challenge_finding_counts": {
                            "critical": 0,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            for fam in [
                "capability-scout",
                "mesh-as-code",
                "interop-discovery",
                "cicd-uplift",
                "frontend-uplift",
                "zerotrust-scout",
            ]:
                mod.emit(
                    fam,
                    "disc-1",
                    str(disc_seed),
                    {
                        "status": "complete",
                        "candidate_count": "14",
                        "critique_critical": "1",
                        "critique_high": "2",
                        "critique_medium": "0",
                        "critique_low": "3",
                    },
                    str(log),
                )

            # --- C. spike: the emit passes values via --field (not --state). spike v2's
            #     spike-checkpoint.py, at the terminal, forwards the DERIVED verdict
            #     (--field verdict=YES/NO/UNCERTAIN, from decision.json/state) AND the reviewer
            #     gate (--field spike_review_verdict=, from review.json/state) AND a terminal
            #     --field status=, exactly-once. The two verdict signals must stay DISTINCT.
            mod.emit(
                "spike",
                "foo-spike-1",
                None,
                {
                    "spike_review_verdict": "ACCEPT",
                    "verdict": "NO",
                    "status": "accept",
                },
                str(log),
            )

            # --- A non-ACCEPT spike terminal: status set, note-verdict set, no reviewer gate
            #     (mirrors a --skip-review or cap-reached terminal).
            mod.emit(
                "spike",
                "bar-spike-1",
                None,
                {
                    "verdict": "UNCERTAIN",
                    "status": "skip-review",
                },
                str(log),
            )

            # --- roadmap: no state.json; slug id + complete status
            mod.emit(
                "roadmap",
                "my-slug",
                None,
                {
                    "status": "complete",
                    "source_state_path": "plans/my-slug-roadmap.md",
                },
                str(log),
            )

            # --- argoops: no state.json; PID-disambiguated ISO-ts id + complete status
            mod.emit(
                "argoops",
                "20260617T150000Z-4242",
                None,
                {"status": "complete"},
                str(log),
            )

            records, malformed = mod.read_records(log)
            self.assertEqual(malformed, 0)
            by_pipe = {}
            for r in records:
                by_pipe.setdefault(r["pipeline"], []).append(r)

            # Milestone: critique_* + rectification_* came via --field (NOT state.json).
            self.assertEqual(by_pipe["milestone"][0]["critique_high"], 3)
            self.assertEqual(by_pipe["milestone"][0]["critique_critical"], 0)
            self.assertEqual(by_pipe["milestone"][0]["rectification_count"], 2)
            self.assertEqual(by_pipe["milestone"][0]["rectification_commit"], "deadbee")
            self.assertIsNotNone(by_pipe["milestone"][0]["id"])

            # Each discovery family: --field counts WIN over the all-zero seed state.json.
            for fam in [
                "capability-scout",
                "mesh-as-code",
                "interop-discovery",
                "cicd-uplift",
                "frontend-uplift",
                "zerotrust-scout",
            ]:
                self.assertIn(
                    fam, by_pipe, "missing emit for discovery family %s" % fam
                )
                self.assertEqual(
                    by_pipe[fam][0]["candidate_count"],
                    14,
                    "candidate_count must come from --field, not the seed-0 state.json",
                )
                self.assertEqual(by_pipe[fam][0]["critique_critical"], 1)
                self.assertEqual(by_pipe[fam][0]["critique_high"], 2)

            # Spike: the TWO verdict signals are populated AND distinct (verdict != review).
            spike_accept = by_pipe["spike"][0]
            self.assertEqual(spike_accept["spike_review_verdict"], "ACCEPT")
            self.assertEqual(spike_accept["verdict"], "NO")
            self.assertNotEqual(
                spike_accept["verdict"],
                spike_accept["spike_review_verdict"],
                "note-verdict and reviewer-verdict must not be conflated",
            )
            self.assertEqual(spike_accept["status"], "accept")

            # Non-ACCEPT spike terminal: status + note-verdict present, reviewer gate null.
            spike_skip = by_pipe["spike"][1]
            self.assertEqual(spike_skip["status"], "skip-review")
            self.assertEqual(spike_skip["verdict"], "UNCERTAIN")
            self.assertIsNone(spike_skip["spike_review_verdict"])

            # Roadmap + argoops.
            self.assertEqual(by_pipe["roadmap"][0]["status"], "complete")
            self.assertEqual(by_pipe["argoops"][0]["status"], "complete")
            self.assertIn(
                "-", by_pipe["argoops"][0]["id"], "argoops id is PID-disambiguated"
            )

            # Coverage assert: all distinct families present (one missed wire => fails here).
            expected_families = {
                "milestone",
                "capability-scout",
                "mesh-as-code",
                "interop-discovery",
                "cicd-uplift",
                "frontend-uplift",
                "zerotrust-scout",
                "spike",
                "roadmap",
                "argoops",
            }
            self.assertEqual(set(by_pipe.keys()), expected_families)


class TestSummaryByVerdict(unittest.TestCase):
    """The summary "by verdict" grouping keys off the pipeline's OWN verdict, NOT the
    reviewer gate. Regression guard for the bug where a spike record with verdict=NO +
    spike_review_verdict=ACCEPT was grouped under ACCEPT."""

    @staticmethod
    def _by_verdict_section(stdout):
        """Parse the indented '<key> <count>' lines under the 'by verdict:' header."""
        out = {}
        in_section = False
        for line in stdout.splitlines():
            if line.strip() == "by verdict:":
                in_section = True
                continue
            if in_section:
                # Section ends at the next top-level (2-space-indented) header or EOF.
                if line.startswith("  ") and not line.startswith("    "):
                    break
                parts = line.split()
                if len(parts) == 2:
                    out[parts[0]] = int(parts[1])
        return out

    def test_by_verdict_groups_on_verdict_not_review_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "out.jsonl"
            # The exact shape from the bug report: note-verdict NO, reviewer gate ACCEPT.
            mod.emit(
                "spike",
                "self-improving-tooling-otel-spike-1",
                None,
                {"verdict": "NO", "spike_review_verdict": "ACCEPT", "status": "accept"},
                str(log),
            )
            res = subprocess.run(
                [sys.executable, str(SCRIPT), "summary", "--log", str(log)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            by_verdict = self._by_verdict_section(res.stdout)
            self.assertEqual(
                by_verdict.get("NO"),
                1,
                "spike must be grouped under its own verdict (NO), not the reviewer gate",
            )
            self.assertNotIn(
                "ACCEPT",
                by_verdict,
                "reviewer gate (spike_review_verdict=ACCEPT) must NOT appear in by-verdict",
            )


class TestSchemaStability(unittest.TestCase):
    """Every record carries the full, stable column set (nulls, never omissions)."""

    def test_record_has_all_columns(self):
        rec = mod.build_record("spike", "foo-spike-1", None, {})
        self.assertEqual(list(rec.keys()), mod.RECORD_FIELDS)
        self.assertEqual(rec["schema_version"], mod.SCHEMA_VERSION)


class TestStringFieldCoercion(unittest.TestCase):
    """Identifier/string columns keep their raw --field string; numeric-looking SHAs must
    NOT JSON-coerce to a float (regression for the 10365e3 -> 10365000.0 corruption
    caught by self-improving-tooling-sealed-eval-spike-1)."""

    def test_numeric_looking_sha_stays_string(self):
        rec = mod.build_record(
            "milestone", "m3", None, {"rectification_commit": "10365e3"}
        )
        self.assertEqual(rec["rectification_commit"], "10365e3")
        self.assertIsInstance(rec["rectification_commit"], str)

    def test_hex_sha_without_e_stays_string(self):
        rec = mod.build_record(
            "milestone", "m3", None, {"rectification_commit": "5cd102d"}
        )
        self.assertEqual(rec["rectification_commit"], "5cd102d")

    def test_numeric_columns_still_json_coerce(self):
        rec = mod.build_record(
            "milestone", "m3", None,
            {"token_cost": "327185", "critique_high": "1"},
        )
        self.assertEqual(rec["token_cost"], 327185)
        self.assertIsInstance(rec["token_cost"], int)
        self.assertEqual(rec["critique_high"], 1)


class TestRunBucket(unittest.TestCase):
    """run_bucket cardinality: bounded, deterministic, correctly computed (m4 E4)."""

class TestMetricEmitBestEffort(unittest.TestCase):
    """The additive metric push MUST NOT compromise the authoritative JSONL append."""

if __name__ == "__main__":
    unittest.main(verbosity=2)

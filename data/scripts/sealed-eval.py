#!/usr/bin/env python3
"""
sealed-eval.py — E5 sealed-eval harness (self-improving-tooling-m5 capstone).

Productionised from the SP3 POC (self-improving-tooling-sealed-eval-spike-1).
stdlib only; no boto3; no network; no live-infra mutation.

Subcommands:
  python3 sealed-eval.py seal [--corpus PATH] [--manifest PATH] [--invalidated-by WHY]
      Hash a corpus and write a seal manifest (sha256 + custodian + date + count).
  python3 sealed-eval.py verify MANIFEST [--corpus PATH]
      Recompute the corpus hash and compare to the sealed value.
      MATCH -> exit 0; MISMATCH or corpus-not-found -> exit 1.
  python3 sealed-eval.py metric [--corpus PATH] [--baseline PATH] [--json]
      Compute the (C+H)/milestone baseline->post delta with honest n exposure.
  python3 sealed-eval.py probe-bad-name
      Exercise the INV3 metric-name guard (a bad name MUST be rejected).

KR3 isolation: this harness NEVER reads or writes any the dispatcher service ConflictBench
store (no boto3, no S3, no shared kr-name-table.json). The corpus + custodian
are wholly separate from the dispatcher service grader. The metric is (C+H)/milestone,
NOT an FP-rate; metric names are gated by METRIC_NAME_TABLE below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CUSTODIAN = "Chris Dare"
SEAL_VERSION = "v0"
MIN_N = 5

# Frozen pre-calibration milestone ids (matches plans/self-improving-tooling-m3-baseline.md).
BASELINE_IDS = frozenset(
    {
        "self-improving-tooling-m4",
        "self-improving-tooling-m4b",
        "self-improving-tooling-m4b-pivot",
    }
)
# Calibrated-regime milestone ids (current).
POST_IDS = frozenset({"self-improving-tooling-m3"})

# KR3 separation invariant: this harness must touch none of these. Kept as a
# documented guard; no runtime path resolves to any of them.
DISPATCHER_STORE_PATHS = (
    "conflictbench",
    "dispatcher-grader",
    "platform-crossplane-dispatcher-grader",
)

# INV3 analogue: the SOLE registry of reportable metric names. Reused verbatim
# (pattern-only) from the SP3 POC; the ConflictBench grader.py/seal.py code is
# NOT inherited (boto3 + wrong corpus shape).
METRIC_NAME_TABLE = {
    "ch_per_milestone": {
        "definition": "Mean of (critique_critical + critique_high) over milestone pipeline records with non-null severity fields.",
        "threshold": "lower is better; overclaim_guard fires when n_post < 5",
        "source": "outcomes.jsonl pipeline==milestone records; nulls excluded from n",
    },
    "baseline_ch_per_milestone": {
        "definition": "ch_per_milestone over 3 frozen pre-calibration milestone ids (m4, m4b, m4b-pivot).",
        "threshold": "1.0 (frozen from plans/self-improving-tooling-m3-baseline.md)",
        "source": "outcomes.jsonl filtered by BASELINE_IDS",
    },
    "post_ch_per_milestone": {
        "definition": "ch_per_milestone over calibrated-regime milestone records (currently m3 only).",
        "threshold": "compared to baseline; overclaim_guard fires at n_post < MIN_N",
        "source": "outcomes.jsonl filtered by POST_IDS",
    },
}


def _repo_root() -> Path:
    # data/scripts/sealed-eval.py -> repo root is two parents up.
    return Path(__file__).resolve().parent.parent.parent


def _default_corpus_path() -> Path:
    return _repo_root() / ".claude" / "notes" / "pipeline-outcomes" / "outcomes.jsonl"


def _default_manifest_path() -> Path:
    return _repo_root() / "data" / "scripts" / "sealed-eval-v0-manifest.json"


def _assert_metric_name(name: str) -> None:
    if name not in METRIC_NAME_TABLE:
        raise ValueError(
            f"METRIC_GUARD: metric '{name}' not in METRIC_NAME_TABLE (INV3 analogue). "
            f"Known: {sorted(METRIC_NAME_TABLE)}"
        )


def _load_corpus(path: Path, skipped: list | None = None) -> list[dict]:
    records: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # Surface, don't swallow: a malformed line drops a record from n, so the
            # exclusion must be visible (matches the null-severity integrity-note treatment).
            if skipped is not None:
                skipped.append(lineno)
            continue
    return records


ORIGIN = "committed-v0, agent-kit data/scripts/"


def _read_manifest(path: Path) -> dict | None:
    """The manifest being replaced, or None. Unreadable counts as absent."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _next_seal_version(prior: dict | None, prior_sha: str | None, sha: str) -> str:
    """Bump only when a DIFFERENT corpus is being sealed over an existing one.

    Re-sealing identical bytes is a no-op and must not inflate the version, or
    the number stops meaning "how many times this corpus was invalidated".
    """
    current = (prior or {}).get("seal_version", SEAL_VERSION)
    if not prior_sha or prior_sha == sha:
        return current if prior else SEAL_VERSION
    if isinstance(current, str) and current.startswith("v") and current[1:].isdigit():
        return f"v{int(current[1:]) + 1}"
    return SEAL_VERSION


def _provenance_with_history(
    prior: dict | None, prior_sha: str | None, sha: str, invalidated_by: str | None
) -> dict:
    """Carry every superseded digest forward, newest last.

    Structured rather than the free string it used to be, because "retain the
    prior digest" has to be machine-checkable — a sentence in a text field is
    not something a gate can verify, and this record exists to be verified.
    """
    previous = (prior or {}).get("provenance")
    if isinstance(previous, dict):
        provenance = {
            "origin": previous.get("origin", ORIGIN),
            "superseded": list(previous.get("superseded") or []),
        }
    else:
        # Migrate the old free-text field rather than dropping it.
        provenance = {"origin": previous if isinstance(previous, str) and previous else ORIGIN,
                      "superseded": []}

    if prior_sha and prior_sha != sha:
        provenance["superseded"].append({
            "seal_version": (prior or {}).get("seal_version"),
            "seal_date": (prior or {}).get("seal_date"),
            "corpus_sha256": prior_sha,
            "invalidated_by": invalidated_by,
        })
    return provenance


def cmd_seal(args) -> int:
    corpus_path = Path(args.corpus) if args.corpus else _default_corpus_path()
    if not corpus_path.exists():
        print(f"seal: CORPUS NOT FOUND at {corpus_path}", file=sys.stderr)
        return 1
    corpus_bytes = corpus_path.read_bytes()
    sha = hashlib.sha256(corpus_bytes).hexdigest()
    records = _load_corpus(corpus_path)
    manifest_path = Path(args.manifest) if args.manifest else _default_manifest_path()

    # Re-sealing used to rebuild this manifest from scratch, which meant the
    # outgoing corpus_sha256 was simply gone. A seal whose history is erased by
    # the routine act of re-sealing is not tamper-evidence: the manifest would
    # assert a clean seal over a corpus that had been silently altered, and
    # nothing would record that it ever changed. That is exactly what happened
    # to v0 during the genericization fork.
    prior = _read_manifest(manifest_path)
    prior_sha = prior.get("corpus_sha256") if prior else None
    invalidated_by = getattr(args, "invalidated_by", None)

    if prior_sha and prior_sha != sha and not invalidated_by:
        print(
            f"seal: REFUSED -- {manifest_path.name} seals {prior_sha} but the corpus "
            f"now hashes to {sha}.\n"
            "seal: Re-sealing would discard the sealed digest with no record that it "
            "changed.\n"
            "seal: State the cause to proceed:  --invalidated-by \"<why the corpus "
            "changed>\"",
            file=sys.stderr,
        )
        return 1

    provenance = _provenance_with_history(prior, prior_sha, sha, invalidated_by)
    manifest = {
        "seal_version": _next_seal_version(prior, prior_sha, sha),
        "custodian": CUSTODIAN,
        "seal_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # POSIX separators always. `str(Path(...))` yields backslashes on
        # Windows, which put an OS-dependent value into a COMMITTED artifact:
        # the manifest then resolved on the machine that sealed it and nowhere
        # else, and the harness died with FileNotFoundError on Linux.
        "corpus_path": corpus_path.as_posix(),
        "corpus_sha256": sha,
        "record_count": len(records),
        "provenance": provenance,
        "immutability_note": (
            "Tamper-EVIDENCE (detectable change), not write-prevention. Re-verify with: "
            "python3 sealed-eval.py verify data/scripts/sealed-eval-v0-manifest.json "
            "--corpus data/scripts/sealed-eval-v0-corpus.jsonl"
        ),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    reread_sha = json.loads(manifest_path.read_text(encoding="utf-8"))["corpus_sha256"]
    if reread_sha != sha:
        print("seal: RE-READ MISMATCH — manifest not idempotent", file=sys.stderr)
        return 1
    print(f"seal: wrote {manifest_path}")
    print(f"seal: corpus_sha256={sha}")
    print(f"seal: record_count={len(records)}")
    return 0


def cmd_verify(args) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    sealed_sha = manifest["corpus_sha256"]
    corpus_path = Path(args.corpus) if args.corpus else Path(manifest["corpus_path"])
    if not corpus_path.exists():
        print(f"verify: CORPUS NOT FOUND at {corpus_path}", file=sys.stderr)
        return 1
    actual_sha = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    if actual_sha == sealed_sha:
        print(f"verify: MATCH sha256={actual_sha}")
        return 0
    print(f"verify: MISMATCH sealed={sealed_sha} actual={actual_sha}")
    return 1


def _compute_metric(corpus_path: Path, baseline_path: Path | None) -> dict:
    _assert_metric_name("ch_per_milestone")
    _assert_metric_name("baseline_ch_per_milestone")
    _assert_metric_name("post_ch_per_milestone")

    integrity_notes: list[str] = []

    def _ch_for(records: list[dict], wanted: frozenset) -> list[int]:
        vals: list[int] = []
        for r in records:
            if r.get("pipeline") != "milestone":
                continue
            c = r.get("critique_critical")
            h = r.get("critique_high")
            if c is None or h is None:
                integrity_notes.append(
                    f"record '{r.get('id', '')}': pipeline=milestone but critique_critical/high null — excluded from n"
                )
                continue
            rid = r.get("id", "")
            rc = r.get("rectification_commit")
            if isinstance(rc, float):
                integrity_notes.append(
                    f"record '{rid}': rectification_commit is float ({rc}) — scientific-notation parse of a SHA; critique_* unaffected"
                )
            if r.get("verdict") == "":
                integrity_notes.append(f"record '{rid}': verdict is empty string (not null)")
            if rid in wanted:
                vals.append(c + h)
        return vals

    skipped_lines: list = []
    post_records = _load_corpus(corpus_path, skipped_lines)
    if skipped_lines:
        integrity_notes.append(
            f"corpus '{corpus_path}': skipped {len(skipped_lines)} malformed line(s) "
            f"{skipped_lines} — excluded from n"
        )
    if baseline_path is not None:
        # Derive the baseline from the committed v0 corpus (forward-compatible:
        # the post set is any milestone record in --corpus not in BASELINE_IDS).
        bp = Path(baseline_path)
        if bp.name.endswith(".json"):
            bp = Path(json.loads(bp.read_text(encoding="utf-8"))["corpus_path"])
        bp_skipped: list = []
        baseline_vals = _ch_for(_load_corpus(bp, bp_skipped), BASELINE_IDS)
        if bp_skipped:
            integrity_notes.append(
                f"baseline corpus '{bp}': skipped {len(bp_skipped)} malformed line(s) {bp_skipped}"
            )
        post_vals = [
            r["critique_critical"] + r["critique_high"]
            for r in post_records
            if r.get("pipeline") == "milestone"
            and r.get("critique_critical") is not None
            and r.get("critique_high") is not None
            and r.get("id", "") not in BASELINE_IDS
        ]
    else:
        all_records = post_records
        baseline_vals = _ch_for(all_records, BASELINE_IDS)
        post_vals = _ch_for(all_records, POST_IDS)

    baseline_mean = sum(baseline_vals) / len(baseline_vals) if baseline_vals else 0.0
    post_mean = sum(post_vals) / len(post_vals) if post_vals else 0.0
    n_baseline = len(baseline_vals)
    n_post = len(post_vals)
    delta = post_mean - baseline_mean
    overclaim_fired = n_post < MIN_N
    if overclaim_fired:
        verdict = (
            f"insufficient n (n_post={n_post} < {MIN_N}) — no reduction claimed; "
            f"re-measure when milestone records >= {MIN_N}"
        )
    else:
        verdict = (
            f"delta={delta:+.2f} (post={post_mean:.2f} vs baseline={baseline_mean:.2f}, n_post={n_post})"
        )

    return {
        "baseline_ch_per_milestone": baseline_mean,
        "post_ch_per_milestone": post_mean,
        "n_baseline": n_baseline,
        "n_post": n_post,
        "baseline_ch_values": baseline_vals,
        "post_ch_values": post_vals,
        "delta": delta,
        "min_n": MIN_N,
        "overclaim_guard_fired": overclaim_fired,
        "delta_verdict": verdict,
        "corpus_integrity_notes": sorted(set(integrity_notes)),
    }


def cmd_metric(args) -> int:
    corpus_path = Path(args.corpus) if args.corpus else _default_corpus_path()
    if not corpus_path.exists():
        print(f"metric: CORPUS NOT FOUND at {corpus_path}", file=sys.stderr)
        return 1
    baseline_path = args.baseline
    if baseline_path is None:
        # Pin the frozen baseline to the COMMITTED immutable v0 seal, not the mutable
        # live log — otherwise the baseline silently drifts as the live corpus accrues
        # post-calibration records (the seal would be decorative). --baseline overrides.
        default_manifest = _default_manifest_path()
        if default_manifest.exists():
            baseline_path = str(default_manifest)
    m = _compute_metric(corpus_path, baseline_path)
    if args.json:
        print(json.dumps(m, indent=2))
        return 0
    print(f"baseline_ch_per_milestone={m['baseline_ch_per_milestone']:.2f}")
    print(f"post_ch_per_milestone={m['post_ch_per_milestone']:.2f}")
    print(f"n_baseline={m['n_baseline']}")
    print(f"n_post={m['n_post']}")
    print(f"baseline_ch_values={m['baseline_ch_values']}")
    print(f"post_ch_values={m['post_ch_values']}")
    print(f"delta={m['delta']:+.2f}")
    print(f"min_n={m['min_n']}")
    print(f"overclaim_guard_fired={m['overclaim_guard_fired']}")
    print(f"delta_verdict={m['delta_verdict']}")
    for note in m["corpus_integrity_notes"]:
        print(f"corpus_integrity_note: {note}")
    return 0


def cmd_probe_bad_name(args) -> int:
    try:
        _assert_metric_name("undefined_kr99_misattributed")
    except ValueError as e:
        print(f"probe-bad-name: REJECTED — {e}")
        return 0
    print("probe-bad-name: NOT REJECTED — METRIC_GUARD is broken", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sealed-eval")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("seal")
    sp.add_argument("--corpus", dest="corpus", default=None)
    sp.add_argument("--manifest", dest="manifest", default=None)
    # Required to re-seal over a DIFFERENT corpus. The flag is the record:
    # without it the outgoing digest would vanish with nothing saying why.
    sp.add_argument("--invalidated-by", dest="invalidated_by", default=None,
                    help="why the sealed corpus changed; required when re-sealing "
                         "over a manifest whose digest no longer matches")

    vp = sub.add_parser("verify")
    vp.add_argument("manifest", metavar="MANIFEST")
    vp.add_argument("--corpus", dest="corpus", default=None)

    mp = sub.add_parser("metric")
    mp.add_argument("--corpus", dest="corpus", default=None)
    mp.add_argument("--baseline", dest="baseline", default=None)
    mp.add_argument("--json", dest="json", action="store_true")

    sub.add_parser("probe-bad-name")

    args = parser.parse_args(argv)

    dispatch = {
        "seal": cmd_seal,
        "verify": cmd_verify,
        "metric": cmd_metric,
        "probe-bad-name": cmd_probe_bad_name,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

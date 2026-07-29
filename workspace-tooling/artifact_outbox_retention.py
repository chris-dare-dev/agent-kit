#!/usr/bin/env python3
"""Classify artifact-memory outbox directories by RETENTION OBLIGATION.

This tool answers one question per outbox directory: *is anything still
depending on these bytes?* It never deletes, moves, or modifies anything —
ADR-002 §Decision 7 is explicit that "this ADR grants no deletion authority",
so disposition of any directory remains a separately approved human action.
This reports; a human decides.

Why it exists
-------------
The outbox grew to ~769 MB (94% of it full-corpus catalog-run renders) with no
pruning anywhere, because the operative belief was that ADR-002 forbids pruning
replay authority outright. It does not. The actual clause is CONDITIONAL:

    "it prohibits pruning the replay set WHILE IT IS THE ONLY REBUILD
     REPRESENTATION OF A REVISION"                        -- ADR-002 line 44

and the authority is a named frozen set, not whatever is on disk:

    "Discovery of arbitrary outbox directories is not sufficient authority
     to replay them."                                     -- ADR-002 line 24

So the real test is computable: a directory is retention-bound only if
something concrete still depends on it. Three INDEPENDENT predicates decide it.

The three predicates
--------------------
1. ``state_bound`` -- the consumer's event state records this outbox in
   ``consumer_events.outbox_path``. The receipt loop re-validates a completed
   event by loading that outbox's manifest and comparing identity, so removing
   it converts a completed event into a poison/dead-letter. Hard RETAIN.

2. ``holds_irreproducible`` -- the directory contains units for a revision that
   is currently SERVING but is neither (a) covered by the frozen materialized
   replay set nor (b) still current in the catalog (i.e. regenerable from the
   canonical source file). With no CAS, those superseded bytes exist nowhere
   else. This is precisely ADR-002 line 44. Hard RETAIN.

3. ``manifest_named`` -- the frozen replay manifest names this directory as the
   provenance of at least one selected unit. The unit CONTENT is already
   materialized (``selected_units.unit_json``), so the directory is not the
   only representation and line 44 does not bind — but removing it forfeits the
   ability to re-verify the materialization against its original source.
   RETAIN-BY-DEFAULT: prunable only under an explicit human decision.

A directory matching none of the three is PRUNABLE.

Fail-closed
-----------
Every uncertainty resolves toward RETAIN. A missing or unreadable input
database, an unreadable units file, or a skipped scan all mark the affected
predicate ``unknown`` and force RETAIN. This tool must never report PRUNABLE on
incomplete information — the entire remediation this came out of was about
surfaces that reported "all clear" when they simply could not tell.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import artifact_unit_archive as unit_archive  # noqa: E402
import artifact_runtime

SCHEMA_VERSION = 1

DEFAULT_DERIVED_ROOT = artifact_runtime.derived_root()
DEFAULT_OUTBOX_ROOT = DEFAULT_DERIVED_ROOT / "outbox"
DEFAULT_CATALOG = DEFAULT_DERIVED_ROOT / "artifact-catalog.sqlite3"
DEFAULT_INGESTION_STATE = DEFAULT_DERIVED_ROOT / "ingestion-state.sqlite3"
DEFAULT_CONSUMER_STATE = DEFAULT_DERIVED_ROOT / "artifact-event-consumer.sqlite3"
DEFAULT_REPLAY = DEFAULT_DERIVED_ROOT / "qdrant-shadow-replay.sqlite3"
DEFAULT_ARCHIVE = DEFAULT_DERIVED_ROOT / "artifact-unit-archive.sqlite3"

# Which sink targets count as "serving". Deliberately matches the ACTIVE
# collection across url sinks rather than one exact descriptor string: being
# over-inclusive here can only enlarge the irreproducible set, which biases
# toward RETAIN. Narrowing it is the risky direction, so it is opt-in.
DEFAULT_TARGET_LIKE = "url:%personal_artifact_chunks_p20260721v1%"

UNITS_FILENAME = "ingest-units.jsonl.gz"
MANIFEST_FILENAME = "manifest.json"

VERDICT_RETAIN = "RETAIN"
VERDICT_RETAIN_DEFAULT = "RETAIN-BY-DEFAULT"
VERDICT_PRUNABLE = "PRUNABLE"


class RetentionError(RuntimeError):
    """Unrecoverable input problem — never downgraded into a PRUNABLE verdict."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    """Open a database read-only. Returns None when it cannot be read.

    A None result is propagated as an ``unknown`` predicate, never as an
    absence of obligation.
    """
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        connection.execute("SELECT 1")
        return connection
    except (sqlite3.Error, OSError):
        return None


def _dir_bytes(path: Path) -> int:
    total = 0
    for parent, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += (Path(parent) / name).stat().st_size
            except OSError:
                continue
    return total


def _outbox_basename(raw: Any) -> str | None:
    """Normalise a recorded outbox reference to its directory name."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw.strip().rstrip("/")).name or None


def collect_revision_sets(
    *,
    catalog: Path,
    ingestion_state: Path,
    replay: Path,
    target_like: str,
    archive: Path | None = None,
) -> dict[str, Any]:
    """Serving / frozen / current revision sets, and the irreproducible residue.

    Any input that cannot be read leaves ``complete=False``, which forces every
    directory to RETAIN downstream.
    """
    problems: list[str] = []

    ingestion_connection = _connect_ro(ingestion_state)
    if ingestion_connection is None:
        problems.append(f"ingestion state unreadable: {ingestion_state}")
        serving: set[str] = set()
    else:
        with ingestion_connection:
            serving = {
                str(row[0])
                for row in ingestion_connection.execute(
                    "SELECT DISTINCT revision_id FROM sink_units "
                    "WHERE sink='qdrant' AND status='completed' AND target LIKE ?",
                    (target_like,),
                )
            }
        ingestion_connection.close()

    replay_connection = _connect_ro(replay)
    if replay_connection is None:
        problems.append(f"frozen replay set unreadable: {replay}")
        frozen: set[str] = set()
        manifest_named: set[str] = set()
    else:
        with replay_connection:
            frozen = {
                str(row[0])
                for row in replay_connection.execute(
                    "SELECT DISTINCT revision_id FROM selected_units"
                )
            }
            manifest_named = {
                name
                for name in (
                    _outbox_basename(row[0])
                    for row in replay_connection.execute(
                        "SELECT DISTINCT source_outbox FROM selected_units"
                    )
                )
                if name
            }
        replay_connection.close()

    catalog_connection = _connect_ro(catalog)
    if catalog_connection is None:
        problems.append(f"catalog unreadable: {catalog}")
        current: set[str] = set()
    else:
        with catalog_connection:
            current = {
                str(row[0])
                for row in catalog_connection.execute(
                    "SELECT revision_id FROM current_artifact_revisions"
                )
            }
        catalog_connection.close()

    # Coverage authority. A revision is "no longer outbox-only" only when its
    # CURRENT serving unit set is fully preserved in the archive — recomputed
    # every run by the ONE shared query in artifact_unit_archive. Reading
    # `SELECT DISTINCT revision_id FROM archived_units` here instead let a
    # partial or wrong-profile insert (a row exists, but not the served units)
    # forge coverage and mark PRUNABLE the directory holding the only complete
    # copy. An ABSENT archive is legitimately empty; one that EXISTS but cannot
    # be read is a problem and fails closed like any other unreadable input.
    if archive is None:
        archived: set[str] = set()
    else:
        archived, archive_readable = unit_archive.covered_revisions(
            archive, ingestion_state, target_like, serving
        )
        if archive.exists() and not archive_readable:
            problems.append(f"unit archive unreadable: {archive}")

    # ADR-002 line 44: a revision is protected only while the outbox is its ONLY
    # rebuild representation. Covered by the frozen materialised set, still
    # reproducible from the canonical source, or preserved in the unit archive
    # all mean it is not.
    irreproducible = serving - frozen - current - archived

    return {
        "complete": not problems,
        "problems": problems,
        "serving": serving,
        "frozen": frozen,
        "current": current,
        "archived": archived,
        "irreproducible": irreproducible,
        "manifest_named": manifest_named,
    }


def collect_state_bound(consumer_state: Path) -> tuple[set[str], bool]:
    """Outbox directory names referenced by consumer event state."""
    connection = _connect_ro(consumer_state)
    if connection is None:
        return set(), False
    try:
        with connection:
            rows = connection.execute(
                "SELECT DISTINCT outbox_path FROM consumer_events "
                "WHERE outbox_path IS NOT NULL"
            ).fetchall()
    except sqlite3.Error:
        return set(), False
    finally:
        connection.close()
    return {name for name in (_outbox_basename(row[0]) for row in rows) if name}, True


def scan_units_for(path: Path, wanted: set[str]) -> tuple[int, str]:
    """Count how many WANTED revision ids this outbox holds.

    Returns (count, scan_status). A scan that cannot complete reports
    ``error``; the caller must treat that as an unknown predicate (RETAIN).
    """
    units = path / UNITS_FILENAME
    if not units.is_file():
        return 0, "missing-units-file"
    if not wanted:
        # Nothing can be held; skipping the (large) gzip read is safe because
        # the answer is zero by construction, not by assumption.
        return 0, "not-needed"
    found: set[str] = set()
    try:
        with gzip.open(units, "rt", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    revision = json.loads(line).get("revision_id")
                except json.JSONDecodeError:
                    return len(found), "error"
                if isinstance(revision, str) and revision in wanted:
                    found.add(revision)
    except (OSError, EOFError, gzip.BadGzipFile):
        return len(found), "error"
    return len(found), "complete"


def classify(
    *,
    outbox_root: Path,
    catalog: Path,
    ingestion_state: Path,
    consumer_state: Path,
    replay: Path,
    target_like: str,
    scan: bool,
    archive: Path | None = None,
) -> dict[str, Any]:
    if not outbox_root.is_dir():
        raise RetentionError(f"outbox root is not a directory: {outbox_root}")

    revisions = collect_revision_sets(
        catalog=catalog,
        ingestion_state=ingestion_state,
        replay=replay,
        target_like=target_like,
        archive=archive,
    )
    state_bound, state_readable = collect_state_bound(consumer_state)
    problems = list(revisions["problems"])
    if not state_readable:
        problems.append(f"consumer state unreadable: {consumer_state}")

    # If ANY input is missing we cannot prove absence of obligation for any
    # directory, so nothing is prunable this run.
    inputs_complete = revisions["complete"] and state_readable

    irreproducible = revisions["irreproducible"]
    entries: list[dict[str, Any]] = []

    try:
        children = sorted(p for p in outbox_root.iterdir())
    except OSError as exc:
        raise RetentionError(f"outbox root is unreadable: {exc}") from exc

    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        name = child.name
        held, scan_status = (
            scan_units_for(child, irreproducible) if scan else (0, "skipped")
        )

        predicate_state = name in state_bound
        predicate_manifest = name in revisions["manifest_named"]
        # Unknown whenever the scan could not answer authoritatively.
        if scan_status in {"complete", "not-needed"}:
            predicate_irreproducible: bool | None = held > 0
        else:
            predicate_irreproducible = None

        reasons: list[str] = []
        if not inputs_complete:
            verdict = VERDICT_RETAIN
            reasons.append("inputs incomplete — cannot prove absence of obligation")
        elif predicate_state:
            verdict = VERDICT_RETAIN
            reasons.append("referenced by consumer event state (removal poisons events)")
        elif predicate_irreproducible is None:
            verdict = VERDICT_RETAIN
            reasons.append(f"irreproducibility undetermined (scan: {scan_status})")
        elif predicate_irreproducible:
            verdict = VERDICT_RETAIN
            reasons.append(
                f"holds {held} serving revision(s) with no other representation "
                "(ADR-002 line 44)"
            )
        elif predicate_manifest:
            verdict = VERDICT_RETAIN_DEFAULT
            reasons.append(
                "named by the frozen replay manifest; content is materialised in "
                "selected_units, so line 44 does not bind, but pruning forfeits "
                "source re-verification — explicit human decision required"
            )
        else:
            verdict = VERDICT_PRUNABLE
            reasons.append("no state binding, no irreproducible revision, not manifest-named")

        entries.append(
            {
                "name": name,
                "bytes": _dir_bytes(child),
                "kind": (
                    # A .tmp- prefix is an outbox render that never completed:
                    # prepare_outbox stages into .tmp-<name>-<pid>-<rand> and
                    # renames on success, so a surviving one is an orphan from
                    # an interrupted run. It was never published and nothing can
                    # reference it — but it still occupies full-corpus bytes.
                    "interrupted-render"
                    if name.startswith(".tmp-")
                    else "catalog-run"
                    if name.startswith("catalog-run-")
                    else "skill-event"
                    if name.startswith("skill-event-")
                    else "other"
                ),
                "predicates": {
                    "state_bound": predicate_state,
                    "holds_irreproducible": predicate_irreproducible,
                    "manifest_named": predicate_manifest,
                    "scan": scan_status,
                },
                "irreproducible_held": held,
                "verdict": verdict,
                "reason": "; ".join(reasons),
            }
        )

    def _sum(predicate: Any) -> dict[str, int]:
        chosen = [e for e in entries if predicate(e)]
        return {"dirs": len(chosen), "bytes": sum(e["bytes"] for e in chosen)}

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "mode": "report-only",
        "deletion": "not-supported: ADR-002 grants no deletion authority",
        "inputs_complete": inputs_complete,
        "problems": problems,
        "target_like": target_like,
        "revisions": {
            "serving": len(revisions["serving"]),
            "frozen_replay_set": len(revisions["frozen"]),
            "current_in_catalog": len(revisions["current"]),
            "unit_archive": len(revisions["archived"]),
            "irreproducible": len(irreproducible),
        },
        "totals": {
            "all": _sum(lambda e: True),
            "retain": _sum(lambda e: e["verdict"] == VERDICT_RETAIN),
            "retain_by_default": _sum(lambda e: e["verdict"] == VERDICT_RETAIN_DEFAULT),
            "prunable": _sum(lambda e: e["verdict"] == VERDICT_PRUNABLE),
        },
        "entries": entries,
    }


def render_human(report: dict[str, Any]) -> str:
    lines: list[str] = []
    rev = report["revisions"]
    lines.append(
        f"artifact outbox retention  ({report['generated_at']})  MODE: report-only"
    )
    if not report["inputs_complete"]:
        lines.append("  !! INPUTS INCOMPLETE — everything reported RETAIN (fail-closed)")
    for problem in report["problems"]:
        lines.append(f"  !! {problem}")
    # Set SIZES, not intersections: only `irreproducible` is derived
    # (serving - frozen - current - archived), and it is the number that pins
    # directories.
    lines.append(
        f"  revision sets: serving={rev['serving']} "
        f"frozen-replay={rev['frozen_replay_set']} "
        f"catalog-current={rev['current_in_catalog']} "
        f"unit-archive={rev.get('unit_archive', 0)}"
    )
    lines.append(
        f"  -> irreproducible (serving - frozen - current - archived) = {rev['irreproducible']}"
        "   <- these are what pin directories"
    )
    kinds: dict[str, int] = {}
    for entry in report["entries"]:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    if kinds.get("interrupted-render"):
        lines.append(
            f"  !! {kinds['interrupted-render']} interrupted render(s) present "
            "— never published, referenced by nothing"
        )
    lines.append("")
    lines.append(f"  {'directory':<50} {'size':>7}  {'verdict':<18} why")
    for entry in sorted(
        report["entries"], key=lambda e: (e["verdict"], -e["bytes"])
    ):
        lines.append(
            f"  {entry['name'][:50]:<50} {entry['bytes'] / 1e6:>6.0f}M  "
            f"{entry['verdict']:<18} {entry['reason'][:72]}"
        )
    totals = report["totals"]
    lines.append("")
    for label in ("all", "retain", "retain_by_default", "prunable"):
        item = totals[label]
        lines.append(
            f"  {label:<18} {item['dirs']:>3} dirs  {item['bytes'] / 1e6:>7.0f} MB"
        )
    lines.append("")
    lines.append("  Disposition of any directory remains a separately approved action.")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--outbox-root", type=Path, default=DEFAULT_OUTBOX_ROOT)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--ingestion-state", type=Path, default=DEFAULT_INGESTION_STATE)
    parser.add_argument("--consumer-state", type=Path, default=DEFAULT_CONSUMER_STATE)
    parser.add_argument("--replay", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument(
        "--archive",
        type=Path,
        default=DEFAULT_ARCHIVE,
        help="unit archive; revisions preserved there are no longer outbox-only",
    )
    parser.add_argument("--target-like", default=DEFAULT_TARGET_LIKE)
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help=(
            "skip reading units files; every directory then reports an unknown "
            "irreproducibility predicate and RETAINs (fail-closed)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = classify(
            outbox_root=args.outbox_root,
            catalog=args.catalog,
            ingestion_state=args.ingestion_state,
            consumer_state=args.consumer_state,
            replay=args.replay,
            archive=args.archive,
            target_like=args.target_like,
            scan=not args.no_scan,
        )
    except RetentionError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_human(report))
    # Exit 0 always: this is a reporter, and "there is nothing prunable" is a
    # perfectly healthy answer. Failure to READ its inputs is the error case.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

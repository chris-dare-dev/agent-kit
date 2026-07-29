#!/usr/bin/env python3
"""Materialise published units so outbox directories stop being their only copy.

What problem this solves
------------------------
There is no CAS yet. Once a source file changes, the only surviving
representation of the superseded revision is the derived unit payloads (the
chunk records the outbox rendered) — NOT the revision's source bytes. Chunk
rendering normalises destructively (blank lines dropped, boundary whitespace
stripped, CRLF→LF, undecodable bytes replaced), so the source bytes are
unreconstructible from an outbox with or without this archive; that is the CAS
gap, and this store does not close it. What an outbox is the *only* copy of is
those derived payloads, so ADR-002 line 44 pins that directory forever:

    "it prohibits pruning the replay set WHILE IT IS THE ONLY REBUILD
     REPRESENTATION OF A REVISION"

Measured, that obligation is monotonically increasing: ordinary editing
supersedes revisions, and each newly superseded revision permanently pins the
~50 MB directory holding it. A "keep the last N" policy is therefore actively
wrong — directories accrue obligations instead of ageing out.

This archive copies those units' payloads into one self-contained store, which
removes the "only representation" condition. The directory is then prunable on
its own merits, and the obligation stops growing.

What this is NOT
----------------
This does **not** grant replay authority. ADR-002 line 24 is explicit:

    "Discovery of arbitrary outbox directories is not sufficient authority
     to replay them."

Replay authority comes from a frozen, checkpoint-authorised manifest with
recorded identity, ordering and digests. This store makes no such claim; it
preserves CONTENT so that line 44's precondition stops being met. Those are
different claims and are deliberately not conflated.

Why a separate database
-----------------------
``qdrant-shadow-replay.sqlite3`` is the FROZEN replay set. Its
``selected_digest`` / ``checkpoint_digest`` / ``manifest_digest`` are recorded
precisely so it can be proven unchanged. Appending to it would destroy that
property. This store is strictly additive and separate; the frozen set is never
opened for writing.

Operation
---------
Backfill-driven and idempotent, rather than wired into the consumer's hot path:
a missed run is simply caught by the next one, and no publication or receipt
processing can be blocked by an archiving failure. Writes require ``--apply``;
the default is a dry run.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import artifact_runtime
import artifact_memory_provision as provision

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

SCHEMA_VERSION = 1

DEFAULT_DERIVED_ROOT = artifact_runtime.derived_root()
DEFAULT_OUTBOX_ROOT = DEFAULT_DERIVED_ROOT / "outbox"
DEFAULT_CATALOG = DEFAULT_DERIVED_ROOT / "artifact-catalog.sqlite3"
DEFAULT_INGESTION_STATE = DEFAULT_DERIVED_ROOT / "ingestion-state.sqlite3"
DEFAULT_REPLAY = DEFAULT_DERIVED_ROOT / "qdrant-shadow-replay.sqlite3"
DEFAULT_ARCHIVE = DEFAULT_DERIVED_ROOT / "artifact-unit-archive.sqlite3"

# Derived, not hardcoded: pinning the generation here meant a generation
# bump silently kept targeting the previous collection.
DEFAULT_TARGET_LIKE = f"url:%{provision.COLLECTION}%"

UNITS_FILENAME = "ingest-units.jsonl.gz"
MANIFEST_FILENAME = "manifest.json"


class ArchiveError(RuntimeError):
    """Unrecoverable input problem. Never downgraded into a partial success."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        connection.execute("SELECT 1")
        return connection
    except (sqlite3.Error, OSError):
        return None


def open_archive(
    path: Path, *, create: bool, forbid: tuple[Path, ...] = ()
) -> sqlite3.Connection:
    """Open (optionally creating) the archive.

    The guard compares against every precious path THIS RUN was given, not one
    constant: protecting only `DEFAULT_REPLAY` left `--archive` pointed at a
    non-default replay copy, the catalog, or the ingestion state free to be
    opened for write and have `CREATE TABLE`/`PRAGMA journal_mode=WAL` executed
    inside it.
    """
    resolved = path.resolve()
    for other in (DEFAULT_REPLAY, *forbid):
        try:
            if resolved == Path(other).resolve():
                raise ArchiveError(
                    f"refusing to write into {other}; the archive must be a "
                    "separate, additive database"
                )
        except OSError:
            continue
    if path.exists():
        # Only an existing ARCHIVE may be reopened: never initialise schema
        # inside some other database that happens to be named as the target.
        probe = _connect_ro(path)
        if probe is not None:
            try:
                names = {
                    r[0]
                    for r in probe.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            except sqlite3.Error:
                names = set()
            finally:
                probe.close()
            if names and not ({"archived_units", "archive_meta"} & names):
                raise ArchiveError(
                    f"{path} exists and is not a unit archive (tables: "
                    f"{sorted(names)[:4]}); refusing to write into it"
                )
    if not path.exists() and not create:
        raise ArchiveError(f"archive does not exist: {path} (run with --apply)")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS archived_units (
            unit_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL,
            point_id TEXT,
            source_outbox TEXT NOT NULL,
            source_units_sha256 TEXT,
            catalog_run_id INTEGER,
            archived_at TEXT NOT NULL,
            unit_sha256 TEXT,
            unit_json TEXT NOT NULL
        );
        -- Coverage attestation. A revision counts as preserved ONLY when its
        -- archived unit_id set EQUALS the completed sink_units set for the
        -- active target. Granting coverage on "any row exists" let a partial
        -- insert forge a durability proof that un-pinned the directory holding
        -- the only complete copy.
        CREATE TABLE IF NOT EXISTS archived_revisions (
            revision_id TEXT PRIMARY KEY,
            unit_count INTEGER NOT NULL,
            source_outbox TEXT NOT NULL,
            attested_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS archived_units_revision_idx
          ON archived_units(revision_id);
        CREATE TABLE IF NOT EXISTS archive_meta (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );
        """
    )
    connection.commit()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return connection


def _payload_intact(unit_id: str, revision_id: str, unit_sha256, unit_json) -> bool:
    """A stored archive row counts as a VALID preserved unit only when its payload is
    CONSISTENT WITH ITS CO-LOCATED DIGEST: the recorded unit_sha256 matches sha256 of
    the stored unit_json bytes, the payload parses as JSON, and the ids embedded in
    the payload match the row (Sol #5). Attesting on unit_id MEMBERSHIP alone let a
    corrupted/partial `unit_json` (bit rot, a torn/partial write, a naive edit) forge
    coverage and un-pin the directory holding the only COMPLETE copy -- authorising
    destruction of the last good bytes. This is CORRUPTION-evidence, not
    tamper-evidence: the digest is self-anchored (stored beside the payload it hashes),
    so a coherent in-DB rewrite that recomputes unit_sha256 AND preserves the embedded
    ids is OUT OF SCOPE -- no per-unit external anchor exists in the schema yet (a
    roadmap item). The embedded-id check still catches a self-consistent but
    wrong-identity payload (e.g. `{}` re-hashed)."""
    if not isinstance(unit_json, str) or not unit_json:
        return False
    if unit_sha256 != hashlib.sha256(unit_json.encode("utf-8")).hexdigest():
        return False
    try:
        payload = json.loads(unit_json)
    except (ValueError, TypeError):
        return False
    return (
        isinstance(payload, dict)
        and str(payload.get("unit_id")) == unit_id
        and str(payload.get("revision_id")) == revision_id
    )


def covered_revisions(
    archive: Path,
    ingestion_state: Path,
    target_like: str,
    serving: set[str],
) -> tuple[set[str], bool]:
    """Serving revisions FULLY preserved in the archive — the ONE coverage query.

    A revision is covered only when its CURRENT serving unit_id set (sink_units,
    this target) is a subset of the unit_ids present in archived_units for it WITH
    AN INTACT stored payload -- the recorded digest matches, the JSON parses, and
    the embedded ids match the row (Sol #5; see `_payload_intact`). This is
    deliberately NOT `SELECT DISTINCT revision_id FROM
    archived_units`: granting coverage on "a row exists" let a partial insert
    (truncated read) or a wrong chunk profile (different unit_ids for the same
    revision) forge full preservation, so the retention classifier un-pinned the
    directory holding the only COMPLETE copy — a forged durability proof
    authorising destruction of the last good bytes. Recomputing `expected` from
    live sink_units every call also means a later-grown serving set is
    re-evaluated, so a once-valid attestation cannot go stale unnoticed. The
    archiver and the retention classifier both route through here, so their two
    notions of "covered" can never diverge.

    Returns (covered, archive_readable). archive_readable describes the ARCHIVE
    only: an ABSENT archive is legitimately empty -> (set(), True); one that
    EXISTS but cannot be read -> (set(), False) so callers fail closed. An
    unreadable ingestion_state is handled separately by the caller; here it just
    yields an empty `expected` and therefore no coverage (the safe direction).
    """
    if not archive.exists():
        return set(), True
    connection = _connect_ro(archive)
    if connection is None:
        return set(), False
    have: dict[str, set[str]] = {}
    try:
        with connection:
            for revision, unit, unit_sha256, unit_json in connection.execute(
                "SELECT revision_id, unit_id, unit_sha256, unit_json FROM archived_units"
            ):
                key = str(revision)
                if key not in serving:
                    continue
                # PAYLOAD-VALIDATED coverage (Sol #5): a row is a preserved unit only
                # if its stored payload is intact -- a corrupted `unit_json` must not
                # forge coverage on unit_id membership alone and un-pin the only copy.
                if not _payload_intact(str(unit), key, unit_sha256, unit_json):
                    continue
                have.setdefault(key, set()).add(str(unit))
    except sqlite3.Error:
        return set(), False
    finally:
        connection.close()
    expected = expected_unit_ids(ingestion_state, target_like, serving)
    covered = {
        revision
        for revision, need in expected.items()
        if need and need <= have.get(revision, set())
    }
    return covered, True


def expected_unit_ids(
    ingestion_state: Path, target_like: str, revisions: set[str]
) -> dict[str, set[str]]:
    """The unit_id set the sink says each revision must have, to attest against."""
    connection = _connect_ro(ingestion_state)
    if connection is None:
        return {}
    expected: dict[str, set[str]] = {}
    try:
        with connection:
            for revision, unit in connection.execute(
                "SELECT revision_id, unit_id FROM sink_units WHERE sink='qdrant' "
                "AND status='completed' AND target LIKE ?",
                (target_like,),
            ):
                if str(revision) in revisions:
                    expected.setdefault(str(revision), set()).add(str(unit))
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return expected


def _read_set(connection: sqlite3.Connection | None, sql: str, params: tuple = ()) -> set[str]:
    if connection is None:
        return set()
    with connection:
        return {str(row[0]) for row in connection.execute(sql, params)}


def compute_unrepresented(
    *,
    catalog: Path,
    ingestion_state: Path,
    replay: Path,
    archive: Path,
    target_like: str,
) -> dict[str, Any]:
    """Serving revisions whose bytes exist ONLY inside an outbox directory."""
    problems: list[str] = []

    ingestion_connection = _connect_ro(ingestion_state)
    if ingestion_connection is None:
        problems.append(f"ingestion state unreadable: {ingestion_state}")
    serving = _read_set(
        ingestion_connection,
        "SELECT DISTINCT revision_id FROM sink_units WHERE sink='qdrant' "
        "AND status='completed' AND target LIKE ?",
        (target_like,),
    )
    if ingestion_connection is not None:
        ingestion_connection.close()

    replay_connection = _connect_ro(replay)
    if replay_connection is None:
        problems.append(f"frozen replay set unreadable: {replay}")
    frozen = _read_set(
        replay_connection, "SELECT DISTINCT revision_id FROM selected_units"
    )
    if replay_connection is not None:
        replay_connection.close()

    catalog_connection = _connect_ro(catalog)
    if catalog_connection is None:
        problems.append(f"catalog unreadable: {catalog}")
    current = _read_set(
        catalog_connection, "SELECT revision_id FROM current_artifact_revisions"
    )
    if catalog_connection is not None:
        catalog_connection.close()

    already, archive_readable = covered_revisions(
        archive, ingestion_state, target_like, serving
    )
    if archive.exists() and not archive_readable:
        # Mirrors the retention classifier. Discarding this flag let `status`
        # report inputs_complete=true with coverage silently zero, so an
        # operator could not tell "archive corrupt" from "coverage lost".
        problems.append(f"unit archive unreadable: {archive}")

    return {
        "complete": not problems,
        "problems": problems,
        "serving": serving,
        "frozen": frozen,
        "current": current,
        "archived": already,
        # The set this tool exists to empty.
        "unrepresented": serving - frozen - current - already,
    }


def _complete_source(path: Path) -> tuple[bool, str]:
    """Whether this outbox may source the archive.

    ONLY a complete render may. An interrupted render (`.tmp-<name>-<pid>-<rand>`,
    which `prepare_outbox` stages into and renames on success) or any outbox
    whose manifest does not assert completeness can hold a PARTIAL unit set for
    a revision. Archiving from one would mark that revision covered while
    preserving only some of its units — converting "outbox-only" into "archived
    but silently incomplete", which is strictly worse than the problem this
    tool exists to solve, because the classifier would then un-pin the
    directory holding the missing remainder.

    This is not a theoretical ordering concern: `sorted()` places `.tmp-*`
    FIRST, so an interrupted render is the first candidate source for every
    revision it contains.
    """
    if path.name.startswith(".tmp-"):
        return False, "interrupted render (never published)"
    manifest_path = path / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return False, "no manifest"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"unreadable manifest: {type(exc).__name__}"
    if manifest.get("complete") is not True:
        return False, "manifest does not assert complete"
    return True, "complete"


def _iter_outbox_units(path: Path, wanted: set[str]):
    """Yield (unit_dict, units_sha256, catalog_run_id) for wanted revisions."""
    units_file = path / UNITS_FILENAME
    if not units_file.is_file():
        return
    manifest: dict[str, Any] = {}
    manifest_path = path / MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
    units_sha = manifest.get("units_sha256")
    run_id = manifest.get("catalog_run_id")
    with gzip.open(units_file, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                unit = json.loads(line)
            except json.JSONDecodeError:
                raise ArchiveError(f"corrupt unit line in {units_file}")
            if unit.get("revision_id") in wanted:
                yield unit, units_sha, run_id


def backfill(
    *,
    outbox_root: Path,
    catalog: Path,
    ingestion_state: Path,
    replay: Path,
    archive: Path,
    target_like: str,
    apply: bool,
) -> dict[str, Any]:
    # Imported lazily so the lean retention classifier can import this module
    # for covered_revisions() without pulling in the heavy ingestion stack.
    import artifact_ingestion as ingestion

    sets = compute_unrepresented(
        catalog=catalog,
        ingestion_state=ingestion_state,
        replay=replay,
        archive=archive,
        target_like=target_like,
    )
    if not sets["complete"]:
        raise ArchiveError(
            "inputs incomplete, refusing to archive: " + "; ".join(sets["problems"])
        )

    wanted: set[str] = set(sets["unrepresented"])
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "mode": "apply" if apply else "plan",
        "authority": "content-preservation only; NOT replay authority (ADR-002 line 24)",
        "target_like": target_like,
        "revisions": {
            "serving": len(sets["serving"]),
            "already_attested": len(sets["archived"]),
            "unrepresented_before": len(wanted),
        },
        "sources": [],
        "skipped_sources": [],
        "units_archived": 0,
        "revisions_archived": 0,
        "unrepresented_after": len(wanted),
        "unrecoverable": [],
        "unrecoverable_count": 0,
    }
    if not wanted:
        return result
    if not outbox_root.is_dir():
        raise ArchiveError(f"outbox root is not a directory: {outbox_root}")

    # Unit-level truth. Coverage must be attested against the units the sink is
    # actually SERVING, because revision-granular coverage cannot tell a
    # complete archive from a truncated one, nor a 5000-char catalog rendering
    # from the 4000-char event rendering of the same revision (different
    # unit_ids entirely — archiving the wrong profile preserves units that are
    # not the ones being served).
    expected = expected_unit_ids(ingestion_state, target_like, wanted)

    connection = (
        open_archive(
            archive, create=True, forbid=(replay, catalog, ingestion_state)
        )
        if apply
        else None
    )
    covered: set[str] = set()
    simulated: dict[str, set[str]] = {}
    try:
        for child in sorted(outbox_root.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            remaining = wanted - covered
            if not remaining:
                break
            usable, why = _complete_source(child)
            if not usable:
                result["skipped_sources"].append({"outbox": child.name, "reason": why})
                continue

            if connection is not None:
                connection.execute("SAVEPOINT outbox_scan")
            found_units = 0
            touched: set[str] = set()
            try:
                manifest = ingestion.load_outbox_manifest(child)
                units_sha = manifest.get("units_sha256")
                run_id = manifest.get("catalog_run_id")
                # The VERIFIED reader: it enforces the outbox schema version and
                # recomputes the units digest against the manifest. Note it does
                # that check only AFTER yielding every unit, so the savepoint
                # below is what actually makes a corrupt tail harmless.
                for unit in ingestion.iter_outbox_units(child):
                    revision = str(unit.get("revision_id"))
                    if revision not in remaining:
                        continue
                    unit_id = str(unit.get("unit_id"))
                    found_units += 1
                    touched.add(revision)
                    if connection is None:
                        simulated.setdefault(revision, set()).add(unit_id)
                        continue
                    payload = json.dumps(
                        unit, separators=(",", ":"), ensure_ascii=False
                    )
                    # OR REPLACE (not OR IGNORE): re-archiving an unrepresented
                    # revision whose stored row is corrupt REPAIRS it with the freshly
                    # verified bytes -- self-heal (V-H1). A covered revision is not in
                    # `wanted`, so this never rewrites an already-good row.
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO archived_units(
                            unit_id, revision_id, point_id, source_outbox,
                            source_units_sha256, catalog_run_id, archived_at,
                            unit_sha256, unit_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            unit_id,
                            revision,
                            (
                                str(unit.get("qdrant_point_id"))
                                if unit.get("qdrant_point_id") is not None
                                else None
                            ),
                            child.name,
                            units_sha,
                            run_id,
                            _now(),
                            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                            payload,
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 - any failure discards the source
                # Truncated stream, end-of-iteration digest mismatch, schema
                # refusal: everything this outbox contributed is rolled back.
                # Without this, partial rows persisted and the next run counted
                # the revision as covered.
                if connection is not None:
                    connection.execute("ROLLBACK TO SAVEPOINT outbox_scan")
                    connection.execute("RELEASE SAVEPOINT outbox_scan")
                result["sources"].append(
                    {"outbox": child.name, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            if connection is not None:
                connection.execute("RELEASE SAVEPOINT outbox_scan")

            attested_now: list[str] = []
            for revision in sorted(touched):
                need = expected.get(revision, set())
                if not need:
                    continue
                if connection is not None:
                    # PAYLOAD-VALIDATED, exactly as covered_revisions() (V-H1): a
                    # corrupt stored row must not attest HERE either, or backfill's
                    # own report (revisions_archived / unrecoverable / the
                    # archived_revisions stamp) would diverge from the read-time
                    # coverage authority -- the "ONE coverage query" invariant.
                    have = {
                        str(unit_id)
                        for unit_id, unit_sha256, unit_json in connection.execute(
                            "SELECT unit_id, unit_sha256, unit_json FROM "
                            "archived_units WHERE revision_id=?",
                            (revision,),
                        )
                        if _payload_intact(str(unit_id), revision, unit_sha256, unit_json)
                    }
                else:
                    have = simulated.get(revision, set())
                # SUBSET, not equality: every unit the sink serves must be
                # preserved; extra units (e.g. another chunk profile) are
                # harmless. Checked against the DB so a revision whose units
                # span several outboxes still attests once complete.
                if need <= have:
                    covered.add(revision)
                    attested_now.append(revision)
                    if connection is not None:
                        connection.execute(
                            """
                            INSERT OR REPLACE INTO archived_revisions(
                                revision_id, unit_count, source_outbox, attested_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (revision, len(need), child.name, _now()),
                        )
            if found_units:
                result["sources"].append(
                    {
                        "outbox": child.name,
                        "units": found_units,
                        "revisions": len(touched),
                        "attested": len(attested_now),
                    }
                )
                result["units_archived"] += found_units
        if connection is not None:
            for key, value in (
                ("authority", "content-preservation-only"),
                (
                    "last_backfill",
                    {"at": _now(), "units": result["units_archived"]},
                ),
            ):
                connection.execute(
                    "INSERT INTO archive_meta(key, value_json) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                    (key, json.dumps(value)),
                )
            connection.commit()
    finally:
        if connection is not None:
            connection.close()

    result["revisions_archived"] = len(covered)
    missing = sorted(wanted - covered)
    result["unrepresented_after"] = len(missing)
    result["unrecoverable"] = missing[:50]
    result["unrecoverable_count"] = len(missing)
    return result


def status(
    *,
    catalog: Path,
    ingestion_state: Path,
    replay: Path,
    archive: Path,
    target_like: str,
) -> dict[str, Any]:
    sets = compute_unrepresented(
        catalog=catalog,
        ingestion_state=ingestion_state,
        replay=replay,
        archive=archive,
        target_like=target_like,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "archive": str(archive),
        "archive_exists": archive.is_file(),
        "inputs_complete": sets["complete"],
        "problems": sets["problems"],
        "serving": len(sets["serving"]),
        "covered_by_frozen_replay_set": len(sets["serving"] & sets["frozen"]),
        "reproducible_from_canonical": len(sets["serving"] & sets["current"]),
        "covered_by_archive": len(sets["serving"] & sets["archived"]),
        "unrepresented": len(sets["unrepresented"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backfill", "status"):
        p = sub.add_parser(name)
        p.add_argument("--outbox-root", type=Path, default=DEFAULT_OUTBOX_ROOT)
        p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
        p.add_argument("--ingestion-state", type=Path, default=DEFAULT_INGESTION_STATE)
        p.add_argument("--replay", type=Path, default=DEFAULT_REPLAY)
        p.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
        p.add_argument("--target-like", default=DEFAULT_TARGET_LIKE)
        if name == "backfill":
            p.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            payload = status(
                catalog=args.catalog,
                ingestion_state=args.ingestion_state,
                replay=args.replay,
                archive=args.archive,
                target_like=args.target_like,
            )
        else:
            payload = backfill(
                outbox_root=args.outbox_root,
                catalog=args.catalog,
                ingestion_state=args.ingestion_state,
                replay=args.replay,
                archive=args.archive,
                target_like=args.target_like,
                apply=args.apply,
            )
    except ArchiveError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

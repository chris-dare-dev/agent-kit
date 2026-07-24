#!/usr/bin/env python3
"""One composite watermark across the whole artifact-memory chain.

Every component of this system already reports its own health, and on
2026-07-18 every one of those surfaces was green while the pipeline had been
dead for roughly fourteen hours.  The bootstrap health file proved only that
``docker compose up`` succeeded; the consumer health file was thirteen hours
stale and still said ``healthy: true``; the service reported
``catalog_freshness: null``.  Component health is not system health.

This module answers the system-level question instead — how far behind is
each stage of ``receipt -> catalog -> outbox -> Qdrant -> retrieval``, who is
the active writer, and is anybody actually running — and it applies two rules
the per-component files do not:

* **Max age.**  A health file older than its own expected cadence is
  ``stale``, and a stale file NEVER contributes its self-reported ``healthy``
  verdict.  Silence is a failure signal, not an absence of one.
* **Own the gaps.**  A stage nobody observed is ``unknown``, which is an issue
  code, not a pass.

Nothing here mutates state, opens the vector store, or holds a lock, so it is
safe to run from a watchdog, a status command, or an incident shell.  It uses
only the standard library plus ``artifact_runtime`` for path discovery, so it
runs under ``/usr/bin/python3`` without the project virtualenv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import artifact_runtime  # noqa: E402


SCHEMA_VERSION = 1

CONSUMER_LABEL = "com.personal.artifact-event-consumer"
SERVICE_LABEL = "com.personal.artifact-memory-service"
BOOTSTRAP_LABEL = "com.personal.artifact-qdrant-bootstrap"
WATCHDOG_LABEL = "com.personal.artifact-watchdog"

# Expected cadences, derived from the installed LaunchAgents:
#   consumer  StartInterval 900   -> allow three intervals before alarming
#   bootstrap StartInterval 300   -> allow three intervals
#   service   KeepAlive, writes health on activity and at least every ~60 s
#   watchdog  StartInterval 300   -> allow three intervals
CONSUMER_MAX_AGE_SECONDS = 2700
BOOTSTRAP_MAX_AGE_SECONDS = 900
SERVICE_MAX_AGE_SECONDS = 600
WATCHDOG_MAX_AGE_SECONDS = 900
# The catalog is refreshed by the consumer run; give it two consumer cadences
# plus scan time before calling the run itself stale.
CATALOG_MAX_AGE_SECONDS = 7200
# ADR-002 SLO: artifact-to-search p95 <= 5 min.  A receipt the consumer has
# never observed is the direct measure of that SLO.
RECEIPT_MAX_AGE_SECONDS = 300
# Snapshots have no scheduled cadence yet (F-04); 24 h keeps the gap visible.
SNAPSHOT_MAX_AGE_SECONDS = 86400
VAULT_REPORT_MAX_AGE_SECONDS = 86400

STATE_OK = "ok"
STATE_STALE = "stale"
STATE_DEGRADED = "degraded"
STATE_MISSING = "missing"
STATE_UNKNOWN = "unknown"

# States that make the composite verdict unhealthy.
FAILING_STATES = frozenset((STATE_STALE, STATE_DEGRADED, STATE_MISSING, STATE_UNKNOWN))


class WatermarkError(RuntimeError):
    """The watermark could not be computed at all."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 or epoch timestamp, returning None when unusable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(moment: datetime | None, *, now: datetime | None = None) -> float | None:
    if moment is None:
        return None
    reference = now or _now()
    return max(0.0, (reference - moment).total_seconds())


def _isoformat(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, tolerating every way a health file can be absent."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _launchctl_loaded(label: str, *, timeout: float = 5.0) -> bool | None:
    """True/False when launchctl answers, None when it cannot be consulted.

    ``launchctl list <label>`` exits non-zero for a label that is not loaded,
    which is exactly the condition that let the consumer disappear silently.
    """
    try:
        completed = subprocess.run(
            ["/bin/launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.returncode == 0


def _health_file_component(
    path: Path,
    *,
    max_age_seconds: float,
    healthy_key: str,
    healthy_values: Iterable[Any],
    timestamp_keys: Sequence[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Grade a component health file under the max-age rule.

    A file that is too old is ``stale`` regardless of what it says about
    itself — that is the whole point.  The self-reported verdict is still
    carried through as ``reported`` so an operator can see the contradiction.
    """
    component: dict[str, Any] = {
        "path": str(path),
        "max_age_seconds": max_age_seconds,
        "observed_at": None,
        "age_seconds": None,
        "reported": None,
        "state": STATE_MISSING,
        "detail": None,
    }
    payload = _read_json(path)
    if payload is None:
        component["detail"] = "health file is absent or unreadable"
        return component

    moment: datetime | None = None
    for key in timestamp_keys:
        moment = _parse_timestamp(payload.get(key))
        if moment is not None:
            break
    age = _age_seconds(moment, now=now)
    reported = payload.get(healthy_key)
    component["observed_at"] = _isoformat(moment)
    component["age_seconds"] = age
    component["reported"] = reported

    if age is None:
        component["state"] = STATE_UNKNOWN
        component["detail"] = "health file carries no usable timestamp"
        return component
    if age > max_age_seconds:
        component["state"] = STATE_STALE
        component["detail"] = (
            f"health file is {age:.0f}s old, past its {max_age_seconds:.0f}s cadence"
            f" (it reports {healthy_key}={reported!r}, which is not trustworthy)"
        )
        return component
    if reported not in set(healthy_values):
        component["state"] = STATE_DEGRADED
        component["detail"] = f"health file reports {healthy_key}={reported!r}"
        return component
    component["state"] = STATE_OK
    return component


def catalog_component(
    catalog_path: Path, *, now: datetime | None = None
) -> dict[str, Any]:
    """Age and status of the newest complete catalog scan run."""
    component: dict[str, Any] = {
        "path": str(catalog_path),
        "max_age_seconds": CATALOG_MAX_AGE_SECONDS,
        "authoritative_run_id": None,
        "latest_attempt_run_id": None,
        "latest_attempt_status": None,
        "finished_at": None,
        "age_seconds": None,
        "state": STATE_MISSING,
        "detail": None,
    }
    if not catalog_path.is_file():
        component["detail"] = "catalog database is absent"
        return component
    try:
        with sqlite3.connect(
            f"file:{catalog_path.resolve()}?mode=ro", uri=True, timeout=5.0
        ) as connection:
            connection.row_factory = sqlite3.Row
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(scan_runs)")
            }
            if not columns:
                component["state"] = STATE_UNKNOWN
                component["detail"] = "catalog has no scan_runs table"
                return component
            has_status = "status" in columns
            latest = connection.execute(
                "SELECT run_id, finished_at"
                + (", status" if has_status else "")
                + " FROM scan_runs WHERE finished_at IS NOT NULL"
                " ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            if has_status:
                authoritative = connection.execute(
                    "SELECT run_id, finished_at FROM scan_runs"
                    " WHERE finished_at IS NOT NULL AND status='complete'"
                    " ORDER BY run_id DESC LIMIT 1"
                ).fetchone()
            else:
                authoritative = latest
    except sqlite3.Error as exc:
        component["state"] = STATE_UNKNOWN
        component["detail"] = f"catalog is unreadable: {type(exc).__name__}: {exc}"[:500]
        return component

    if latest is not None:
        component["latest_attempt_run_id"] = int(latest["run_id"])
        component["latest_attempt_status"] = (
            str(latest["status"]) if has_status else "complete"
        )
    if authoritative is None:
        component["state"] = STATE_MISSING
        component["detail"] = "catalog has no complete scan run"
        return component

    component["authoritative_run_id"] = int(authoritative["run_id"])
    finished = _parse_timestamp(authoritative["finished_at"])
    age = _age_seconds(finished, now=now)
    component["finished_at"] = _isoformat(finished)
    component["age_seconds"] = age
    if age is None:
        component["state"] = STATE_UNKNOWN
        component["detail"] = "complete scan run has no usable finished_at"
    elif age > CATALOG_MAX_AGE_SECONDS:
        component["state"] = STATE_STALE
        component["detail"] = f"newest complete catalog run is {age:.0f}s old"
    elif component["latest_attempt_status"] not in (None, "complete"):
        component["state"] = STATE_DEGRADED
        component["detail"] = (
            f"latest attempt run {component['latest_attempt_run_id']} is"
            f" {component['latest_attempt_status']}"
        )
    else:
        component["state"] = STATE_OK
    return component


def _observed_event_ids(consumer_state: Path) -> set[str] | None:
    if not consumer_state.is_file():
        return set()
    try:
        with sqlite3.connect(
            f"file:{consumer_state.resolve()}?mode=ro", uri=True, timeout=5.0
        ) as connection:
            return {
                str(row[0])
                for row in connection.execute("SELECT event_id FROM consumer_events")
            }
    except sqlite3.Error:
        return None


def receipts_component(
    receipt_root: Path, consumer_state: Path, *, now: datetime | None = None
) -> dict[str, Any]:
    """Oldest receipt the consumer has never observed — the SLO watermark.

    This is the field that would have caught the outage: receipts kept landing
    while nothing consumed them, and no surface reported the growing gap.
    """
    component: dict[str, Any] = {
        "receipt_root": str(receipt_root),
        "max_age_seconds": RECEIPT_MAX_AGE_SECONDS,
        "total": 0,
        "unobserved": 0,
        "oldest_unobserved_event_id": None,
        "oldest_unobserved_at": None,
        "oldest_unobserved_age_seconds": None,
        "state": STATE_UNKNOWN,
        "detail": None,
    }
    if not receipt_root.is_dir():
        component["state"] = STATE_MISSING
        component["detail"] = "receipt root is absent"
        return component

    observed = _observed_event_ids(consumer_state)
    if observed is None:
        component["state"] = STATE_UNKNOWN
        component["detail"] = "consumer state is unreadable; cannot compute the gap"
        return component

    oldest_moment: datetime | None = None
    oldest_event: str | None = None
    total = 0
    unobserved = 0
    for path in sorted(receipt_root.rglob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        total += 1
        event_id = f"event:{path.stem}"
        if event_id in observed:
            continue
        unobserved += 1
        try:
            moment = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if oldest_moment is None or moment < oldest_moment:
            oldest_moment = moment
            oldest_event = event_id

    component["total"] = total
    component["unobserved"] = unobserved
    component["oldest_unobserved_event_id"] = oldest_event
    component["oldest_unobserved_at"] = _isoformat(oldest_moment)
    age = _age_seconds(oldest_moment, now=now)
    component["oldest_unobserved_age_seconds"] = age
    if unobserved == 0:
        component["state"] = STATE_OK
    elif age is not None and age > RECEIPT_MAX_AGE_SECONDS:
        component["state"] = STATE_STALE
        component["detail"] = (
            f"{unobserved} unobserved receipt(s); oldest is {age:.0f}s old,"
            f" past the {RECEIPT_MAX_AGE_SECONDS:.0f}s artifact-to-search SLO"
        )
    else:
        component["state"] = STATE_DEGRADED
        component["detail"] = f"{unobserved} unobserved receipt(s) within the SLO window"
    return component


def _consumer_last_success(consumer_state: Path) -> datetime | None:
    if not consumer_state.is_file():
        return None
    try:
        with sqlite3.connect(
            f"file:{consumer_state.resolve()}?mode=ro", uri=True, timeout=5.0
        ) as connection:
            row = connection.execute(
                "SELECT MAX(updated_at) FROM consumer_events WHERE status='completed'"
            ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else _parse_timestamp(row[0])


def consumer_component(
    health_path: Path,
    consumer_state: Path,
    *,
    label: str = CONSUMER_LABEL,
    loaded_probe: Any = _launchctl_loaded,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Consumer liveness: health-file freshness AND launchd loaded-ness.

    A consumer that is not loaded cannot write a health file at all, so the
    absence of a fresh file and the absence of the job are the same outage
    seen from two sides.  Both are reported; either one is disqualifying.
    """
    component = _health_file_component(
        health_path,
        max_age_seconds=CONSUMER_MAX_AGE_SECONDS,
        healthy_key="healthy",
        healthy_values=(True,),
        timestamp_keys=("observed_at",),
        now=now,
    )
    payload = _read_json(health_path) or {}
    component["label"] = label
    component["issue_codes"] = payload.get("issue_codes") or []
    component["unobserved_receipts"] = payload.get("unobserved_receipts")
    last_success = _consumer_last_success(consumer_state)
    component["last_success_at"] = _isoformat(last_success)
    component["last_success_age_seconds"] = _age_seconds(last_success, now=now)

    loaded = loaded_probe(label)
    component["loaded"] = loaded
    if loaded is False:
        component["state"] = STATE_MISSING
        component["detail"] = (
            f"LaunchAgent {label} is NOT loaded; the consumer cannot run"
            " and cannot report its own absence"
        )
    elif loaded is None and component["state"] == STATE_OK:
        component["state"] = STATE_UNKNOWN
        component["detail"] = "launchctl could not be consulted"
    return component


def _plist_arguments(plist_path: Path) -> list[str]:
    try:
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return []
    arguments = payload.get("ProgramArguments")
    return [str(item) for item in arguments] if isinstance(arguments, list) else []


def _argument_value(arguments: Sequence[str], flag: str) -> str | None:
    for index, item in enumerate(arguments):
        if item == flag and index + 1 < len(arguments):
            return arguments[index + 1]
    return None


def writer_component(
    runtime: Any,
    consumer_plist: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Identity of the ACTIVE writer sink, and whether it matches the reader.

    The installed consumer job writes to an embedded path via
    ``--qdrant-path``/``--collection`` while the runtime config serves from the
    server generation.  Nothing in either process compares the two, so this is
    the surface that makes the split-brain visible before a reload creates it.
    """
    del now
    component: dict[str, Any] = {
        "plist": str(consumer_plist),
        "configured_sink": None,
        "active_backend": None,
        "active_generation": None,
        "active_collection": None,
        "rollback_embedded_mode": None,
        "aligned": None,
        "state": STATE_UNKNOWN,
        "detail": None,
    }
    if runtime is None:
        component["detail"] = "runtime configuration is unavailable"
        return component

    component["active_backend"] = getattr(runtime, "active_backend", None)
    component["active_generation"] = getattr(runtime, "qdrant_generation", None)
    component["active_collection"] = getattr(runtime, "qdrant_collection", None)
    component["rollback_embedded_mode"] = getattr(runtime, "rollback_mode", None)

    arguments = _plist_arguments(consumer_plist)
    if not arguments:
        component["detail"] = "consumer job definition is absent or unreadable"
        return component

    qdrant_path = _argument_value(arguments, "--qdrant-path")
    qdrant_url = _argument_value(arguments, "--qdrant-url")
    collection = _argument_value(arguments, "--collection")
    if qdrant_url:
        sink = f"url:{qdrant_url}|{collection or 'unknown'}"
        sink_backend = "server"
    elif qdrant_path:
        sink = f"local:{Path(qdrant_path).expanduser().absolute()}|{collection or 'unknown'}"
        sink_backend = "embedded"
    else:
        component["detail"] = "consumer job names no Qdrant sink"
        return component
    component["configured_sink"] = sink
    component["configured_backend"] = sink_backend

    backend_matches = sink_backend == component["active_backend"]
    collection_matches = collection == component["active_collection"]
    component["aligned"] = bool(backend_matches and collection_matches)
    if component["aligned"]:
        component["state"] = STATE_OK
        return component

    component["state"] = STATE_DEGRADED
    reasons = []
    if not backend_matches:
        reasons.append(
            f"writer targets {sink_backend} but the active backend is"
            f" {component['active_backend']}"
        )
    if not collection_matches:
        reasons.append(
            f"writer targets collection {collection!r} but readers serve"
            f" {component['active_collection']!r}"
        )
    if component["rollback_embedded_mode"] == "read-only" and sink_backend == "embedded":
        reasons.append("the embedded store is declared read-only for rollback")
    component["detail"] = "; ".join(reasons)
    return component


QUARANTINE_ACK_FILENAME = "quarantine-acknowledgments.json"


# Folded into the digest, so changing the algorithm invalidates every stored
# acknowledgment: they REOPEN for re-review instead of silently inheriting an
# approval that was granted under different semantics.
QUARANTINE_FINGERPRINT_VERSION = "v2-content"


def quarantine_fingerprint(path: Path) -> str | None:
    """Content identity for one quarantined outbox directory.

    An acknowledgment is bound to the exact on-disk state that was reviewed:
    any change afterwards must REOPEN the entry rather than inherit the old
    approval. v1 hashed only relative path + size and skipped every non-file,
    so a SAME-SIZE byte flip kept its acknowledgment, and an added directory or
    symlink was invisible. v2 hashes file CONTENT and records an explicit type
    marker for every entry, including symlink targets (never followed).
    """
    def _reraise(error: OSError) -> None:
        # os.walk swallows scan errors by default (as rglob did), so an
        # unreadable SUBDIRECTORY was silently omitted and the fingerprint
        # stayed stable across hidden content changes — fail-open in the one
        # invariant this function exists to hold. An unreadable FILE already
        # failed closed, so the guarantee depended on which node type lost
        # permissions. Re-raise into the OSError handler below.
        raise error

    digest = hashlib.sha256()
    digest.update(f"{QUARANTINE_FINGERPRINT_VERSION}\0".encode("utf-8"))
    try:
        walked: list[Path] = []
        for parent, dirnames, filenames in os.walk(path, onerror=_reraise):
            base = Path(parent)
            walked.extend(base / name for name in dirnames)
            walked.extend(base / name for name in filenames)
        for child in sorted(walked):
            relative = child.relative_to(path).as_posix()
            if child.is_symlink():
                # Recorded, never followed — swapping a symlink must not be
                # able to smuggle different content past the acknowledgment.
                try:
                    target = child.readlink().as_posix()
                except OSError:
                    target = "<unreadable>"
                digest.update(
                    f"L\0{relative}\0{target}\n".encode("utf-8", "replace")
                )
                continue
            if child.is_dir():
                digest.update(f"D\0{relative}\n".encode("utf-8", "replace"))
                continue
            if not child.is_file():
                # Sockets/FIFOs/devices: recorded by name, never opened.
                digest.update(f"?\0{relative}\n".encode("utf-8", "replace"))
                continue
            content = hashlib.sha256()
            with child.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    content.update(block)
            digest.update(
                f"F\0{relative}\0{child.stat().st_size}\0"
                f"{content.hexdigest()}\n".encode("utf-8", "replace")
            )
    except OSError:
        return None
    return digest.hexdigest()


def load_quarantine_acknowledgments(path: Path) -> dict[str, dict[str, Any]]:
    """Acknowledgment registry keyed by quarantined-directory name."""
    payload = _read_json(path)
    if not payload or not isinstance(payload.get("entries"), list):
        return {}
    ledger: dict[str, dict[str, Any]] = {}
    for raw in payload["entries"]:
        if isinstance(raw, dict) and isinstance(raw.get("name"), str):
            ledger[raw["name"]] = raw
    return ledger


def outbox_quarantine_component(
    dead_letter_root: Path,
    ack_ledger: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Quarantined outbox directories — the second, unsurfaced 'dead letter'.

    ``consumer status`` reports dead-letter DB *records*; a quarantined outbox
    *directory* is a different object that shares the name and appears in no
    status output, so an operator auditing dead letters misses it entirely.

    Surfacing must not mean forever-red: a reviewed directory that is
    deliberately retained (see the residue ledger) can be ACKNOWLEDGED via
    ``consumer quarantine-ack``. Every directory stays in ``entries`` and
    ``count`` — nothing is hidden — but only ``open`` (unacknowledged,
    content-changed, or past its review date) items degrade health.
    """
    ledger_path = (
        ack_ledger
        if ack_ledger is not None
        else dead_letter_root.parent / QUARANTINE_ACK_FILENAME
    )
    component: dict[str, Any] = {
        "root": str(dead_letter_root),
        "count": 0,
        "open": 0,
        "acknowledged": 0,
        "ack_ledger": str(ledger_path),
        "entries": [],
        "state": STATE_OK,
        "detail": None,
    }
    try:
        root_exists = dead_letter_root.is_dir()
    except OSError as exc:
        # An unreadable PARENT makes even the existence probe raise EACCES.
        component["state"] = STATE_UNKNOWN
        component["detail"] = f"quarantine root is unreadable: {exc}"[:500]
        return component
    if not root_exists:
        component["detail"] = "no outbox quarantine directory exists"
        return component

    acknowledgments = load_quarantine_acknowledgments(ledger_path)
    moment = now or _now()
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(dead_letter_root.iterdir())
    except OSError as exc:
        component["state"] = STATE_UNKNOWN
        component["detail"] = f"quarantine root is unreadable: {exc}"[:500]
        return component

    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
        except OSError as exc:
            # An r-without-x root LISTS names but cannot stat them. Uncaught,
            # this PermissionError escaped the component, propagated through
            # compute_watermark, and killed watchdog.run() before it wrote its
            # log, health file, or notification — the exact silent no-alarm
            # class this surface exists to prevent. Fail closed instead.
            component["state"] = STATE_UNKNOWN
            component["detail"] = (
                f"quarantine entry {child.name!r} is unreadable: {exc}"[:500]
            )
            return component
        entry: dict[str, Any] = {"name": child.name, "quarantined_at": None, "reason": None}
        try:
            entry["quarantined_at"] = _isoformat(
                datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
            )
        except OSError:
            pass
        audit = _read_json(child / "audit.json")
        if audit is not None:
            for key in ("reason", "code", "status", "detail"):
                if isinstance(audit.get(key), str):
                    entry["reason"] = audit[key]
                    break
        acknowledgment = acknowledgments.get(child.name)
        if acknowledgment is None:
            # Nothing to verify against — the entry is open whatever it holds,
            # so skip the (potentially very large) content hash entirely.
            entry["fingerprint"] = None
            entry["ack"] = "open"
        else:
            fingerprint = quarantine_fingerprint(child)
            entry["fingerprint"] = fingerprint
            if fingerprint is None or acknowledgment.get("fingerprint") != fingerprint:
                entry["ack"] = "reopened-changed"
            else:
                review_by = _parse_timestamp(acknowledgment.get("review_by"))
                if review_by is None:
                    # FAIL CLOSED. A missing or unparseable review date is NOT
                    # "no deadline": it is an acknowledgment that can never age
                    # out, so it would suppress this entry forever. Previously
                    # _parse_timestamp returning None fell through to
                    # "acknowledged"; now it demands re-review.
                    entry["ack"] = "reopened-invalid-review"
                elif moment >= review_by:
                    entry["ack"] = "reopened-review-due"
                else:
                    entry["ack"] = "acknowledged"
                    entry["acknowledged_at"] = acknowledgment.get("acknowledged_at")
                    entry["ack_reason"] = acknowledgment.get("reason")
                    entry["review_by"] = acknowledgment.get("review_by")
        entries.append(entry)

    open_entries = [item for item in entries if item.get("ack") != "acknowledged"]
    component["entries"] = entries
    component["count"] = len(entries)
    component["open"] = len(open_entries)
    component["acknowledged"] = len(entries) - len(open_entries)
    if open_entries:
        component["state"] = STATE_DEGRADED
        acknowledged = len(entries) - len(open_entries)
        component["detail"] = (
            f"{len(open_entries)} quarantined outbox director(ies) await "
            "disposition"
            + (f"; {acknowledged} acknowledged" if acknowledged else "")
        )
    elif entries:
        component["detail"] = (
            f"{len(entries)} quarantined director(ies) acknowledged as "
            "retained residue"
        )
    return component


def snapshots_component(
    snapshot_root: Path, *, now: datetime | None = None
) -> dict[str, Any]:
    component: dict[str, Any] = {
        "root": str(snapshot_root),
        "max_age_seconds": SNAPSHOT_MAX_AGE_SECONDS,
        "count": 0,
        "newest_name": None,
        "newest_at": None,
        "newest_age_seconds": None,
        "state": STATE_MISSING,
        "detail": None,
    }
    if not snapshot_root.is_dir():
        component["detail"] = "no snapshot directory exists"
        return component

    newest_moment: datetime | None = None
    newest_name: str | None = None
    count = 0
    for path in sorted(snapshot_root.glob("*.snapshot")):
        if path.is_symlink() or not path.is_file():
            continue
        # Restore-drill probes are evidence, not recovery points.
        if path.name.startswith("corrupt-"):
            continue
        count += 1
        try:
            moment = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if newest_moment is None or moment > newest_moment:
            newest_moment = moment
            newest_name = path.name

    component["count"] = count
    component["newest_name"] = newest_name
    component["newest_at"] = _isoformat(newest_moment)
    age = _age_seconds(newest_moment, now=now)
    component["newest_age_seconds"] = age
    if count == 0:
        component["detail"] = "no snapshots exist"
    elif age is None:
        component["state"] = STATE_UNKNOWN
        component["detail"] = "newest snapshot has no usable timestamp"
    elif age > SNAPSHOT_MAX_AGE_SECONDS:
        component["state"] = STATE_STALE
        component["detail"] = (
            f"newest snapshot is {age / 3600:.1f}h old and no snapshot job is scheduled"
        )
    else:
        component["state"] = STATE_OK
    return component


def vault_component(report_path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Last known Obsidian validator verdict.

    The validator is too expensive to run inside a 5-minute watchdog, so the
    watermark reports the newest cached report and calls an absent or stale
    one ``unknown`` rather than pretending the projection is clean.
    """
    component: dict[str, Any] = {
        "report": str(report_path),
        "max_age_seconds": VAULT_REPORT_MAX_AGE_SECONDS,
        "error_count": None,
        "warning_count": None,
        "observed_at": None,
        "age_seconds": None,
        "state": STATE_UNKNOWN,
        "detail": "no cached validator report; run obsidian_vault_validate.py --json",
    }
    payload = _read_json(report_path)
    if payload is None:
        return component

    moment: datetime | None = None
    for key in ("generated_at", "observed_at", "updated_at", "checked_at"):
        moment = _parse_timestamp(payload.get(key))
        if moment is not None:
            break
    if moment is None:
        try:
            moment = datetime.fromtimestamp(report_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            moment = None
    age = _age_seconds(moment, now=now)
    errors = payload.get("error_count")
    warnings = payload.get("warning_count")
    component.update(
        {
            "error_count": errors if isinstance(errors, int) else None,
            "warning_count": warnings if isinstance(warnings, int) else None,
            "observed_at": _isoformat(moment),
            "age_seconds": age,
            "detail": None,
        }
    )
    if age is not None and age > VAULT_REPORT_MAX_AGE_SECONDS:
        component["state"] = STATE_STALE
        component["detail"] = f"validator report is {age / 3600:.1f}h old"
    elif component["error_count"] is None:
        component["state"] = STATE_UNKNOWN
        component["detail"] = "validator report carries no error_count"
    elif component["error_count"]:
        component["state"] = STATE_DEGRADED
        component["detail"] = f"validator reports {component['error_count']} error(s)"
    else:
        component["state"] = STATE_OK
    return component


def graphiti_component(runtime: Any) -> dict[str, Any]:
    """Graphiti must stay candidate-only and write-disabled."""
    del runtime
    return {
        "write_enabled": False,
        "state": STATE_OK,
        "detail": "graphiti writes remain disabled (candidate metadata only)",
    }


def _resolve_paths(runtime: Any, derived_root: Path) -> dict[str, Path]:
    """Prefer runtime-declared paths, fall back to the derived-root layout."""

    def _from_runtime(attribute: str, default: Path) -> Path:
        value = getattr(runtime, attribute, None) if runtime is not None else None
        return Path(value) if isinstance(value, (str, Path)) else default

    return {
        "catalog": _from_runtime("catalog", derived_root / "artifact-catalog.sqlite3"),
        "consumer_state": _from_runtime(
            "consumer_state", derived_root / "artifact-event-consumer.sqlite3"
        ),
        "receipt_root": _from_runtime("receipt_root", derived_root / "skill-events"),
        "outbox_root": _from_runtime("outbox_root", derived_root / "outbox"),
        "snapshot_root": _from_runtime(
            "snapshot_root", derived_root / "services" / "qdrant" / "snapshots"
        ),
    }


def compute_watermark(
    *,
    derived_root: Path | None = None,
    script_dir: Path | None = None,
    runtime_config: Path | None = None,
    loaded_probe: Any = _launchctl_loaded,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the composite watermark across every stage of the chain."""
    moment = now or _now()
    derived = (derived_root or artifact_runtime.DEFAULT_DERIVED_ROOT).expanduser()
    scripts = script_dir or SCRIPT_DIR

    runtime: Any = None
    runtime_error: str | None = None
    try:
        config_path = runtime_config or (derived / "artifact-memory-runtime.json")
        runtime = artifact_runtime.load_runtime(config_path)
    except Exception as exc:  # runtime schema and privacy errors are both possible
        runtime_error = f"{type(exc).__name__}: {exc}"[:500]

    paths = _resolve_paths(runtime, derived)
    # Snapshot root can also be discovered from the runtime's key-file parent.
    admin_key = getattr(runtime, "qdrant_admin_key_file", None)
    if admin_key is not None:
        candidate = Path(admin_key).parent / "snapshots"
        if candidate.is_dir():
            paths["snapshot_root"] = candidate

    components = {
        "catalog": catalog_component(paths["catalog"], now=moment),
        "receipts": receipts_component(
            paths["receipt_root"], paths["consumer_state"], now=moment
        ),
        "consumer": consumer_component(
            derived / "artifact-event-consumer-health.json",
            paths["consumer_state"],
            loaded_probe=loaded_probe,
            now=moment,
        ),
        "writer": writer_component(
            runtime, scripts / f"{CONSUMER_LABEL}.plist", now=moment
        ),
        "service": _health_file_component(
            derived / "artifact-memory-service-health.json",
            max_age_seconds=SERVICE_MAX_AGE_SECONDS,
            healthy_key="status",
            healthy_values=("healthy",),
            timestamp_keys=("updated_unix", "updated_at"),
            now=moment,
        ),
        "bootstrap": _health_file_component(
            derived / "artifact-qdrant-bootstrap-health.json",
            max_age_seconds=BOOTSTRAP_MAX_AGE_SECONDS,
            healthy_key="status",
            healthy_values=("healthy",),
            timestamp_keys=("updated_at",),
            now=moment,
        ),
        "watchdog": _health_file_component(
            derived / "artifact-watchdog-health.json",
            max_age_seconds=WATCHDOG_MAX_AGE_SECONDS,
            healthy_key="status",
            healthy_values=("healthy",),
            timestamp_keys=("observed_at",),
            now=moment,
        ),
        "snapshots": snapshots_component(paths["snapshot_root"], now=moment),
        "outbox_quarantine": outbox_quarantine_component(
            derived / "outbox-dead-letter"
        ),
        "vault": vault_component(derived / "vault-validator-report.json", now=moment),
        "graphiti": graphiti_component(runtime),
    }

    issue_codes = sorted(
        f"{name}_{component['state']}"
        for name, component in components.items()
        if component["state"] in FAILING_STATES
    )
    if runtime_error is not None:
        issue_codes.append("runtime_config_unreadable")
        issue_codes.sort()

    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": moment.isoformat(),
        "healthy": not issue_codes,
        "issue_codes": issue_codes,
        "runtime_error": runtime_error,
        "active_backend": getattr(runtime, "active_backend", None),
        "active_generation": getattr(runtime, "qdrant_generation", None),
        "components": components,
    }


def render_human(watermark: dict[str, Any]) -> str:
    """One line per stage, worst news first in the header."""
    verdict = "HEALTHY" if watermark["healthy"] else "UNHEALTHY"
    lines = [
        f"artifact-memory watermark: {verdict}  ({watermark['observed_at']})",
        f"  active: backend={watermark['active_backend']}"
        f" generation={watermark['active_generation']}",
    ]
    if watermark.get("runtime_error"):
        lines.append(f"  runtime config: {watermark['runtime_error']}")
    for name, component in watermark["components"].items():
        state = str(component["state"]).upper()
        detail = component.get("detail")
        line = f"  {name:<18} {state:<9}"
        age = component.get("age_seconds")
        if age is None:
            age = component.get("oldest_unobserved_age_seconds")
        if age is None:
            age = component.get("newest_age_seconds")
        if isinstance(age, (int, float)):
            line += f" age={age / 60:.1f}m"
        if detail:
            line += f"  {detail}"
        lines.append(line)
    if watermark["issue_codes"]:
        lines.append(f"  issues: {', '.join(watermark['issue_codes'])}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--derived-root",
        type=Path,
        default=None,
        help="derived state root (default: ~/.local/share/personal-artifacts)",
    )
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=None,
        help="runtime configuration file (default: <derived-root>/artifact-memory-runtime.json)",
    )
    parser.add_argument("--json", action="store_true", help="emit the stable JSON document")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    watermark = compute_watermark(
        derived_root=args.derived_root,
        runtime_config=args.runtime_config,
    )
    if args.json:
        print(json.dumps(watermark, indent=2, sort_keys=True))
    else:
        print(render_human(watermark))
    return 0 if watermark["healthy"] else 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())

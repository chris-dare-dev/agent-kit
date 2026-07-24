#!/usr/bin/env python3
"""External watchdog for the artifact-memory pipeline.

The only alert channel this system had was an ``osascript`` notification fired
*from the consumer itself*, on a health-state transition.  A consumer that is
not loaded never runs, never transitions, and never notifies — so the one
failure the alerting most needed to catch was the exact failure it structurally
could not report.  The pipeline was down roughly fourteen hours and told
nobody.

This watchdog is deliberately **not the patient**.  It runs from its own
LaunchAgent (and is also invoked by the Qdrant bootstrap job, which already
runs every 300 s), it makes no assumption that the consumer is alive, and it
asserts two things the consumer cannot assert about itself:

1. the consumer LaunchAgent is *loaded*, and
2. the consumer *ran* within its expected cadence.

Alerts go to two durable channels: an append-only JSONL log that survives
reboots and unread notifications, and a desktop notification raised by the
watchdog process.  Notifications fire on state transitions so a persistent
outage does not become a notification storm, but ``--force-notify`` and the
log line make the condition observable at any time.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import artifact_runtime  # noqa: E402
import artifact_security as security  # noqa: E402
import artifact_watermark as watermark  # noqa: E402


SCHEMA_VERSION = 1

DEFAULT_LOG = Path("~/Library/Logs/personal-artifact-watchdog.jsonl").expanduser()
HEALTH_FILENAME = "artifact-watchdog-health.json"

# Issue codes that mean the pipeline itself is not running, as opposed to a
# downstream component being merely behind.  These are what the watchdog
# exists to shout about.
PIPELINE_DOWN_CODES = frozenset(
    (
        "consumer_missing",
        "consumer_stale",
        "consumer_unknown",
        "receipts_stale",
        "writer_degraded",
    )
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _desktop_notify(title: str, message: str, *, timeout: float = 5.0) -> bool:
    """Raise a notification FROM THE WATCHDOG, never from the patient."""
    safe_title = title.replace('"', "'")[:100]
    safe_message = message.replace('"', "'")[:200]
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        completed = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _append_log(log_path: Path, record: dict[str, Any]) -> bool:
    """Append one JSON line.  The log is the channel that cannot be missed."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        return False
    return True


def evaluate(
    watermark_payload: dict[str, Any],
    *,
    consumer_max_age_seconds: float,
) -> dict[str, Any]:
    """Turn a watermark into the watchdog's own verdict.

    The watchdog's question is narrower than the watermark's: *is the write
    path running at all?*  A stale snapshot or a dirty vault projection is
    reported but does not raise the alarm.
    """
    components = watermark_payload.get("components", {})
    consumer = components.get("consumer", {})
    receipts = components.get("receipts", {})

    loaded = consumer.get("loaded")
    last_run_age = consumer.get("age_seconds")
    ran_recently = (
        last_run_age is not None and float(last_run_age) <= consumer_max_age_seconds
    )

    failures: list[str] = []
    if loaded is False:
        failures.append("consumer LaunchAgent is not loaded")
    elif loaded is None:
        failures.append("consumer loaded-state could not be determined")
    if last_run_age is None:
        failures.append("consumer has never reported a run")
    elif not ran_recently:
        failures.append(
            f"consumer last ran {float(last_run_age) / 60:.0f}m ago,"
            f" past its {consumer_max_age_seconds / 60:.0f}m cadence"
        )

    unobserved = receipts.get("unobserved") or 0
    oldest_age = receipts.get("oldest_unobserved_age_seconds")
    if unobserved and oldest_age is not None and float(oldest_age) > 900:
        failures.append(
            f"{unobserved} receipt(s) unconsumed; oldest {float(oldest_age) / 60:.0f}m old"
        )

    writer = components.get("writer", {})
    if writer.get("aligned") is False:
        failures.append(f"writer sink misaligned: {writer.get('detail')}")

    # H5: retained residue (DEGRADED = open items) stays deliberately quiet
    # here — that is "downstream behind", not "the write path is down". But an
    # UNKNOWN quarantine component means the surface itself could not be READ,
    # which is exactly the can't-verify class this watchdog exists to shout
    # about. It previously reached no alarm at all: quarantine codes are
    # excluded from PIPELINE_DOWN_CODES, and pipeline_issue_codes never affect
    # status (which keys only on `failures`).
    quarantine = components.get("outbox_quarantine", {})
    if quarantine.get("state") == watermark.STATE_UNKNOWN:
        failures.append(
            f"quarantine state undeterminable: {quarantine.get('detail')}"
        )

    pipeline_issues = sorted(
        set(watermark_payload.get("issue_codes", [])) & PIPELINE_DOWN_CODES
    )
    return {
        "status": "healthy" if not failures else "unhealthy",
        "consumer_loaded": loaded,
        "consumer_ran_recently": ran_recently,
        "consumer_last_run_age_seconds": last_run_age,
        "consumer_last_success_at": consumer.get("last_success_at"),
        "unobserved_receipts": unobserved,
        "oldest_unobserved_age_seconds": oldest_age,
        "failures": failures,
        "pipeline_issue_codes": pipeline_issues,
    }


def _previous_status(health_path: Path) -> str | None:
    try:
        payload = json.loads(health_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    status = payload.get("status") if isinstance(payload, dict) else None
    return status if isinstance(status, str) else None


def run(
    *,
    derived_root: Path | None = None,
    log_path: Path | None = None,
    consumer_max_age_seconds: float = watermark.CONSUMER_MAX_AGE_SECONDS,
    notify: bool = True,
    force_notify: bool = False,
    notifier: Callable[[str, str], bool] = _desktop_notify,
    loaded_probe: Any = watermark._launchctl_loaded,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = now or _now()
    derived = (derived_root or artifact_runtime.DEFAULT_DERIVED_ROOT).expanduser()
    health_path = derived / HEALTH_FILENAME
    log = log_path or DEFAULT_LOG

    # A watchdog must not die of the patient's disease. An unreadable directory
    # anywhere in the watermark walk used to raise straight through here, so
    # run() exited with a traceback BEFORE writing its log line, health file,
    # or notification — leaving the previous (possibly "healthy") health file
    # as the last word. Nothing else notices: the watchdog-health staleness
    # component is evaluated only by this watchdog, so the failure is perfectly
    # circular. Degrade to a loud synthetic verdict instead of dying.
    try:
        payload = watermark.compute_watermark(
            derived_root=derived, loaded_probe=loaded_probe, now=moment
        )
        verdict = evaluate(payload, consumer_max_age_seconds=consumer_max_age_seconds)
    except Exception as exc:  # noqa: BLE001 - deliberate last-resort boundary
        detail = f"{type(exc).__name__}: {exc}"
        payload = {
            "issue_codes": ["watermark_computation_failed"],
            "healthy": False,
            "components": {},
            "error": detail[:2000],
        }
        verdict = {
            "status": "unhealthy",
            "consumer_loaded": None,
            "consumer_ran_recently": False,
            "consumer_last_run_age_seconds": None,
            "consumer_last_success_at": None,
            "unobserved_receipts": 0,
            "oldest_unobserved_age_seconds": None,
            "failures": [f"watermark computation crashed: {detail}"[:500]],
            "pipeline_issue_codes": [],
        }

    previous = _previous_status(health_path)
    transition = previous != verdict["status"]

    # Notify first, so the durable log records whether the alert actually
    # fired.  A notification that silently failed to display is exactly the
    # kind of thing the log has to be able to prove after the fact.
    notified = False
    notify_attempted = bool(
        notify and verdict["status"] != "healthy" and (transition or force_notify)
    )
    if notify_attempted:
        headline = verdict["failures"][0] if verdict["failures"] else "pipeline unhealthy"
        notified = notifier("workspace artifact memory", f"Pipeline down: {headline}")

    record = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": moment.isoformat(),
        "status": verdict["status"],
        "previous_status": previous,
        "transition": transition,
        "failures": verdict["failures"],
        "consumer_loaded": verdict["consumer_loaded"],
        "consumer_last_run_age_seconds": verdict["consumer_last_run_age_seconds"],
        "unobserved_receipts": verdict["unobserved_receipts"],
        "watermark_issue_codes": payload.get("issue_codes", []),
        "watermark_healthy": payload.get("healthy"),
        "notify_attempted": notify_attempted,
        "notified": notified,
    }
    record["log_written"] = _append_log(log, record)

    health = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": moment.isoformat(),
        "status": verdict["status"],
        "failures": verdict["failures"],
        "verdict": verdict,
        "watermark": payload,
        "log_path": str(log),
        "notified": notified,
    }
    try:
        security.atomic_write_json(health_path, health, replace=True)
        record["health_written"] = True
    except Exception as exc:  # a watchdog must not die writing its own health
        record["health_written"] = False
        record["health_error"] = f"{type(exc).__name__}: {exc}"[:300]

    return record


def _log_lines(log_path: Path) -> list[dict[str, Any]]:
    """Read back the append-only log; used by tests and incident review."""
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            lines.append(json.loads(line))
        except ValueError:
            continue
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--derived-root", type=Path, default=None)
    parser.add_argument(
        "--log", type=Path, default=None, help=f"append-only log (default: {DEFAULT_LOG})"
    )
    parser.add_argument(
        "--consumer-max-age-seconds",
        type=float,
        default=watermark.CONSUMER_MAX_AGE_SECONDS,
        help="how long the consumer may go without a run before alarming",
    )
    parser.add_argument(
        "--no-notify", action="store_true", help="suppress the desktop notification"
    )
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="notify on every unhealthy run, not only on transitions",
    )
    parser.add_argument("--json", action="store_true", help="emit the run record as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    record = run(
        derived_root=args.derived_root,
        log_path=args.log,
        consumer_max_age_seconds=args.consumer_max_age_seconds,
        notify=not args.no_notify,
        force_notify=args.force_notify,
    )
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"artifact-watchdog: {record['status']}")
        for failure in record["failures"]:
            print(f"  - {failure}")
    return 0 if record["status"] == "healthy" else 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())

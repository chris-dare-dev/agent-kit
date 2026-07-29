#!/usr/bin/env python3
"""Append-only receipts for artifact-memory LaunchAgent state changes.

F-18 recorded the gap this closes: when the receipt consumer left ``launchctl``
around 2026-07-18 00:57Z, nothing recorded when or why. The last consumer
output and the plist mtime bracket the window, but the transition itself —
and, more importantly, the *intent* behind it — is unrecoverable. Given F-02
the unload may even have been the correct protective act; no artifact says so.

A supervisor receipt is the same class of object as a skill-capture receipt
(``artifact_skill_capture.py``): content-addressed, written once via an atomic
no-replace link, never overwritten, and carrying an explicit safety block. It
differs in one deliberate way — the observation instant is part of the event
identity, because two loads of the same agent at different times are two
distinct facts. Re-running the same command with the same ``--observed-at`` is
idempotent; running it an hour later is a new receipt.

This tool RECORDS a state change; it never performs one. Keeping it
non-mutating means an operator can call it before or after the ``launchctl``
command without the tool itself becoming a supervisor. It probes ``launchctl
print`` read-only so the receipt captures the state the system was actually
in, not merely what the operator intended.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import artifact_security as security
import artifact_runtime


SCHEMA_VERSION = 1
PRODUCER = "supervisor"
DEFAULT_RECEIPT_ROOT = Path(
    str(artifact_runtime.derived_root() / "supervisor-events")
).expanduser()

#: Only artifact-memory supervisors are in scope. A typo that would otherwise
#: mint a receipt for an unrelated agent is rejected instead.
LABEL_PATTERN = re.compile(r"^com\.personal\.artifact-[a-z0-9-]{1,64}$")

ACTIONS = (
    "load",
    "unload",
    "bootstrap",
    "bootout",
    "enable",
    "disable",
    "kickstart",
)

RUN_ID_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")

SAFETY = {
    "source_mutation": "none",
    "sink_writes": "none",
    "supervisor_mutation": "none",
    "receipt_mode": "append-only",
}


class SupervisorReceiptError(ValueError):
    """The requested supervisor receipt is unsafe or invalid."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def validate_label(label: str) -> str:
    if not LABEL_PATTERN.fullmatch(label):
        raise SupervisorReceiptError(
            f"label must match {LABEL_PATTERN.pattern}, got {label!r}"
        )
    return label


def validate_action(action: str) -> str:
    if action not in ACTIONS:
        raise SupervisorReceiptError(
            f"unsupported action {action!r}; expected one of: {', '.join(ACTIONS)}"
        )
    return action


def validate_reason(reason: str) -> str:
    """Intent is the field F-18 is actually about; an empty one defeats it."""
    cleaned = reason.strip()
    if len(cleaned) < 8:
        raise SupervisorReceiptError(
            "reason must state intent in at least 8 characters; "
            "an unexplained state change is the defect this receipt exists to close"
        )
    if not RUN_ID_PATTERN.fullmatch(cleaned):
        raise SupervisorReceiptError("reason must be 1-200 printable characters")
    return cleaned


def validate_observed_at(raw: str | None) -> str:
    if raw is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SupervisorReceiptError(
            f"observed-at must be an ISO-8601 timestamp: {raw!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise SupervisorReceiptError("observed-at must carry an explicit offset")
    return parsed.astimezone(timezone.utc).isoformat()


def plist_identity(path: Path | None) -> dict[str, Any] | None:
    """Bind the receipt to the plist bytes that were in force at the time."""
    if path is None:
        return None
    resolved = path.expanduser().absolute()
    if resolved.is_symlink():
        raise SupervisorReceiptError(f"plist must not be a symlink: {resolved}")
    try:
        info = resolved.lstat()
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise SupervisorReceiptError(f"plist is not readable: {resolved}: {exc}") from exc
    return {
        "path": str(resolved),
        "sha256": digest,
        "byte_size": info.st_size,
        "mtime": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
    }


def probe_launchctl(label: str) -> dict[str, Any]:
    """Read-only observation of the agent's actual state.

    Recording observed state alongside declared intent is what lets a later
    reader distinguish "operator unloaded it" from "it was already gone".
    """
    target = f"gui/{os.getuid()}/{label}"
    try:
        completed = subprocess.run(
            ["/bin/launchctl", "print", target],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"probed": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    if completed.returncode != 0:
        return {"probed": True, "loaded": False, "launchctl_returncode": completed.returncode}
    state: dict[str, Any] = {"probed": True, "loaded": True}
    for field, pattern in (
        ("pid", r"^\s*pid\s*=\s*(\d+)"),
        ("last_exit_status", r"^\s*last exit code\s*=\s*(-?\d+)"),
    ):
        match = re.search(pattern, completed.stdout, re.MULTILINE)
        if match:
            state[field] = int(match.group(1))
    return state


def make_event_id(identity: dict[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(identity)).hexdigest()


def build_receipt(
    *,
    label: str,
    action: str,
    reason: str,
    observed_at: str,
    run_id: str | None,
    plist: dict[str, Any] | None,
    observed_state: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    identity = {
        "schema_version": SCHEMA_VERSION,
        "producer": PRODUCER,
        "label": label,
        "action": action,
        "observed_at": observed_at,
        "reason": reason,
        "run_id": run_id,
        "plist_sha256": plist["sha256"] if plist else None,
    }
    event_hex = make_event_id(identity)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"event:{event_hex}",
        "producer": PRODUCER,
        "label": label,
        "action": action,
        "reason": reason,
        "observed_at": observed_at,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "plist": plist,
        "observed_state": observed_state,
        "safety": dict(SAFETY),
    }
    return event_hex, receipt


def write_receipt(
    receipt_root: Path,
    event_hex: str,
    receipt: dict[str, Any],
) -> tuple[str, Path]:
    try:
        security.ensure_private_directory(receipt_root)
        directory = receipt_root / event_hex[:2]
        security.ensure_private_directory(directory)
    except security.PrivateStateError as exc:
        raise SupervisorReceiptError(str(exc)) from exc
    path = directory / f"{event_hex}.json"
    if path.exists():
        security.require_private_file(path)
        return "idempotent", path
    try:
        security.atomic_write_json(path, receipt)
    except FileExistsError:
        security.require_private_file(path)
        return "idempotent", path
    return "created", path


def emit(args: argparse.Namespace) -> dict[str, Any]:
    label = validate_label(args.label)
    action = validate_action(args.action)
    reason = validate_reason(args.reason)
    observed_at = validate_observed_at(args.observed_at)
    run_id = args.run_id
    if run_id is not None and not RUN_ID_PATTERN.fullmatch(run_id):
        raise SupervisorReceiptError("run id must be 1-200 printable characters")
    plist = plist_identity(args.plist)
    observed_state = (
        {"probed": False, "reason": "probe disabled"}
        if args.no_probe
        else probe_launchctl(label)
    )
    event_hex, receipt = build_receipt(
        label=label,
        action=action,
        reason=reason,
        observed_at=observed_at,
        run_id=run_id,
        plist=plist,
        observed_state=observed_state,
    )
    receipt_root = args.receipt_root.expanduser().absolute()
    receipt_path = receipt_root / event_hex[:2] / f"{event_hex}.json"
    if not args.apply:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "plan",
            "status": "planned",
            "event_id": f"event:{event_hex}",
            "receipt": receipt,
            "would_write": str(receipt_path),
            "safety": dict(SAFETY),
        }
    status, written = write_receipt(receipt_root, event_hex, receipt)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply",
        "status": status,
        "event_id": receipt["event_id"],
        "label": label,
        "action": action,
        "receipt_path": str(written),
        "safety": receipt["safety"],
    }


def history(args: argparse.Namespace) -> dict[str, Any]:
    """Replay the supervisor timeline the F-18 window was missing."""
    root = args.receipt_root.expanduser().absolute()
    if not root.exists():
        return {"schema_version": SCHEMA_VERSION, "events": [], "count": 0}
    security.require_private_directory(root)
    events: list[dict[str, Any]] = []
    for path in sorted(root.glob("[0-9a-f][0-9a-f]/*.json")):
        security.require_private_file(path)
        record = json.loads(path.read_text(encoding="utf-8"))
        if args.label and record.get("label") != args.label:
            continue
        events.append(
            {
                "observed_at": record.get("observed_at"),
                "label": record.get("label"),
                "action": record.get("action"),
                "reason": record.get("reason"),
                "loaded_after": record.get("observed_state", {}).get("loaded"),
                "event_id": record.get("event_id"),
            }
        )
    events.sort(key=lambda item: (item["observed_at"] or "", item["event_id"] or ""))
    return {"schema_version": SCHEMA_VERSION, "count": len(events), "events": events}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    emit_parser = subparsers.add_parser(
        "emit", help="record one supervisor state change"
    )
    emit_parser.add_argument("--label", required=True, help="com.personal.artifact-*")
    emit_parser.add_argument("--action", choices=ACTIONS, required=True)
    emit_parser.add_argument(
        "--reason", required=True, help="why this change was made (intent is the point)"
    )
    emit_parser.add_argument(
        "--observed-at",
        help="ISO-8601 instant of the change with offset; defaults to now (UTC)",
    )
    emit_parser.add_argument("--run-id")
    emit_parser.add_argument("--plist", type=Path, help="bind the receipt to plist bytes")
    emit_parser.add_argument(
        "--no-probe", action="store_true", help="skip the read-only launchctl probe"
    )
    emit_parser.add_argument("--receipt-root", type=Path, default=DEFAULT_RECEIPT_ROOT)
    emit_parser.add_argument(
        "--apply", action="store_true", help="write the append-only receipt"
    )
    emit_parser.set_defaults(handler=emit)

    history_parser = subparsers.add_parser(
        "history", help="replay recorded supervisor state changes"
    )
    history_parser.add_argument("--label")
    history_parser.add_argument("--receipt-root", type=Path, default=DEFAULT_RECEIPT_ROOT)
    history_parser.set_defaults(handler=history)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except (SupervisorReceiptError, security.PrivateStateError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

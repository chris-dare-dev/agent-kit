#!/usr/bin/env python3
"""Build an age-encrypted off-device backup bundle of the irreplaceable set.

Implements the backup set enumerated in
`plans/artifact-memory-backup-dr-policy-2026-07-18.md` §3 (F-04).

Three deliberate constraints:

1. DRY-RUN IS THE DEFAULT. `--apply` is required to write anything. This is the
   same lesson as F-03: a plan that silently mutates is not a plan.
2. NO REMOTE UPLOAD. This script writes to a local or mounted path only. Pushing
   the bundle to S3 or any other network destination is an external write under
   the External System Write Policy and must be individually confirmed by Chris;
   remote-looking destinations are refused, not silently attempted.
3. KEYS ARE NOT IN THE BUNDLE (policy §3 tier 3). Shipping the decryption key
   inside the blob it protects defeats the encryption. The manifest records key
   *digests* so a restore can prove it recovered the right keys from their
   separate custody path.

The gold suite under `evals/` is copied as opaque bytes AND integrity-hashed over
those bytes (a per-file sha256 that lives ONLY inside the encrypted MANIFEST.json).
This script never parses, interprets, or LOGS holdout contents -- the digests are
one-way and are never printed to stdout/logs; sealed custody (ADR-010) is preserved.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import artifact_backup_snapshot as snapshot
import artifact_runtime
import artifact_security as security

SCHEMA_VERSION = 1
# /opt/homebrew/bin/age is the Homebrew (macOS) location; resolve from PATH
# first so apt/other installs work, keeping the old literal as the fallback.
AGE = shutil.which("age") or "/opt/homebrew/bin/age"

# Destinations this script will not write to. Any of these is a gated external
# write, not an automation decision.
REMOTE_PREFIXES = ("s3://", "gs://", "az://", "http://", "https://", "ssh://", "rsync://")


@dataclass(frozen=True)
class _Required:
    """One canonical required-inventory entry.

    `hard=True`  -> absence makes the bundle INCOMPLETE (nonzero exit, RPO clock
                    frozen, rotation withheld). The core state that always exists
                    on a healthy system.
    `hard=False` -> a "represent-when-absent" evidence CATEGORY: the category is
                    always represented in the plan (so a producer that drops it is
                    caught at verify), but a legitimately-empty category (an
                    evaluation-quiet night) is NOT a backup failure. `pattern` is
                    the glob that identifies its files.
    """

    name: str
    tier: int
    hard: bool
    pattern: str | None = None


# The SINGLE completeness authority, consulted by BOTH the producer (`_plan`/
# `build`) AND the verifier (`verify_extracted_bundle`). Before this existed the
# verifier trusted `manifest["plan"]`, which is itself authored by the same
# `_plan()` call whose regression it is meant to catch -- so a dropped required
# item never entered the plan and the bundle verified clean (Sol #4). Mirrors
# plans/artifact-memory-backup-dr-policy-2026-07-18.md section 3 (F-04); the
# names match `_plan()`'s manifest item names exactly.
REQUIRED_INVENTORY: tuple[_Required, ...] = (
    # Tier 1 -- irreplaceable core (always present on a healthy system).
    _Required("skill-events", 1, True),
    _Required("consumer-state", 1, True),
    _Required("ingestion-state", 1, True),
    # FRESH-DEPLOYMENT LINEAGE (personal fork, 2026-07-22). The upstream kit was
    # built on a workspace MIGRATED from an embedded store, with a Graphiti pilot
    # and a sealed eval suite; these five items are artifacts of that history and
    # are hard-required THERE. This deployment was ingested fresh, runs no eval
    # program, and never piloted Graphiti, so it legitimately has none of them --
    # and holding them hard made EVERY nightly bundle "incomplete", which
    # permanently withheld rotation and froze the RPO clock (observed for the
    # first real backup run, 2026-07-22).
    #
    # They stay in the canonical inventory as represent-when-absent so the
    # producer-regression guarantee is untouched: the plan-name reconciliation
    # (see verify_extracted_bundle, "SOL #4") still fails if a producer stops
    # ENUMERATING them; only absence-on-disk is now tolerated, and it is reported
    # as `empty_categories` rather than hidden.
    #
    # Re-promote to hard=True when the corresponding program starts here:
    #   unit-archive   -> before enabling ANY outbox pruning (it is what makes
    #                     pruning safe: pruned revisions live only there)
    #   snapshot-restore-evidence.json -> once the personal restore drill runs
    #   evals          -> if a retrieval-eval suite is adopted
    _Required("evals", 1, False),
    _Required("graphiti-pilots", 1, False),
    _Required("evidence/qdrant-shadow-verification.json", 1, False),
    _Required("evidence/snapshot-restore-evidence.json", 1, False),
    # Tier 1 -- the three accumulate-over-time evidence CATEGORIES (DR policy
    # section 3 item 4). Represent-when-absent, NON-failing: an evaluation-quiet
    # night legitimately has zero of these, and failing the whole backup for it
    # would itself be dishonest reporting.
    _Required("evidence-category/artifact-retrieval-eval", 1, False, "artifact-retrieval-eval-*.json"),
    _Required("evidence-category/artifact-retrieval-migration", 1, False, "artifact-retrieval-migration-*.json"),
    _Required("evidence-category/phase0-projection-audit", 1, False, "phase0-projection-audit*.json"),
    # Tier 2 -- rebuild authority, irreplaceable in practice today (fail-when-
    # absent -- Sol #3: a missing catalog/outbox/replay/archive/config/snapshot is
    # an incomplete bundle, not a silent exit-0).
    _Required("artifact-catalog", 2, True),
    _Required("outbox", 2, True),
    # Migration-lineage (see the tier-1 note above): the frozen replay set and
    # the materialised unit archive exist only in a migrated deployment. Here the
    # OUTBOX is the rebuild authority and outbox pruning is off, so both are
    # legitimately absent. unit-archive MUST return to hard=True before pruning.
    _Required("shadow-replay", 2, False),
    _Required("unit-archive", 2, False),
    _Required("runtime-config", 2, True),
    _Required("qdrant-snapshot", 2, True),
)

# The represent-when-absent category names. Their absence is NEVER a backup
# failure (they remain in `items`/the plan so the verifier can still detect a
# producer that drops the whole category). Both build()'s failure accounting and
# verify_extracted_bundle exclude them from failure signals.
_REPRESENTATIONAL = frozenset(r.name for r in REQUIRED_INVENTORY if not r.hard)


class BackupError(RuntimeError):
    """The off-device bundle could not be built."""


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _rpo_completed_at(
    *, complete: bool, run_at: str, prior_completed_at: str | None
) -> str | None:
    """The RPO clock (completed_at) advances only on a COMPLETE backup.

    Completeness spans the full hard-required tier-1+tier-2 inventory
    (`bundle_complete`), not tier-1 alone: a run missing any required item records
    itself elsewhere (last_run_at, tier1_complete, bundle_complete,
    incomplete_items) but must NOT stamp a fresh completed_at over a hole -- else a
    backup that silently dropped a required item reports "fresh + within RPO"
    while missing it.
    """
    return run_at if complete else prior_completed_at


def _root(runtime: artifact_runtime.ArtifactRuntime) -> Path:
    """The derived-state root for THIS runtime -- `config_path.parent`.

    Both `_plan()` (which resolves receipt_root/evals/evidence against it) and the
    health-watermark write derive from this ONE expression, so a non-default
    `--config` can never plan against one root while stamping health under another
    (the Sol #6 hazard). For the default config this is `DEFAULT_DERIVED_ROOT`, so
    production behavior is unchanged.
    """
    return runtime.config_path.parent


def missing_required(items: Sequence[dict[str, Any]]) -> list[str]:
    """Hard-required inventory items (`REQUIRED_INVENTORY` hard=True) absent from `items`.

    A producer that never emits a hard item (a dropped `add()`) leaves it out of
    `items` entirely, so it is missing here too -- the build-side detector for the
    same class `verify_extracted_bundle` catches on the manifest side.
    """
    present = {i["name"] for i in items if i.get("present")}
    return sorted(r.name for r in REQUIRED_INVENTORY if r.hard and r.name not in present)


def bundle_complete(items: Sequence[dict[str, Any]]) -> bool:
    """True iff every HARD-required item is present -- the one completeness authority.

    Spans tier-1 AND tier-2 rebuild authority, so a missing catalog/outbox/
    shadow-replay/unit-archive/runtime-config/snapshot makes the bundle incomplete
    rather than a silent exit-0 (Sol #3). Represent-when-absent evidence categories
    (hard=False) never count against it -- an evaluation-quiet night is not a failed
    backup. `build()`'s exit code, the RPO clock, and the rotation gate all consult
    this, and `verify_extracted_bundle` re-derives it from the manifest plan.
    """
    return not missing_required(items)


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    """Copy a live SQLite database consistently.

    A plain file copy of a WAL-mode database that is being written races the
    -wal/-shm files and can yield a torn or stale image. The online backup API
    takes a transactionally consistent copy instead, which is what makes the
    consumer/ingestion state actually restorable.
    """
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
    security.secure_created_file(destination)
    return {
        "kind": "sqlite-online-backup",
        "source": str(source),
        "bytes": destination.stat().st_size,
        "sha256": _digest_file(destination),
    }


def _copy_file(source: Path, destination: Path) -> dict[str, Any]:
    shutil.copy2(source, destination)
    security.secure_created_file(destination)
    return {
        "kind": "file",
        "source": str(source),
        "bytes": destination.stat().st_size,
        "sha256": _digest_file(destination),
    }


def _mode_str(info: os.stat_result) -> str:
    """Permission bits as a stable 4-digit octal string, e.g. '0600'."""
    return f"{stat.S_IMODE(info.st_mode):04o}"


def _tree_manifest(root: Path) -> dict[str, Any]:
    """Per-ENTRY integrity manifest for a copied tree.

    Records every entry -- files, directories AND symlinks -- so restore-time
    verification catches a CORRUPTED, an OMITTED, or an UNEXPECTED entry, not
    merely a changed total (the H6 gap: the old manifest carried only file count
    + total bytes, which a single-byte flip or a swapped file leaves unchanged).

    Symlink policy is EXPLICIT and matches the copy (`copytree(symlinks=True)`
    recreates links, never dereferences them): a symlink is recorded by its
    target, never followed and never digested; verification compares the target.
    Traversal uses `os.walk(followlinks=False)` so a symlinked directory is
    recorded as a symlink, not descended into. Modes are recorded for forensics
    but are ADVISORY on verify -- a restore re-applies 0600/0700 via
    `artifact_security --repair`, so a mode delta is expected-and-corrected, not
    corruption (this is the false-positive the E3 risk note warned about).
    Symlinks: `--repair`/`--sweep` REJECT linked entries (derived state must
    contain none), so a symlink in a backed-up tree is surfaced as a verify
    advisory -- a symlink-bearing bundle signals a pre-existing anomaly to
    investigate, not a clean recovery point.

    `tree_sha256` is a single aggregate digest over the sorted entries, encoded
    like `security.private_tree_manifest` so a whole tree has one comparable hash
    -- which is what makes "every entry in MANIFEST.json carries a sha256" true
    for trees, not just for the file/sqlite items.
    """
    paths: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in dirnames:
            paths.append(base / name)
        for name in filenames:
            paths.append(base / name)

    entries: list[dict[str, Any]] = []
    aggregate = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            entries.append({"path": rel, "type": "symlink", "target": target})
            aggregate.update(f"{rel}\0symlink\0{target}\n".encode("utf-8", "surrogatepass"))
        elif stat.S_ISDIR(info.st_mode):
            entries.append({"path": rel, "type": "dir", "mode": _mode_str(info)})
            aggregate.update(f"{rel}\0dir\n".encode("utf-8", "surrogatepass"))
        elif stat.S_ISREG(info.st_mode):
            digest = _digest_file(path)
            entries.append(
                {
                    "path": rel,
                    "type": "file",
                    "bytes": info.st_size,
                    "sha256": digest,
                    "mode": _mode_str(info),
                }
            )
            aggregate.update(f"{rel}\0file\0{info.st_size}\0{digest}\n".encode("utf-8", "surrogatepass"))
        else:
            # Sockets/fifos/devices have no meaningful content digest. Record the
            # type so their presence/absence is still verifiable, and never try to
            # read them (a fifo read would block).
            entries.append({"path": rel, "type": "other", "mode": _mode_str(info)})
            aggregate.update(f"{rel}\0other\n".encode("utf-8", "surrogatepass"))

    file_count = sum(1 for entry in entries if entry["type"] == "file")
    total_bytes = sum(entry.get("bytes", 0) for entry in entries if entry["type"] == "file")
    return {
        "kind": "tree",
        "files": file_count,
        "bytes": total_bytes,
        "entry_count": len(entries),
        "tree_sha256": aggregate.hexdigest(),
        "symlink_policy": "recorded-by-target; not followed, not digested",
        "entries": entries,
    }


def _copy_tree(source: Path, destination: Path) -> dict[str, Any]:
    shutil.copytree(source, destination, dirs_exist_ok=False, symlinks=True)
    manifest = _tree_manifest(destination)
    manifest["source"] = str(source)
    return manifest


def _verify_tree(
    recorded: dict[str, Any], root: Path
) -> tuple[list[str], list[str], int]:
    """Compare a restored/extracted tree against its recorded manifest.

    Returns (problems, mode_advisories, files_rehashed). `problems` is non-empty on
    any content or presence defect -- a corrupted file, a missing entry, an
    unexpected entry, a changed type, or a moved symlink target -- each of which
    must fail the restore verdict. Mode-only differences are advisory (see
    `_tree_manifest`). `files_rehashed` is how many file entries were actually
    re-digested (0 when the tree is missing), so callers never over-report coverage.
    """
    problems: list[str] = []
    advisories: list[str] = []
    if not root.is_dir() or root.is_symlink():
        return [f"tree missing (or replaced by non-directory): {root}"], [], 0

    recorded_entries = {entry["path"]: entry for entry in recorded.get("entries", [])}
    actual = _tree_manifest(root)
    actual_entries = {entry["path"]: entry for entry in actual["entries"]}
    files_rehashed = sum(1 for entry in actual_entries.values() if entry["type"] == "file")

    for rel, want in recorded_entries.items():
        have = actual_entries.get(rel)
        if have is None:
            problems.append(f"missing entry: {rel}")
            continue
        if have["type"] != want["type"]:
            problems.append(f"type changed for {rel}: {want['type']} -> {have['type']}")
            continue
        if want["type"] == "file" and have.get("sha256") != want.get("sha256"):
            problems.append(f"content changed (sha256 mismatch): {rel}")
        elif want["type"] == "symlink" and have.get("target") != want.get("target"):
            problems.append(
                f"symlink target changed for {rel}: "
                f"{want.get('target')!r} -> {have.get('target')!r}"
            )
        if want["type"] == "symlink":
            # Derived state should contain NO symlinks: the restore runbook's
            # `artifact_security --repair`/`--sweep` reject linked entries by
            # design. Surface it so a symlink-bearing bundle is not a silent
            # clean-verify -> restore-abort surprise. Not a hard failure: the H6
            # tree manifest records symlinks by target for completeness detection.
            advisories.append(
                f"symlink present -- restore `artifact_security --repair` rejects it; "
                f"derived state should contain none: {rel}"
            )
        if want.get("mode") is not None and have.get("mode") != want.get("mode"):
            advisories.append(f"mode {want.get('mode')} -> {have.get('mode')}: {rel}")

    for rel in actual_entries:
        if rel not in recorded_entries:
            problems.append(f"unexpected entry (not in manifest): {rel}")

    # Belt-and-braces: if the aggregate disagrees with no per-entry cause, the
    # recorded manifest is internally inconsistent -- surface it rather than
    # reporting a clean tree.
    if not problems and actual["tree_sha256"] != recorded.get("tree_sha256"):
        problems.append("tree_sha256 mismatch with no per-entry cause (manifest inconsistent)")
    return problems, advisories, files_rehashed


def _within(base: Path, candidate: Path) -> bool:
    """True iff `candidate` resolves inside `base` -- a path-traversal guard."""
    try:
        return candidate.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


def verify_extracted_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Re-verify an already-decrypted, extracted bundle against its MANIFEST.json.

    The automated form of the restore runbook's "re-digest each restored entry and
    compare" step, now covering TREE entries (H6). It detects ACCIDENTAL corruption,
    truncation, or omission of an extracted bundle -- bit rot, a partial `tar -x`, a
    dropped item -- and is fail-closed: an unreadable manifest, a missing item, an
    item without a recorded digest, or an INCOMPLETE bundle (a planned item absent)
    is a problem, never an assumed-clean. It is NOT an anti-tamper control: the
    extracted MANIFEST.json is unauthenticated and sits beside the data it
    describes, so the age encryption + recipient keys on the OUTER bundle remain the
    tamper anchor. Run this on the extracted `artifact-memory-<stamp>/` directory
    BEFORE trusting a restore.
    """
    bundle_dir = bundle_dir.expanduser()
    manifest_path = bundle_dir / "MANIFEST.json"
    if manifest_path.is_symlink():
        raise BackupError(f"MANIFEST.json is a symlink -- refused: {manifest_path}")
    if not manifest_path.is_file():
        raise BackupError(f"no MANIFEST.json in {bundle_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"unreadable MANIFEST.json in {bundle_dir}: {exc}") from exc

    # A manifest this verifier does not understand cannot be trusted complete -- an
    # unknown schema may carry item kinds or completeness rules this code silently
    # skips. Reject rather than pass it clean (schema_version was previously only
    # echoed, so schema_version:999 verified ok).
    manifest_schema = manifest.get("schema_version")
    if manifest_schema != SCHEMA_VERSION:
        raise BackupError(
            f"unsupported backup manifest schema {manifest_schema!r} "
            f"(this verifier understands {SCHEMA_VERSION})"
        )

    items = manifest.get("items")
    if not isinstance(items, dict):
        raise BackupError(f"MANIFEST.json has no items map: {manifest_path}")

    problems: list[str] = []
    advisories: list[str] = []
    files_verified = 0
    trees_verified = 0
    # Every on-disk path the manifest accounts for; anything else under the bundle
    # is an unexpected addition and is surfaced.
    expected: set[str] = {"MANIFEST.json"}

    def _walk_error(exc: OSError) -> None:
        # A dir that cannot be traversed (e.g. mode 000) would otherwise be
        # silently skipped, hiding a dropped or stowaway entry inside it. Treat
        # every traversal failure as a verification failure.
        problems.append(
            f"traversal failed (cannot verify contents): {getattr(exc, 'filename', exc)}"
        )

    # COMPLETENESS: the bundle must contain everything it PLANNED to. Checking only
    # disk-against-`items` misses an item that was absent at backup time -- it never
    # enters `items` at all, so an incomplete bundle (even one missing an
    # irreplaceable tier-1 tree) would otherwise verify clean.
    plan = manifest.get("plan")
    if plan is None:
        problems.append(
            "manifest has no plan -- cannot prove completeness (pre-completeness bundle)"
        )
    elif not isinstance(plan, list):
        problems.append("manifest plan is malformed (not a list) -- cannot prove completeness")
    elif not plan:
        problems.append("manifest plan is empty -- a real backup always plans the tier-1 set")
    else:
        for planned in plan:
            if not isinstance(planned, dict):
                problems.append(f"malformed plan entry: {planned!r}")
                continue
            pname, tier = planned.get("name"), planned.get("tier")
            if not planned.get("present"):
                if pname in _REPRESENTATIONAL:
                    # A represent-when-absent evidence category (DR policy section 3
                    # item 4): its absence is a legitimate evaluation-quiet night,
                    # represented but never a failure. Its PRESENCE in the plan is
                    # still asserted by the canonical reconciliation below.
                    continue
                problems.append(
                    f"{pname}: planned tier-{tier} item was ABSENT at backup time "
                    "-- bundle is incomplete"
                )
            elif pname not in items:
                problems.append(
                    f"{pname}: planned item missing from manifest items "
                    "(manifest inconsistent)"
                )

        # SOL #4: the plan is producer-authored -- a regression that drops a
        # required item from `_plan()` removes it from `plan` too, so the checks
        # above (which only iterate `plan`) cannot see the gap. Reconcile the plan's
        # NAME SET against the canonical REQUIRED_INVENTORY, which is independent of
        # this bundle, so a dropped required item/category is caught even when the
        # producer and its own manifest agree with each other.
        planned_names = {
            p.get("name") for p in plan if isinstance(p, dict) and p.get("name")
        }
        for required in REQUIRED_INVENTORY:
            if required.pattern is not None:
                # A glob category is satisfied by EITHER >=1 matching evidence file
                # named in the plan OR its represent-when-absent sentinel.
                satisfied = required.name in planned_names or any(
                    isinstance(n, str)
                    and n.startswith("evidence/")
                    and fnmatch.fnmatch(n[len("evidence/") :], required.pattern)
                    for n in planned_names
                )
                if not satisfied:
                    built = manifest.get("created_at") or "unknown"
                    problems.append(
                        f"{required.name}: required evidence category unrepresented in "
                        f"the plan (no matching file AND no sentinel; bundle built {built}) "
                        "-- a producer regression OR a bundle predating the M4 sentinel "
                        "contract (2026-07-20). Fail-closed either way; verify the era "
                        "before discarding a recovery point (restore runbook §era-note)."
                    )
            elif required.name not in planned_names:
                problems.append(
                    f"{required.name}: required item missing from the plan entirely "
                    "-- the canonical inventory requires it (producer regression)"
                )

    # Keys must never travel in the bundle (policy §3 tier 3); verify is the last
    # place to catch a build that violated it.
    if manifest.get("keys_in_bundle") is not False:
        problems.append("keys_in_bundle is not False -- refusing to trust this bundle")

    for name, entry in items.items():
        # Path-traversal guard: a crafted/corrupt manifest name must not make verify
        # read (or "verify" against) a file outside the extracted bundle.
        if os.path.isabs(str(name)) or not _within(bundle_dir, bundle_dir / name):
            problems.append(f"{name}: unsafe item name escapes the bundle -- refused")
            continue
        target = bundle_dir / name
        # A nested item name (e.g. "evidence/x.json") implies its ancestor dirs.
        parts = Path(name).parts
        for depth in range(1, len(parts) + 1):
            expected.add("/".join(parts[:depth]))
        kind = entry.get("kind")
        if kind == "tree":
            expected.add(name)
            for sub in entry.get("entries", []):
                expected.add(f"{name}/{sub['path']}")
            if "entries" not in entry:
                # Pre-H6 bundle: no per-entry manifest to verify against. Register
                # its actual contents as expected so they are not ALSO reported as
                # spurious unexpected entries, then flag the tree as unverifiable.
                if target.is_dir() and not target.is_symlink():
                    for directory, dirnames, filenames in os.walk(
                        target, followlinks=False, onerror=_walk_error
                    ):
                        base = Path(directory)
                        for child in list(dirnames) + filenames:
                            expected.add((base / child).relative_to(bundle_dir).as_posix())
                problems.append(
                    f"{name}: legacy tree entry without per-entry manifest "
                    "(pre-H6 bundle) -- not content-verifiable"
                )
                continue
            tree_problems, tree_advisories, rehashed = _verify_tree(entry, target)
            problems.extend(f"{name}/{item}" for item in tree_problems)
            advisories.extend(f"{name}/{item}" for item in tree_advisories)
            if target.is_dir() and not target.is_symlink():
                trees_verified += 1  # present-and-checked, whatever the verdict
            files_verified += rehashed
        elif kind in ("file", "sqlite-online-backup"):
            if not target.is_file() or target.is_symlink():
                problems.append(f"{name}: missing (expected {kind} file)")
                continue
            recorded = entry.get("sha256")
            if not recorded:
                problems.append(f"{name}: manifest entry carries no sha256")
                continue
            if _digest_file(target) != recorded:
                problems.append(f"{name}: content changed (sha256 mismatch)")
            files_verified += 1
        else:
            # An unknown kind used to fall into the file branch, so a bogus item
            # that happened to have a matching file passed clean. Reject it.
            problems.append(f"{name}: unknown item kind {kind!r} -- not verifiable")
            continue

    # followlinks=False so a symlinked directory is checked as one entry, never
    # descended into (which would yield the target's contents as false "extras").
    for directory, dirnames, filenames in os.walk(
        bundle_dir, followlinks=False, onerror=_walk_error
    ):
        base = Path(directory)
        for name in list(dirnames) + filenames:
            rel = (base / name).relative_to(bundle_dir).as_posix()
            if rel not in expected:
                problems.append(f"unexpected entry not in manifest: {rel}")

    return {
        "ok": not problems,
        "bundle_dir": str(bundle_dir),
        "schema_version": manifest.get("schema_version"),
        "items_declared": len(items),
        "planned_items": len(plan) if isinstance(plan, list) else None,
        "trees_verified": trees_verified,
        "files_verified": files_verified,
        "keys_in_bundle": manifest.get("keys_in_bundle"),
        "key_digests": manifest.get("key_digests", {}),
        "problems": problems,
        "mode_advisories": advisories,
    }


def _manifest_summary(manifest_items: dict[str, Any]) -> dict[str, Any]:
    """A per-item summary SAFE to print to stdout/logs.

    Strips the per-ENTRY tree manifest (`entries`) -- for the sealed `evals` tree
    those are per-holdout-file sha256s, and the daily agent captures stdout to a
    plaintext log outside the encrypted bundle. Full detail stays in MANIFEST.json.
    """
    return {
        name: {k: v for k, v in entry.items() if k != "entries"}
        for name, entry in manifest_items.items()
    }


def _plan(runtime: artifact_runtime.ArtifactRuntime) -> list[dict[str, Any]]:
    """Enumerate the backup set. Policy §3 tiers 1 and 2."""
    # Derive the derived-state root from the runtime config's own directory rather
    # than the module default, so the fixed items (receipt_root, evals, ...) and
    # the evidence/snapshot paths all resolve against ONE root -- consistent under
    # a non-default --config and hermetically testable.
    root = _root(runtime)
    services = runtime.qdrant_admin_key_file.parent
    items: list[dict[str, Any]] = []

    def add(name: str, path: Path, mode: str, tier: int, why: str) -> None:
        items.append(
            {
                "name": name,
                "path": path,
                "mode": mode,
                "tier": tier,
                "why": why,
                "present": path.exists(),
            }
        )

    # Tier 1 -- irreplaceable.
    add("skill-events", runtime.receipt_root, "tree", 1, "append-only receipts")
    add("consumer-state", runtime.consumer_state, "sqlite", 1, "exactly-once state")
    add("ingestion-state", runtime.ingestion_state, "sqlite", 1, "ingestion checkpoints")
    add("evals", root / "evals", "tree", 1, "gold suite (SEALED -- opaque bytes, integrity-hashed, never parsed/logged)")
    add("graphiti-pilots", root / "graphiti-pilots", "tree", 1, "pilot reports")

    # Evidence JSONs (policy §3 tier-1 item 4). A REQUIRED set is enumerated
    # independently of filesystem discovery so a missing one is represented in the
    # plan (present=False), never silently dropped -- and snapshot-restore-
    # evidence.json lives UNDER services/snapshots/, outside root's top level, so
    # the old `root.glob("*.json")` never bundled it at all. Additional top-level
    # evidence is still discovered; dedup is by manifest name.
    added_evidence: set[str] = set()

    def add_evidence(path: Path, why: str) -> None:
        name = f"evidence/{path.name}"
        if name in added_evidence:
            return
        added_evidence.add(name)
        add(name, path, "file", 1, why)

    for required_evidence in (
        root / "qdrant-shadow-verification.json",
        services / "snapshots" / "snapshot-restore-evidence.json",
    ):
        add_evidence(
            required_evidence, "required evidence JSON (represented even if absent)"
        )
    for evidence in sorted(root.glob("*.json")):
        # The runtime config is added explicitly as tier 2 below; skip it here so
        # it is not bundled twice.
        if evidence.resolve() == runtime.config_path.resolve():
            continue
        add_evidence(evidence, "evidence JSON")

    # Represent the three accumulate-over-time evidence CATEGORIES (DR policy
    # section 3 item 4) even on a night when a category matches NOTHING, so a
    # producer that stops emitting one is detectable at verify. Files that exist
    # are already bundled individually by the glob above; here we add ONLY a
    # present=False sentinel for an EMPTY category -- represented, not a failure
    # (these are hard=False in REQUIRED_INVENTORY, so bundle_complete ignores their
    # absence). A category that matched files needs no sentinel.
    for required in REQUIRED_INVENTORY:
        if required.pattern is None:
            continue
        if not any(root.glob(required.pattern)):
            items.append(
                {
                    "name": required.name,
                    "path": root / f"({required.pattern} -- none matched this run)",
                    "mode": "file",
                    "tier": required.tier,
                    "why": "required evidence category (represented even when empty)",
                    "present": False,
                }
            )

    # Tier 2 -- rebuild authority in practice today.
    add("artifact-catalog", runtime.catalog, "sqlite", 2, "catalog")
    add("outbox", runtime.outbox_root, "tree", 2, "temporary rebuild authority (ADR-002)")
    # Both MATERIALISED unit stores were absent from the bundle while the outbox
    # tree they supersede was in it. That is backwards: the frozen replay set is
    # ADR-002's designated rebuild authority (37,527 unit payloads), and the unit
    # archive becomes the SOLE representation of any revision whose outbox is
    # later pruned. Pruning with either unbacked would move revisions from
    # backed-up to single-copy. "sqlite" mode also records a sha256 per file,
    # which the "tree" mode above still does not.
    add(
        "shadow-replay",
        root / "qdrant-shadow-replay.sqlite3",
        "sqlite",
        2,
        "frozen replay set (ADR-002 rebuild authority)",
    )
    add(
        "unit-archive",
        root / "artifact-unit-archive.sqlite3",
        "sqlite",
        2,
        "materialised units; sole representation once outboxes are pruned",
    )
    add("runtime-config", runtime.config_path, "file", 2, "map to everything else")

    snapshots = sorted(
        (
            p
            for p in (services / "snapshots").glob("*.snapshot")
            if p.name != "corrupt-restore-probe.snapshot"
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if snapshots:
        add("qdrant-snapshot", snapshots[0], "file", 2, "latest verified snapshot")
    else:
        # Represent the REQUIRED latest snapshot even when none exists (policy §3
        # item 10), so an absent snapshot is a visible plan gap rather than a
        # silent omission that lets an unrestorable bundle look complete.
        items.append(
            {
                "name": "qdrant-snapshot",
                "path": services / "snapshots" / "(no snapshot present)",
                "mode": "file",
                "tier": 2,
                "why": "latest verified snapshot (REQUIRED -- none found on disk)",
                "present": False,
            }
        )
    return items


def _key_digests(runtime: artifact_runtime.ArtifactRuntime) -> dict[str, str]:
    """Digest the keys WITHOUT including them in the bundle (policy §3 tier 3)."""
    digests: dict[str, str] = {}
    for label, path in (
        ("qdrant_admin_key", runtime.qdrant_admin_key_file),
        ("qdrant_read_key", runtime.qdrant_read_key_file),
    ):
        if path.exists():
            digests[label] = _digest_file(path)
    return digests


def _encrypt(archive: Path, destination: Path, recipients_file: Path) -> dict[str, Any]:
    if not Path(AGE).exists():
        raise BackupError(f"age is not installed at {AGE}")
    result = subprocess.run(
        [AGE, "--encrypt", "--recipients-file", str(recipients_file),
         "--output", str(destination), str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # A killed/failed age run can leave a truncated ciphertext at the output
        # path; remove it so it cannot masquerade as a recovery point in the next
        # run's `_prune_bundles` mtime window (V-L1/M2).
        Path(destination).unlink(missing_ok=True)
        raise BackupError(f"age encryption failed: {result.stderr[-2000:]}")
    security.secure_created_file(destination)
    return {
        "bytes": destination.stat().st_size,
        "sha256": _digest_file(destination),
        "recipients_file": str(recipients_file),
    }


def _prune_bundles(destination: Path, keep: int) -> dict[str, Any]:
    """Rotate old encrypted bundles, keeping the newest `keep`.

    Scoped to bundles this script created, in its own destination directory.
    Like snapshot rotation this is backup hygiene, not the ADR-002 §7
    generation-deletion gate.
    """
    bundles = sorted(
        destination.glob("artifact-memory-*.tar.age"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for path in bundles[keep:]:
        size = path.stat().st_size
        path.unlink()
        removed.append({"name": path.name, "bytes": size})
    return {
        "pruned": removed,
        "kept": keep,
        "reclaimed_bytes": sum(item["bytes"] for item in removed),
    }


def build(
    *,
    runtime: artifact_runtime.ArtifactRuntime,
    destination: Path,
    recipients_file: Path | None,
    apply: bool,
    ensure_snapshot_days: float | None = None,
    prune_keep: int | None = None,
) -> dict[str, Any]:
    # A bundle is only as fresh as the snapshot inside it. Refreshing on age
    # rather than every run keeps one daily job cheap: the 536 MB snapshot is
    # the expensive, most-rebuildable component, while the irreplaceable state
    # is small and gets copied every time.
    snapshot_refresh: dict[str, Any] | None = None
    if apply and ensure_snapshot_days is not None:
        root = snapshot.snapshot_root(runtime)
        age_days = snapshot.newest_age_days(root)
        if age_days is None or age_days > ensure_snapshot_days:
            snapshot_refresh = {
                "reason": "absent" if age_days is None else f"age {age_days:.1f}d",
                "result": snapshot.capture(runtime=runtime, root=root),
            }
        else:
            snapshot_refresh = {"reason": f"fresh ({age_days:.1f}d)", "result": None}

    items = _plan(runtime)

    estimate = 0
    for item in items:
        if not item["present"]:
            continue
        path: Path = item["path"]
        if item["mode"] == "tree":
            estimate += sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        else:
            estimate += path.stat().st_size

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "planned_at": datetime.now(timezone.utc).isoformat(),
        "applied": apply,
        "destination": str(destination),
        "estimated_plaintext_bytes": estimate,
        "estimated_plaintext_gigabytes": round(estimate / 1024**3, 2),
        "items": [
            {k: (str(v) if isinstance(v, Path) else v) for k, v in item.items()}
            for item in items
        ],
        "missing": [
            i["name"]
            for i in items
            if not i["present"] and i["name"] not in _REPRESENTATIONAL
        ],
        "keys_in_bundle": False,
        "key_digests": _key_digests(runtime),
        "remote_upload": "refused-by-design (gated external write)",
    }

    if not apply:
        report["note"] = (
            "DRY RUN -- nothing written. Re-run with --apply and "
            "--recipients-file to build the encrypted bundle."
        )
        return report

    if recipients_file is None:
        raise BackupError("--recipients-file is required with --apply")
    if not recipients_file.exists():
        raise BackupError(f"recipients file not found: {recipients_file}")

    destination = destination.expanduser().absolute()
    security.ensure_private_directory(destination)

    # Sol #1: stage the plaintext copy tree AND the plaintext tar on SOURCE-LOCAL
    # storage -- a private dir under the derived root, which already holds these
    # files unencrypted -- NEVER inside `destination`. When `destination` is an
    # external/mounted disk, only the final encrypted `.tar.age` ever touches it;
    # a crash strands plaintext on the source volume (where it already lives),
    # never on removable media. The sealed evals (ADR-010) never reach the
    # destination in plaintext. For the default config the derived root IS the
    # destination's parent, so this is behavior-neutral there.
    staging_root = security.ensure_private_directory(
        _root(runtime) / ".offdevice-build-staging"
    )

    # Reclaim plaintext staging stranded by a prior HARD crash (SIGKILL/power loss)
    # between mkdtemp and the finally-rmtree below. Post-Sol-#1 these land on the
    # SOURCE volume, so an unswept strand silently accumulates unencrypted copies of
    # the sealed set on the live-store disk (L1/V-M1). Only dirs older than 24h are
    # swept, so a hypothetical concurrent build is never raced.
    stale_cutoff = datetime.now(timezone.utc).timestamp() - 86400
    reclaimed = 0
    for stale in staging_root.glob("personal-backup-*"):
        try:
            if (
                stale.is_dir()
                and not stale.is_symlink()
                and stale.stat().st_mtime < stale_cutoff
            ):
                shutil.rmtree(stale, ignore_errors=True)
                reclaimed += 1
        except OSError:
            continue
    if reclaimed:
        report["reclaimed_stale_staging"] = reclaimed

    # Sol #1 moved the ~2x-plaintext-set transient (copy tree + tar) onto the SOURCE
    # volume for an external `--destination`. Fail CLEAN with a clear shortfall
    # rather than ENOSPC mid-copy (M1/V-L2): require room for the copy AND the tar.
    try:
        free = shutil.disk_usage(staging_root).free
    except OSError:
        free = None
    if free is not None and free < estimate * 2:
        raise BackupError(
            "insufficient source-volume space to stage the backup: need ~"
            f"{estimate * 2 / 1024**3:.2f} GiB free under {staging_root} (copy + tar "
            f"of a {estimate / 1024**3:.2f} GiB set), have {free / 1024**3:.2f} GiB"
        )

    stamp = _now_stamp()
    staging_parent = tempfile.mkdtemp(prefix="personal-backup-", dir=str(staging_root))
    staging = Path(staging_parent) / f"artifact-memory-{stamp}"
    staging.mkdir(mode=0o700)

    manifest_items: dict[str, Any] = {}
    try:
        for item in items:
            if not item["present"]:
                continue
            # Sol #7 to its full stated scope (V-M4): the intra-tree symlink check
            # below catches links INSIDE a copied tree, but a plan-item PATH that is
            # itself a symlink (a symlinked evidence file, sqlite, or tree root) is
            # silently dereferenced by copy2/copytree before _tree_manifest runs. One
            # lstat per item refuses that uniformly -- derived state must contain none.
            if item["path"].is_symlink():
                raise BackupError(
                    f"{item['name']}: plan-item path {item['path']} is a symlink -- "
                    "derived state must contain none (Sol #7); refusing to "
                    "dereference it into the bundle."
                )
            target = staging / item["name"]
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if item["mode"] == "sqlite":
                manifest_items[item["name"]] = _copy_sqlite(item["path"], target)
            elif item["mode"] == "tree":
                tree_manifest = _copy_tree(item["path"], target)
                # Sol #7: derived state must contain NO symlinks -- the restore
                # runbook's `artifact_security --repair`/`--sweep` reject them, so a
                # symlink-bearing bundle verifies "clean" yet is guaranteed to fail
                # restore. Refuse to build one (owner decision: fail fast at build).
                # `_copy_tree`'s manifest already recorded each symlink by target for
                # diagnosis; verify keeps its advisory for pre-existing bundles.
                symlinks = [
                    entry["path"]
                    for entry in tree_manifest.get("entries", [])
                    if entry.get("type") == "symlink"
                ]
                if symlinks:
                    raise BackupError(
                        f"{item['name']}: source tree contains symlink(s) "
                        f"{symlinks[:5]} -- derived state must contain none (Sol #7). "
                        "Refusing to build a bundle that restore would reject; "
                        "investigate and remove the symlink, then re-run."
                    )
                manifest_items[item["name"]] = tree_manifest
            else:
                manifest_items[item["name"]] = _copy_file(item["path"], target)
            manifest_items[item["name"]]["tier"] = item["tier"]

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation": runtime.qdrant_generation,
            "collection": runtime.qdrant_collection,
            "rollback_until": runtime.rollback_until,
            # The FULL plan -- present or not. `verify_extracted_bundle` asserts the
            # bundle is COMPLETE against this. Without it, an item absent at backup
            # time simply never enters `items`, leaves no trace, and an incomplete
            # bundle (even one missing an irreplaceable tier-1 tree) verifies clean.
            "plan": [
                {"name": item["name"], "tier": item["tier"], "present": item["present"]}
                for item in items
            ],
            "items": manifest_items,
            "key_digests": _key_digests(runtime),
            "keys_in_bundle": False,
            "restore_runbook": "plans/artifact-memory-restore-runbook-2026-07-18.md",
        }
        security.atomic_write_json(staging / "MANIFEST.json", manifest)

        archive = Path(staging_parent) / f"artifact-memory-{stamp}.tar"
        with tarfile.open(archive, "w") as tar:
            tar.add(staging, arcname=staging.name)

        bundle = destination / f"artifact-memory-{stamp}.tar.age"
        report["encryption"] = _encrypt(archive, bundle, recipients_file)
        report["bundle"] = str(bundle)
        # SUMMARY only -- never emit per-ENTRY tree manifests (sealed evals
        # per-holdout-file digests) to stdout/logs. Full detail is in MANIFEST.json
        # inside the encrypted bundle (same age protection as the bytes it digests).
        report["manifest"] = _manifest_summary(manifest_items)
        archive.unlink(missing_ok=True)
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)

    report["next_step"] = (
        "Bundle is encrypted and on local/mounted storage only. Moving it to a "
        "network destination is a separate, individually-confirmed external write."
    )

    # A backup that silently dropped a required item is a FAILED backup, not a
    # healthy one -- record it so the watermark, the report, the exit code, the
    # rotation gate, and restore-time verify all agree instead of stamping
    # "fresh + healthy" over a hole. `tier1_complete` is kept for the deliberately
    # tier-1-scoped message and backward-compat; `bundle_complete` is the full
    # tier-1+tier-2 completeness authority (Sol #3). Represent-when-absent evidence
    # categories never count against completeness. (verify_extracted_bundle
    # independently re-derives this from the manifest plan against the same
    # REQUIRED_INVENTORY.)
    # Represent-when-absent categories are NOT failures (H1/L1): exclude them from
    # every absence signal so an evaluation-quiet night does not flip tier1_complete
    # false or pollute absent_items/missing with sentinel noise. They stay in
    # `items`/the plan for the verifier; empty_categories reports them separately.
    absent_items = [
        item["name"]
        for item in items
        if not item["present"] and item["name"] not in _REPRESENTATIONAL
    ]
    empty_categories = [
        item["name"]
        for item in items
        if not item["present"] and item["name"] in _REPRESENTATIONAL
    ]
    tier1_absent = [
        item["name"]
        for item in items
        if not item["present"]
        and item["tier"] == 1
        and item["name"] not in _REPRESENTATIONAL
    ]
    tier1_complete = not tier1_absent
    incomplete_items = missing_required(items)
    complete = not incomplete_items
    report["tier1_complete"] = tier1_complete
    report["bundle_complete"] = complete
    if tier1_absent:
        report["tier1_absent"] = tier1_absent
    if incomplete_items:
        report["incomplete_items"] = incomplete_items
    if empty_categories:
        report["empty_categories"] = empty_categories

    # Durable watermark so `artifact_memory.py status` can report RPO freshness.
    # Without this the off-device tier has no observable age and a stalled backup
    # looks identical to a healthy one -- the F-08 silent-failure shape. The RPO
    # clock (completed_at) advances ONLY on a COMPLETE backup (the full hard-
    # required inventory): an incomplete run keeps the prior successful timestamp
    # so it cannot report "fresh + within RPO" while missing a required item. The
    # health path is derived from THIS runtime's root, not the module default, so a
    # non-default --config apply cannot stamp the production RPO health file (Sol #6).
    run_at = datetime.now(timezone.utc).isoformat()
    health_path = _root(runtime) / "artifact-backup-offdevice-health.json"
    prior: dict[str, Any] = {}
    if health_path.exists():
        try:
            prior = json.loads(health_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}
    completed_at = _rpo_completed_at(
        complete=complete,
        run_at=run_at,
        prior_completed_at=prior.get("completed_at"),
    )
    security.atomic_write_json(
        health_path,
        {
            "schema_version": SCHEMA_VERSION,
            "completed_at": completed_at,
            "last_run_at": run_at,
            "bundle": str(report.get("bundle")),
            "bytes": report.get("encryption", {}).get("bytes"),
            "sha256": report.get("encryption", {}).get("sha256"),
            "destination": str(destination),
            "off_site": "unknown-until-moved",
            "generation": runtime.qdrant_generation,
            "tier1_complete": tier1_complete,
            "bundle_complete": complete,
            "absent_items": absent_items,
            "incomplete_items": incomplete_items,
            "empty_categories": empty_categories,
        },
        replace=True,
    )
    report["completed_at"] = completed_at
    report["last_run_at"] = run_at
    if snapshot_refresh is not None:
        report["snapshot_refresh"] = snapshot_refresh
    # Rotation is backup hygiene, but rotating older recovery points when the NEW
    # bundle is incomplete would turn a bad night into a worse one (Sol #2): the
    # newest .tar.age is the incomplete one, so "keep the newest N" evicts a good
    # older bundle. Gate BOTH bundle and snapshot rotation on completeness.
    # Decrypt-verifying the just-written .tar.age is not an option here -- this job
    # holds only the public recipients file, never a private age key -- so the
    # build-time completeness predicate is the correct (and only available) bar.
    if prune_keep is not None:
        if complete:
            report["bundle_rotation"] = _prune_bundles(destination, prune_keep)
            report["snapshot_rotation"] = snapshot.prune(
                snapshot.snapshot_root(runtime), prune_keep
            )
        else:
            withheld = {
                "skipped": "bundle incomplete -- rotation withheld",
                "kept": prune_keep,
                "incomplete_items": incomplete_items,
            }
            report["bundle_rotation"] = withheld
            report["snapshot_rotation"] = dict(withheld)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=artifact_runtime.DEFAULT_CONFIG)
    parser.add_argument(
        "--destination",
        # Deliberately a str, not a Path: Path() collapses "s3://bucket" to
        # "s3:/bucket", which would slip straight past the remote-destination
        # refusal below. The guard has to see the string the user actually typed.
        default=str(artifact_runtime.DEFAULT_DERIVED_ROOT / "offdevice-staging"),
        help="local or mounted directory (e.g. an external disk mountpoint)",
    )
    parser.add_argument(
        "--recipients-file",
        type=Path,
        help="age recipients file (public keys); required with --apply",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually build the bundle (default is a dry-run plan)",
    )
    parser.add_argument(
        "--ensure-snapshot-days",
        type=float,
        help="take a fresh Qdrant snapshot first if the newest is older than N days",
    )
    parser.add_argument(
        "--prune-keep",
        type=int,
        help="keep the newest N bundles and N snapshots; rotate the rest away",
    )
    parser.add_argument(
        "--verify",
        type=Path,
        metavar="EXTRACTED_BUNDLE_DIR",
        help="re-verify an already-decrypted, extracted bundle directory against "
        "its MANIFEST.json (every file, sqlite, AND tree entry); read-only; "
        "exit 4 on any corrupted/missing/unexpected entry",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.verify is not None:
        # Read-only path: verify an extracted bundle. It never writes and never
        # touches a destination, so the remote-destination guard does not apply.
        try:
            report = verify_extracted_bundle(args.verify)
        except BackupError as exc:
            # An unreadable/absent/malformed manifest is the WORST failure, not a
            # softer one -- fold it into the same non-clean exit code so the whole
            # contract is "0 = verified clean, 4 = do not trust this bundle".
            print(json.dumps({"ok": False, "problems": [str(exc)]}), file=sys.stderr)
            return 4
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0 if report["ok"] else 4
    raw = str(args.destination).strip()
    if raw.lower().startswith(REMOTE_PREFIXES) or "://" in raw:
        print(
            f"REFUSED: '{raw}' is a remote destination. Uploading the bundle is an "
            "external write requiring explicit confirmation from Chris; this script "
            "writes to local or mounted paths only.",
            file=sys.stderr,
        )
        return 3
    try:
        report = build(
            runtime=artifact_runtime.load_runtime(args.config),
            destination=Path(raw),
            recipients_file=args.recipients_file,
            apply=args.apply,
            ensure_snapshot_days=args.ensure_snapshot_days,
            prune_keep=args.prune_keep,
        )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    # A backup that silently dropped a required item is a FAILED backup: exit
    # non-zero so a scheduler/watchdog treats it as a failure and the RPO clock
    # (which did not advance) is not misread as fresh. bundle_complete spans the
    # full tier-1+tier-2 required set (Sol #3); tier1_complete is still honored so a
    # report authored before bundle_complete existed still fails correctly.
    if args.apply and (
        report.get("tier1_complete") is False or report.get("bundle_complete") is False
    ):
        absent = (
            report.get("incomplete_items")
            or report.get("tier1_absent")
            or report.get("absent_items")
            or []
        )
        print(
            "INCOMPLETE: required items absent at backup time: " + ", ".join(absent),
            file=sys.stderr,
        )
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

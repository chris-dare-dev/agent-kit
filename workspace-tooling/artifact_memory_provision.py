#!/usr/bin/env python3
"""Provision private local Qdrant and the Artifact Memory UDS runtime.

Two modes, with a hard boundary between them:

``plan`` (the default, no ``--apply``)
    STRICTLY READ-ONLY.  It reports what *would* change — which directories
    are absent, which API keys are absent, whether ``compose.yaml`` / ``.env``
    would be written, and a field-level diff of the runtime configuration —
    and writes nothing at all.  Running a plan against the live service must
    leave the derived-state tree byte-identical (see the hash-invariance test
    in ``tests/test_artifact_memory_provision.py``).

``apply`` (``--apply``)
    Performs every write: directories, missing API keys, ``compose.yaml``,
    ``.env``, ``docker compose up``, and finally the runtime configuration.
    The configuration is published LAST, after Docker has come up, so a
    Docker failure can no longer leave a replaced configuration pointing at a
    service that never started.

Replacing an existing runtime configuration always requires
``--replace-runtime``, whatever its profile — the earlier guard covered only
``exact-hybrid-v2``/retrieval runtimes and replaced a legacy one silently.  A
re-provision that would produce an identical configuration is a no-op and
needs no opt-in, so routine re-runs stay idempotent.

The rollback retention deadline (``rollback.retain_embedded_until``) is
PRESERVED from an existing configuration.  Only a first install, or an
explicit ``--replace-runtime``, mints a fresh now+30d window; a routine
re-provision must not silently extend the embedded-store retention clock.

This no longer creates or uses a service bearer token.  A pre-existing
``service-token`` from the TCP deployment is inert legacy state and is left in
place for an operator to remove manually after a verified UDS cutover.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import artifact_ingestion as ingestion
import artifact_runtime
import artifact_security as security


SCHEMA_VERSION = 2
DEFAULT_WORKSPACE = Path(__file__).resolve().parents[1]
CANONICAL_COMPOSE = Path(__file__).parent / "services" / "qdrant" / "compose.yaml"
SERVICE_ROOT = artifact_runtime.DEFAULT_DERIVED_ROOT / "services" / "qdrant"
GENERATION = "p20260721v1"
COLLECTION = f"personal_artifact_chunks_{GENERATION}"
# Loopback port for this deployment's Qdrant (6343 main / 6345 restore-test), kept
# clear of the upstream 6333/6335 so a co-resident work stack cannot collide. A
# module constant so the runtime template and its tests cannot drift apart.
QDRANT_URL = "http://127.0.0.1:6343"
RETENTION = timedelta(days=30)
SECRET_FILES = {
    "admin": "admin-api-key",
    "read_only": "read-only-api-key",
    "restore_admin": "restore-admin-api-key",
    "restore_read_only": "restore-read-only-api-key",
}

_MISSING = object()


class ProvisionError(RuntimeError):
    """Provisioning would overwrite protected runtime state."""


def _secret_state(path: Path) -> str:
    """Classify one API-key file without creating it."""
    return "present" if path.exists() else "absent"


def _create_secret(path: Path) -> tuple[str, str]:
    """Read an existing API key, or mint one.  APPLY PATH ONLY — this writes."""
    if path.exists():
        return artifact_runtime.read_secret(path, path.name), "existing"
    value = secrets.token_urlsafe(48)
    security.atomic_write_bytes(path, (value + "\n").encode("utf-8"))
    return value, "created"


def _directory_state(path: Path) -> str:
    """Classify one directory without creating or re-permissioning it."""
    if not path.exists():
        return "absent"
    return "present" if path.is_dir() else "conflicting-non-directory"


def _file_state(path: Path, expected: bytes | None) -> str:
    """Compare a file against the bytes provisioning would publish.

    ``expected is None`` means the content is not knowable yet (the ``.env``
    body depends on API keys that do not exist until an apply run mints them).
    """
    if not path.exists():
        return "absent"
    if expected is None:
        return "present-content-unknown-until-apply"
    try:
        current = path.read_bytes()
    except OSError:
        return "present-unreadable"
    return "identical" if current == expected else "differs"


def _read_existing_runtime(config: Path) -> tuple[str | None, dict[str, Any] | None]:
    """Classify and return a safely readable runtime before replacing it.

    The exact retrieval release pins are intentionally expensive to generate
    and must not be silently discarded by a routine UDS re-provision.  Read
    only the envelope here: an older runtime schema may be migrated, but any
    existing configuration needs an explicit replacement decision.
    """
    config = config.expanduser().absolute()
    try:
        config.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise ProvisionError(
            "existing runtime configuration cannot be safely inspected; "
            "refusing to replace it"
        ) from exc
    try:
        security.require_private_file(config)
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, security.PrivateStateError) as exc:
        raise ProvisionError(
            "existing runtime configuration cannot be safely inspected; "
            "refusing to replace it"
        ) from exc
    if not isinstance(payload, dict):
        raise ProvisionError(
            "existing runtime configuration is not an object; refusing to replace it"
        )
    if (
        payload.get("active_retrieval") == "exact-hybrid-v2"
        or payload.get("retrieval") is not None
    ):
        return "exact-hybrid-v2/retrieval", payload
    return "legacy-or-unknown", payload


def _legacy_token_warning(root: Path) -> dict[str, str] | None:
    token = root / "service-token"
    try:
        token.lstat()
    except FileNotFoundError:
        return None
    return {
        "code": "legacy-service-token-inert",
        "path": str(token),
        "message": (
            "Inert legacy TCP bearer token retained; manually remove it only "
            "after a verified UDS cutover."
        ),
    }


def _lexical_index(generation: str) -> str | None:
    """Name a lexical index only when one actually exists for this generation.

    The legacy vector generation has no lexical index.  Deriving the name
    unconditionally from ``GENERATION`` published a path to a file that never
    existed, which reads to an operator as a missing artifact rather than an
    absent feature.
    """
    candidate = (
        artifact_runtime.DEFAULT_DERIVED_ROOT
        / f"artifact-retrieval-{generation}.sqlite3"
    )
    return str(candidate) if candidate.exists() else None


def _retention(
    existing: dict[str, Any] | None,
    *,
    replace_runtime: bool,
    now: datetime,
) -> tuple[str, str]:
    """Preserve an existing rollback deadline; never silently extend it."""
    if existing is not None and not replace_runtime:
        rollback = existing.get("rollback")
        if isinstance(rollback, dict):
            current = rollback.get("retain_embedded_until")
            if isinstance(current, str) and current:
                return current, "preserved"
    return (now + RETENTION).isoformat(), "minted"


def _runtime_payload(
    *,
    workspace: Path,
    root: Path,
    snapshots: Path,
    retain_embedded_until: str,
) -> dict[str, Any]:
    return {
        "schema_version": artifact_runtime.SCHEMA_VERSION,
        "active_backend": "server",
        "qdrant": {
            "url": QDRANT_URL,
            "collection": COLLECTION,
            "generation": GENERATION,
            "admin_key_file": str(root / SECRET_FILES["admin"]),
            "read_key_file": str(root / SECRET_FILES["read_only"]),
            "embedded_path": str(ingestion.DEFAULT_QDRANT_PATH),
        },
        "service": {
            "socket_path": str(root / "artifact-memory.sock"),
        },
        "paths": {
            "workspace": str(workspace),
            "catalog": str(ingestion.DEFAULT_CATALOG),
            "outbox_root": str(artifact_runtime.DEFAULT_DERIVED_ROOT / "outbox"),
            "ingestion_state": str(ingestion.DEFAULT_STATE),
            "consumer_state": str(
                artifact_runtime.DEFAULT_DERIVED_ROOT
                / "artifact-event-consumer.sqlite3"
            ),
            "receipt_root": str(
                artifact_runtime.DEFAULT_DERIVED_ROOT / "skill-events"
            ),
            "lexical_index": _lexical_index(GENERATION),
            "build_manifest": str(root / "build-manifest.json"),
            "snapshot_root": str(snapshots),
        },
        "rollback": {
            "embedded_mode": "read-only",
            "retain_embedded_until": retain_embedded_until,
            "deletion_requires_separate_approval": True,
        },
    }


def _flatten(payload: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(payload, dict):
        flat: dict[str, Any] = {}
        for key, value in payload.items():
            flat.update(_flatten(value, f"{prefix}{key}."))
        return flat
    return {prefix.rstrip("."): payload}


def _payload_changes(
    existing: dict[str, Any] | None,
    planned: dict[str, Any],
) -> list[dict[str, Any]]:
    """Field-level diff of the runtime configuration.

    The runtime configuration holds key FILE PATHS, never key material, so
    reporting before/after values here leaks no secret.
    """
    if existing is None:
        return []
    before = _flatten(existing)
    after = _flatten(planned)
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key, _MISSING)
        new = after.get(key, _MISSING)
        if old != new:
            changes.append(
                {
                    "path": key,
                    "from": None if old is _MISSING else old,
                    "to": None if new is _MISSING else new,
                    "kind": (
                        "added"
                        if old is _MISSING
                        else "removed"
                        if new is _MISSING
                        else "changed"
                    ),
                }
            )
    return changes


def provision(
    *,
    workspace: Path,
    apply: bool,
    replace_runtime: bool = False,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve(strict=True)
    config = artifact_runtime.DEFAULT_CONFIG.expanduser().absolute()
    root = SERVICE_ROOT.expanduser().absolute()
    storage = root / "storage"
    restore_storage = root / "restore-storage"
    snapshots = root / "snapshots"
    model_cache = ingestion.DEFAULT_MODEL_CACHE.expanduser().absolute()
    compose = root / "compose.yaml"
    environment = root / ".env"

    # ---- read-only assessment: this block must not write anything ----------
    previous_profile, previous_payload = _read_existing_runtime(config)
    now = datetime.now(timezone.utc)
    retain_embedded_until, retention_source = _retention(
        previous_payload,
        replace_runtime=replace_runtime,
        now=now,
    )
    planned = _runtime_payload(
        workspace=workspace,
        root=root,
        snapshots=snapshots,
        retain_embedded_until=retain_embedded_until,
    )
    changes = _payload_changes(previous_payload, planned)
    if previous_payload is None:
        runtime_action = "create"
    elif not changes:
        runtime_action = "unchanged"
    else:
        runtime_action = "replace"
    blocked = runtime_action == "replace" and not replace_runtime
    if blocked and apply:
        # A plan performs no write, so it REPORTS the block instead of
        # raising: an operator must be able to read the diff without first
        # reaching for the flag that authorises the overwrite.
        raise ProvisionError(
            f"refusing to replace the existing {previous_profile} runtime "
            f"configuration at {config} "
            f"({len(changes)} field(s) would change: "
            f"{', '.join(change['path'] for change in changes[:5])}"
            f"{', …' if len(changes) > 5 else ''}); "
            "rerun with --replace-runtime only after a verified replacement "
            "decision"
        )

    directories = {
        str(path): _directory_state(path)
        for path in (root, storage, restore_storage, snapshots, model_cache)
    }
    compose_bytes = CANONICAL_COMPOSE.read_bytes()
    secret_paths = {
        name: root / filename for name, filename in SECRET_FILES.items()
    }
    warnings = [warning] if (warning := _legacy_token_warning(root)) else []

    if not apply:
        # Compute the .env body only when every key already exists; a plan run
        # must never mint one to satisfy its own diff.
        environment_bytes: bytes | None = None
        if all(path.exists() for path in secret_paths.values()):
            try:
                environment_bytes = _environment_bytes(
                    {
                        name: artifact_runtime.read_secret(path, path.name)
                        for name, path in secret_paths.items()
                    }
                )
            except Exception:  # unreadable/invalid key: report as unknown
                environment_bytes = None
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "plan",
            "read_only": True,
            "service_root": str(root),
            "storage": str(storage),
            "restore_storage": str(restore_storage),
            "config": str(config),
            "collection": COLLECTION,
            "generation": GENERATION,
            "directories": directories,
            "files": {
                str(compose): _file_state(compose, compose_bytes),
                str(environment): _file_state(environment, environment_bytes),
            },
            "runtime": {
                "previous_profile": previous_profile or "absent",
                "replace_runtime": replace_runtime,
                "action": runtime_action,
                "blocked": blocked,
                "requires": "--replace-runtime" if blocked else None,
                "validated": False,
                "retain_embedded_until": retain_embedded_until,
                "retention": retention_source,
                "changes": changes,
                "planned": planned,
            },
            "secrets": {
                name: _secret_state(path) for name, path in secret_paths.items()
            },
            "warnings": warnings,
            "docker": "planned",
        }

    # ---- apply path: every write lives below this line ---------------------
    root = security.ensure_private_directory(root)
    storage = security.ensure_private_directory(storage)
    restore_storage = security.ensure_private_directory(restore_storage)
    snapshots = security.ensure_private_directory(snapshots)
    security.ensure_private_directory(model_cache)

    keys: dict[str, str] = {}
    secret_status: dict[str, str] = {}
    for name, path in secret_paths.items():
        keys[name], secret_status[name] = _create_secret(path)
    # Deliberately do not delete a legacy root/service-token here. It is no
    # longer read by this service, but secret deletion is a separate verified
    # cutover action rather than an implicit provisioning side effect.

    file_status = {
        str(compose): _write_if_changed(compose, compose_bytes),
        str(environment): _write_if_changed(
            environment, _environment_bytes(keys)
        ),
    }

    # Bring Docker up BEFORE publishing the runtime configuration, so a Docker
    # failure cannot leave a replaced configuration naming a service that
    # never started (the partial-activation window).
    completed = subprocess.run(
        [
            shutil.which("docker") or "/usr/local/bin/docker",
            "compose",
            "--env-file",
            str(environment),
            "-f",
            str(compose),
            "up",
            "-d",
            "qdrant",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "docker compose failed: "
            + (completed.stderr or completed.stdout)[-2000:]
        )
    docker_result = completed.stdout.strip() or "started"

    if runtime_action == "unchanged":
        config_status = "unchanged"
    else:
        config_status = security.atomic_write_json(
            config, planned, replace=config.exists()
        )
    artifact_runtime.load_runtime(config)

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply",
        "read_only": False,
        "service_root": str(root),
        "storage": str(storage),
        "restore_storage": str(restore_storage),
        "config": str(config),
        "collection": COLLECTION,
        "generation": GENERATION,
        "directories": directories,
        "files": file_status,
        "runtime": {
            "previous_profile": previous_profile or "absent",
            "replace_runtime": replace_runtime,
            "action": runtime_action,
            "blocked": False,
            "requires": None,
            "validated": True,
            "retain_embedded_until": retain_embedded_until,
            "retention": retention_source,
            "changes": changes,
            "config_write": config_status,
        },
        "secrets": secret_status,
        "warnings": warnings,
        "docker": docker_result,
    }


def _environment_bytes(keys: dict[str, str]) -> bytes:
    return (
        f"QDRANT_API_KEY={keys['admin']}\n"
        f"QDRANT_READ_ONLY_API_KEY={keys['read_only']}\n"
        f"QDRANT_RESTORE_API_KEY={keys['restore_admin']}\n"
        f"QDRANT_RESTORE_READ_ONLY_API_KEY={keys['restore_read_only']}\n"
    ).encode("utf-8")


def _write_if_changed(path: Path, data: bytes) -> str:
    """Publish bytes only when they differ.  APPLY PATH ONLY — this writes."""
    if path.exists():
        try:
            if path.read_bytes() == data:
                return "unchanged"
        except OSError:
            pass
    return security.atomic_write_bytes(path, data, replace=path.exists())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform writes; without it the run is a strictly read-only plan",
    )
    parser.add_argument(
        "--replace-runtime",
        action="store_true",
        help="allow replacement of an existing runtime configuration",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        print(
            json.dumps(
                provision(
                    workspace=args.workspace,
                    apply=args.apply,
                    replace_runtime=args.replace_runtime,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

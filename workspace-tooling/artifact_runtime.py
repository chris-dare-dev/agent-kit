#!/usr/bin/env python3
"""Validated runtime configuration for the local Artifact Memory Service.

The configuration is private derived state, not a source of authority.  Qdrant
remains a loopback-only dependency with owner-only credential files.  The
adapter-to-service boundary is instead a private Unix-domain socket: its
parent must be 0700 and the bound socket must be an owner-owned 0600 socket.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import artifact_security as security
import platform_compat


SCHEMA_VERSION = 2

# The ONE definition of where derived state lives.
#
# This path used to be re-declared as a literal in eleven modules, and the
# TypeScript adapter spelled it "workspace-artifacts" while every Python module
# spelled it "personal-artifacts". That was not a coincidence to be patched: with
# no single definition, there was nothing for the two sides to agree WITH, so the
# socket the adapter dialled and the socket the provisioner bound were free to
# drift apart — which is exactly what happened, leaving four tools dead on
# arrival on every clean install.
#
# Legacy names, kept ONLY for the migration branch below. Nothing else may
# reference them; test_no_module_level_derived_root_literals enforces that.
_LEGACY_ROOTS = (
    Path("~/.local/share/personal-artifacts").expanduser(),
    Path("~/.local/share/workspace-artifacts").expanduser(),
)
_APP_DIRNAME = "agent-kit"
_migration_warned = False


PROFILE_RE = re.compile(r"^[a-z0-9-]{1,32}$")
PROFILE_ENV = "AGENT_KIT_PROFILE"
#: Loopback base port. A profile shifts off this by a deterministic offset so two
#: profiles on one machine cannot collide, and the same profile always lands on
#: the same port across restarts (a random port would break every stored config).
QDRANT_BASE_PORT = 6343


class ProfileError(ValueError):
    """The profile name is not usable as a path or container-name segment."""


def validate_profile(name: str) -> str:
    if not PROFILE_RE.match(name or ""):
        raise ProfileError(
            f"invalid profile {name!r}: must match {PROFILE_RE.pattern} "
            "(lowercase letters, digits and hyphens, 1-32 characters). "
            "The name becomes a directory, a Qdrant collection suffix and a "
            "container name, so it cannot contain separators or spaces."
        )
    return name


def profile() -> str | None:
    """The active profile, or None for the unprofiled default."""
    name = os.environ.get(PROFILE_ENV)
    return validate_profile(name) if name else None


def profile_suffix(name: str | None = None) -> str:
    resolved = name if name is not None else profile()
    return f"-{resolved}" if resolved else ""


def qdrant_port(name: str | None = None, *, offset: int = 0) -> int:
    """Deterministic per-profile port: same profile, same port, every time.

    Derived from the name rather than allocated, so a provisioned runtime config
    stays valid across restarts and two profiles never negotiate for a port.
    """
    resolved = name if name is not None else profile()
    if not resolved:
        return QDRANT_BASE_PORT + offset
    validate_profile(resolved)
    # Small, stable, collision-resistant enough for a handful of local profiles.
    span = sum(ord(char) * (index + 1) for index, char in enumerate(resolved))
    return QDRANT_BASE_PORT + offset + 10 + (span % 200) * 2


def default_derived_root() -> Path:
    """Per-OS default, with no environment consulted."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / _APP_DIRNAME
        return Path.home() / "AppData" / "Local" / _APP_DIRNAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_DIRNAME
    return Path.home() / ".local" / "share" / _APP_DIRNAME


def derived_root(name: str | None = None) -> Path:
    """Resolve the derived-state root.

    Order: AGENT_KIT_DERIVED_ROOT, then $XDG_DATA_HOME/agent-kit, then the
    per-OS default. Every module must call this rather than rebuilding a path,
    so there is exactly one answer per process and per machine.

    If a legacy installation exists and the resolved root does not, say so once
    on stderr. Silently starting a second, empty store next to a populated one
    is the worst available outcome: retrieval would quietly return nothing.
    """
    global _migration_warned

    suffix = profile_suffix(name)

    override = os.environ.get("AGENT_KIT_DERIVED_ROOT")
    if override:
        # An explicit root is honoured as given; the profile still separates
        # beneath it, or two profiles pointed at one override would share a store.
        base = Path(override).expanduser()
        return base.parent / (base.name + suffix) if suffix else base

    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() / _APP_DIRNAME if xdg else default_derived_root()
    root = base.parent / (base.name + suffix) if suffix else base

    if not _migration_warned and not root.exists():
        for legacy in _LEGACY_ROOTS:
            if legacy.exists():
                _migration_warned = True
                print(
                    f"[agent-kit] derived state found at the legacy path {legacy}, "
                    f"but this build reads {root}.\n"
                    f"[agent-kit] Move it once:  mv {legacy} {root}\n"
                    f"[agent-kit] Or set AGENT_KIT_DERIVED_ROOT={legacy} to keep using it.",
                    file=sys.stderr,
                )
                break
    return root


def default_config_path(name: str | None = None) -> Path:
    return derived_root(name) / "artifact-memory-runtime.json"


# Import-time snapshots, kept so the ~20 existing call sites that read these as
# argparse defaults keep working. Anything that must honour an environment
# change made after import should call derived_root() / default_config_path().
DEFAULT_DERIVED_ROOT = derived_root()
DEFAULT_CONFIG = default_config_path()

COLLECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ACTIVE_RETRIEVALS = ("legacy-vector-v1", "exact-hybrid-v2")
ROLLBACK_MODES = ("read-only", "read-write")
PRIVATE_SOCKET_MODE = 0o600


class RuntimeConfigError(ValueError):
    """Runtime configuration is absent, unsafe, or internally inconsistent."""


def _loopback_url(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeConfigError(f"{label} must be a string")
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeConfigError(
            f"{label} must be an uncredentialed http://127.0.0.1:<port> URL"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeConfigError(f"{label} has an invalid port") from exc
    if port is None or not 1 <= port <= 65535:
        raise RuntimeConfigError(f"{label} must include a valid port")
    return f"http://127.0.0.1:{port}"


def _private_path(value: Any, label: str, *, file: bool) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeConfigError(f"{label} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeConfigError(f"{label} must be an absolute path")
    try:
        if file:
            security.require_private_file(path)
        else:
            security.require_private_directory(path)
    except security.PrivateStateError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    return path.absolute()


def _future_private_file(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeConfigError(f"{label} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeConfigError(f"{label} must be an absolute path")
    security.require_private_directory(path.absolute().parent)
    if path.exists():
        security.require_private_file(path)
    return path.absolute()


def _optional_future_private_file(value: Any, label: str) -> Path | None:
    """Accept an absent optional derived-state file.

    A ``null`` (or omitted) value means the feature has no artifact for this
    generation — the legacy vector generation has no lexical index.  Naming a
    path for a file that will never exist reads to an operator as a MISSING
    artifact rather than an ABSENT feature, so the provisioner now emits null
    instead of deriving a name unconditionally.
    """
    if value is None:
        return None
    return _future_private_file(value, label)


def require_private_socket(path: Path, label: str = "service socket") -> Path:
    """Require one existing owner-private Unix-domain socket.

    Socket paths are not regular private files, so the generic derived-state
    helpers deliberately do not accept them.  Keep the same ownership,
    symlink, and direct-parent guarantees here before a client connects or the
    service removes a stale endpoint.
    """
    path = path.expanduser()
    if not path.is_absolute():
        raise RuntimeConfigError(f"{label} must be an absolute path")
    path = path.absolute()
    try:
        security.require_private_directory(path.parent)
    except security.PrivateStateError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    try:
        information = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeConfigError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(information.st_mode) or not stat.S_ISSOCK(information.st_mode):
        raise RuntimeConfigError(f"{label} must be a real Unix-domain socket: {path}")
    if not platform_compat.supports_posix_privacy():
        platform_compat.owner_check_degraded(
            "artifact_runtime._private_socket", f"{label}: {path}"
        )
    elif information.st_uid != platform_compat.current_uid():
        raise RuntimeConfigError(
            f"{label} is not owned by uid {platform_compat.current_uid()}: {path}"
        )
    mode = stat.S_IMODE(information.st_mode)
    if mode != PRIVATE_SOCKET_MODE:
        raise RuntimeConfigError(
            f"{label} mode must be 0600, found {mode:04o}: {path}"
        )
    return path


def _future_private_socket(value: Any, label: str) -> Path:
    """Validate a configured socket pathname before the service binds it."""
    if not isinstance(value, str) or not value:
        raise RuntimeConfigError(f"{label} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeConfigError(f"{label} must be an absolute path")
    path = path.absolute()
    try:
        security.require_private_directory(path.parent)
    except security.PrivateStateError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    try:
        path.lstat()
    except FileNotFoundError:
        return path
    return require_private_socket(path, label)


def read_secret(path: Path, label: str) -> str:
    path = _private_path(str(path), label, file=True)
    value = path.read_text(encoding="utf-8").strip()
    if not 32 <= len(value) <= 512 or "\0" in value or "\n" in value:
        raise RuntimeConfigError(f"{label} must contain one 32-512 character secret")
    return value


@dataclass(frozen=True)
class RetrievalRuntime:
    collection: str
    generation: str
    manifest: Path
    manifest_sha256: str
    span_manifest_digest: str
    profile_digest: str
    embedding_model: str
    embedding_model_snapshot: Path
    embedding_model_manifest_digest: str
    reranker_model: str
    reranker_model_snapshot: Path
    reranker_model_manifest_digest: str
    ranking_version: str
    policy_file: Path
    policy_digest: str
    development_evidence: Path
    development_evidence_digest: str
    holdout_evidence: Path
    holdout_evidence_digest: str


@dataclass(frozen=True)
class ArtifactRuntime:
    config_path: Path
    active_backend: str
    active_retrieval: str
    retrieval: RetrievalRuntime | None
    workspace: Path
    catalog: Path
    outbox_root: Path
    ingestion_state: Path
    consumer_state: Path
    receipt_root: Path
    qdrant_url: str
    qdrant_collection: str
    qdrant_generation: str
    qdrant_admin_key_file: Path
    qdrant_read_key_file: Path
    embedded_path: Path
    service_socket_path: Path
    lexical_index: Path | None
    build_manifest: Path
    rollback_until: str
    rollback_mode: str

    def qdrant_admin_key(self) -> str:
        return read_secret(self.qdrant_admin_key_file, "Qdrant admin key")

    def qdrant_read_key(self) -> str:
        return read_secret(self.qdrant_read_key_file, "Qdrant read-only key")


def load_runtime(path: Path = DEFAULT_CONFIG) -> ArtifactRuntime:
    path = path.expanduser().absolute()
    security.require_private_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeConfigError(f"invalid runtime configuration: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeConfigError(
            f"runtime schema must be exactly {SCHEMA_VERSION}"
        )
    active = payload.get("active_backend")
    if active not in ("embedded", "server"):
        raise RuntimeConfigError("active_backend must be embedded or server")
    active_retrieval = payload.get("active_retrieval", "legacy-vector-v1")
    if active_retrieval not in ACTIVE_RETRIEVALS:
        raise RuntimeConfigError(
            "active_retrieval must be legacy-vector-v1 or exact-hybrid-v2"
        )

    qdrant = payload.get("qdrant")
    service = payload.get("service")
    paths = payload.get("paths")
    rollback = payload.get("rollback")
    if not all(isinstance(value, dict) for value in (qdrant, service, paths, rollback)):
        raise RuntimeConfigError(
            "runtime requires qdrant, service, paths, and rollback objects"
        )
    expected_service_fields = {"socket_path"}
    if set(service) != expected_service_fields:
        missing = sorted(expected_service_fields - set(service))
        unknown = sorted(set(service) - expected_service_fields)
        raise RuntimeConfigError(
            f"service fields mismatch; missing={missing}, unknown={unknown}"
        )
    collection = qdrant.get("collection")
    generation = qdrant.get("generation")
    if not isinstance(collection, str) or not COLLECTION.fullmatch(collection):
        raise RuntimeConfigError("Qdrant collection name is invalid")
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        raise RuntimeConfigError("Qdrant generation name is invalid")
    rollback_until = rollback.get("retain_embedded_until")
    if not isinstance(rollback_until, str) or len(rollback_until) > 64:
        raise RuntimeConfigError("rollback retention timestamp is invalid")
    rollback_mode = rollback.get("embedded_mode", "read-only")
    if rollback_mode not in ROLLBACK_MODES:
        raise RuntimeConfigError(
            "rollback embedded_mode must be read-only or read-write"
        )

    retrieval_payload = payload.get("retrieval")
    retrieval: RetrievalRuntime | None = None
    if retrieval_payload is not None:
        if not isinstance(retrieval_payload, dict):
            raise RuntimeConfigError("retrieval must be an object")
        required = {
            "collection",
            "generation",
            "manifest",
            "manifest_sha256",
            "span_manifest_digest",
            "profile_digest",
            "embedding_model",
            "embedding_model_snapshot",
            "embedding_model_manifest_digest",
            "reranker_model",
            "reranker_model_snapshot",
            "reranker_model_manifest_digest",
            "ranking_version",
            "policy_file",
            "policy_digest",
            "development_evidence",
            "development_evidence_digest",
            "holdout_evidence",
            "holdout_evidence_digest",
        }
        if set(retrieval_payload) != required:
            missing = sorted(required - set(retrieval_payload))
            unknown = sorted(set(retrieval_payload) - required)
            raise RuntimeConfigError(
                f"retrieval fields mismatch; missing={missing}, unknown={unknown}"
            )

        def retrieval_string(name: str, maximum: int = 500) -> str:
            value = retrieval_payload[name]
            if (
                not isinstance(value, str)
                or not value
                or len(value) > maximum
                or "\0" in value
            ):
                raise RuntimeConfigError(f"retrieval.{name} is invalid")
            return value

        retrieval_collection = retrieval_string("collection", 128)
        retrieval_generation = retrieval_string("generation", 128)
        if not COLLECTION.fullmatch(retrieval_collection):
            raise RuntimeConfigError("retrieval.collection is invalid")
        if not GENERATION.fullmatch(retrieval_generation):
            raise RuntimeConfigError("retrieval.generation is invalid")
        digests: dict[str, str] = {}
        for name in (
            "manifest_sha256",
            "span_manifest_digest",
            "profile_digest",
            "embedding_model_manifest_digest",
            "reranker_model_manifest_digest",
            "policy_digest",
            "development_evidence_digest",
            "holdout_evidence_digest",
        ):
            value = retrieval_string(name, 64)
            if not DIGEST.fullmatch(value):
                raise RuntimeConfigError(f"retrieval.{name} must be SHA-256")
            digests[name] = value
        retrieval = RetrievalRuntime(
            collection=retrieval_collection,
            generation=retrieval_generation,
            manifest=_private_path(
                retrieval_payload["manifest"],
                "retrieval manifest",
                file=True,
            ),
            manifest_sha256=digests["manifest_sha256"],
            span_manifest_digest=digests["span_manifest_digest"],
            profile_digest=digests["profile_digest"],
            embedding_model=retrieval_string("embedding_model"),
            embedding_model_snapshot=_private_path(
                retrieval_payload["embedding_model_snapshot"],
                "retrieval embedding snapshot",
                file=False,
            ),
            embedding_model_manifest_digest=digests[
                "embedding_model_manifest_digest"
            ],
            reranker_model=retrieval_string("reranker_model"),
            reranker_model_snapshot=_private_path(
                retrieval_payload["reranker_model_snapshot"],
                "retrieval reranker snapshot",
                file=False,
            ),
            reranker_model_manifest_digest=digests[
                "reranker_model_manifest_digest"
            ],
            ranking_version=retrieval_string("ranking_version"),
            policy_file=_private_path(
                retrieval_payload["policy_file"],
                "retrieval policy",
                file=True,
            ),
            policy_digest=digests["policy_digest"],
            development_evidence=_private_path(
                retrieval_payload["development_evidence"],
                "retrieval development evidence",
                file=True,
            ),
            development_evidence_digest=digests[
                "development_evidence_digest"
            ],
            holdout_evidence=_private_path(
                retrieval_payload["holdout_evidence"],
                "retrieval holdout evidence",
                file=True,
            ),
            holdout_evidence_digest=digests["holdout_evidence_digest"],
        )
    if active_retrieval == "exact-hybrid-v2" and retrieval is None:
        raise RuntimeConfigError(
            "exact-hybrid-v2 requires the frozen retrieval object"
        )
    if active_retrieval == "exact-hybrid-v2" and active != "server":
        raise RuntimeConfigError(
            "exact-hybrid-v2 requires active_backend=server"
        )

    workspace_value = paths.get("workspace")
    if not isinstance(workspace_value, str):
        raise RuntimeConfigError("workspace must be an absolute path")
    workspace = Path(workspace_value).expanduser()
    if not workspace.is_absolute() or not workspace.is_dir():
        raise RuntimeConfigError("workspace directory does not exist")

    return ArtifactRuntime(
        config_path=path,
        active_backend=active,
        active_retrieval=active_retrieval,
        retrieval=retrieval,
        workspace=workspace.absolute().resolve(strict=True),
        catalog=_private_path(paths.get("catalog"), "catalog", file=True),
        outbox_root=_private_path(paths.get("outbox_root"), "outbox root", file=False),
        ingestion_state=_private_path(
            paths.get("ingestion_state"), "ingestion state", file=True
        ),
        consumer_state=_private_path(
            paths.get("consumer_state"), "consumer state", file=True
        ),
        receipt_root=_private_path(
            paths.get("receipt_root"), "receipt root", file=False
        ),
        qdrant_url=_loopback_url(qdrant.get("url"), "Qdrant URL"),
        qdrant_collection=collection,
        qdrant_generation=generation,
        qdrant_admin_key_file=_private_path(
            qdrant.get("admin_key_file"), "Qdrant admin-key file", file=True
        ),
        qdrant_read_key_file=_private_path(
            qdrant.get("read_key_file"), "Qdrant read-key file", file=True
        ),
        embedded_path=_private_path(
            qdrant.get("embedded_path"), "embedded Qdrant path", file=False
        ),
        service_socket_path=_future_private_socket(
            service.get("socket_path"), "service socket"
        ),
        lexical_index=_optional_future_private_file(
            paths.get("lexical_index"), "lexical index"
        ),
        build_manifest=_future_private_file(
            paths.get("build_manifest"), "build manifest"
        ),
        rollback_until=rollback_until,
        rollback_mode=rollback_mode,
    )


def update_active_backend(
    backend: str,
    path: Path = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Atomically switch only the service-side backend selector."""
    if backend not in ("embedded", "server"):
        raise RuntimeConfigError("backend must be embedded or server")
    runtime = load_runtime(path)
    if runtime.active_retrieval == "exact-hybrid-v2" and backend != "server":
        raise RuntimeConfigError(
            "exact-hybrid-v2 cannot switch away from the server backend"
        )
    payload = json.loads(runtime.config_path.read_text(encoding="utf-8"))
    previous = payload["active_backend"]
    payload["active_backend"] = backend
    security.atomic_write_json(runtime.config_path, payload, replace=True)
    return {"previous": previous, "active": backend, "config": str(runtime.config_path)}

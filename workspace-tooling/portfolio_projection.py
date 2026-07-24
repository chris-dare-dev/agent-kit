#!/usr/bin/env python3
"""Build a deterministic, read-only Obsidian projection of roadmap delivery state.

The roadmap documents, roadmap registers, and milestone pipeline state remain authoritative.
This script never writes back to them.  It emits one normalized JSON index plus generated
Markdown records and an Obsidian Base under Notes/ so tracking, implementation, and operational
state can be inspected independently.

Legacy checkbox ``done`` and v1 pipeline ``complete`` are intentionally *not* translated into
``published`` or ``verified``.  Missing delivery evidence is represented as ``unknown``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from path_contract import load_project_manifest  # noqa: E402


DEFAULT_MANIFEST = SCRIPT_DIR / "project-map.json"
SCHEMA_PATH = SCRIPT_DIR / "schemas" / "portfolio-index.schema.json"
GENERATOR = "scripts/portfolio_projection.py"

TRACKING = {"pending", "in_progress", "complete", "unknown"}
IMPLEMENTATION = {
    "pending",
    "in_progress",
    "validated",
    "committed",
    "published",
    "not_owned",
    "not_required",
    "unknown",
}
OPERATIONAL = {
    "not_required",
    "pending",
    "applying",
    "applied",
    "verified",
    "failed",
    "waived",
    "not_owned",
    "unknown",
}
RECORD_ROLES = {"canonical", "alias", "unresolved"}
DISPOSITIONS = {"active", "delegated", "consumed", "superseded", "cancelled", "unknown"}

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".next",
    "AgentDocs",
    "Notes",
    "Vault",
}


class ProjectionError(RuntimeError):
    """Fail-closed projection contract violation."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectionError(f"expected JSON object: {path}")
    return value


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value or "unnamed"


def relpath(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def normalize_tracking(value: Any) -> str:
    raw = str(value or "unknown").strip().lower().replace("-", "_")
    aliases = {
        "active": "in_progress",
        "wip": "in_progress",
        "done": "complete",
        "completed": "complete",
        "shipped": "complete",
    }
    result = aliases.get(raw, raw)
    return result if result in TRACKING else "unknown"


def normalize_enum(value: Any, allowed: set[str]) -> str:
    raw = str(value or "unknown").strip().lower().replace("-", "_")
    return raw if raw in allowed else "unknown"


def roadmap_kind(record_id: str, item_id: str, title: str) -> str:
    haystack = f"{record_id} {item_id} {title}".lower()
    return "spike" if "spike" in haystack or re.match(r"^sp\d+", item_id.lower()) else "milestone"


def attention_for(record: dict[str, Any]) -> str:
    if record["record_role"] == "alias":
        return "delegated-record"
    if record["disposition"] in {"superseded", "cancelled", "consumed"}:
        return "archived-record"

    tracking = record["tracking_status"]
    implementation = record["implementation_status"]
    operational = record["operational_status"]
    if implementation == "published":
        if operational in {"verified", "not_required", "waived"}:
            return "delivery-complete"
        if operational == "applied":
            return "applied-not-verified"
        return "code-complete-ops-open"
    if tracking == "complete":
        return "tracking-complete-delivery-unknown"
    if tracking == "in_progress":
        return "active-work"
    if tracking == "pending":
        return "planned-work"
    return "state-needs-resolution"


def validate_manifest(manifest: dict[str, Any]) -> None:
    projects = manifest.get("projects")
    if not isinstance(projects, dict) or not projects:
        raise ProjectionError("manifest.projects must be a non-empty object")
    ids: list[str] = []
    for display, cfg in projects.items():
        project_id = cfg.get("project_id")
        if not isinstance(project_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", project_id):
            raise ProjectionError(f"project {display!r} has invalid or missing project_id")
        ids.append(project_id)
    duplicates = sorted(k for k, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ProjectionError(f"duplicate project_id values: {', '.join(duplicates)}")


def discover_registers(workspace: Path) -> list[Path]:
    found: list[Path] = []
    for root, dirs, files in os.walk(workspace, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".kargo-critique"))
        if "milestones.json" not in files:
            continue
        candidate = Path(root) / "milestones.json"
        parts = candidate.parts
        try:
            marker = parts.index(".claude")
        except ValueError:
            continue
        tail = parts[marker:]
        if len(tail) == 5 and tail[1:3] == ("notes", "roadmaps") and tail[-1] == "milestones.json":
            found.append(candidate)
    return sorted(found, key=lambda path: path.as_posix())


def repo_root_for_register(path: Path) -> Path:
    parts = path.parts
    marker = parts.index(".claude")
    return Path(*parts[:marker])


def source_document(register: dict[str, Any], register_path: Path) -> Path | None:
    raw = register.get("roadmap_doc")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else repo_root_for_register(register_path) / path


def state_for_item(item: dict[str, Any], register_path: Path) -> tuple[Path | None, dict[str, Any] | None]:
    run = item.get("run") if isinstance(item.get("run"), dict) else {}
    raw = run.get("state_path") or item.get("state_path")
    if not isinstance(raw, str) or not raw:
        return None, None
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root_for_register(register_path) / path
    if not path.is_file():
        return path, None
    return path, load_json(path)


def filename_matches(filename: str, cfg: dict[str, Any]) -> bool:
    low = filename.lower()
    if any(str(x).lower() in low for x in cfg.get("excludes", [])):
        return False
    if any(low.startswith(str(s).lower()) for s in cfg.get("slugs", [])):
        return True
    if any(str(c).lower() in low for c in cfg.get("contains", [])):
        return True
    for raw in cfg.get("slugs", []):
        token = str(raw).lower().rstrip("-.")
        if len(token) >= 4 and re.search(r"(?:^|[-_])" + re.escape(token) + r"(?:[-_.]|$)", low):
            return True
    return False


def discover_legacy_roadmaps(
    manifest: dict[str, Any],
) -> tuple[dict[Path, list[tuple[str, str]]], Any]:
    sys.path.insert(0, str(SCRIPT_DIR))
    import roadmap_status_excalidraw as roadmap_renderer  # noqa: PLC0415

    owners: dict[Path, list[tuple[str, str]]] = defaultdict(list)
    for display, cfg in manifest["projects"].items():
        project_id = cfg["project_id"]
        for raw_path in roadmap_renderer.discover_roadmaps(manifest, cfg):
            path = Path(raw_path).resolve()
            owner = (display, project_id)
            if owner not in owners[path]:
                owners[path].append(owner)
    return dict(owners), roadmap_renderer


def owner_for_register(
    manifest: dict[str, Any],
    roadmap_doc: Path | None,
    register_path: Path,
    discovered: dict[Path, list[tuple[str, str]]],
    canonical_owner: dict[str, Any] | None = None,
) -> tuple[str, str, str, list[str]]:
    by_id = {cfg["project_id"]: display for display, cfg in manifest["projects"].items()}
    if canonical_owner and isinstance(canonical_owner.get("project"), str):
        project_id = canonical_owner["project"]
        if project_id in by_id:
            return by_id[project_id], project_id, "register-v2", [project_id]

    if roadmap_doc is not None:
        exact = discovered.get(roadmap_doc.resolve(), [])
        if len(exact) == 1:
            display, project_id = exact[0]
            return display, project_id, "manifest-source-match", [project_id]
        if len(exact) > 1:
            candidates = sorted({project_id for _display, project_id in exact})
            return "Unassigned", "unassigned", "conflicting", candidates

    repo_root = repo_root_for_register(register_path).resolve()
    repo_matches: list[tuple[str, str]] = []
    workspace = Path(manifest["vault_root"]).resolve()
    for display, cfg in manifest["projects"].items():
        for raw in cfg.get("app_dirs", []):
            app = (workspace / raw).resolve()
            if repo_root == app or app in repo_root.parents:
                repo_matches.append((display, cfg["project_id"]))
                break
    if len(repo_matches) == 1:
        display, project_id = repo_matches[0]
        return display, project_id, "manifest-repo-match", [project_id]
    if len(repo_matches) > 1:
        candidates = sorted({project_id for _display, project_id in repo_matches})
        return "Unassigned", "unassigned", "conflicting", candidates

    name = str((roadmap_doc or register_path.parent).name)
    alias_matches = [
        (display, cfg["project_id"])
        for display, cfg in manifest["projects"].items()
        if filename_matches(name, cfg)
    ]
    if len(alias_matches) == 1:
        display, project_id = alias_matches[0]
        return display, project_id, "manifest-alias-match", [project_id]
    candidates = sorted({project_id for _display, project_id in alias_matches})
    return "Unassigned", "unassigned", ("conflicting" if candidates else "unresolved"), candidates


def alias_location(
    manifest: dict[str, Any], project_display: str, source: Path | None
) -> tuple[str | None, str | None]:
    if source is None or not source.is_file() or project_display == "Unassigned":
        return None, None
    workspace = Path(manifest["vault_root"]).resolve()
    source = source.resolve()
    cfg = manifest["projects"][project_display]
    label: str | None = None
    for region_label, raw in manifest.get("regions", {}).items():
        if source.parent == (workspace / raw).resolve():
            label = region_label
            break
    if label is None:
        for raw in cfg.get("deliverable_dirs", []):
            if source.parent == (workspace / raw).resolve():
                label = "deliverables"
                break
    if label is None:
        for raw in cfg.get("app_dirs", []):
            app = (workspace / raw).resolve()
            if source.parent == app / "plans":
                label = "src-plans"
                break
            if source.parent == app / "docs":
                label = "src-docs"
                break
    if label is None:
        return None, None

    presentation = manifest.get("presentation_vault", {})
    projects_root = presentation.get("projects_root", manifest.get("projects_root", "Notes/Projects"))
    alias_dir = presentation.get("source_alias_dir", "_sources")
    vault_file = Path(projects_root) / project_display / alias_dir / label / source.name
    vault_name = presentation.get("name", "Vault")
    query = urllib.parse.urlencode({"vault": vault_name, "file": vault_file.as_posix()})
    return vault_file.as_posix(), f"obsidian://open?{query}"


def record_quality(record: dict[str, Any], state: dict[str, Any] | None) -> list[str]:
    quality: list[str] = []
    if record["source_mode"] == "legacy-markdown":
        quality.extend(["legacy-checkbox-only", "delivery-state-not-modeled", "canonical-role-unresolved"])
    elif record["source_mode"] == "roadmap-register-v1":
        quality.extend(["v1-register-overloaded-status", "delivery-state-not-modeled", "canonical-role-unresolved"])
    if record["owner_resolution"] in {"unresolved", "conflicting"}:
        quality.append(f"owner-{record['owner_resolution']}")
    if not record["source_exists"]:
        quality.append("source-document-missing")
    if record["implementation_status"] == "unknown":
        quality.append("implementation-evidence-unknown")
    if record["operational_status"] == "unknown":
        quality.append("operational-evidence-unknown")
    if state is None and record.get("state_path"):
        quality.append("pipeline-state-missing")
    return sorted(set(quality))


def register_records(
    manifest: dict[str, Any], discovered: dict[Path, list[tuple[str, str]]]
) -> tuple[list[dict[str, Any]], set[Path]]:
    workspace = Path(manifest["vault_root"]).resolve()
    records: list[dict[str, Any]] = []
    registered_docs: set[Path] = set()
    for register_path in discover_registers(workspace):
        register = load_json(register_path)
        schema_version = register.get("schema_version", 1)
        if not isinstance(schema_version, int) or schema_version < 1:
            raise ProjectionError(f"invalid schema_version in {register_path}")
        roadmap_id = str(register.get("slug") or register_path.parent.name)
        roadmap_doc = source_document(register, register_path)
        if roadmap_doc is not None and roadmap_doc.exists():
            registered_docs.add(roadmap_doc.resolve())
        items = register.get("milestones")
        if not isinstance(items, list):
            raise ProjectionError(f"milestones must be an array: {register_path}")

        for item in items:
            if not isinstance(item, dict):
                raise ProjectionError(f"milestone must be an object: {register_path}")
            record_id = str(item.get("id") or "").strip()
            if not record_id:
                raise ProjectionError(f"milestone without id: {register_path}")
            canonical_owner = item.get("canonical_owner") if isinstance(item.get("canonical_owner"), dict) else None
            display, project_id, owner_resolution, owner_candidates = owner_for_register(
                manifest, roadmap_doc, register_path, discovered, canonical_owner
            )
            state_path, state = state_for_item(item, register_path)
            tracking = normalize_tracking(item.get("status"))
            role = normalize_enum(item.get("record_role"), RECORD_ROLES) if schema_version >= 2 else "unresolved"
            disposition = normalize_enum(item.get("disposition"), DISPOSITIONS) if schema_version >= 2 else "unknown"
            implementation = normalize_enum(item.get("implementation_status"), IMPLEMENTATION)
            operational = normalize_enum(item.get("operational_status"), OPERATIONAL)
            run = item.get("run") if isinstance(item.get("run"), dict) else {}
            alias_path, source_uri = alias_location(manifest, display, roadmap_doc)
            external_writes = item.get("external_writes") if isinstance(item.get("external_writes"), list) else []
            completed_writes = state.get("external_writes_completed", []) if state else []
            if not isinstance(completed_writes, list):
                completed_writes = []
            record: dict[str, Any] = {
                "record_id": record_id,
                "kind": roadmap_kind(record_id, record_id, str(item.get("title") or record_id)),
                "record_role": role,
                "canonical_id": item.get("canonical_id") if isinstance(item.get("canonical_id"), str) else None,
                "disposition": disposition,
                "project_id": project_id,
                "project_name": display,
                "owner_resolution": owner_resolution,
                "owner_candidates": owner_candidates,
                "roadmap_id": roadmap_id,
                "title": str(item.get("title") or record_id),
                "source_subject": str(item.get("title") or record_id),
                "tracking_status": tracking,
                "pipeline_phase": str(state.get("phase")) if state and state.get("phase") else None,
                "implementation_status": implementation,
                "operational_status": operational,
                "required_targets": item.get("required_targets") if isinstance(item.get("required_targets"), list) else [],
                "depends_on": [str(value) for value in item.get("depends_on", [])] if isinstance(item.get("depends_on"), list) else [],
                "source_mode": f"roadmap-register-v{schema_version}",
                "source_path": relpath(roadmap_doc or register_path, workspace),
                "source_exists": bool((roadmap_doc or register_path).is_file()),
                "source_alias": alias_path,
                "source_uri": source_uri,
                "register_path": relpath(register_path, workspace),
                "state_path": relpath(state_path, workspace),
                "updated_at": (state.get("updated_at") if state else None) or run.get("completed_at") or run.get("started_at"),
                "external_writes_declared": len(external_writes),
                "external_writes_completed": len(completed_writes),
            }
            record["attention"] = attention_for(record)
            record["data_quality"] = record_quality(record, state)
            records.append(record)
    return records, registered_docs


def legacy_records(
    manifest: dict[str, Any],
    discovered: dict[Path, list[tuple[str, str]]],
    renderer: Any,
    registered_docs: set[Path],
) -> list[dict[str, Any]]:
    workspace = Path(manifest["vault_root"]).resolve()
    records: list[dict[str, Any]] = []
    for roadmap_path in sorted(discovered, key=lambda path: path.as_posix()):
        if roadmap_path in registered_docs:
            continue
        candidates = discovered[roadmap_path]
        if len(candidates) == 1:
            display, project_id = candidates[0]
            owner_resolution = "manifest-source-match"
        else:
            display, project_id = "Unassigned", "unassigned"
            owner_resolution = "conflicting"
        owner_candidates = sorted({candidate_id for _display, candidate_id in candidates})
        parsed = renderer.parse_roadmap(str(roadmap_path))
        roadmap_id = slugify(roadmap_path.stem.removesuffix("-roadmap"))
        disposition = normalize_enum(parsed.get("status"), DISPOSITIONS)
        if disposition not in {"superseded", "cancelled"}:
            disposition = "unknown"
        alias_path, source_uri = alias_location(manifest, display, roadmap_path)
        for item in parsed.get("items", []):
            item_id = str(item.get("id") or "item")
            record_id = slugify(f"{project_id}-{roadmap_id}-{item_id}")
            record: dict[str, Any] = {
                "record_id": record_id,
                "kind": roadmap_kind(record_id, item_id, str(item.get("title") or item_id)),
                "record_role": "unresolved",
                "canonical_id": None,
                "disposition": disposition,
                "project_id": project_id,
                "project_name": display,
                "owner_resolution": owner_resolution,
                "owner_candidates": owner_candidates,
                "roadmap_id": roadmap_id,
                "title": str(item.get("title") or item_id),
                "source_subject": str(item.get("source_subject") or item.get("title") or item_id),
                "tracking_status": normalize_tracking(item.get("status")),
                "pipeline_phase": None,
                "implementation_status": "unknown",
                "operational_status": "unknown",
                "required_targets": [],
                "depends_on": [],
                "source_mode": "legacy-markdown",
                "source_path": relpath(roadmap_path, workspace),
                "source_exists": True,
                "source_alias": alias_path,
                "source_uri": source_uri,
                "register_path": None,
                "state_path": None,
                "updated_at": None,
                "external_writes_declared": 0,
                "external_writes_completed": 0,
            }
            record["attention"] = attention_for(record)
            record["data_quality"] = record_quality(record, None)
            records.append(record)
    return records


def validate_records(records: list[dict[str, Any]]) -> None:
    required = {
        "record_id",
        "kind",
        "record_role",
        "disposition",
        "project_id",
        "project_name",
        "owner_resolution",
        "roadmap_id",
        "title",
        "source_subject",
        "tracking_status",
        "implementation_status",
        "operational_status",
        "attention",
        "source_mode",
        "source_path",
        "source_exists",
        "data_quality",
    }
    seen: set[str] = set()
    for record in records:
        missing = sorted(required - record.keys())
        if missing:
            raise ProjectionError(f"record {record.get('record_id')} missing: {', '.join(missing)}")
        record_id = record["record_id"]
        if record_id in seen:
            raise ProjectionError(f"duplicate record_id: {record_id}")
        seen.add(record_id)
        if record["tracking_status"] not in TRACKING:
            raise ProjectionError(f"invalid tracking_status on {record_id}")
        if record["implementation_status"] not in IMPLEMENTATION:
            raise ProjectionError(f"invalid implementation_status on {record_id}")
        if record["operational_status"] not in OPERATIONAL:
            raise ProjectionError(f"invalid operational_status on {record_id}")
        if record["record_role"] not in RECORD_ROLES:
            raise ProjectionError(f"invalid record_role on {record_id}")
        if record["disposition"] not in DISPOSITIONS:
            raise ProjectionError(f"invalid disposition on {record_id}")
        if record["record_role"] == "alias" and not record.get("canonical_id"):
            raise ProjectionError(f"alias {record_id} has no canonical_id")


def counter(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def build_index(manifest: dict[str, Any]) -> dict[str, Any]:
    validate_manifest(manifest)
    discovered, renderer = discover_legacy_roadmaps(manifest)
    registered, registered_docs = register_records(manifest, discovered)
    legacy = legacy_records(manifest, discovered, renderer, registered_docs)
    records = sorted(registered + legacy, key=lambda r: (r["project_id"], r["roadmap_id"], r["record_id"]))
    validate_records(records)

    normalized = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    snapshot = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    project_counts: dict[str, int] = defaultdict(int)
    for record in records:
        project_counts[record["project_id"]] += 1
    summary = {
        "total_records": len(records),
        "by_tracking_status": counter([record["tracking_status"] for record in records]),
        "by_implementation_status": counter([record["implementation_status"] for record in records]),
        "by_operational_status": counter([record["operational_status"] for record in records]),
        "by_attention": counter([record["attention"] for record in records]),
        "by_owner_resolution": counter([record["owner_resolution"] for record in records]),
        "by_source_mode": counter([record["source_mode"] for record in records]),
        "by_data_quality": counter([flag for record in records for flag in record["data_quality"]]),
        "by_project": dict(sorted(project_counts.items())),
    }
    presentation = manifest.get("presentation_vault", {})
    return {
        "schema_version": 1,
        "generated_by": GENERATOR,
        "source_snapshot_sha256": snapshot,
        "source_root": manifest["vault_root"],
        "presentation_vault": {
            "name": presentation.get("name", "Vault"),
            "projects_root": presentation.get("projects_root", manifest.get("projects_root", "Notes/Projects")),
        },
        "summary": summary,
        "records": records,
    }


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def yaml_list(values: list[Any]) -> str:
    return json.dumps(values, ensure_ascii=False)


def record_note(record: dict[str, Any]) -> str:
    normalized = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    record_snapshot = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    tags = [
        "type/portfolio-record",
        f"project/{record['project_id']}",
        f"attention/{record['attention']}",
        f"tracking/{record['tracking_status']}",
    ]
    lines = [
        "---",
        "type: portfolio-record",
        f"record_id: {yaml_scalar(record['record_id'])}",
        f"kind: {yaml_scalar(record['kind'])}",
        f"record_role: {yaml_scalar(record['record_role'])}",
        f"canonical_id: {yaml_scalar(record['canonical_id'])}",
        f"disposition: {yaml_scalar(record['disposition'])}",
        f"project: {yaml_scalar(record['project_id'])}",
        f"project_id: {yaml_scalar(record['project_id'])}",
        f"project_name: {yaml_scalar(record['project_name'])}",
        f"owner_resolution: {yaml_scalar(record['owner_resolution'])}",
        f"owner_candidates: {yaml_list(record['owner_candidates'])}",
        f"roadmap_id: {yaml_scalar(record['roadmap_id'])}",
        f"title: {yaml_scalar(record['title'])}",
        f"tracking_status: {yaml_scalar(record['tracking_status'])}",
        f"pipeline_phase: {yaml_scalar(record['pipeline_phase'])}",
        f"implementation_status: {yaml_scalar(record['implementation_status'])}",
        f"operational_status: {yaml_scalar(record['operational_status'])}",
        f"attention: {yaml_scalar(record['attention'])}",
        f"source_mode: {yaml_scalar(record['source_mode'])}",
        f"source_path: {yaml_scalar(record['source_path'])}",
        f"source_exists: {yaml_scalar(record['source_exists'])}",
        f"state_path: {yaml_scalar(record['state_path'])}",
        f"depends_on: {yaml_list(record['depends_on'])}",
        f"required_targets: {yaml_list(record['required_targets'])}",
        f"data_quality: {yaml_list(record['data_quality'])}",
        f"record_snapshot: {yaml_scalar(record_snapshot)}",
        "tags:",
        *[f"  - {yaml_scalar(tag)}" for tag in tags],
        "---",
        f"# {record['title']}",
        "",
        f"> [!abstract] Generated projection — `{record['record_id']}`",
        "> This note reports source facts without promoting checklist completion to deployment truth.",
        "",
        "| Axis | State |",
        "|---|---|",
        f"| Tracking/checklist | **{record['tracking_status']}** |",
        f"| Pipeline phase | **{record['pipeline_phase'] or 'unknown'}** |",
        f"| Implementation delivery | **{record['implementation_status']}** |",
        f"| Operational delivery | **{record['operational_status']}** |",
        f"| Attention | **{record['attention']}** |",
        "",
        "## Provenance",
        "",
        f"- Project: `{record['project_id']}` ({record['owner_resolution']})",
        f"- Roadmap: `{record['roadmap_id']}`",
        f"- Source mode: `{record['source_mode']}`",
    ]
    if record.get("source_uri"):
        lines.append(f"- Source: [{record['source_path']}]({record['source_uri']})")
    else:
        lines.append(f"- Source workspace path: `{record['source_path']}`")
    if record.get("register_path"):
        lines.append(f"- Register: `{record['register_path']}`")
    if record.get("state_path"):
        lines.append(f"- Pipeline state: `{record['state_path']}`")
    if record["source_subject"] != record["title"]:
        lines.extend(["", "## Source subject", "", record["source_subject"]])
    if record["data_quality"]:
        lines.extend(["", "## Data-quality flags", "", *[f"- `{flag}`" for flag in record["data_quality"]]])
    lines.extend(["", f"_Generated by `{GENERATOR}`. Do not edit by hand._", ""])
    return "\n".join(lines)


def base_document() -> str:
    return """filters:
  and:
    - note.type == "portfolio-record"
views:
  - type: table
    name: Tracking complete, delivery unknown
    filters:
      and:
        - note.attention == "tracking-complete-delivery-unknown"
    groupBy:
      property: project_id
      direction: ASC
    order:
      - project_id
      - roadmap_id
      - tracking_status
      - implementation_status
      - operational_status
      - owner_resolution
      - title
      - file.name
  - type: table
    name: Code complete, operations open
    filters:
      and:
        - note.attention == "code-complete-ops-open"
    groupBy:
      property: operational_status
      direction: ASC
    order:
      - project_id
      - roadmap_id
      - implementation_status
      - operational_status
      - title
      - file.name
  - type: table
    name: Applied, not verified
    filters:
      and:
        - note.attention == "applied-not-verified"
    order:
      - project_id
      - roadmap_id
      - operational_status
      - title
      - file.name
  - type: table
    name: Active work
    filters:
      and:
        - note.attention == "active-work"
    groupBy:
      property: project_id
      direction: ASC
    order:
      - project_id
      - roadmap_id
      - pipeline_phase
      - title
      - file.name
  - type: table
    name: Owner unresolved
    filters:
      and:
        - note.project_id == "unassigned"
    groupBy:
      property: roadmap_id
      direction: ASC
    order:
      - roadmap_id
      - owner_candidates
      - tracking_status
      - title
      - file.name
  - type: table
    name: Security dashboard boundary
    filters:
      or:
        - note.roadmap_id == "admin-web-app-security-dashboard"
        - note.roadmap_id == "security-dashboard-edge-auth"
    groupBy:
      property: roadmap_id
      direction: ASC
    order:
      - project_id
      - tracking_status
      - implementation_status
      - operational_status
      - title
      - file.name
  - type: table
    name: Canonicalization migration
    filters:
      and:
        - note.record_role == "unresolved"
    groupBy:
      property: owner_resolution
      direction: ASC
    order:
      - owner_resolution
      - owner_candidates
      - project_id
      - roadmap_id
      - title
      - file.name
  - type: table
    name: Entire portfolio
    groupBy:
      property: project_id
      direction: ASC
    order:
      - project_id
      - roadmap_id
      - tracking_status
      - implementation_status
      - operational_status
      - attention
      - title
      - file.name
"""


def portfolio_note(index: dict[str, Any]) -> str:
    summary = index["summary"]
    by_attention = summary["by_attention"]
    by_quality = summary["by_data_quality"]
    by_owner = summary["by_owner_resolution"]
    delivery_file = "Notes/Projects/Pipeline Tooling/_sources/plans/milestone-pipeline-delivery-state-v2.md"
    delivery_query = urllib.parse.urlencode(
        {"vault": index["presentation_vault"]["name"], "file": delivery_file}
    )
    delivery_uri = f"obsidian://open?{delivery_query}"
    lines = [
        "---",
        "type: portfolio-dashboard",
        "project: portfolio",
        f"projection_snapshot: {yaml_scalar(index['source_snapshot_sha256'])}",
        "tags:",
        "  - type/portfolio-dashboard",
        "  - project/portfolio",
        "---",
        "# workspace milestone portfolio",
        "",
        "> [!warning] Tracking completion is not delivery completion",
        "> A checked roadmap item or v1 pipeline `complete` is retained as a tracking fact. It is not",
        "> promoted to `published`, `applied`, or `verified` without explicit structured evidence.",
        "",
        "## Current projection",
        "",
        "| Measure | Count |",
        "|---|---:|",
        f"| Total milestone/spike records | {summary['total_records']} |",
        f"| Active work | {by_attention.get('active-work', 0)} |",
        f"| Planned work | {by_attention.get('planned-work', 0)} |",
        f"| Tracking complete, delivery unknown | {by_attention.get('tracking-complete-delivery-unknown', 0)} |",
        f"| Code complete, operations open | {by_attention.get('code-complete-ops-open', 0)} |",
        f"| Applied, not independently verified | {by_attention.get('applied-not-verified', 0)} |",
        f"| Delivery complete under v2 evidence | {by_attention.get('delivery-complete', 0)} |",
        f"| Records with unresolved ownership | {by_owner.get('unresolved', 0)} |",
        f"| Records whose declared source document is missing | {by_quality.get('source-document-missing', 0)} |",
        "",
        "## Portfolio views",
        "",
        "![[Notes/Bases/Milestone Portfolio.base]]",
        "",
        "## Projection contract",
        "",
        "```mermaid",
        "flowchart LR",
        '  A["Authoritative roadmaps/registers"] --> B["Deterministic portfolio index"]',
        '  C["Milestone pipeline state/evidence"] --> B',
        '  B --> D["Generated Obsidian records"]',
        '  D --> E["Bases, project views, Canvas, Excalidraw"]',
        "```",
        "",
        "The projection is one-way. Generated notes contain no actionable checkboxes and must never",
        f"be edited to change source state. See [delivery-state v2]({delivery_uri})",
        "for the target implementation/operations evidence contract.",
        "",
        f"Snapshot: `{index['source_snapshot_sha256']}`",
        "",
        f"_Generated by `{GENERATOR}`. Do not edit by hand._",
        "",
    ]
    return "\n".join(lines)


def expected_outputs(index: dict[str, Any], workspace: Path) -> dict[Path, str]:
    notes = workspace / "Notes"
    output: dict[Path, str] = {
        notes / "Portfolio" / "portfolio-index.json": json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        notes / "Portfolio" / "Portfolio.md": portfolio_note(index),
        notes / "Bases" / "Milestone Portfolio.base": base_document(),
    }
    records_dir = notes / "Portfolio" / "Milestones"
    filenames: set[str] = set()
    for record in index["records"]:
        filename = slugify(record["record_id"]) + ".md"
        if filename in filenames:
            raise ProjectionError(f"record filenames collide after normalization: {filename}")
        filenames.add(filename)
        output[records_dir / filename] = record_note(record)
    return output


def owned_stale_files(expected: dict[Path, str], workspace: Path) -> list[Path]:
    records_dir = workspace / "Notes" / "Portfolio" / "Milestones"
    if not records_dir.is_dir():
        return []
    expected_paths = set(expected)
    return sorted((path for path in records_dir.glob("*.md") if path not in expected_paths), key=lambda path: path.name)


def apply_outputs(expected: dict[Path, str], stale: list[Path], check: bool) -> tuple[list[Path], list[Path]]:
    changed = [
        path
        for path, content in expected.items()
        if not path.is_file() or path.read_text(encoding="utf-8") != content
    ]
    if check:
        return sorted(changed), stale
    for path in sorted(changed):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected[path], encoding="utf-8")
    for path in stale:
        path.unlink()
    return sorted(changed), stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--check", action="store_true", help="report drift; do not write")
    parser.add_argument("--json-summary", action="store_true", help="emit summary as JSON")
    args = parser.parse_args()

    try:
        manifest = load_project_manifest(args.manifest.resolve())
        workspace = Path(manifest["vault_root"]).resolve()
        index = build_index(manifest)
        expected = expected_outputs(index, workspace)
        stale = owned_stale_files(expected, workspace)
        changed, stale = apply_outputs(expected, stale, args.check)
    except (KeyError, ProjectionError, OSError) as exc:
        print(f"portfolio projection failed: {exc}", file=sys.stderr)
        return 2

    if args.json_summary:
        print(json.dumps(index["summary"], indent=2, sort_keys=True))
    if args.check:
        if changed or stale:
            for path in changed:
                print(f"DRIFT {relpath(path, workspace)}")
            for path in stale:
                print(f"STALE {relpath(path, workspace)}")
            return 1
        print(f"PASS portfolio projection ({index['summary']['total_records']} records; no drift)")
        return 0

    print(
        f"WROTE portfolio projection: {index['summary']['total_records']} records, "
        f"{len(changed)} changed files, {len(stale)} stale files removed"
    )
    print(f"SNAPSHOT {index['source_snapshot_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

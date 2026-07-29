#!/usr/bin/env python3
"""Run a fail-closed Graphiti extraction-quality pilot.

The pilot consumes three fixed units from an immutable artifact outbox,
creates one model-specific FalkorDB namespace per case, and audits each graph
before proceeding to the next case. Existing graph data is never deleted or
rewritten. A failed case stops the run and leaves its graph as evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import artifact_ingestion as ingestion
import graphiti_policy
import artifact_runtime


PILOT_SCHEMA_VERSION = 1
PILOT_VERSION = 3
DEFAULT_OUTBOX = artifact_runtime.derived_root() / "outbox" / "catalog-run-7-chunks-v2"
DEFAULT_REPORT_ROOT = artifact_runtime.derived_root() / "graphiti-pilots"
DEFAULT_LLM_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_EMBEDDING_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
DEFAULT_EMBEDDING_DIM = 768
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6379
DEFAULT_CONTROLLER_GRAPH = "graphiti_pilot_controller"
DEFAULT_REASONING_EFFORT = "none"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 180.0
DEFAULT_CASE_TIMEOUT_SECONDS = 600.0
ENTITY_TYPES = ("Decision", "WorkItem", "Component", "Actor", "Requirement")
EDGE_TYPES = (
    "AppliesTo",
    "Blocks",
    "Changes",
    "DependsOn",
    "Implements",
    "OwnedBy",
    "Owns",
    "Requires",
    "Satisfies",
    "Supersedes",
)
UNRESOLVED_EDGE_MARKERS = (
    "Source entity not found in nodes for edge relation",
    "Target entity not found in nodes for edge relation",
)
FILE_EXTENSION = re.compile(
    r"\.(?:go|js|jsx|ts|tsx|py|sh|md|json|ya?ml|toml|tf|cue|sql|log)$",
    re.IGNORECASE,
)
ENVIRONMENT_KEY = re.compile(r"^[A-Z][A-Z0-9]*_[A-Z0-9_]+$")
LOWER_CAMEL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+$")
CALLABLE_IDENTIFIER = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\([^)]*\)$"
)
IDENTIFIER_SAFE = re.compile(r"[^a-z0-9_]+")


class PilotError(ValueError):
    """The pilot cannot continue without weakening a safety gate."""


class _CompletionsWithDefaults:
    """Inject pilot request controls without patching Graphiti or the SDK."""

    def __init__(self, completions: Any, reasoning_effort: str) -> None:
        self._completions = completions
        self._reasoning_effort = reasoning_effort

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("reasoning_effort", self._reasoning_effort)
        return await self._completions.create(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._completions, name)


class _ChatWithDefaults:
    def __init__(self, chat: Any, reasoning_effort: str) -> None:
        self._chat = chat
        self.completions = _CompletionsWithDefaults(
            chat.completions,
            reasoning_effort,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)


class _OpenAIClientWithDefaults:
    """Proxy an AsyncOpenAI client while controlling every chat completion."""

    def __init__(self, client: Any, reasoning_effort: str) -> None:
        self._client = client
        self.chat = _ChatWithDefaults(client.chat, reasoning_effort)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


@dataclass(frozen=True)
class PilotCase:
    case_id: str
    artifact_type: str
    unit_id: str
    required_terms: tuple[str, ...]
    minimum_term_matches: int


PILOT_CASES = (
    PilotCase(
        case_id="decision",
        artifact_type="decision",
        unit_id="c936e0fa-bb8b-5f47-bcc3-88f572bf5a18",
        required_terms=("ConfirmationPolicy", "pure-Go", "cel-go"),
        minimum_term_matches=2,
    ),
    PilotCase(
        case_id="handoff",
        artifact_type="handoff",
        unit_id="1ce77bda-801d-55a2-a3a8-487057d64149",
        required_terms=("agents-dispatcher", "AWS Secrets Manager", "Keycloak"),
        minimum_term_matches=2,
    ),
    PilotCase(
        case_id="roadmap",
        artifact_type="roadmap",
        unit_id="899dff64-aa8c-5854-8799-999b1bdb5788",
        required_terms=("L3", "Kyverno", "OSMS"),
        minimum_term_matches=2,
    ),
)


PILOT_INSTRUCTIONS = """
Extract only explicit project-management facts from this agent artifact.

Allowed entity classes:
- Decision: an explicit accepted, rejected, or superseded choice.
- WorkItem: a named task, milestone, gate, or deliverable with actionable state.
- Component: a real system, service, repository, chart, environment, or process.
- Actor: a person, team, or organization explicitly responsible for work.
- Requirement: an explicit constraint, acceptance condition, or prerequisite.

Never create entities for file paths, filenames, configuration keys,
environment variables, commands, code symbols, credentials, example values,
line numbers, or revision hashes. Ignore code blocks except when their prose
states an explicit decision, dependency, owner, status, deadline, requirement,
or supersession. Do not infer facts that the episode does not state. Use only
the supplied custom edge types. Prefer no edge over a vague or generic edge.
""".strip()


def _slug(value: str) -> str:
    slug = IDENTIFIER_SAFE.sub("_", value.lower()).strip("_")
    if not slug:
        raise PilotError("model name cannot be converted to an identifier")
    return slug[:40]


def pilot_namespace(model: str, case: PilotCase) -> str:
    digest = hashlib.sha256(case.unit_id.encode("utf-8")).hexdigest()[:10]
    return (
        f"graphiti_pilot_v{PILOT_VERSION}_{_slug(model)}_"
        f"{case.case_id}_{digest}"
    )


def _pilot_ontology() -> tuple[dict[str, Any], dict[str, Any], dict[tuple[str, str], list[str]]]:
    try:
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise PilotError("Pydantic is required for the Graphiti pilot") from exc

    class Decision(BaseModel):
        """An explicit accepted, rejected, or superseded project choice."""

        verdict: str | None = Field(
            None,
            description="Explicit verdict such as accepted, rejected, or qualified",
        )
        status: str | None = Field(
            None,
            description="Explicit lifecycle status of the decision",
        )
        rationale: str | None = Field(
            None,
            description="Brief rationale stated in the artifact",
        )

    class WorkItem(BaseModel):
        """A named task, milestone, gate, or deliverable."""

        kind: str | None = Field(
            None,
            description="Task, milestone, gate, deliverable, or follow-up",
        )
        status: str | None = Field(
            None,
            description="Explicit status such as planned, blocked, in progress, or done",
        )
        deadline: str | None = Field(
            None,
            description="Explicitly stated deadline or target date",
        )

    class Component(BaseModel):
        """A real system, service, repository, chart, environment, or process."""

        component_kind: str | None = Field(
            None,
            description="System, service, repository, chart, environment, or process",
        )

    class Actor(BaseModel):
        """A person, team, or organization explicitly responsible for work."""

        actor_kind: str | None = Field(
            None,
            description="Person, team, or organization",
        )

    class Requirement(BaseModel):
        """An explicit constraint, acceptance condition, or prerequisite."""

        priority: str | None = Field(
            None,
            description="Explicit priority or severity when present",
        )
        status: str | None = Field(
            None,
            description="Explicit state of the requirement",
        )

    class AppliesTo(BaseModel):
        """The source explicitly applies to the target."""

    class Blocks(BaseModel):
        """The source explicitly prevents the target from proceeding."""

    class Changes(BaseModel):
        """The source explicitly changes the target."""

    class DependsOn(BaseModel):
        """The source explicitly depends on the target."""

    class Implements(BaseModel):
        """The source explicitly implements the target decision or requirement."""

    class OwnedBy(BaseModel):
        """The source is explicitly owned by the target actor."""

    class Owns(BaseModel):
        """The source actor explicitly owns the target."""

    class Requires(BaseModel):
        """The source explicitly requires the target."""

    class Satisfies(BaseModel):
        """The source explicitly satisfies the target requirement."""

    class Supersedes(BaseModel):
        """The source explicitly replaces or supersedes the target."""

    entity_types = {
        "Decision": Decision,
        "WorkItem": WorkItem,
        "Component": Component,
        "Actor": Actor,
        "Requirement": Requirement,
    }
    edge_types = {
        "AppliesTo": AppliesTo,
        "Blocks": Blocks,
        "Changes": Changes,
        "DependsOn": DependsOn,
        "Implements": Implements,
        "OwnedBy": OwnedBy,
        "Owns": Owns,
        "Requires": Requires,
        "Satisfies": Satisfies,
        "Supersedes": Supersedes,
    }
    edge_type_map = {
        ("Decision", "Decision"): ["Supersedes", "DependsOn"],
        ("Decision", "WorkItem"): ["AppliesTo", "Blocks", "Requires"],
        ("Decision", "Component"): ["AppliesTo", "Changes", "Requires"],
        ("Decision", "Actor"): ["OwnedBy"],
        ("Decision", "Requirement"): ["AppliesTo", "Requires", "Satisfies"],
        ("WorkItem", "Decision"): ["DependsOn", "Implements"],
        ("WorkItem", "WorkItem"): ["Blocks", "DependsOn", "Supersedes"],
        ("WorkItem", "Component"): ["AppliesTo", "Changes", "DependsOn"],
        ("WorkItem", "Actor"): ["OwnedBy"],
        ("WorkItem", "Requirement"): ["DependsOn", "Satisfies"],
        ("Component", "Decision"): ["Implements", "DependsOn"],
        ("Component", "WorkItem"): ["Blocks", "DependsOn", "Implements"],
        ("Component", "Component"): ["Changes", "DependsOn", "Supersedes"],
        ("Component", "Actor"): ["OwnedBy"],
        ("Component", "Requirement"): ["DependsOn", "Satisfies"],
        ("Actor", "Decision"): ["Owns"],
        ("Actor", "WorkItem"): ["Owns"],
        ("Actor", "Component"): ["Owns"],
        ("Actor", "Actor"): ["DependsOn"],
        ("Actor", "Requirement"): ["Owns"],
        ("Requirement", "Decision"): ["AppliesTo", "Blocks", "Requires"],
        ("Requirement", "WorkItem"): ["AppliesTo", "Blocks", "Requires"],
        ("Requirement", "Component"): ["AppliesTo", "Requires"],
        ("Requirement", "Actor"): ["OwnedBy"],
        ("Requirement", "Requirement"): ["DependsOn", "Supersedes"],
    }
    expected_pairs = {
        (source, target) for source in ENTITY_TYPES for target in ENTITY_TYPES
    }
    if set(edge_type_map) != expected_pairs:
        raise PilotError("pilot edge map does not cover every entity type pair")
    return entity_types, edge_types, edge_type_map


def _load_case_units(outbox: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = ingestion.load_outbox_manifest(outbox)
    wanted = {case.unit_id for case in PILOT_CASES}
    found: dict[str, dict[str, Any]] = {}
    for unit in ingestion.iter_outbox_units(outbox):
        unit_id = str(unit["unit_id"])
        if unit_id in wanted:
            found[unit_id] = unit
    missing = sorted(wanted - set(found))
    if missing:
        raise PilotError(f"pilot units are missing from the outbox: {missing}")
    for case in PILOT_CASES:
        unit = found[case.unit_id]
        if unit["artifact_type"] != case.artifact_type:
            raise PilotError(
                f"pilot case {case.case_id} expected {case.artifact_type}, "
                f"got {unit['artifact_type']}"
            )
    return manifest, found


def _forbidden_reason(name: str) -> str | None:
    stripped = name.strip().strip("`")
    if "/" in stripped or "\\" in stripped:
        return "path-like"
    if FILE_EXTENSION.search(stripped):
        return "file-extension"
    if ENVIRONMENT_KEY.fullmatch(stripped):
        return "environment-key"
    if CALLABLE_IDENTIFIER.fullmatch(stripped):
        return "code-symbol"
    if LOWER_CAMEL_IDENTIFIER.fullmatch(stripped):
        return "code-identifier"
    if re.fullmatch(r"(?:revision:)?[0-9a-f]{7,64}", stripped, re.IGNORECASE):
        return "revision-hash"
    return None


def _term_matches(
    required_terms: Iterable[str],
    entities: Sequence[dict[str, Any]],
    facts: Sequence[dict[str, Any]],
) -> list[str]:
    corpus = "\n".join(
        [str(entity["name"]) for entity in entities]
        + [str(fact["fact"]) for fact in facts]
    ).lower()
    return [term for term in required_terms if term.lower() in corpus]


def _audit_payload(
    *,
    case: PilotCase,
    namespace: str,
    entities: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    episode_count: int,
    unresolved_warnings: Sequence[str],
) -> dict[str, Any]:
    forbidden = [
        {"name": entity["name"], "reason": reason}
        for entity in entities
        if (reason := _forbidden_reason(str(entity["name"]))) is not None
    ]
    generic_count = sum(
        str(fact["type"]).upper() in {"RELATES_TO", "RELATED_TO"}
        for fact in facts
    )
    unexpected_types = sorted(
        {str(fact["type"]) for fact in facts if fact["type"] not in EDGE_TYPES}
    )
    missing_provenance = sum(not fact.get("episodes") for fact in facts)
    missing_valid_at = sum(fact.get("valid_at") is None for fact in facts)
    group_mismatches = sum(
        entity.get("group_id") != namespace for entity in entities
    ) + sum(fact.get("group_id") != namespace for fact in facts)
    untyped_entities = [
        entity["name"]
        for entity in entities
        if not set(entity.get("labels", ())).intersection(ENTITY_TYPES)
    ]
    matched_terms = _term_matches(case.required_terms, entities, facts)
    violations: list[str] = []
    if episode_count != 1:
        violations.append(f"expected exactly 1 episode, found {episode_count}")
    if not 2 <= len(entities) <= 18:
        violations.append(f"entity count {len(entities)} is outside 2..18")
    if not 1 <= len(facts) <= 16:
        violations.append(f"fact count {len(facts)} is outside 1..16")
    if forbidden:
        violations.append(f"found {len(forbidden)} incidental entity names")
    if generic_count:
        violations.append(f"found {generic_count} generic RELATES_TO facts")
    if unexpected_types:
        violations.append(f"unexpected edge types: {unexpected_types}")
    if missing_provenance:
        violations.append(f"{missing_provenance} facts lack episode provenance")
    if missing_valid_at:
        violations.append(f"{missing_valid_at} facts lack valid_at")
    if group_mismatches:
        violations.append(f"{group_mismatches} records escaped the namespace")
    if untyped_entities:
        violations.append(f"{len(untyped_entities)} entities lack a pilot type")
    if unresolved_warnings:
        violations.append(f"{len(unresolved_warnings)} unresolved edge warnings")
    if len(matched_terms) < case.minimum_term_matches:
        violations.append(
            f"matched {len(matched_terms)} required terms; "
            f"minimum is {case.minimum_term_matches}"
        )
    return {
        "case_id": case.case_id,
        "namespace": namespace,
        "passed": not violations,
        "metrics": {
            "episodes": episode_count,
            "entities": len(entities),
            "facts": len(facts),
            "forbidden_entities": len(forbidden),
            "generic_relates_to": generic_count,
            "unexpected_edge_types": len(unexpected_types),
            "missing_provenance": missing_provenance,
            "missing_valid_at": missing_valid_at,
            "group_mismatches": group_mismatches,
            "untyped_entities": len(untyped_entities),
            "unresolved_edge_warnings": len(unresolved_warnings),
            "required_term_matches": len(matched_terms),
        },
        "required_terms": list(case.required_terms),
        "matched_terms": matched_terms,
        "forbidden": forbidden,
        "untyped_entity_names": untyped_entities,
        "unexpected_edge_types": unexpected_types,
        "unresolved_warnings": list(unresolved_warnings),
        "violations": violations,
        "entities_detail": entities,
        "facts_detail": facts,
    }


def audit_namespace(
    *,
    host: str,
    port: int,
    password_env: str,
    namespace: str,
    case: PilotCase,
    unresolved_warnings: Sequence[str] = (),
) -> dict[str, Any]:
    try:
        from falkordb import FalkorDB
    except ImportError as exc:
        raise PilotError("FalkorDB client is required for pilot audit") from exc
    client = FalkorDB(
        host=host,
        port=port,
        password=os.environ.get(password_env),
    )
    try:
        if namespace not in client.list_graphs():
            return {
                "case_id": case.case_id,
                "namespace": namespace,
                "passed": False,
                "status": "missing",
                "violations": ["pilot namespace does not exist"],
            }
        graph = client.select_graph(namespace)
        entity_rows = graph.query(
            "MATCH (n:Entity) "
            "RETURN n.name, labels(n), n.group_id ORDER BY n.name"
        ).result_set
        fact_rows = graph.query(
            "MATCH (:Entity)-[r]->(:Entity) "
            "RETURN r.name, r.fact, r.group_id, r.episodes, "
            "r.valid_at, r.invalid_at ORDER BY r.fact"
        ).result_set
        episode_rows = graph.query(
            "MATCH (e:Episodic) RETURN e.uuid"
        ).result_set
        entities = [
            {"name": row[0], "labels": row[1], "group_id": row[2]}
            for row in entity_rows
        ]
        facts = [
            {
                "type": row[0],
                "fact": row[1],
                "group_id": row[2],
                "episodes": row[3],
                "valid_at": row[4],
                "invalid_at": row[5],
            }
            for row in fact_rows
        ]
        return _audit_payload(
            case=case,
            namespace=namespace,
            entities=entities,
            facts=facts,
            episode_count=len(episode_rows),
            unresolved_warnings=unresolved_warnings,
        )
    finally:
        client.close()


class _GraphitiWarningCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if any(marker in message for marker in UNRESOLVED_EDGE_MARKERS):
            self.messages.append(message)


def _graph_counts(host: str, port: int, password_env: str) -> dict[str, int]:
    try:
        from falkordb import FalkorDB
    except ImportError as exc:
        raise PilotError("FalkorDB client is required for the pilot") from exc
    client = FalkorDB(
        host=host,
        port=port,
        password=os.environ.get(password_env),
    )
    try:
        counts: dict[str, int] = {}
        for graph_name in client.list_graphs():
            result = client.select_graph(graph_name).query(
                "MATCH (e:Episodic) RETURN count(e)"
            )
            counts[graph_name] = int(result.result_set[0][0])
        return counts
    finally:
        client.close()


def pilot_plan(
    *,
    outbox: Path,
    model: str,
    host: str,
    port: int,
    password_env: str,
) -> dict[str, Any]:
    manifest, units = _load_case_units(outbox)
    graph_counts = _graph_counts(host, port, password_env)
    cases = []
    for case in PILOT_CASES:
        unit = units[case.unit_id]
        namespace = pilot_namespace(model, case)
        episode_count = graph_counts.get(namespace, 0)
        status = "new"
        if namespace in graph_counts and episode_count == 1:
            status = "existing"
        elif namespace in graph_counts:
            status = "blocked-partial"
        cases.append(
            {
                "case_id": case.case_id,
                "unit_id": case.unit_id,
                "artifact_type": unit["artifact_type"],
                "relative_path": unit["relative_path"],
                "chunk_index": unit["chunk_index"],
                "content_chars": len(unit["content"]),
                "namespace": namespace,
                "existing_episodes": episode_count,
                "status": status,
            }
        )
    return {
        "schema_version": PILOT_SCHEMA_VERSION,
        "mode": "plan",
        "pilot_version": PILOT_VERSION,
        "model": model,
        "outbox": str(outbox.expanduser().resolve()),
        "outbox_sha256": manifest["units_sha256"],
        "cases": cases,
        "would_ingest": sum(case["status"] == "new" for case in cases),
        "point_deletion": "disabled",
        "graph_deletion": "disabled",
        "source_mutation": "disabled",
    }


async def _run_cases_async(
    *,
    base_driver: Any,
    units: dict[str, dict[str, Any]],
    model: str,
    llm_base_url: str,
    embedding_base_url: str,
    embedding_model: str,
    embedding_dim: int,
    api_key_env: str,
    host: str,
    port: int,
    password_env: str,
    reasoning_effort: str,
    request_timeout_seconds: float,
    case_timeout_seconds: float,
) -> list[dict[str, Any]]:
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
    os.environ.setdefault("SEMAPHORE_LIMIT", "1")
    try:
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import (
            OpenAIRerankerClient,
        )
        from graphiti_core.embedder.openai import (
            OpenAIEmbedder,
            OpenAIEmbedderConfig,
        )
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import (
            OpenAIGenericClient,
        )
        from graphiti_core.nodes import EpisodeType
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise PilotError("Graphiti dependencies are required for apply") from exc
    api_key = os.environ.get(api_key_env)
    if not api_key and any(
        local in llm_base_url or local in embedding_base_url
        for local in ("127.0.0.1", "localhost")
    ):
        api_key = "ollama"
    if not api_key:
        raise PilotError(f"API key environment variable is unset: {api_key_env}")
    config = LLMConfig(
        api_key=api_key,
        model=model,
        small_model=model,
        base_url=llm_base_url,
        temperature=0,
        max_tokens=8192,
    )
    raw_llm_client = AsyncOpenAI(
        api_key=api_key,
        base_url=llm_base_url,
        timeout=request_timeout_seconds,
        max_retries=0,
    )
    llm = OpenAIGenericClient(
        config=config,
        client=_OpenAIClientWithDefaults(raw_llm_client, reasoning_effort),
        structured_output_mode="json_schema",
        max_tokens=8192,
    )
    GuardedGraphiti = graphiti_policy.guarded_graphiti_class(
        Graphiti,
        allowed_edge_types=EDGE_TYPES,
    )
    graphiti = GuardedGraphiti(
        graph_driver=base_driver,
        llm_client=llm,
        embedder=OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key=api_key,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                base_url=embedding_base_url,
            )
        ),
        cross_encoder=OpenAIRerankerClient(client=llm, config=config),
        max_coroutines=1,
    )
    entity_types, edge_types, edge_type_map = _pilot_ontology()
    warning_logger = logging.getLogger(
        "graphiti_core.utils.maintenance.edge_operations"
    )
    reports: list[dict[str, Any]] = []
    try:
        graph_counts = _graph_counts(host, port, password_env)
        for case in PILOT_CASES:
            namespace = pilot_namespace(model, case)
            print(
                f"pilot v{PILOT_VERSION}: case={case.case_id} "
                f"namespace={namespace}",
                file=sys.stderr,
                flush=True,
            )
            existing_episodes = graph_counts.get(namespace)
            if existing_episodes not in (None, 1):
                raise PilotError(
                    f"pilot namespace {namespace} has {existing_episodes} episodes; "
                    "refusing automatic retry or cleanup"
                )
            if existing_episodes == 1:
                report = audit_namespace(
                    host=host,
                    port=port,
                    password_env=password_env,
                    namespace=namespace,
                    case=case,
                )
                report["ingestion"] = "existing"
                report["elapsed_seconds"] = 0
                reports.append(report)
                if not report["passed"]:
                    break
                continue

            graphiti.driver = graphiti.driver.with_database(namespace)
            graphiti.clients.driver = graphiti.driver
            await graphiti.build_indices_and_constraints()
            unit = units[case.unit_id]
            capture = _GraphitiWarningCapture()
            warning_logger.addHandler(capture)
            started = time.monotonic()
            ingestion_error: Exception | None = None
            try:
                async with asyncio.timeout(case_timeout_seconds):
                    with graphiti_policy.extracted_node_guard(
                        group_id=namespace,
                        allowed_entity_types=ENTITY_TYPES,
                    ):
                        result = await graphiti.add_episode(
                            name=(
                                f"pilot-v{PILOT_VERSION}:{case.case_id}:"
                                f"{unit['relative_path']}#chunk-{unit['chunk_index']}"
                            ),
                            episode_body=ingestion._graphiti_episode_body(unit),
                            source_description=(
                                f"workspace Graphiti pilot v{PILOT_VERSION}; "
                                f"catalog revision {unit['revision_id']}"
                            ),
                            reference_time=datetime.fromisoformat(unit["reference_time"]),
                            source=EpisodeType.text,
                            group_id=namespace,
                            entity_types=entity_types,
                            excluded_entity_types=["Entity"],
                            edge_types=edge_types,
                            edge_type_map=edge_type_map,
                            custom_extraction_instructions=PILOT_INSTRUCTIONS,
                        )
                ingestion._validate_graphiti_result_namespace(result, namespace)
            except Exception as exc:
                ingestion_error = exc
            finally:
                elapsed = time.monotonic() - started
                warning_logger.removeHandler(capture)
            report = audit_namespace(
                host=host,
                port=port,
                password_env=password_env,
                namespace=namespace,
                case=case,
                unresolved_warnings=capture.messages,
            )
            report["ingestion"] = (
                "failed" if ingestion_error is not None else "applied"
            )
            report["elapsed_seconds"] = round(elapsed, 3)
            if ingestion_error is not None:
                if isinstance(ingestion_error, TimeoutError):
                    error_message = (
                        f"case exceeded {case_timeout_seconds:g}-second deadline"
                    )
                else:
                    error_message = str(ingestion_error) or repr(ingestion_error)
                report["error"] = {
                    "type": type(ingestion_error).__name__,
                    "message": error_message,
                }
                if isinstance(ingestion_error, graphiti_policy.GraphitiPolicyError):
                    report["pre_persistence_policy"] = ingestion_error.as_dict()
                report["passed"] = False
                report.setdefault("violations", []).append(
                    f"ingestion failed: {error_message}"
                )
            reports.append(report)
            print(
                f"pilot v{PILOT_VERSION}: case={case.case_id} "
                f"ingestion={report['ingestion']} passed={report['passed']} "
                f"elapsed_seconds={report['elapsed_seconds']}",
                file=sys.stderr,
                flush=True,
            )
            if not report["passed"]:
                break
        return reports
    finally:
        await graphiti.close()
        await raw_llm_client.close()


def _report_path(report_root: Path, model: str) -> Path:
    return (
        report_root.expanduser().resolve()
        / _slug(model)
        / f"pilot-v{PILOT_VERSION}-report.json"
    )


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_pilot(
    *,
    outbox: Path,
    report_root: Path,
    model: str,
    host: str,
    port: int,
    password_env: str,
    llm_base_url: str,
    embedding_base_url: str,
    embedding_model: str,
    embedding_dim: int,
    api_key_env: str,
    reasoning_effort: str,
    request_timeout_seconds: float,
    case_timeout_seconds: float,
    apply: bool,
) -> dict[str, Any]:
    plan = pilot_plan(
        outbox=outbox,
        model=model,
        host=host,
        port=port,
        password_env=password_env,
    )
    if not apply:
        return plan
    report_path = _report_path(report_root, model)
    if report_path.exists():
        raise PilotError(
            f"pilot report already exists; refusing to replace it: {report_path}"
        )
    readiness = ingestion.graphiti_readiness(
        host=host,
        port=port,
        password_env=password_env,
        llm_base_url=llm_base_url,
        llm_model=model,
        embedding_base_url=embedding_base_url,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        api_key_env=api_key_env,
        probe_models=True,
        timeout_seconds=180,
    )
    if not readiness["ready"]:
        raise PilotError("Graphiti readiness gates did not pass")
    _, units = _load_case_units(outbox)
    try:
        from graphiti_core.driver.falkordb_driver import FalkorDriver
    except ImportError as exc:
        raise PilotError("Graphiti dependencies are required for apply") from exc
    # Construct outside the event loop so FalkorDriver does not schedule its
    # own untracked background index task.
    base_driver = FalkorDriver(
        host=host,
        port=port,
        password=os.environ.get(password_env),
        database=DEFAULT_CONTROLLER_GRAPH,
    )
    case_reports = asyncio.run(
        _run_cases_async(
            base_driver=base_driver,
            units=units,
            model=model,
            llm_base_url=llm_base_url,
            embedding_base_url=embedding_base_url,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            api_key_env=api_key_env,
            host=host,
            port=port,
            password_env=password_env,
            reasoning_effort=reasoning_effort,
            request_timeout_seconds=request_timeout_seconds,
            case_timeout_seconds=case_timeout_seconds,
        )
    )
    all_cases_ran = len(case_reports) == len(PILOT_CASES)
    passed = all_cases_ran and all(report["passed"] for report in case_reports)
    manifest = ingestion.load_outbox_manifest(outbox)
    result = {
        "schema_version": PILOT_SCHEMA_VERSION,
        "mode": "applied",
        "pilot_version": PILOT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "request_timeout_seconds": request_timeout_seconds,
        "case_timeout_seconds": case_timeout_seconds,
        "model_approved": passed,
        "bulk_unlock": "disabled",
        "outbox": str(outbox.expanduser().resolve()),
        "outbox_sha256": manifest["units_sha256"],
        "readiness": readiness,
        "cases_completed": len(case_reports),
        "cases_required": len(PILOT_CASES),
        "cases": case_reports,
        "report": str(report_path),
        "point_deletion": "disabled",
        "graph_deletion": "disabled",
        "source_mutation": "disabled",
    }
    _write_report(report_path, result)
    return result


def audit_model(
    *,
    model: str,
    host: str,
    port: int,
    password_env: str,
) -> dict[str, Any]:
    reports = [
        audit_namespace(
            host=host,
            port=port,
            password_env=password_env,
            namespace=pilot_namespace(model, case),
            case=case,
        )
        for case in PILOT_CASES
    ]
    return {
        "schema_version": PILOT_SCHEMA_VERSION,
        "mode": "audit",
        "pilot_version": PILOT_VERSION,
        "model": model,
        "model_approved": all(report["passed"] for report in reports),
        "cases": reports,
        "graph_deletion": "disabled",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run", "audit"):
        command = commands.add_parser(name)
        command.add_argument("--model", required=True)
        command.add_argument("--host", default=DEFAULT_HOST)
        command.add_argument("--port", type=int, default=DEFAULT_PORT)
        command.add_argument("--password-env", default="FALKORDB_PASSWORD")
        if name in {"plan", "run"}:
            command.add_argument("--outbox", default=str(DEFAULT_OUTBOX))
        if name == "run":
            command.add_argument("--report-root", default=str(DEFAULT_REPORT_ROOT))
            command.add_argument("--llm-base-url", default=DEFAULT_LLM_BASE_URL)
            command.add_argument(
                "--embedding-base-url",
                default=DEFAULT_EMBEDDING_BASE_URL,
            )
            command.add_argument(
                "--embedding-model",
                default=DEFAULT_EMBEDDING_MODEL,
            )
            command.add_argument(
                "--embedding-dim",
                type=int,
                default=DEFAULT_EMBEDDING_DIM,
            )
            command.add_argument(
                "--api-key-env",
                default="GRAPHITI_LLM_API_KEY",
            )
            command.add_argument(
                "--reasoning-effort",
                choices=("none", "minimal", "low", "medium", "high"),
                default=DEFAULT_REASONING_EFFORT,
            )
            command.add_argument(
                "--request-timeout-seconds",
                type=float,
                default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
            command.add_argument(
                "--case-timeout-seconds",
                type=float,
                default=DEFAULT_CASE_TIMEOUT_SECONDS,
            )
            command.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        result = pilot_plan(
            outbox=Path(args.outbox),
            model=args.model,
            host=args.host,
            port=args.port,
            password_env=args.password_env,
        )
    elif args.command == "run":
        if args.embedding_dim < 1:
            raise PilotError("embedding dimension must be positive")
        if args.request_timeout_seconds <= 0:
            raise PilotError("request timeout must be positive")
        if args.case_timeout_seconds <= 0:
            raise PilotError("case timeout must be positive")
        result = run_pilot(
            outbox=Path(args.outbox),
            report_root=Path(args.report_root),
            model=args.model,
            host=args.host,
            port=args.port,
            password_env=args.password_env,
            llm_base_url=args.llm_base_url,
            embedding_base_url=args.embedding_base_url,
            embedding_model=args.embedding_model,
            embedding_dim=args.embedding_dim,
            api_key_env=args.api_key_env,
            reasoning_effort=args.reasoning_effort,
            request_timeout_seconds=args.request_timeout_seconds,
            case_timeout_seconds=args.case_timeout_seconds,
            apply=args.apply,
        )
    else:
        result = audit_model(
            model=args.model,
            host=args.host,
            port=args.port,
            password_env=args.password_env,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("model_approved", True) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PilotError, ingestion.IngestionError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)

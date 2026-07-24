#!/usr/bin/env python3
"""Pre-persistence policy enforcement for workspace Graphiti extraction.

Graphiti's custom entity and edge types guide an LLM but do not constrain its
output.  This module validates the exact extracted objects used by
``Graphiti.add_episode`` before any episode, entity, or fact is persisted.

The optional Graphiti dependency is imported only when the guarded adapter is
constructed so the pure policy functions remain testable with the standard
workspace Python.
"""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence


POLICY_SCHEMA_VERSION = 1
DEFAULT_ENTITY_TYPES = ("Actor", "Component", "Decision", "Requirement", "WorkItem")
DEFAULT_EDGE_TYPES = (
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
FILE_EXTENSION = re.compile(
    r"\.(?:go|js|jsx|ts|tsx|py|sh|md|json|ya?ml|toml|tf|cue|sql|log)$",
    re.IGNORECASE,
)
ENVIRONMENT_KEY = re.compile(r"^[A-Z][A-Z0-9]*_[A-Z0-9_]+$")
LOWER_CAMEL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+$")
CALLABLE_IDENTIFIER = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\([^)]*\)$"
)
REVISION_HASH = re.compile(r"^(?:revision:)?[0-9a-f]{7,64}$", re.IGNORECASE)
GENERIC_EDGES = {"RELATED_TO", "RELATES_TO"}
_PATCH_LOCK = threading.Lock()


class GraphitiPolicyError(ValueError):
    """An extracted Graphiti episode violates the pre-persistence contract."""

    def __init__(self, stage: str, violations: Sequence[dict[str, Any]]):
        self.stage = stage
        self.violations = list(violations)
        summary = "; ".join(
            f"{item['code']}:{item.get('value', '')}" for item in self.violations[:8]
        )
        super().__init__(
            f"Graphiti pre-persistence {stage} policy rejected "
            f"{len(self.violations)} item(s): {summary}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "stage": self.stage,
            "violations": self.violations,
            "persistence": "blocked",
        }


def graphiti_ontology() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[tuple[str, str], list[str]],
]:
    """Build the constrained entity/edge ontology used by guarded ingestion."""
    try:
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise GraphitiPolicyError(
            "configuration",
            [{"code": "pydantic-unavailable", "value": str(exc)}],
        ) from exc

    class Decision(BaseModel):
        """An explicit accepted, rejected, or superseded project choice."""

        verdict: str | None = Field(None, description="Explicit decision verdict")
        status: str | None = Field(None, description="Explicit lifecycle status")
        rationale: str | None = Field(None, description="Stated rationale")

    class WorkItem(BaseModel):
        """A named task, milestone, gate, or deliverable."""

        kind: str | None = Field(None, description="Task, milestone, gate, or deliverable")
        status: str | None = Field(None, description="Explicit work status")
        deadline: str | None = Field(None, description="Explicit deadline")

    class Component(BaseModel):
        """A real system, service, repository, chart, environment, or process."""

        component_kind: str | None = Field(None, description="Component category")

    class Actor(BaseModel):
        """A person, team, or organization explicitly responsible for work."""

        actor_kind: str | None = Field(None, description="Person, team, or organization")

    class Requirement(BaseModel):
        """An explicit constraint, acceptance condition, or prerequisite."""

        priority: str | None = Field(None, description="Explicit priority")
        status: str | None = Field(None, description="Explicit requirement state")

    class AppliesTo(BaseModel):
        """The source explicitly applies to the target."""

    class Blocks(BaseModel):
        """The source explicitly prevents the target from proceeding."""

    class Changes(BaseModel):
        """The source explicitly changes the target."""

    class DependsOn(BaseModel):
        """The source explicitly depends on the target."""

    class Implements(BaseModel):
        """The source explicitly implements the target."""

    class OwnedBy(BaseModel):
        """The source is explicitly owned by the target actor."""

    class Owns(BaseModel):
        """The source actor explicitly owns the target."""

    class Requires(BaseModel):
        """The source explicitly requires the target."""

    class Satisfies(BaseModel):
        """The source explicitly satisfies the target requirement."""

    class Supersedes(BaseModel):
        """The source explicitly replaces the target."""

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
    expected = {
        (source, target)
        for source in entity_types
        for target in entity_types
    }
    if set(edge_type_map) != expected:
        raise GraphitiPolicyError(
            "configuration",
            [{"code": "incomplete-edge-map", "value": len(edge_type_map)}],
        )
    return entity_types, edge_types, edge_type_map


def forbidden_entity_reason(name: str) -> str | None:
    """Return the deterministic rejection reason for an incidental entity."""
    stripped = name.strip().strip("`")
    if not stripped:
        return "empty-name"
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
    if REVISION_HASH.fullmatch(stripped):
        return "revision-hash"
    return None


def validate_extracted_nodes(
    nodes: Sequence[Any],
    *,
    group_id: str,
    allowed_entity_types: Sequence[str] = DEFAULT_ENTITY_TYPES,
    minimum: int = 2,
    maximum: int = 18,
) -> None:
    """Reject invalid extracted nodes before Graphiti resolves or saves them."""
    violations: list[dict[str, Any]] = []
    allowed = set(allowed_entity_types)
    if not minimum <= len(nodes) <= maximum:
        violations.append(
            {
                "code": "entity-count",
                "value": len(nodes),
                "expected": f"{minimum}..{maximum}",
            }
        )
    seen: set[str] = set()
    for node in nodes:
        name = str(getattr(node, "name", "")).strip()
        labels = {str(value) for value in getattr(node, "labels", ())}
        node_group = str(getattr(node, "group_id", ""))
        if name.casefold() in seen:
            violations.append({"code": "duplicate-entity-name", "value": name})
        seen.add(name.casefold())
        if reason := forbidden_entity_reason(name):
            violations.append(
                {"code": "incidental-entity", "value": name, "reason": reason}
            )
        typed = labels & allowed
        if len(typed) != 1:
            violations.append(
                {
                    "code": "entity-type",
                    "value": name,
                    "labels": sorted(labels),
                    "allowed": sorted(allowed),
                }
            )
        unexpected = labels - allowed - {"Entity"}
        if unexpected:
            violations.append(
                {
                    "code": "unexpected-entity-label",
                    "value": name,
                    "labels": sorted(unexpected),
                }
            )
        if node_group != group_id:
            violations.append(
                {
                    "code": "entity-group-mismatch",
                    "value": name,
                    "observed": node_group,
                    "expected": group_id,
                }
            )
    if violations:
        raise GraphitiPolicyError("entity", violations)


def validate_extracted_edges(
    edges: Sequence[Any],
    *,
    group_id: str,
    allowed_edge_types: Sequence[str] = DEFAULT_EDGE_TYPES,
    minimum: int = 1,
    maximum: int = 16,
) -> None:
    """Reject invalid extracted facts before Graphiti resolves or saves them."""
    violations: list[dict[str, Any]] = []
    allowed = set(allowed_edge_types)
    if not minimum <= len(edges) <= maximum:
        violations.append(
            {
                "code": "fact-count",
                "value": len(edges),
                "expected": f"{minimum}..{maximum}",
            }
        )
    for edge in edges:
        name = str(getattr(edge, "name", "")).strip()
        fact = str(getattr(edge, "fact", "")).strip()
        edge_group = str(getattr(edge, "group_id", ""))
        episodes = list(getattr(edge, "episodes", ()) or ())
        if name.upper() in GENERIC_EDGES:
            violations.append({"code": "generic-edge-type", "value": name})
        elif name not in allowed:
            violations.append(
                {
                    "code": "unexpected-edge-type",
                    "value": name,
                    "allowed": sorted(allowed),
                }
            )
        if not fact:
            violations.append({"code": "empty-fact", "value": name})
        if not episodes:
            violations.append({"code": "missing-provenance", "value": fact[:160]})
        if getattr(edge, "valid_at", None) is None:
            violations.append({"code": "missing-valid-at", "value": fact[:160]})
        if edge_group != group_id:
            violations.append(
                {
                    "code": "fact-group-mismatch",
                    "value": fact[:160],
                    "observed": edge_group,
                    "expected": group_id,
                }
            )
    if violations:
        raise GraphitiPolicyError("fact", violations)


def guarded_graphiti_class(
    base_class: type[Any],
    *,
    allowed_edge_types: Sequence[str] = DEFAULT_EDGE_TYPES,
    minimum_edges: int = 1,
    maximum_edges: int = 16,
) -> type[Any]:
    """Return a Graphiti subclass that validates the exact extracted edges."""

    class GuardedGraphiti(base_class):
        async def _extract_and_resolve_edges(
            self,
            episode: Any,
            extracted_nodes: list[Any],
            previous_episodes: list[Any],
            edge_type_map: dict[tuple[str, str], list[str]],
            group_id: str,
            edge_types: dict[str, Any] | None,
            nodes: list[Any],
            uuid_map: dict[str, str],
            custom_extraction_instructions: str | None = None,
        ) -> tuple[list[Any], list[Any], list[Any]]:
            from graphiti_core.utils.maintenance.edge_operations import (
                extract_edges,
                resolve_extracted_edges,
            )
            from graphiti_core.utils.bulk_utils import resolve_edge_pointers

            episodes = episode if isinstance(episode, list) else [episode]
            primary_episode = episodes[0]
            extracted_edges = await extract_edges(
                self.clients,
                episode,
                extracted_nodes,
                previous_episodes,
                edge_type_map,
                group_id,
                edge_types,
                custom_extraction_instructions,
            )
            validate_extracted_edges(
                extracted_edges,
                group_id=group_id,
                allowed_edge_types=allowed_edge_types,
                minimum=minimum_edges,
                maximum=maximum_edges,
            )
            edges = resolve_edge_pointers(extracted_edges, uuid_map)
            return await resolve_extracted_edges(
                self.clients,
                edges,
                primary_episode,
                nodes,
                edge_types or {},
                edge_type_map,
            )

    GuardedGraphiti.__name__ = f"PolicyGuarded{base_class.__name__}"
    return GuardedGraphiti


@contextmanager
def extracted_node_guard(
    *,
    group_id: str,
    allowed_entity_types: Sequence[str] = DEFAULT_ENTITY_TYPES,
    minimum_nodes: int = 2,
    maximum_nodes: int = 18,
) -> Iterator[None]:
    """Guard Graphiti's module-level extractor for one sequential add call.

    Graphiti imports ``extract_nodes`` into its main module, so wrapping that
    exact binding is the only way to inspect the objects that ``add_episode``
    will resolve. The process-wide lock forbids concurrent guarded calls.
    """
    if not _PATCH_LOCK.acquire(blocking=False):
        raise GraphitiPolicyError(
            "configuration",
            [{"code": "concurrent-policy-guard", "value": group_id}],
        )
    import graphiti_core.graphiti as graphiti_module

    original = graphiti_module.extract_nodes

    async def guarded_extract_nodes(*args: Any, **kwargs: Any) -> Any:
        nodes, index_map = await original(*args, **kwargs)
        validate_extracted_nodes(
            nodes,
            group_id=group_id,
            allowed_entity_types=allowed_entity_types,
            minimum=minimum_nodes,
            maximum=maximum_nodes,
        )
        return nodes, index_map

    graphiti_module.extract_nodes = guarded_extract_nodes
    try:
        yield
    finally:
        if graphiti_module.extract_nodes is guarded_extract_nodes:
            graphiti_module.extract_nodes = original
        _PATCH_LOCK.release()

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import graphiti_policy as policy  # noqa: E402


@dataclass
class Node:
    name: str
    labels: list[str]
    group_id: str = "pilot"


@dataclass
class Edge:
    name: str
    fact: str
    group_id: str = "pilot"
    episodes: list[str] = field(default_factory=lambda: ["episode-1"])
    valid_at: str | None = "2026-07-17T00:00:00Z"


class GraphitiPolicyTests(unittest.TestCase):
    def test_accepts_typed_domain_entities_and_temporal_fact(self) -> None:
        policy.validate_extracted_nodes(
            [
                Node("workspace artifact pipeline", ["Entity", "Component"]),
                Node("Qdrant ingestion milestone", ["Entity", "WorkItem"]),
            ],
            group_id="pilot",
        )
        policy.validate_extracted_edges(
            [
                Edge(
                    "Implements",
                    "The artifact pipeline implements the ingestion milestone.",
                )
            ],
            group_id="pilot",
        )

    def test_rejects_paths_code_symbols_and_untyped_entities(self) -> None:
        with self.assertRaises(policy.GraphitiPolicyError) as context:
            policy.validate_extracted_nodes(
                [
                    Node("scripts/ingest.py", ["Entity", "Component"]),
                    Node("writescope.Check()", ["Entity", "Component"]),
                    Node("Unclassified", ["Entity"]),
                ],
                group_id="pilot",
            )
        codes = {item["code"] for item in context.exception.violations}
        self.assertIn("incidental-entity", codes)
        self.assertIn("entity-type", codes)

    def test_rejects_unapproved_or_generic_relations_and_missing_provenance(self) -> None:
        with self.assertRaises(policy.GraphitiPolicyError) as context:
            policy.validate_extracted_edges(
                [
                    Edge("RELATED_TO", "generic"),
                    Edge("Extends", "unsupported", episodes=[], valid_at=None),
                ],
                group_id="pilot",
            )
        codes = {item["code"] for item in context.exception.violations}
        self.assertIn("generic-edge-type", codes)
        self.assertIn("unexpected-edge-type", codes)
        self.assertIn("missing-provenance", codes)
        self.assertIn("missing-valid-at", codes)

    def test_rejects_namespace_escape(self) -> None:
        with self.assertRaises(policy.GraphitiPolicyError):
            policy.validate_extracted_nodes(
                [
                    Node("Artifact platform", ["Entity", "Component"]),
                    Node("Search milestone", ["Entity", "WorkItem"], group_id="other"),
                ],
                group_id="pilot",
            )


if __name__ == "__main__":
    unittest.main()

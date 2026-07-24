from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import graphiti_pilot as pilot  # noqa: E402


class GraphitiPilotTests(unittest.TestCase):
    def test_namespace_is_stable_and_model_specific(self) -> None:
        case = pilot.PILOT_CASES[0]

        first = pilot.pilot_namespace("qwen3:14b", case)
        repeated = pilot.pilot_namespace("qwen3:14b", case)
        other_model = pilot.pilot_namespace("qwen3:8b", case)

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other_model)
        self.assertRegex(
            first,
            r"^graphiti_pilot_v3_qwen3_14b_decision_[0-9a-f]{10}$",
        )

    def test_openai_proxy_injects_reasoning_effort(self) -> None:
        class FakeCompletions:
            def __init__(self) -> None:
                self.kwargs: dict[str, object] = {}

            async def create(self, **kwargs: object) -> str:
                self.kwargs = kwargs
                return "ok"

        completions = FakeCompletions()
        proxy = pilot._CompletionsWithDefaults(completions, "none")

        result = asyncio.run(proxy.create(model="qwen3:14b"))

        self.assertEqual(result, "ok")
        self.assertEqual(completions.kwargs["reasoning_effort"], "none")

    def test_openai_proxy_preserves_explicit_reasoning_effort(self) -> None:
        class FakeCompletions:
            async def create(self, **kwargs: object) -> dict[str, object]:
                return kwargs

        proxy = pilot._CompletionsWithDefaults(FakeCompletions(), "none")

        result = asyncio.run(proxy.create(reasoning_effort="low"))

        self.assertEqual(result["reasoning_effort"], "low")

    def test_forbidden_entity_detection_is_conservative(self) -> None:
        self.assertEqual(pilot._forbidden_reason("src/config.yaml"), "path-like")
        self.assertEqual(pilot._forbidden_reason("CONFIG_API_KEY"), "environment-key")
        self.assertEqual(
            pilot._forbidden_reason("serviceAccountsEnabled"),
            "code-identifier",
        )
        self.assertEqual(
            pilot._forbidden_reason("writescope.Check()"),
            "code-symbol",
        )
        self.assertEqual(pilot._forbidden_reason("main.go"), "file-extension")
        self.assertIsNone(pilot._forbidden_reason("AWS Secrets Manager"))
        self.assertIsNone(pilot._forbidden_reason("agents-dispatcher"))

    def test_audit_uses_semantic_edge_name_not_storage_type(self) -> None:
        case = pilot.PILOT_CASES[0]
        namespace = pilot.pilot_namespace("qwen3:14b", case)
        entities = [
            {
                "name": "ConfirmationPolicy",
                "labels": ["Entity", "Component"],
                "group_id": namespace,
            },
            {
                "name": "pure-Go",
                "labels": ["Entity", "Decision"],
                "group_id": namespace,
            },
            {
                "name": "cel-go",
                "labels": ["Entity", "Component"],
                "group_id": namespace,
            },
        ]
        facts = [
            {
                "type": "AppliesTo",
                "fact": "The pure-Go decision applies to ConfirmationPolicy.",
                "group_id": namespace,
                "episodes": ["episode-1"],
                "valid_at": "2026-06-02T00:00:00Z",
                "invalid_at": None,
            }
        ]

        result = pilot._audit_payload(
            case=case,
            namespace=namespace,
            entities=entities,
            facts=facts,
            episode_count=1,
            unresolved_warnings=(),
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["metrics"]["generic_relates_to"], 0)
        self.assertEqual(result["metrics"]["required_term_matches"], 3)

    def test_audit_fails_closed_on_incidental_and_generic_output(self) -> None:
        case = pilot.PILOT_CASES[0]
        namespace = pilot.pilot_namespace("qwen3:14b", case)
        entities = [
            {
                "name": "config.yaml",
                "labels": ["Entity"],
                "group_id": namespace,
            },
            {
                "name": "ConfirmationPolicy",
                "labels": ["Entity", "Component"],
                "group_id": namespace,
            },
        ]
        facts = [
            {
                "type": "RELATED_TO",
                "fact": "config.yaml is related to ConfirmationPolicy.",
                "group_id": namespace,
                "episodes": [],
                "valid_at": None,
                "invalid_at": None,
            }
        ]

        result = pilot._audit_payload(
            case=case,
            namespace=namespace,
            entities=entities,
            facts=facts,
            episode_count=1,
            unresolved_warnings=["Target entity not found"],
        )

        self.assertFalse(result["passed"])
        self.assertGreaterEqual(len(result["violations"]), 5)


if __name__ == "__main__":
    unittest.main()

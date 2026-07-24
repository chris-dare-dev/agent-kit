import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "portfolio_projection.py"
SPEC = importlib.util.spec_from_file_location("portfolio_projection", MODULE_PATH)
projection = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(projection)
RENDERER_PATH = MODULE_PATH.parent / "roadmap_status_excalidraw.py"
RENDERER_SPEC = importlib.util.spec_from_file_location("roadmap_status_excalidraw", RENDERER_PATH)
renderer = importlib.util.module_from_spec(RENDERER_SPEC)
assert RENDERER_SPEC and RENDERER_SPEC.loader
RENDERER_SPEC.loader.exec_module(renderer)


def record(**overrides):
    value = {
        "record_id": "project-roadmap-m1",
        "kind": "milestone",
        "record_role": "unresolved",
        "canonical_id": None,
        "disposition": "unknown",
        "project_id": "project",
        "project_name": "Project",
        "owner_resolution": "manifest-source-match",
        "owner_candidates": ["project"],
        "roadmap_id": "roadmap",
        "title": "Milestone",
        "source_subject": "Milestone",
        "tracking_status": "complete",
        "pipeline_phase": "complete",
        "implementation_status": "unknown",
        "operational_status": "unknown",
        "required_targets": [],
        "depends_on": [],
        "source_mode": "roadmap-register-v1",
        "source_path": "plans/roadmap.md",
        "source_exists": True,
        "source_alias": None,
        "source_uri": None,
        "register_path": "repo/.claude/notes/roadmaps/roadmap/milestones.json",
        "state_path": None,
        "updated_at": None,
        "external_writes_declared": 1,
        "external_writes_completed": 1,
        "attention": "tracking-complete-delivery-unknown",
        "data_quality": ["delivery-state-not-modeled"],
    }
    value.update(overrides)
    return value


class PortfolioProjectionTests(unittest.TestCase):
    def test_v1_complete_does_not_imply_delivery(self):
        value = record()
        self.assertEqual(projection.attention_for(value), "tracking-complete-delivery-unknown")
        self.assertEqual(value["implementation_status"], "unknown")
        self.assertEqual(value["operational_status"], "unknown")

    def test_applied_is_not_verified(self):
        value = record(implementation_status="published", operational_status="applied")
        self.assertEqual(projection.attention_for(value), "applied-not-verified")

    def test_alias_requires_canonical_id(self):
        value = record(record_role="alias", canonical_id=None)
        with self.assertRaisesRegex(projection.ProjectionError, "has no canonical_id"):
            projection.validate_records([value])

    def test_tracking_status_normalization_is_bounded(self):
        self.assertEqual(projection.normalize_tracking("done"), "complete")
        self.assertEqual(projection.normalize_tracking("WIP"), "in_progress")
        self.assertEqual(projection.normalize_tracking("deployed"), "unknown")

    def test_global_snapshot_change_does_not_churn_record_note(self):
        summary = {
            "total_records": 1,
            "by_attention": {},
            "by_data_quality": {},
            "by_owner_resolution": {},
        }
        first = {
            "source_snapshot_sha256": "a" * 64,
            "presentation_vault": {"name": "Test Vault"},
            "summary": summary,
            "records": [record()],
        }
        second = {**first, "source_snapshot_sha256": "b" * 64}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_outputs = projection.expected_outputs(first, root)
            second_outputs = projection.expected_outputs(second, root)
        note = root / "Notes" / "Portfolio" / "Milestones" / "project-roadmap-m1.md"
        self.assertEqual(first_outputs[note], second_outputs[note])

    def test_renderer_keeps_semantic_title_and_truncates_only_drawing(self):
        long_title = "A complete milestone title that is intentionally longer than forty characters"
        with tempfile.TemporaryDirectory() as tmp:
            roadmap = Path(tmp) / "project-roadmap.md"
            roadmap.write_text(f"## M1: {long_title}\n\n- [ ] pending\n")
            parsed = renderer.parse_roadmap(roadmap)
        self.assertEqual(parsed["items"][0]["title"], long_title)
        self.assertEqual(renderer._display_title(long_title)[-1], "…")
        self.assertEqual(len(renderer._display_title(long_title)), 40)

    def test_renderer_preserves_code_nouns_and_splits_planning_metadata(self):
        source = "Render `platform-admin` view — milestone ID `roadmap-m1`. Depends on M0."
        cleaned = renderer._strip_md(source)
        self.assertIn("platform-admin", cleaned)
        self.assertEqual(renderer._semantic_title(cleaned), "Render platform-admin view")
        closed = "(CLOSED - ): Reachability of policy reporter. Verdict YES — decision.md"
        self.assertEqual(renderer._semantic_title(closed), "Reachability of policy reporter")


if __name__ == "__main__":
    unittest.main()

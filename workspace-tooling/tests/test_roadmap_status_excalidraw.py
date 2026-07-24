from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "roadmap_status_excalidraw.py"
SPEC = importlib.util.spec_from_file_location("roadmap_status_excalidraw", MODULE_PATH)
renderer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(renderer)


class RoadmapStatusExcalidrawTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "plans").mkdir()
        self.manifest = {
            "vault_root": str(self.root),
            "projects_root": "Notes/Projects",
            "regions": {"plans": "plans"},
            "presentation_vault": {
                "name": "Presentation",
                "root": str(self.root / "Presentation"),
                "projects_root": "Notes/Projects",
                "source_alias_dir": "_sources",
            },
        }
        self.cfg = {"project_id": "alpha", "slugs": ["alpha"], "contains": []}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_missing_sources_preserve_existing_board_by_default(self) -> None:
        board = (
            self.root
            / "Notes/Projects/Alpha/Alpha-roadmaps.excalidraw.md"
        )
        board.parent.mkdir(parents=True)
        board.write_text("preserve\n", encoding="utf-8")

        path, done, total = renderer.build_for_project(
            self.manifest, "Alpha", self.cfg
        )

        self.assertEqual(Path(path), board)
        self.assertEqual((done, total), (0, 0))
        self.assertEqual(board.read_text(encoding="utf-8"), "preserve\n")

        renderer.build_for_project(
            self.manifest, "Alpha", self.cfg, allow_delete=True
        )
        self.assertFalse(board.exists())

    def write_roadmap(self, reviewer: str = "gpt-5.6-sol-ultra") -> Path:
        path = self.root / "plans" / "alpha-roadmap.md"
        path.write_text(
            f"""---
status: active
---
## Now
### E1: Foundation
#### M1: Build a sufficiently descriptive milestone — `alpha-m1`
- [/] in progress
#### SP1: Prove the difficult assumption — `alpha-spike-1`
- [ ] pending

### Review checkpoints
<!-- Template populated by the handoff workflow:
     - [ ] (optional) session audit <date> — covers `<slug>-mN` · handoff: `plans/<file>` · reviewer: <target> -->
- [ ] (optional) session audit 2026-07-12 — covers alpha-m1 · handoff: `plans/HANDOFF-2026-07-12-alpha-session-review.md` · reviewer: {reviewer}

## Appendix
- [ ] this is not a review checkpoint
""",
            encoding="utf-8",
        )
        return path

    def write_handoff(self) -> Path:
        path = self.root / "plans" / "HANDOFF-2026-07-12-alpha-session-review.md"
        path.write_text(
            """---
authorship: agent-generated
type: handoff
handoff_kind: review
project: alpha
date: 2026-07-12
status: complete
companion: HANDOFF-2026-07-12-alpha-continuation.md
roadmap: plans/alpha-roadmap.md
reviewer_target: gpt-5.6-sol-ultra
review_status: requested
milestones_covered:
  - alpha-m1
tags:
  - type/handoff
  - project/alpha
  - handoff/review
  - review/requested
  - authorship/agent-generated
---
""",
            encoding="utf-8",
        )
        return path

    def parsed(self, reviewer: str = "gpt-5.6-sol-ultra"):
        roadmap_path = self.write_roadmap(reviewer)
        self.write_handoff()
        roadmap = renderer.parse_roadmap(roadmap_path)
        roadmap["checkpoints"] = renderer.enrich_checkpoints(
            self.manifest, roadmap["checkpoints"], roadmap_path, roadmap["items"]
        )
        return roadmap_path, roadmap

    def test_parser_preserves_type_identity_epic_and_lane(self) -> None:
        _path, roadmap = self.parsed()
        milestone, spike = roadmap["items"]
        self.assertEqual(milestone["canonical_id"], "alpha-m1")
        self.assertEqual(milestone["epic_id"], "E1")
        self.assertEqual(milestone["lane"], "now")
        self.assertEqual(milestone["kind"], "milestone")
        self.assertEqual(spike["kind"], "spike")
        self.assertEqual(len(roadmap["checkpoints"]), 1)

    def test_commented_checkpoint_template_is_ignored(self) -> None:
        lines = [
            "### Review checkpoints",
            "<!-- Template populated by the handoff workflow:",
            "     - [ ] (optional) session audit <date> — covers `<slug>-mN` "
            "· handoff: `plans/<file>` · reviewer: <target> -->",
        ]
        self.assertEqual(renderer.parse_checkpoints(lines), [])

    def test_handoff_frontmatter_is_authoritative_and_drift_is_visible(self) -> None:
        _path, clean = self.parsed()
        checkpoint = clean["checkpoints"][0]
        self.assertEqual(checkpoint["review_status"], "requested")
        self.assertEqual(checkpoint["display_status"], "requested")
        self.assertEqual(checkpoint["resolved_item_ids"], ["M1"])

        _path, drifted = self.parsed(reviewer="fable-high-effort")
        checkpoint = drifted["checkpoints"][0]
        self.assertEqual(checkpoint["display_status"], "drift")
        self.assertIn("reviewer", checkpoint["mismatches"])
        self.assertEqual(renderer.roadmap_lane(drifted), "focus")

    def test_board_is_fixed_width_system_font_and_links_review_handoff(self) -> None:
        roadmap_path, roadmap = self.parsed()
        text, _done, _total, sig = renderer.build_board(
            self.manifest, "Alpha", self.cfg, [(str(roadmap_path), roadmap)]
        )
        payload = re.search(r"## Drawing\n```json\n(.*?)\n```", text, re.S)
        self.assertIsNotNone(payload)
        scene = json.loads(payload.group(1))
        self.assertLessEqual(max(e["x"] + e.get("width", 0) for e in scene["elements"]), 1392)
        self.assertEqual({e["fontFamily"] for e in scene["elements"] if e["type"] == "text"}, {2})
        rectangles = [e for e in scene["elements"] if e["type"] == "rectangle"]
        self.assertTrue(all(e["roundness"] is None for e in rectangles))
        by_id = {e["id"]: e for e in scene["elements"]}
        for rail in (e for e in rectangles if e["id"].endswith("-rail")):
            card = by_id[rail["id"][:-5]]
            content = by_id[card["id"] + "-content"]
            label = next(e for e in scene["elements"] if e.get("containerId") == content["id"])
            self.assertEqual(content["x"] - card["x"], 24)
            self.assertGreaterEqual(label["x"] - card["x"], 32)
            self.assertEqual((rail["y"], rail["height"]), (card["y"], card["height"]))
        output = self.root / "board.excalidraw.md"
        output.write_text(text, encoding="utf-8")
        self.assertTrue(renderer.scene_fingerprint_present(output, self.cfg, sig))
        output.write_text(text.replace(f"scene_sig={sig}", "scene_sig=stale"),
                          encoding="utf-8")
        self.assertFalse(renderer.scene_fingerprint_present(output, self.cfg, sig))
        links = [urllib.parse.unquote(e["link"]) for e in scene["elements"] if e.get("link")]
        self.assertTrue(any("HANDOFF-2026-07-12-alpha-session-review.md" in link for link in links))
        surfaces = {e.get("backgroundColor") for e in scene["elements"] if e["type"] == "rectangle"}
        self.assertIn(renderer.C_MILESTONE[0], surfaces)
        self.assertIn(renderer.C_SPIKE[0], surfaces)

    def test_short_label_does_not_collapse_distinct_root_roadmaps(self) -> None:
        first = renderer.short_label(Path("/tmp/service-registry-roadmap.md"),
                                     {"slugs": ["service-registry"]})
        second = renderer.short_label(Path("/tmp/sre-catalog-deploy-roadmap.md"),
                                      {"slugs": ["sre-catalog-deploy"]})
        self.assertEqual(first, "service-registry")
        self.assertEqual(second, "sre-catalog-deploy")


if __name__ == "__main__":
    unittest.main()

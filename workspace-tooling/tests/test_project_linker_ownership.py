"""Single-owner projection contract (F-11).

A handoff filename carries a project slug, and cross-cutting work legitimately names two. Without
arbitration each claimant materialized its own `_sources` symlink, so two vault paths resolved to
one canonical file — the vault validator's DUPLICATE_MARKDOWN_ALIAS class. These tests pin the
ownership rule and the no-delete posture so the class cannot regress at projection time.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location(
    "project_linker", SCRIPT_DIR / "project-linker.py"
)
assert spec and spec.loader
linker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(linker)


def manifest(root: Path, projects: dict) -> dict:
    return {
        "vault_root": str(root),
        "projects_root": "Notes/Projects",
        "regions": {"plans": "plans"},
        "presentation_vault": {
            "name": "Vault",
            "root": str(root / "Vault"),
            "projects_root": "Notes/Projects",
            "source_alias_dir": "_sources",
        },
        "projects": projects,
    }


# Mirrors the live collision: Service Registry claims `svcreg`, Kargo claims `kargo`, and a
# handoff named `…-svcreg-kargo-prod-…` carries both.
TWO_CLAIMANTS = {
    "Service Registry": {"project_id": "service-registry", "slugs": [], "contains": ["svcreg"]},
    "Kargo": {"project_id": "kargo", "slugs": ["kargo"]},
}


class OwnershipResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "plans").mkdir()
        linker._M = {"vault_root": str(self.root)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def plan(self, name: str, frontmatter: str = "") -> Path:
        path = self.root / "plans" / name
        path.write_text(frontmatter + "body\n", encoding="utf-8")
        return path

    def test_earliest_signal_wins(self) -> None:
        m = manifest(self.root, TWO_CLAIMANTS)
        path = self.plan("HANDOFF-2026-07-16-svcreg-kargo-prod-continuation.md")

        self.assertEqual(
            sorted(p for p, _ in linker.claimants(m, str(path))),
            ["Kargo", "Service Registry"],
        )
        # `svcreg` precedes `kargo` in the filename.
        self.assertEqual(linker.resolve_owner(m, str(path)), "Service Registry")

    def test_resolution_is_independent_of_manifest_order(self) -> None:
        """First-slug-wins would flip here; earliest-signal must not."""
        path = self.plan("HANDOFF-2026-07-16-svcreg-kargo-prod-continuation.md")
        forward = manifest(self.root, TWO_CLAIMANTS)
        reversed_order = manifest(self.root, dict(reversed(list(TWO_CLAIMANTS.items()))))

        self.assertEqual(
            linker.resolve_owner(forward, str(path)),
            linker.resolve_owner(reversed_order, str(path)),
        )

    def test_frontmatter_vault_owner_overrides_signal_order(self) -> None:
        m = manifest(self.root, TWO_CLAIMANTS)
        path = self.plan(
            "HANDOFF-2026-07-16-svcreg-kargo-prod-continuation.md",
            "---\ntype: handoff\nvault_owner: kargo\n---\n",
        )

        self.assertEqual(linker.resolve_owner(m, str(path)), "Kargo")

    def test_manifest_owns_overrides_signal_order(self) -> None:
        projects = json.loads(json.dumps(TWO_CLAIMANTS))
        name = "HANDOFF-2026-07-16-svcreg-kargo-prod-continuation.md"
        projects["Kargo"]["owns"] = [name]
        m = manifest(self.root, projects)
        path = self.plan(name)

        self.assertEqual(linker.resolve_owner(m, str(path)), "Kargo")

    def test_excludes_still_veto_before_ownership(self) -> None:
        projects = json.loads(json.dumps(TWO_CLAIMANTS))
        projects["Kargo"]["excludes"] = ["svcreg-kargo-prod"]
        m = manifest(self.root, projects)
        path = self.plan("HANDOFF-2026-07-16-svcreg-kargo-prod-continuation.md")

        self.assertEqual([p for p, _ in linker.claimants(m, str(path))], ["Service Registry"])

    def test_single_claimant_is_unaffected(self) -> None:
        m = manifest(self.root, TWO_CLAIMANTS)
        path = self.plan("HANDOFF-2026-07-16-kargo-only.md")

        self.assertEqual(linker.resolve_owner(m, str(path)), "Kargo")


class ProjectionTests(unittest.TestCase):
    """End-to-end: what actually lands on disk for a two-claimant source."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "plans").mkdir()
        self.m = manifest(self.root, TWO_CLAIMANTS)
        linker._M = self.m
        self.name = "HANDOFF-2026-07-16-svcreg-kargo-prod-continuation.md"
        self.source = self.root / "plans" / self.name
        self.source.write_text("body\n", encoding="utf-8")
        self.owner_alias = (
            self.root / "Notes/Projects/Service Registry/_sources/plans" / self.name
        )
        self.secondary_alias = self.root / "Notes/Projects/Kargo/_sources/plans" / self.name

    def tearDown(self) -> None:
        self.temp.cleanup()

    def sync(self, **kwargs) -> list[str]:
        actions = []
        for project, cfg in self.m["projects"].items():
            actions += linker.sync_project(self.m, project, cfg, dry_run=True, **kwargs)
            # dry_run=True skips hub/board generation, which needs the full manifest; do the
            # filesystem half for real so the resulting tree can be asserted.
            actions += self._materialize(project, cfg, **kwargs)
        return actions

    def _materialize(self, project, cfg, **kwargs) -> list[str]:
        allow_convert = kwargs.get("allow_delete", False) or kwargs.get(
            "repair_duplicates", False
        )
        out = []
        desired = linker.project_sources_map(self.m, project, cfg)
        for label, targets in desired.items():
            for target in targets:
                link = os.path.join(
                    self.root, "Notes/Projects", project, "_sources", label,
                    os.path.basename(target),
                )
                owner = linker.resolve_owner(self.m, target) or project
                if owner == project:
                    action = linker.ensure_symlink(link, target, dry_run=False)
                else:
                    action = linker.ensure_reference_note(
                        link,
                        linker.render_reference_note(
                            self.m, project, owner, label, target
                        ),
                        dry_run=False,
                        allow_convert=allow_convert,
                    )
                if action:
                    out.append(action)
        return out

    def test_owner_gets_symlink_and_secondary_gets_reference_note(self) -> None:
        self.sync()

        self.assertTrue(self.owner_alias.is_symlink())
        self.assertEqual(self.owner_alias.resolve(), self.source.resolve())

        self.assertTrue(self.secondary_alias.is_file())
        self.assertFalse(self.secondary_alias.is_symlink())
        body = self.secondary_alias.read_text(encoding="utf-8")
        self.assertIn(linker.REFERENCE_MARKER, body)
        self.assertIn("owner_project: service-registry", body)

    def test_exactly_one_vault_path_resolves_to_the_source(self) -> None:
        """The invariant the vault validator checks, asserted at projection time."""
        self.sync()

        resolved = [
            p
            for p in (self.root / "Notes/Projects").rglob("*.md")
            if os.path.realpath(p) == os.path.realpath(self.source)
        ]
        self.assertEqual([Path(p) for p in resolved], [self.owner_alias])

    def test_secondary_path_still_exists_so_hub_and_canvas_links_resolve(self) -> None:
        """Dropping the path instead of repurposing it would break hub/board/canvas links."""
        self.sync()

        self.assertTrue(self.secondary_alias.exists())

    def test_legacy_duplicate_symlink_is_preserved_by_default(self) -> None:
        self.secondary_alias.parent.mkdir(parents=True)
        os.symlink(
            os.path.relpath(self.source, self.secondary_alias.parent), self.secondary_alias
        )

        actions = self.sync()

        self.assertTrue(self.secondary_alias.is_symlink())
        self.assertTrue(any("PRESERVE" in a for a in actions))

    def test_repair_duplicates_converts_legacy_symlink(self) -> None:
        self.secondary_alias.parent.mkdir(parents=True)
        os.symlink(
            os.path.relpath(self.source, self.secondary_alias.parent), self.secondary_alias
        )

        self.sync(repair_duplicates=True)

        self.assertFalse(self.secondary_alias.is_symlink())
        self.assertIn(
            linker.REFERENCE_MARKER, self.secondary_alias.read_text(encoding="utf-8")
        )

    def test_reference_note_regeneration_is_idempotent(self) -> None:
        self.sync()
        stamp = self.secondary_alias.stat().st_mtime_ns

        actions = self.sync()

        self.assertEqual(stamp, self.secondary_alias.stat().st_mtime_ns)
        self.assertFalse(any("reference" in a for a in actions))

    def test_foreign_real_file_is_never_overwritten(self) -> None:
        self.secondary_alias.parent.mkdir(parents=True)
        self.secondary_alias.write_text("hand-written\n", encoding="utf-8")

        actions = self.sync()

        self.assertEqual(
            self.secondary_alias.read_text(encoding="utf-8"), "hand-written\n"
        )
        self.assertTrue(any("SKIP" in a for a in actions))


class OwnershipAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "plans").mkdir()
        linker._M = {"vault_root": str(self.root)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_audit_passes_when_every_source_has_one_owner(self) -> None:
        m = manifest(self.root, TWO_CLAIMANTS)
        (self.root / "plans/HANDOFF-2026-07-16-svcreg-kargo-prod-continuation.md").write_text(
            "body\n", encoding="utf-8"
        )

        duplicates, orphans = linker.audit_ownership(m)

        self.assertEqual(duplicates, [])
        self.assertEqual(orphans, [])

    def test_audit_flags_a_reintroduced_double_claim(self) -> None:
        """Guards the emitted plan, not just the resolver: if arbitration is bypassed, fail."""
        m = manifest(self.root, TWO_CLAIMANTS)
        (self.root / "plans/HANDOFF-2026-07-16-svcreg-kargo-prod-continuation.md").write_text(
            "body\n", encoding="utf-8"
        )
        original = linker.resolve_owner
        try:
            linker.resolve_owner = lambda *_a, **_k: None  # every claimant owns it again
            duplicates, _orphans = linker.audit_ownership(m)
        finally:
            linker.resolve_owner = original

        self.assertEqual(len(duplicates), 1)
        self.assertEqual(len(duplicates[0][1]), 2)


if __name__ == "__main__":
    unittest.main()

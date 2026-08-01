from __future__ import annotations

import os
import importlib.util
import sys
import tempfile
import unittest

import platform_skips
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location(
    "project_linker", SCRIPT_DIR / "project-linker.py"
)
assert spec and spec.loader
linker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(linker)


class ProjectLinkerNoDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        linker._M = {"vault_root": str(self.root)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    @platform_skips.requires_symlinks
    def test_wrong_target_symlink_is_preserved_by_default(self) -> None:
        target = self.root / "target.md"
        old_target = self.root / "old.md"
        target.write_text("new\n", encoding="utf-8")
        old_target.write_text("old\n", encoding="utf-8")
        link = self.root / "project/_sources/plans/item.md"
        link.parent.mkdir(parents=True)
        os.symlink(os.path.relpath(old_target, link.parent), link)
        before = (os.readlink(link), link.lstat().st_mtime_ns)

        action = linker.ensure_symlink(str(link), str(target), dry_run=False)

        self.assertIn("PRESERVE", action)
        self.assertEqual(before, (os.readlink(link), link.lstat().st_mtime_ns))
        self.assertEqual(link.resolve(), old_target.resolve())

    @platform_skips.requires_symlinks
    def test_explicit_allow_delete_can_repoint_symlink(self) -> None:
        target = self.root / "target.md"
        old_target = self.root / "old.md"
        target.write_text("new\n", encoding="utf-8")
        old_target.write_text("old\n", encoding="utf-8")
        link = self.root / "project/_sources/plans/item.md"
        link.parent.mkdir(parents=True)
        os.symlink(os.path.relpath(old_target, link.parent), link)

        linker.ensure_symlink(
            str(link), str(target), dry_run=False, allow_delete=True
        )

        self.assertEqual(link.resolve(), target.resolve())

    def test_old_handoff_mirrors_are_preserved_by_default(self) -> None:
        source = self.root / "plans/HANDOFF-2026-07-17-demo.md"
        source.parent.mkdir()
        source.write_text("current\n", encoding="utf-8")
        project_dir = self.root / "Notes/Projects/Demo"
        handoffs = project_dir / "handoffs"
        handoffs.mkdir(parents=True)
        old = handoffs / "HANDOFF-2026-07-16-demo.md"
        old.write_text("old\n", encoding="utf-8")
        manifest = {
            "vault_root": str(self.root),
            "projects_root": "Notes/Projects",
        }

        mirrored = linker.mirror_handoff_into_vault(
            manifest, "Demo", str(source)
        )

        self.assertTrue(old.exists())
        self.assertEqual(Path(mirrored).read_text(encoding="utf-8"), "current\n")


if __name__ == "__main__":
    unittest.main()

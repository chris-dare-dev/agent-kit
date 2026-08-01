#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from path_contract import (  # noqa: E402
    EXAMPLE_MANIFEST_NAME,
    MANIFEST_NAME,
    default_manifest_path,
    load_project_manifest,
    resolve_project_roots,
)


ENV_NAMES = (
    "PERSONAL_WORKSPACE_ROOT",
    "PERSONAL_SOURCE_ROOT",
    "PERSONAL_VAULT_ROOT",
    "PERSONAL_PRESENTATION_VAULT_ROOT",
    "PERSONAL_OBSIDIAN_VAULT",
)


class PathContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.manifest_path = self.scripts / "project-map.json"
        self.raw = {
            "vault_root": "${PERSONAL_WORKSPACE_ROOT}",
            "presentation_vault": {
                "name": "Vault",
                "root": "${PERSONAL_VAULT_ROOT}",
            },
            "sentinel": "${DO_NOT_EXPAND}",
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def clean_environment(self):
        return mock.patch.dict(os.environ, {name: "" for name in ENV_NAMES}, clear=False)

    def test_defaults_derive_from_manifest_location(self):
        with self.clean_environment():
            workspace, vault = resolve_project_roots(self.manifest_path, self.raw)
        self.assertEqual(workspace, self.root)
        self.assertEqual(vault, self.root / "Vault")

    def test_canonical_environment_overrides_defaults(self):
        custom_workspace = self.root / "moved-workspace"
        custom_vault = self.root / "moved-vault"
        with self.clean_environment(), mock.patch.dict(
            os.environ,
            {
                "PERSONAL_WORKSPACE_ROOT": str(custom_workspace),
                "PERSONAL_VAULT_ROOT": str(custom_vault),
            },
        ):
            workspace, vault = resolve_project_roots(self.manifest_path, self.raw)
        self.assertEqual(workspace, custom_workspace)
        self.assertEqual(vault, custom_vault)

    def test_legacy_environment_aliases_remain_compatible(self):
        custom_workspace = self.root / "legacy-workspace"
        custom_vault = self.root / "legacy-vault"
        with self.clean_environment(), mock.patch.dict(
            os.environ,
            {
                "PERSONAL_SOURCE_ROOT": str(custom_workspace),
                "PERSONAL_PRESENTATION_VAULT_ROOT": str(custom_vault),
            },
        ):
            workspace, vault = resolve_project_roots(self.manifest_path, self.raw)
        self.assertEqual(workspace, custom_workspace)
        self.assertEqual(vault, custom_vault)

    def test_literal_relative_paths_resolve_against_manifest_directory(self):
        raw = {
            "vault_root": "..",
            "presentation_vault": {"root": "../presentation"},
        }
        with self.clean_environment():
            workspace, vault = resolve_project_roots(self.manifest_path, raw)
        self.assertEqual(workspace, self.root)
        self.assertEqual(vault, self.root / "presentation")

    def test_unknown_path_placeholder_is_rejected(self):
        raw = dict(self.raw)
        raw["vault_root"] = "${HOME}/Work/workspace"
        with self.clean_environment(), self.assertRaisesRegex(ValueError, "unsupported placeholder"):
            resolve_project_roots(self.manifest_path, raw)

    def test_loader_preserves_non_path_strings_without_expanding_them(self):
        self.manifest_path.write_text(json.dumps(self.raw), encoding="utf-8")
        with self.clean_environment():
            manifest = load_project_manifest(self.manifest_path)
        self.assertEqual(manifest["vault_root"], str(self.root))
        self.assertEqual(manifest["presentation_vault"]["root"], str(self.root / "Vault"))
        self.assertEqual(manifest["sentinel"], "${DO_NOT_EXPAND}")


class DefaultManifestPathTests(unittest.TestCase):
    """A personal manifest must not have to be committed to be used.

    `project-map.json` describes one person's machine. Committing it is how an
    employer's monorepo layout ended up shipped in this kit, kept quiet by a
    denylist exemption that outlived the closed issue meant to remove it. The
    file is now untracked; `project-map.example.json` is the tracked one, and
    every consumer resolves through this helper so the fallback is not
    reimplemented seven slightly different ways.
    """

    def test_the_personal_manifest_wins_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            (base / MANIFEST_NAME).write_text("{}", encoding="utf-8")
            (base / EXAMPLE_MANIFEST_NAME).write_text("{}", encoding="utf-8")
            self.assertEqual(default_manifest_path(base).name, MANIFEST_NAME)

    def test_it_falls_back_to_the_tracked_example(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            (base / EXAMPLE_MANIFEST_NAME).write_text("{}", encoding="utf-8")
            self.assertEqual(default_manifest_path(base).name, EXAMPLE_MANIFEST_NAME)

    def test_a_missing_manifest_is_reported_against_the_example(self) -> None:
        """Neither exists: name the path that IS in the repository."""
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(
                default_manifest_path(Path(raw)).name, EXAMPLE_MANIFEST_NAME
            )

    def test_the_tracked_example_exists_and_loads(self) -> None:
        """The fallback is only real if the file it names is committed."""
        example = SCRIPT_DIR / EXAMPLE_MANIFEST_NAME
        self.assertTrue(example.exists(), f"{example} is missing from the repository")
        manifest = load_project_manifest(example)
        self.assertIn("projects", manifest)
        self.assertIn("presentation_vault", manifest)


if __name__ == "__main__":
    unittest.main()

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

from path_contract import load_project_manifest, resolve_project_roots  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()

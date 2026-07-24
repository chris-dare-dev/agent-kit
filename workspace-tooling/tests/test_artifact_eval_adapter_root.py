"""Adapter-root resolution must not depend on where the substrate lives (F-10).

Regression cover for the coupling that blocked versioning this tree: the
release-evidence manifest binds the claude-mcp-server adapter files, and used to
locate them by a hardcoded offset from `<workspace>/scripts/`. Moving the
substrate doubled the path and failed 23 of 433 tests, while the same suite was
green in place — a failure mode invisible until someone tried to move it.

The load-bearing case is `test_walks_up_when_substrate_lives_inside_the_adapter`:
that is the F-10 target layout.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import artifact_retrieval_eval as eval_mod


def _make_adapter(root: Path, name: str = eval_mod.ADAPTER_PACKAGE_NAME) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    return root


class AdapterRootIdentificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_identifies_by_package_name_not_directory_name(self) -> None:
        # A directory NAMED claude-mcp-server that is not the package must not
        # satisfy the check — a path component is not evidence of identity.
        impostor = _make_adapter(self.tmp / "claude-mcp-server", name="something-else")
        self.assertFalse(eval_mod._is_adapter_root(impostor))

        genuine = _make_adapter(self.tmp / "renamed-checkout")
        self.assertTrue(eval_mod._is_adapter_root(genuine))

    def test_absent_or_malformed_manifest_is_not_an_adapter_root(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertFalse(eval_mod._is_adapter_root(empty))

        broken = self.tmp / "broken"
        broken.mkdir()
        (broken / "package.json").write_text("{not json", encoding="utf-8")
        self.assertFalse(eval_mod._is_adapter_root(broken))


class AdapterRootResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # Ensure a stray real env var cannot make these pass or fail.
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(eval_mod.ADAPTER_ROOT_ENV, None)

    def test_legacy_workspace_layout_still_resolves(self) -> None:
        """substrate at <workspace>/scripts/ — the pre-F-10 layout."""
        workspace = self.tmp / "workspace"
        substrate = workspace / "scripts"
        substrate.mkdir(parents=True)
        adapter = _make_adapter(
            workspace.joinpath(*eval_mod._LEGACY_ADAPTER_OFFSET)
        )
        self.assertEqual(eval_mod._resolve_adapter_root(substrate), adapter)

    def test_walks_up_when_substrate_lives_inside_the_adapter(self) -> None:
        """substrate at <adapter>/workspace-tooling/ — the F-10 target layout.

        This is the case that used to double the path and fail 23 tests.
        """
        adapter = _make_adapter(self.tmp / "claude-mcp-server")
        substrate = adapter / "workspace-tooling"
        substrate.mkdir()
        self.assertEqual(eval_mod._resolve_adapter_root(substrate), adapter)

    def test_environment_override_wins_over_both_heuristics(self) -> None:
        workspace = self.tmp / "workspace"
        substrate = workspace / "scripts"
        substrate.mkdir(parents=True)
        _make_adapter(workspace.joinpath(*eval_mod._LEGACY_ADAPTER_OFFSET))
        explicit = _make_adapter(self.tmp / "explicit-checkout")

        os.environ[eval_mod.ADAPTER_ROOT_ENV] = str(explicit)
        self.assertEqual(eval_mod._resolve_adapter_root(substrate), explicit)

    def test_override_pointing_at_a_non_adapter_fails_loudly(self) -> None:
        substrate = self.tmp / "scripts"
        substrate.mkdir()
        decoy = self.tmp / "not-the-adapter"
        decoy.mkdir()

        os.environ[eval_mod.ADAPTER_ROOT_ENV] = str(decoy)
        # Must NOT silently fall through to a heuristic: an explicit override
        # that is wrong is an operator error worth surfacing, not routing around.
        with self.assertRaisesRegex(eval_mod.EvalError, "is not a .* checkout"):
            eval_mod._resolve_adapter_root(substrate)

    def test_unlocatable_adapter_fails_closed_with_remediation(self) -> None:
        orphan = self.tmp / "nowhere" / "substrate"
        orphan.mkdir(parents=True)
        with self.assertRaisesRegex(eval_mod.EvalError, "cannot locate"):
            eval_mod._resolve_adapter_root(orphan)


if __name__ == "__main__":
    unittest.main()

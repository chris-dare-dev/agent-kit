"""Adapter-root resolution must not depend on where the substrate lives (F-10).

Regression cover for the coupling that blocked versioning this tree: the
release-evidence manifest binds the adapter files, and used to
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
        # A directory NAMED like the package but WITHOUT the package name in its
        # satisfy the check — a path component is not evidence of identity.
        impostor = _make_adapter(self.tmp / "agent-kit", name="something-else")
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

    # test_legacy_workspace_layout_still_resolves is deliberately gone: it
    # existed only to pin the pre-fork employer offset
    # (a pre-fork employer monorepo offset), which #36
    # removes. That layout cannot occur in a public clone. Resolution by
    # package.json name via the ancestor walk covers the real layouts, and
    # test_walks_up_when_substrate_lives_inside_the_adapter still proves it.

    def test_walks_up_when_substrate_lives_inside_the_adapter(self) -> None:
        """substrate at <adapter>/workspace-tooling/ — the F-10 target layout.

        This is the case that used to double the path and fail 23 tests.
        """
        adapter = _make_adapter(self.tmp / "agent-kit")
        substrate = adapter / "workspace-tooling"
        substrate.mkdir()
        self.assertEqual(eval_mod._resolve_adapter_root(substrate), adapter)

    def test_environment_override_wins_over_the_ancestor_walk(self) -> None:
        # The decoy must be something the WALK would otherwise find, or this
        # degenerates into "the override returns the override" and silently
        # stops asserting precedence.
        workspace = self.tmp / "workspace"
        substrate = workspace / "nested"
        substrate.mkdir(parents=True)
        _make_adapter(workspace)
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

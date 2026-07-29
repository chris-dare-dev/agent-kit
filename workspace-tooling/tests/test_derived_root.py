#!/usr/bin/env python3
"""One definition of the derived-state root, and nothing allowed to re-declare it.

`~/.local/share/personal-artifacts` used to be a literal in eleven modules while
the TypeScript adapter spelled the same directory `workspace-artifacts`. With no
single definition there was nothing for the two sides to agree with, so the
socket the adapter dialled and the socket the provisioner bound drifted apart
and four MCP tools were dead on arrival on every clean install.

These tests pin the resolver's order and, more importantly, fail if any module
reintroduces a hardcoded copy.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifact_runtime  # noqa: E402

SUBSTRATE_DIR = Path(__file__).resolve().parents[1]
LITERAL_RE = re.compile(r"local/share/(?:personal|workspace)-artifacts")


class DerivedRootResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            key: os.environ.get(key)
            for key in ("AGENT_KIT_DERIVED_ROOT", "XDG_DATA_HOME", "LOCALAPPDATA")
        }
        for key in self._saved:
            os.environ.pop(key, None)
        artifact_runtime._migration_warned = True  # silence the migration notice

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_explicit_override_wins(self) -> None:
        os.environ["XDG_DATA_HOME"] = "/tmp/xdg"
        os.environ["AGENT_KIT_DERIVED_ROOT"] = "/tmp/explicit"
        self.assertEqual(artifact_runtime.derived_root(), Path("/tmp/explicit"))

    def test_xdg_data_home_is_second(self) -> None:
        os.environ["XDG_DATA_HOME"] = "/tmp/xdg"
        self.assertEqual(artifact_runtime.derived_root(), Path("/tmp/xdg/agent-kit"))

    def test_per_os_default_is_last(self) -> None:
        """The default differs per platform and never lands outside the user's own area."""
        for platform, expected_tail in (
            ("linux", Path(".local") / "share" / "agent-kit"),
            ("darwin", Path("Library") / "Application Support" / "agent-kit"),
        ):
            with self.subTest(platform=platform):
                original = sys.platform
                try:
                    sys.platform = platform  # type: ignore[misc]
                    root = artifact_runtime.default_derived_root()
                finally:
                    sys.platform = original  # type: ignore[misc]
                self.assertEqual(root, Path.home() / expected_tail)

    def test_windows_default_uses_localappdata(self) -> None:
        original = sys.platform
        try:
            sys.platform = "win32"  # type: ignore[misc]
            os.environ["LOCALAPPDATA"] = os.path.join("C:", "Users", "someone", "AppData", "Local")
            root = artifact_runtime.default_derived_root()
        finally:
            sys.platform = original  # type: ignore[misc]
        self.assertEqual(root.name, "agent-kit")
        self.assertIn("Local", str(root))

    def test_config_path_hangs_off_the_resolved_root(self) -> None:
        os.environ["AGENT_KIT_DERIVED_ROOT"] = "/tmp/explicit"
        self.assertEqual(
            artifact_runtime.default_config_path(),
            Path("/tmp/explicit") / "artifact-memory-runtime.json",
        )


class NoDuplicateDefinitionTests(unittest.TestCase):
    def test_no_module_level_derived_root_literals(self) -> None:
        """Only the legacy-migration branch may name the old paths.

        This is the test that would have caught the original divergence. If it
        fails, a module has gone back to building the path itself instead of
        calling artifact_runtime.derived_root(), and the two halves of the
        system can drift apart again.
        """
        offenders: list[str] = []
        for path in sorted(SUBSTRATE_DIR.glob("*.py")):
            if path.name == "artifact_runtime.py":
                continue  # holds _LEGACY_ROOTS, by design
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if LITERAL_RE.search(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")

        self.assertEqual(
            offenders,
            [],
            "these modules re-declare the derived root instead of calling "
            "artifact_runtime.derived_root():\n  " + "\n  ".join(offenders),
        )

    def test_legacy_roots_are_confined_to_the_migration_branch(self) -> None:
        source = (SUBSTRATE_DIR / "artifact_runtime.py").read_text(encoding="utf-8")
        self.assertIn("_LEGACY_ROOTS", source)
        # Every occurrence of the old name must be inside that one tuple.
        for number, line in enumerate(source.splitlines(), 1):
            if LITERAL_RE.search(line):
                self.assertIn(
                    "expanduser()",
                    line,
                    f"artifact_runtime.py:{number} names a legacy root outside _LEGACY_ROOTS",
                )


if __name__ == "__main__":
    unittest.main()

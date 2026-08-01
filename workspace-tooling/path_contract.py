#!/usr/bin/env python3
"""Resolve the portable path contract used by the vault tooling.

The JSON manifest stores symbolic roots so it can move between machines.  Only
the two documented root fields are expanded; arbitrary manifest strings are
never treated as shell templates.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable


WORKSPACE_TOKEN = "${PERSONAL_WORKSPACE_ROOT}"
VAULT_TOKEN = "${PERSONAL_VAULT_ROOT}"
_PLACEHOLDER_RE = re.compile(r"\$\{[^}]+\}")

MANIFEST_NAME = "project-map.json"
EXAMPLE_MANIFEST_NAME = "project-map.example.json"


def default_manifest_path(directory: Path | str | None = None) -> Path:
    """The personal manifest if it exists, otherwise the tracked example.

    `project-map.json` describes one person's machine — where their vault sits,
    which local directories hold which project. It was committed anyway, which
    is why it carried an employer's monorepo layout into a kit meant to be
    shared, and why a denylist exemption existed to keep the gate quiet about
    it. The file is now untracked and gitignored; `project-map.example.json` is
    the tracked, generic one.

    Every consumer resolves through here rather than joining the filename
    itself, so the fallback cannot be implemented seven slightly different ways
    — which is how the two sides of a path contract drift apart.

    Returns the example even when neither exists, so callers report a missing
    manifest against a path that is actually in the repository.
    """
    base = Path(directory) if directory is not None else Path(__file__).resolve().parent
    personal = base / MANIFEST_NAME
    return personal if personal.exists() else base / EXAMPLE_MANIFEST_NAME


def _normalized_path(value: str, *, base: Path) -> Path:
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        expanded = os.path.join(base, expanded)
    return Path(os.path.abspath(expanded))


def _environment_path(names: Iterable[str], *, default: Path, base: Path) -> Path:
    for name in names:
        value = os.environ.get(name)
        if value:
            return _normalized_path(value, base=base)
    return default


def _resolve_field(value: Any, *, token: str, resolved_token: Path, base: Path, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if value == token:
        return resolved_token
    placeholder = _PLACEHOLDER_RE.search(value)
    if placeholder:
        raise ValueError(f"{field} uses unsupported placeholder {placeholder.group(0)!r}")
    return _normalized_path(value, base=base)


def resolve_project_roots(
    path: Path | str, raw_manifest: Mapping[str, Any]
) -> tuple[Path, Path]:
    """Return absolute workspace and presentation-vault roots for a manifest."""

    manifest_path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    manifest_dir = manifest_path.parent
    if not isinstance(raw_manifest, Mapping):
        raise ValueError(f"expected a JSON object: {manifest_path}")
    presentation = raw_manifest.get("presentation_vault")
    if not isinstance(presentation, Mapping):
        raise ValueError("project-map presentation_vault must be an object")

    workspace_default = manifest_dir.parent
    workspace_token = _environment_path(
        ("PERSONAL_WORKSPACE_ROOT", "PERSONAL_SOURCE_ROOT"),
        default=workspace_default,
        base=manifest_dir,
    )
    workspace = _resolve_field(
        raw_manifest.get("vault_root"),
        token=WORKSPACE_TOKEN,
        resolved_token=workspace_token,
        base=manifest_dir,
        field="vault_root",
    )

    vault_default = workspace / "Vault"
    vault_token = _environment_path(
        ("PERSONAL_VAULT_ROOT", "PERSONAL_PRESENTATION_VAULT_ROOT", "PERSONAL_OBSIDIAN_VAULT"),
        default=vault_default,
        base=manifest_dir,
    )
    vault = _resolve_field(
        presentation.get("root"),
        token=VAULT_TOKEN,
        resolved_token=vault_token,
        base=manifest_dir,
        field="presentation_vault.root",
    )
    return workspace, vault


def load_project_manifest(path: Path | str) -> dict[str, Any]:
    """Load a project manifest and materialize only its two symbolic roots."""

    manifest_path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    with manifest_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object: {manifest_path}")
    workspace, vault = resolve_project_roots(manifest_path, raw)
    presentation = raw.get("presentation_vault")
    if not isinstance(presentation, dict):
        raise ValueError("project-map presentation_vault must be an object")

    resolved = dict(raw)
    resolved["vault_root"] = str(workspace)
    resolved["presentation_vault"] = dict(presentation)
    resolved["presentation_vault"]["root"] = str(vault)
    return resolved

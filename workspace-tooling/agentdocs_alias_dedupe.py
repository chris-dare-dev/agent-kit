#!/usr/bin/env python3
"""Safely identify and remove redundant ``AgentDocs`` Markdown aliases.

``build-agent-vault.sh`` farms scattered Markdown into ``AgentDocs``.  Project
hubs also expose selected source files through ``Notes/Projects/*/_sources``.
When both aliases resolve to the same file, Obsidian indexes the same note twice.

This helper makes the project-local ``_sources`` alias canonical, but only when
all of the following are true for an ``AgentDocs`` path:

* the path is absent (safe to suppress) or is a symlink inside ``AgentDocs``;
* the source target exists;
* at least one existing ``_sources`` symlink resolves to the exact same target;
* no indexed Markdown, Canvas, Base, or Excalidraw artifact explicitly names the
  ``AgentDocs`` path; and
* no Obsidian state file under ``.obsidian`` explicitly names the path.

The default mode is read-only.  ``--apply`` unlinks only candidates that pass
every gate.  ``--emit-safe-destinations`` is the machine-readable mode used by
``build-agent-vault.sh`` to keep safe duplicates out of its desired set before
linking, preserving incremental/idempotent behavior.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Sequence

from path_contract import default_manifest_path, load_project_manifest


SCHEMA_VERSION = 1
INDEXED_SUFFIXES = (".md", ".canvas", ".base")
SKIP_INDEX_DIRS = {".git", ".obsidian", "node_modules", "__pycache__"}
STATE_SUFFIXES = {".json", ".md", ".canvas", ".base", ".css"}


@dataclasses.dataclass
class Candidate:
    alias: Path
    target: Path
    canonical_aliases: tuple[Path, ...]
    exists: bool
    indexed_references: set[str] = dataclasses.field(default_factory=set)
    state_references: set[str] = dataclasses.field(default_factory=set)
    blockers: set[str] = dataclasses.field(default_factory=set)
    removed: bool = False

    @property
    def safe(self) -> bool:
        return not self.blockers

    def as_dict(self, vault: Path) -> dict[str, Any]:
        return {
            "alias": logical_path(vault, self.alias),
            "target": str(self.target),
            "canonical_aliases": [
                logical_path(vault, alias) for alias in self.canonical_aliases
            ],
            "exists": self.exists,
            "safe": self.safe,
            "blockers": sorted(self.blockers),
            "indexed_references": sorted(self.indexed_references),
            "state_references": sorted(self.state_references),
            "removed": self.removed,
        }


@dataclasses.dataclass(frozen=True)
class Config:
    workspace: Path
    vault: Path
    projects_root: Path
    source_alias_dir: str
    farm: Path


@dataclasses.dataclass
class Report:
    config: Config
    mode: str
    canonical_alias_count: int
    canonical_target_count: int
    agentdocs_alias_count: int
    broken_agentdocs_aliases: tuple[str, ...]
    candidates: list[Candidate]
    apply_errors: list[str] = dataclasses.field(default_factory=list)

    @property
    def safe_candidates(self) -> list[Candidate]:
        return [candidate for candidate in self.candidates if candidate.safe]

    @property
    def blocked_candidates(self) -> list[Candidate]:
        return [candidate for candidate in self.candidates if not candidate.safe]

    def as_dict(self) -> dict[str, Any]:
        existing_safe = [candidate for candidate in self.safe_candidates if candidate.exists]
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": self.mode,
            "workspace": str(self.config.workspace),
            "vault": str(self.config.vault),
            "agentdocs_root": logical_path(self.config.vault, self.config.farm),
            "projects_root": logical_path(self.config.vault, self.config.projects_root),
            "summary": {
                "canonical_source_aliases": self.canonical_alias_count,
                "canonical_source_targets": self.canonical_target_count,
                "agentdocs_aliases": self.agentdocs_alias_count,
                "duplicate_candidates": len(self.candidates),
                "safe_to_suppress": len(self.safe_candidates),
                "safe_existing_removals": len(existing_safe),
                "blocked": len(self.blocked_candidates),
                "indexed_reference_blockers": sum(
                    bool(candidate.indexed_references) for candidate in self.candidates
                ),
                "workspace_state_blockers": sum(
                    bool(candidate.state_references) for candidate in self.candidates
                ),
                "removed": sum(candidate.removed for candidate in self.candidates),
                "broken_agentdocs_aliases": len(self.broken_agentdocs_aliases),
                "apply_errors": len(self.apply_errors),
            },
            "broken_agentdocs_aliases": list(self.broken_agentdocs_aliases),
            "candidates": [
                candidate.as_dict(self.config.vault)
                for candidate in sorted(self.candidates, key=lambda item: str(item.alias))
            ],
            "apply_errors": list(self.apply_errors),
        }


def logical_path(vault: Path, path: Path) -> str:
    try:
        return path.relative_to(vault).as_posix()
    except ValueError:
        return str(path)


def load_mapping(path: Path) -> Mapping[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def load_config(
    manifest_path: Path,
    *,
    workspace_override: Path | None = None,
    vault_override: Path | None = None,
) -> Config:
    manifest = load_project_manifest(manifest_path)
    presentation = manifest.get("presentation_vault", {})
    if not isinstance(presentation, Mapping):
        raise ValueError("project-map presentation_vault must be an object")

    workspace_value = workspace_override or Path(str(manifest.get("vault_root", "")))
    vault_value = vault_override or Path(str(presentation.get("root", "")))
    if not str(workspace_value):
        raise ValueError("workspace root is missing from project-map.json")
    if not str(vault_value):
        raise ValueError("presentation vault root is missing from project-map.json")

    workspace = workspace_value.expanduser().absolute()
    vault = vault_value.expanduser().absolute()
    projects_relative = str(
        presentation.get("projects_root", manifest.get("projects_root", "Notes/Projects"))
    )
    source_alias_dir = str(presentation.get("source_alias_dir", "_sources"))
    return Config(
        workspace=workspace,
        vault=vault,
        projects_root=vault / projects_relative,
        source_alias_dir=source_alias_dir,
        farm=vault / "AgentDocs",
    )


def iter_symlink_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        for filename in filenames:
            path = Path(directory) / filename
            if path.is_symlink():
                yield path


def collect_canonical_aliases(config: Config) -> dict[Path, tuple[Path, ...]]:
    by_target: dict[Path, list[Path]] = defaultdict(list)
    for alias in iter_symlink_files(config.projects_root):
        try:
            relative = alias.relative_to(config.projects_root)
        except ValueError:
            continue
        if config.source_alias_dir not in relative.parts:
            continue
        if not alias.name.lower().endswith(".md") or not alias.exists():
            continue
        try:
            target = alias.resolve(strict=True)
        except OSError:
            continue
        by_target[target].append(alias)
    return {
        target: tuple(sorted(aliases, key=str))
        for target, aliases in sorted(by_target.items(), key=lambda item: str(item[0]))
    }


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def candidate_for(
    alias: Path,
    target: Path,
    canonical_aliases: tuple[Path, ...],
    *,
    allow_absent: bool,
    farm: Path,
) -> Candidate:
    exists = alias.exists() or alias.is_symlink()
    candidate = Candidate(alias, target, canonical_aliases, exists=exists)
    if not is_within(alias, farm):
        candidate.blockers.add("alias_outside_agentdocs")
    if not canonical_aliases:
        candidate.blockers.add("canonical_alias_missing")
    if not exists:
        if not allow_absent:
            candidate.blockers.add("alias_missing")
        return candidate
    if not alias.is_symlink():
        candidate.blockers.add("alias_not_owned_symlink")
        return candidate
    if not alias.exists():
        candidate.blockers.add("alias_broken")
        return candidate
    try:
        existing_target = alias.resolve(strict=True)
    except OSError:
        candidate.blockers.add("alias_broken")
        return candidate
    if existing_target != target:
        candidate.blockers.add("alias_target_changed")
    return candidate


def collect_existing_candidates(
    config: Config,
    canonical: Mapping[Path, tuple[Path, ...]],
) -> tuple[list[Candidate], int, tuple[str, ...]]:
    candidates: list[Candidate] = []
    alias_count = 0
    broken: list[str] = []
    for alias in iter_symlink_files(config.farm):
        if not alias.name.lower().endswith(".md"):
            continue
        alias_count += 1
        if not alias.exists():
            broken.append(logical_path(config.vault, alias))
            continue
        try:
            target = alias.resolve(strict=True)
        except OSError:
            broken.append(logical_path(config.vault, alias))
            continue
        canonical_aliases = canonical.get(target)
        if not canonical_aliases:
            continue
        candidates.append(
            candidate_for(
                alias,
                target,
                canonical_aliases,
                allow_absent=False,
                farm=config.farm,
            )
        )
    return candidates, alias_count, tuple(sorted(broken))


def collect_plan_candidates(
    config: Config,
    canonical: Mapping[Path, tuple[Path, ...]],
    plan_path: Path,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[Path] = set()
    with plan_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\n")
            if not line:
                continue
            try:
                target_raw, alias_raw = line.split("\t", 1)
            except ValueError as exc:
                raise ValueError(
                    f"{plan_path}:{line_number}: expected TAB-separated target and destination"
                ) from exc
            alias = Path(alias_raw).absolute()
            if alias in seen:
                continue
            seen.add(alias)
            try:
                target = Path(target_raw).resolve(strict=True)
            except OSError:
                # The normal vault sync should retain the entry and let its
                # existing source-link handling surface the missing target.
                continue
            canonical_aliases = canonical.get(target)
            if not canonical_aliases:
                continue
            candidates.append(
                candidate_for(
                    alias,
                    target,
                    canonical_aliases,
                    allow_absent=True,
                    farm=config.farm,
                )
            )
    return candidates


def iter_logical_files(vault: Path) -> Iterator[tuple[str, Path]]:
    """Walk the vault namespace while following directory aliases safely."""

    def walk(
        path: Path,
        relative: PurePosixPath,
        ancestors: frozenset[tuple[int, int]],
    ) -> Iterator[tuple[str, Path]]:
        try:
            stat_result = path.stat()
        except OSError:
            return
        inode = (stat_result.st_dev, stat_result.st_ino)
        if inode in ancestors:
            return
        next_ancestors = ancestors | {inode}
        try:
            entries = sorted(os.scandir(path), key=lambda entry: entry.name.casefold())
        except OSError:
            return
        for entry in entries:
            if entry.name in SKIP_INDEX_DIRS:
                continue
            child_path = path / entry.name
            child_relative = relative / entry.name
            try:
                if entry.is_dir(follow_symlinks=True):
                    yield from walk(child_path, child_relative, next_ancestors)
                elif entry.is_file(follow_symlinks=True):
                    yield child_relative.as_posix(), child_path
            except OSError:
                continue

    yield from walk(vault, PurePosixPath(), frozenset())


def normalize_reference_text(text: str) -> str:
    normalized = text
    for _ in range(2):
        decoded = urllib.parse.unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    return normalized.replace("\\/", "/")


def scan_reference_file(
    source: str,
    path: Path,
    candidates_by_relative: Mapping[str, Candidate],
    *,
    state: bool,
) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    # All candidate paths start with AgentDocs.  This cheap check avoids doing
    # hundreds of substring comparisons for ordinary source notes.
    if "AgentDocs" not in text and "agentdocs" not in text.lower():
        return
    normalized = normalize_reference_text(text).casefold()
    for relative, candidate in candidates_by_relative.items():
        if relative not in normalized:
            continue
        if state:
            candidate.state_references.add(source)
            candidate.blockers.add("workspace_state_reference")
        else:
            candidate.indexed_references.add(source)
            candidate.blockers.add("indexed_reference")


def attach_references(config: Config, candidates: Iterable[Candidate]) -> None:
    candidates_by_relative = {
        logical_path(config.vault, candidate.alias).casefold(): candidate
        for candidate in candidates
    }
    if not candidates_by_relative:
        return
    for relative, path in iter_logical_files(config.vault):
        if not relative.lower().endswith(INDEXED_SUFFIXES):
            continue
        scan_reference_file(relative, path, candidates_by_relative, state=False)

    obsidian_root = config.vault / ".obsidian"
    if not obsidian_root.is_dir():
        return
    for directory, dirnames, filenames in os.walk(obsidian_root, followlinks=False):
        dirnames.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix.lower() not in STATE_SUFFIXES:
                continue
            scan_reference_file(
                logical_path(config.vault, path),
                path,
                candidates_by_relative,
                state=True,
            )


def build_report(
    config: Config,
    *,
    mode: str = "dry-run",
    farm_plan: Path | None = None,
) -> Report:
    canonical = collect_canonical_aliases(config)
    existing_candidates, agentdocs_count, broken = collect_existing_candidates(config, canonical)
    if farm_plan is None:
        candidates = existing_candidates
    else:
        candidates = collect_plan_candidates(config, canonical, farm_plan)
    attach_references(config, candidates)
    return Report(
        config=config,
        mode=mode,
        canonical_alias_count=sum(len(aliases) for aliases in canonical.values()),
        canonical_target_count=len(canonical),
        agentdocs_alias_count=agentdocs_count,
        broken_agentdocs_aliases=broken,
        candidates=candidates,
    )


def revalidate_before_unlink(candidate: Candidate, config: Config) -> str | None:
    if not candidate.safe:
        return "candidate is blocked"
    if not is_within(candidate.alias, config.farm):
        return "alias escaped AgentDocs"
    if not candidate.alias.is_symlink():
        return "alias is no longer a symlink"
    if not candidate.alias.exists():
        return "alias target no longer exists"
    try:
        if candidate.alias.resolve(strict=True) != candidate.target:
            return "alias target changed after audit"
    except OSError as exc:
        return f"cannot resolve alias: {exc}"
    matching_canonical = False
    for alias in candidate.canonical_aliases:
        if not alias.is_symlink() or not alias.exists():
            continue
        try:
            if alias.resolve(strict=True) == candidate.target:
                matching_canonical = True
                break
        except OSError:
            continue
    if not matching_canonical:
        return "no matching canonical _sources symlink remains"
    return None


def apply_safe_removals(report: Report) -> None:
    for candidate in sorted(report.candidates, key=lambda item: str(item.alias)):
        if not candidate.exists or not candidate.safe:
            continue
        error = revalidate_before_unlink(candidate, report.config)
        if error:
            report.apply_errors.append(f"{logical_path(report.config.vault, candidate.alias)}: {error}")
            continue
        try:
            candidate.alias.unlink()
            candidate.removed = True
        except OSError as exc:
            report.apply_errors.append(
                f"{logical_path(report.config.vault, candidate.alias)}: unlink failed: {exc}"
            )


def render_human(report: Report) -> str:
    summary = report.as_dict()["summary"]
    lines = [
        f"AgentDocs alias dedupe: {report.mode.upper()}",
        f"vault: {report.config.vault}",
        f"canonical _sources aliases: {summary['canonical_source_aliases']} "
        f"({summary['canonical_source_targets']} targets)",
        f"AgentDocs aliases: {summary['agentdocs_aliases']}",
        f"duplicate candidates: {summary['duplicate_candidates']}",
        f"safe existing removals: {summary['safe_existing_removals']}",
        f"blocked: {summary['blocked']} "
        f"(indexed={summary['indexed_reference_blockers']}, "
        f"workspace-state={summary['workspace_state_blockers']})",
        f"removed: {summary['removed']}",
    ]
    if report.broken_agentdocs_aliases:
        lines.append(f"broken AgentDocs aliases: {len(report.broken_agentdocs_aliases)}")
    if report.blocked_candidates:
        lines.append("")
        lines.append("Preserved candidates:")
        for candidate in sorted(report.blocked_candidates, key=lambda item: str(item.alias)):
            alias = logical_path(report.config.vault, candidate.alias)
            reasons = ", ".join(sorted(candidate.blockers))
            references = sorted(candidate.indexed_references | candidate.state_references)
            suffix = f"; refs: {', '.join(references)}" if references else ""
            lines.append(f"- {alias}: {reasons}{suffix}")
    if report.apply_errors:
        lines.append("")
        lines.append("Apply errors:")
        lines.extend(f"- {message}" for message in report.apply_errors)
    if report.mode == "dry-run":
        lines.extend(("", "No files changed. Use --apply to unlink safe existing candidates."))
    return "\n".join(lines)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    default_manifest = default_manifest_path()
    parser.add_argument("--manifest", type=Path, default=default_manifest)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--json", action="store_true", help="emit the stable JSON report")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="unlink only fully validated duplicates")
    mode.add_argument(
        "--emit-safe-destinations",
        action="store_true",
        help="emit safe farm destinations from --farm-plan, one per line",
    )
    parser.add_argument(
        "--farm-plan",
        type=Path,
        help="TAB-separated source target and AgentDocs destination generated by the vault builder",
    )
    args = parser.parse_args(argv)
    if args.emit_safe_destinations and args.farm_plan is None:
        parser.error("--emit-safe-destinations requires --farm-plan")
    if args.farm_plan is not None and not args.emit_safe_destinations:
        parser.error("--farm-plan is only valid with --emit-safe-destinations")
    if args.json and args.emit_safe_destinations:
        parser.error("--json cannot be combined with --emit-safe-destinations")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        config = load_config(
            args.manifest,
            workspace_override=args.workspace,
            vault_override=args.vault,
        )
        mode = "build-filter" if args.emit_safe_destinations else ("apply" if args.apply else "dry-run")
        report = build_report(config, mode=mode, farm_plan=args.farm_plan)
        if args.emit_safe_destinations:
            for candidate in sorted(report.safe_candidates, key=lambda item: str(item.alias)):
                print(candidate.alias)
            return 0
        if args.apply:
            apply_safe_removals(report)
        if args.json:
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            print(render_human(report))
        return 1 if report.apply_errors else 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"agentdocs alias dedupe failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

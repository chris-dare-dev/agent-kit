#!/usr/bin/env python3
"""Plan and audit the default-deny AgentDocs projection.

This module owns policy evaluation only.  It never creates, changes, or removes
vault aliases.  ``build-agent-vault.sh`` consumes the validated plan and keeps
the mutation boundary deliberately small.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence


SCHEMA_VERSION = 1


class PolicyError(ValueError):
    """Raised when policy or path input is unsafe or ambiguous."""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    globs: tuple[str, ...]


@dataclass(frozen=True)
class ProjectionPolicy:
    default_action: str
    candidate_roots: tuple[str, ...]
    prune_directory_names: frozenset[str]
    exclude_rules: tuple[Rule, ...]
    allow_rules: tuple[Rule, ...]


@dataclass(frozen=True)
class Decision:
    action: str
    rule_id: str


@dataclass(frozen=True)
class PlanEntry:
    source: Path
    destination: Path
    source_relative: str
    vault_relative: str
    rule_id: str


def _safe_relative(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{field} must be a non-empty string")
    if "\x00" in value or "\t" in value or "\n" in value or "\r" in value:
        raise PolicyError(f"{field} contains an unsupported control character")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PolicyError(f"{field} must be a normalized workspace-relative path: {value!r}")
    return path.as_posix()


def _load_rules(raw: Any, *, field: str) -> tuple[Rule, ...]:
    if not isinstance(raw, list):
        raise PolicyError(f"{field} must be an array")
    rules: list[Rule] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PolicyError(f"{field}[{index}] must be an object")
        rule_id = item.get("id")
        globs = item.get("globs")
        if not isinstance(rule_id, str) or not rule_id:
            raise PolicyError(f"{field}[{index}].id must be a non-empty string")
        if rule_id in seen:
            raise PolicyError(f"duplicate rule id: {rule_id}")
        if not isinstance(globs, list) or not globs:
            raise PolicyError(f"{field}[{index}].globs must be a non-empty array")
        normalized: list[str] = []
        for glob_index, pattern in enumerate(globs):
            normalized.append(
                _safe_relative(pattern, field=f"{field}[{index}].globs[{glob_index}]")
            )
        seen.add(rule_id)
        rules.append(Rule(rule_id=rule_id, globs=tuple(normalized)))
    return tuple(rules)


def load_policy(path: Path | str) -> ProjectionPolicy:
    """Load and strictly validate the ``vault_projection`` policy."""

    policy_path = Path(path).expanduser().resolve()
    with policy_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise PolicyError("policy root must be an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise PolicyError(
            f"unsupported policy schema_version {raw.get('schema_version')!r}; "
            f"expected {SCHEMA_VERSION}"
        )
    projection = raw.get("vault_projection")
    if not isinstance(projection, dict):
        raise PolicyError("vault_projection must be an object")
    default_action = projection.get("default_action")
    if default_action not in {"allow", "exclude"}:
        raise PolicyError("vault_projection.default_action must be allow or exclude")

    roots_raw = projection.get("candidate_roots")
    if not isinstance(roots_raw, list) or not roots_raw:
        raise PolicyError("vault_projection.candidate_roots must be a non-empty array")
    roots = tuple(
        _safe_relative(value, field=f"vault_projection.candidate_roots[{index}]")
        for index, value in enumerate(roots_raw)
    )
    if len(set(roots)) != len(roots):
        raise PolicyError("vault_projection.candidate_roots contains duplicates")

    prune_raw = projection.get("prune_directory_names", [])
    if not isinstance(prune_raw, list):
        raise PolicyError("vault_projection.prune_directory_names must be an array")
    prune_names: set[str] = set()
    for index, name in enumerate(prune_raw):
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or name in {".", ".."}
            or any(char in name for char in "\x00\t\n\r")
        ):
            raise PolicyError(
                f"vault_projection.prune_directory_names[{index}] is invalid"
            )
        prune_names.add(name)

    return ProjectionPolicy(
        default_action=default_action,
        candidate_roots=roots,
        prune_directory_names=frozenset(prune_names),
        exclude_rules=_load_rules(
            projection.get("exclude_rules", []),
            field="vault_projection.exclude_rules",
        ),
        allow_rules=_load_rules(
            projection.get("allow_rules", []),
            field="vault_projection.allow_rules",
        ),
    )


def classify(relative_path: str, policy: ProjectionPolicy) -> Decision:
    """Classify one normalized workspace-relative POSIX path."""

    relative = _safe_relative(relative_path, field="relative_path")
    for rule in policy.exclude_rules:
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in rule.globs):
            return Decision("exclude", rule.rule_id)
    for rule in policy.allow_rules:
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in rule.globs):
            return Decision("allow", rule.rule_id)
    return Decision(policy.default_action, f"default-{policy.default_action}")


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _dedot(relative: str) -> str:
    parts = []
    for part in PurePosixPath(relative).parts:
        stripped = part[1:] if part.startswith(".") else part
        if not stripped:
            raise PolicyError(f"cannot project empty de-dotted path segment: {relative!r}")
        parts.append(stripped)
    return PurePosixPath(*parts).as_posix()


def _iter_markdown(root: Path, prune_names: frozenset[str]) -> Iterator[Path]:
    """Yield regular Markdown files without following directory or file symlinks."""

    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = current_path / name
            if name in prune_names or candidate.is_symlink():
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names):
            if not name.lower().endswith(".md"):
                continue
            candidate = current_path / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            yield candidate


def build_plan(
    workspace: Path | str,
    vault: Path | str,
    policy: ProjectionPolicy,
) -> list[PlanEntry]:
    """Return a complete, collision-free plan of newly eligible aliases."""

    workspace_root = Path(workspace).expanduser().resolve(strict=True)
    vault_root = Path(vault).expanduser().resolve(strict=False)
    if workspace_root == vault_root:
        raise PolicyError("workspace and vault must be different paths")

    entries: list[PlanEntry] = []
    destinations: dict[Path, str] = {}
    for relative_root in policy.candidate_roots:
        source_root = (workspace_root / relative_root).resolve(strict=True)
        if not _within(source_root, workspace_root):
            raise PolicyError(f"candidate root escapes workspace: {relative_root}")
        if not source_root.is_dir():
            raise PolicyError(f"candidate root is not a directory: {relative_root}")
        for source in _iter_markdown(source_root, policy.prune_directory_names):
            resolved_source = source.resolve(strict=True)
            if not _within(resolved_source, workspace_root):
                raise PolicyError(f"candidate file escapes workspace: {source}")
            relative = source.relative_to(workspace_root).as_posix()
            if any(char in relative for char in "\t\n\r"):
                raise PolicyError(f"unsupported control character in path: {relative!r}")
            decision = classify(relative, policy)
            if decision.action != "allow":
                continue
            destination = vault_root / "AgentDocs" / _dedot(relative)
            previous = destinations.get(destination)
            if previous is not None and previous != relative:
                raise PolicyError(
                    f"projection destination collision: {previous!r} and {relative!r} "
                    f"both map to {destination}"
                )
            destinations[destination] = relative
            entries.append(
                PlanEntry(
                    source=source.absolute(),
                    destination=destination.absolute(),
                    source_relative=relative,
                    vault_relative=destination.relative_to(vault_root).as_posix(),
                    rule_id=decision.rule_id,
                )
            )
    return sorted(entries, key=lambda entry: entry.vault_relative)


def _iter_symlinks(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise PolicyError(f"AgentDocs must be a real directory when present: {root}")
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            children = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            raise PolicyError(f"cannot scan {current}: {exc}") from exc
        for child in children:
            child_path = Path(child.path)
            if child.is_symlink():
                yield child_path
            elif child.is_dir(follow_symlinks=False):
                pending.append(child_path)


def _link_record(
    alias: Path,
    *,
    vault_root: Path,
    workspace_root: Path,
    policy: ProjectionPolicy,
) -> dict[str, Any]:
    raw_target = os.readlink(alias)
    record: dict[str, Any] = {
        "vault_relative": alias.relative_to(vault_root).as_posix(),
        "readlink": raw_target,
    }
    if not alias.exists():
        record["state"] = "broken"
        return record
    resolved = alias.resolve(strict=True)
    record["resolved_target"] = str(resolved)
    if not _within(resolved, workspace_root):
        record["state"] = "live-outside-workspace"
        return record
    relative = resolved.relative_to(workspace_root).as_posix()
    decision = classify(relative, policy)
    record.update(
        {
            "state": "live",
            "source_relative": relative,
            "policy_action": decision.action,
            "policy_rule": decision.rule_id,
        }
    )
    return record


def build_audit(
    workspace: Path | str,
    vault: Path | str,
    policy: ProjectionPolicy,
) -> dict[str, Any]:
    """Build a sorted, strictly read-only projection audit."""

    workspace_root = Path(workspace).expanduser().resolve(strict=True)
    vault_root = Path(vault).expanduser().resolve(strict=False)
    plan = build_plan(workspace_root, vault_root, policy)
    planned_by_destination = {entry.destination: entry for entry in plan}

    existing_allowed: list[dict[str, Any]] = []
    live_excluded: list[dict[str, Any]] = []
    live_wrong_target: list[dict[str, Any]] = []
    broken: list[dict[str, Any]] = []
    live_outside: list[dict[str, Any]] = []
    existing_aliases: set[Path] = set()

    farm = vault_root / "AgentDocs"
    for alias in _iter_symlinks(farm):
        alias_abs = alias.absolute()
        existing_aliases.add(alias_abs)
        record = _link_record(
            alias,
            vault_root=vault_root,
            workspace_root=workspace_root,
            policy=policy,
        )
        if record["state"] == "broken":
            broken.append(record)
            continue
        if record["state"] == "live-outside-workspace":
            live_outside.append(record)
            continue
        planned = planned_by_destination.get(alias_abs)
        if planned is not None:
            if Path(record["resolved_target"]) == planned.source.resolve(strict=True):
                existing_allowed.append(record)
            else:
                record["planned_source_relative"] = planned.source_relative
                live_wrong_target.append(record)
        elif record.get("policy_action") == "exclude":
            live_excluded.append(record)
        else:
            record["reason"] = "allowed-source-at-unplanned-destination"
            live_wrong_target.append(record)

    new_allowed_missing: list[dict[str, Any]] = []
    destination_collisions: list[dict[str, Any]] = []
    for entry in plan:
        if entry.destination in existing_aliases:
            continue
        if os.path.lexists(entry.destination):
            destination_collisions.append(
                {
                    "vault_relative": entry.vault_relative,
                    "source_relative": entry.source_relative,
                    "existing_kind": (
                        "directory" if entry.destination.is_dir() else "regular-file"
                    ),
                }
            )
            continue
        new_allowed_missing.append(
            {
                "vault_relative": entry.vault_relative,
                "source_relative": entry.source_relative,
                "policy_rule": entry.rule_id,
            }
        )

    categories = {
        "allowed_planned": [
            {
                "vault_relative": entry.vault_relative,
                "source_relative": entry.source_relative,
                "policy_rule": entry.rule_id,
            }
            for entry in plan
        ],
        "new_allowed_missing": new_allowed_missing,
        "existing_allowed": existing_allowed,
        "live_excluded_future_prune": live_excluded,
        "live_wrong_target": live_wrong_target,
        "broken_review_only": broken,
        "live_outside_workspace_review_only": live_outside,
        "destination_collision": destination_collisions,
    }
    for records in categories.values():
        records.sort(key=lambda item: item["vault_relative"])
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "read-only-audit",
        "workspace": str(workspace_root),
        "vault": str(vault_root),
        "policy_semantics": {
            "new_aliases": "allowlisted-only",
            "existing_live_aliases": "preserve",
            "existing_broken_aliases": "preserve-and-report",
            "deletions": "disabled",
        },
        "counts": {name: len(records) for name, records in categories.items()},
        "categories": categories,
    }


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise PolicyError(f"refusing to replace existing audit output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Publish atomically without replacement. A concurrent creator causes
        # os.link() to fail instead of losing its report.
        os.link(temporary_name, path)
        os.unlink(temporary_name)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        default=str(Path(__file__).with_name("artifact-policy.json")),
        help="artifact policy JSON",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--vault", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan", help="emit allowed source/destination TSV")
    audit = subparsers.add_parser("audit", help="emit a read-only JSON audit")
    audit.add_argument("--output", help="atomically write a new JSON report here")
    audit.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    policy = load_policy(args.policy)
    if args.command == "plan":
        for entry in build_plan(args.workspace, args.vault, policy):
            print(f"{entry.source}\t{entry.destination}")
        return 0
    report = build_audit(args.workspace, args.vault, policy)
    if args.output:
        output = Path(args.output).expanduser().absolute().resolve(strict=False)
        workspace = Path(args.workspace).expanduser().resolve()
        vault = Path(args.vault).expanduser().resolve()
        if _within(output, workspace) or _within(output, vault):
            raise PolicyError(
                "audit output must be outside the source workspace and presentation vault"
            )
        _atomic_json_write(output, report)
    if args.compact:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, PolicyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

#!/usr/bin/env python3
"""Validate the Obsidian presentation-vault contract without changing it.

The presentation vault is a logical namespace.  In particular, ``Notes`` may be a
directory symlink into the source workspace.  Resolving a Markdown link with
``Path.resolve()`` can therefore incorrectly make ``../../../plans/foo.md`` look
valid even though Obsidian resolves it to the missing vault-relative path
``plans/foo.md``.  This validator normalizes links lexically before checking them.

Checks:
  * links in generated ``Notes/Projects/*/_index.md`` project hubs;
  * ``file`` nodes in every Canvas;
  * project-hub identity/frontmatter against ``project-map.json``;
  * internal, portable links in generated roadmap-status Excalidraw files; and
  * multiple Markdown aliases that resolve to the same real file.

The command is deliberately read-only.  It emits a concise grouped report by
default, or a stable JSON document with ``--json``, and exits non-zero when the
contract has errors.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import posixpath
import re
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence


VALIDATOR_VERSION = "1"
CHECK_ORDER = (
    "project_metadata",
    "hub_links",
    "canvas_nodes",
    "excalidraw_links",
    "duplicate_aliases",
)
SKIP_DIR_NAMES = {".git", ".obsidian", "node_modules", "__pycache__"}
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")
WIKILINK_RE = re.compile(r"!?\[\[([^\]\n]+)\]\]")
DRAWING_RE = re.compile(
    r"(?:^|\n)## Drawing\s*\n```json\s*\n(?P<payload>\{.*?\})\s*\n```",
    re.DOTALL,
)


@dataclasses.dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    code: str
    source: str
    message: str
    target: str | None = None
    details: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "severity": self.severity,
            "check": self.check,
            "code": self.code,
            "source": self.source,
            "message": self.message,
        }
        if self.target is not None:
            result["target"] = self.target
        if self.details:
            result["details"] = list(self.details)
        return result


@dataclasses.dataclass
class CheckStats:
    inspected: int = 0
    errors: int = 0
    warnings: int = 0

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ValidationReport:
    vault: Path
    workspace: Path
    findings: list[Finding] = dataclasses.field(default_factory=list)
    checks: dict[str, CheckStats] = dataclasses.field(
        default_factory=lambda: {name: CheckStats() for name in CHECK_ORDER}
    )

    def inspect(self, check: str, count: int = 1) -> None:
        self.checks[check].inspected += count

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)
        stats = self.checks[finding.check]
        if finding.severity == "error":
            stats.errors += 1
        else:
            stats.warnings += 1

    def sorted_findings(self) -> list[Finding]:
        order = {name: index for index, name in enumerate(CHECK_ORDER)}
        return sorted(
            self.findings,
            key=lambda item: (
                order.get(item.check, len(order)),
                item.code,
                item.source,
                item.target or "",
                item.message,
            ),
        )

    @property
    def error_count(self) -> int:
        return sum(item.errors for item in self.checks.values())

    @property
    def warning_count(self) -> int:
        return sum(item.warnings for item in self.checks.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "validator_version": VALIDATOR_VERSION,
            "vault": str(self.vault),
            "workspace": str(self.workspace),
            "summary": {
                "status": "fail" if self.error_count else "pass",
                "errors": self.error_count,
                "warnings": self.warning_count,
            },
            "checks": {name: self.checks[name].as_dict() for name in CHECK_ORDER},
            "findings": [item.as_dict() for item in self.sorted_findings()],
        }


@dataclasses.dataclass(frozen=True)
class LogicalFile:
    relative: str
    path: Path


class VaultIndex:
    """Logical paths in a vault, retaining aliases instead of resolving them."""

    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self.files: dict[str, Path] = {}
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.broken_markdown_links: list[str] = []
        for logical_file in iter_vault_files(vault):
            relative = logical_file.relative
            self.files[relative] = logical_file.path
            self.by_name[PurePosixPath(relative).name].append(relative)
            if relative.lower().endswith(".md"):
                stem_name = PurePosixPath(relative).name[:-3]
                self.by_name[stem_name].append(relative)
        for candidates in self.by_name.values():
            candidates.sort()

    def exists(self, relative: str) -> bool:
        return relative in self.files

    def resolve_link(
        self,
        source_relative: str,
        raw_target: str,
        *,
        root_relative: bool = False,
        wiki: bool = False,
    ) -> tuple[str | None, str | None]:
        """Return (resolved vault path, error reason) for a local link."""
        target = clean_link_target(raw_target, wiki=wiki)
        if not target:
            return None, None

        parsed = urllib.parse.urlparse(target)
        if parsed.scheme in {"http", "https", "mailto", "tel", "data"}:
            return None, None
        if parsed.scheme:
            return None, f"unsupported URI scheme {parsed.scheme!r}"

        decoded = urllib.parse.unquote(target).replace("\\", "/")
        decoded = decoded.split("#", 1)[0].split("^", 1)[0]
        if not decoded:
            return None, None
        if decoded.startswith("/") or re.match(r"^[A-Za-z]:/", decoded):
            return None, "absolute filesystem path is not vault-portable"

        source_dir = PurePosixPath(source_relative).parent.as_posix()
        base = "" if root_relative else source_dir
        normalized = posixpath.normpath(posixpath.join(base, decoded))
        if normalized == ".." or normalized.startswith("../"):
            return None, "link escapes the presentation vault"

        candidates = candidate_paths(normalized)
        for candidate in candidates:
            if self.exists(candidate):
                return candidate, None

        # Obsidian permits basename-only wikilinks.  Prefer an exact unique
        # filename/stem match after checking the note-local path.
        if wiki and "/" not in decoded:
            matches: list[str] = []
            for key in (decoded, decoded + ".md"):
                matches.extend(self.by_name.get(key, []))
            matches = sorted(set(matches))
            if len(matches) == 1:
                return matches[0], None
            if len(matches) > 1:
                return None, "wikilink is ambiguous: " + ", ".join(matches[:5])

        return None, "target is absent from the presentation-vault namespace"


def iter_vault_files(vault: Path) -> Iterator[LogicalFile]:
    """Walk logical vault paths while following directory symlinks safely.

    Only ancestor cycles are suppressed.  The same real directory can therefore
    still be visited through two independent aliases, which is required for
    duplicate-alias detection.
    """

    def walk(path: Path, relative: PurePosixPath, ancestors: frozenset[tuple[int, int]]) -> Iterator[LogicalFile]:
        try:
            stat_result = path.stat()
        except OSError:
            return
        key = (stat_result.st_dev, stat_result.st_ino)
        if key in ancestors:
            return
        next_ancestors = ancestors | {key}
        try:
            entries = sorted(os.scandir(path), key=lambda entry: entry.name.casefold())
        except OSError:
            return
        for entry in entries:
            if entry.name in SKIP_DIR_NAMES:
                continue
            child_relative = relative / entry.name
            child_path = path / entry.name
            try:
                if entry.is_dir(follow_symlinks=True):
                    yield from walk(child_path, child_relative, next_ancestors)
                elif entry.is_file(follow_symlinks=True):
                    yield LogicalFile(child_relative.as_posix(), child_path)
                elif entry.is_symlink() and entry.name.lower().endswith(".md"):
                    # Preserve broken Markdown aliases so they can be reported.
                    yield LogicalFile(child_relative.as_posix(), child_path)
            except OSError:
                continue

    yield from walk(vault, PurePosixPath(), frozenset())


def candidate_paths(normalized: str) -> list[str]:
    candidates = [normalized]
    for suffix in (".md", ".canvas", ".excalidraw.md"):
        if not normalized.lower().endswith(suffix):
            candidates.append(normalized + suffix)
    return list(dict.fromkeys(candidates))


def clean_link_target(raw: str, *, wiki: bool) -> str:
    target = raw.strip()
    if wiki:
        return target.split("|", 1)[0].strip()
    if target.startswith("<") and ">" in target:
        return target[1 : target.index(">")].strip()
    # Remove an optional Markdown title while retaining percent-encoded spaces.
    target = re.sub(r"\s+(?:\"[^\"]*\"|'[^']*')\s*$", "", target)
    return target.strip()


def load_json(path: Path) -> Mapping[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError("expected a JSON object")
    return value


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the flat scalar/list subset used by generated project hubs."""
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, Any] = {}
    current_list: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        list_match = re.match(r"^\s+-\s+(.+?)\s*$", line)
        if list_match and current_list:
            result.setdefault(current_list, []).append(unquote_yaml(list_match.group(1)))
            continue
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if not match:
            current_list = None
            continue
        key, raw_value = match.groups()
        if not raw_value:
            result[key] = []
            current_list = key
        elif raw_value.startswith("[") and raw_value.endswith("]"):
            inner = raw_value[1:-1].strip()
            result[key] = [unquote_yaml(item.strip()) for item in inner.split(",") if item.strip()]
            current_list = None
        else:
            result[key] = unquote_yaml(raw_value)
            current_list = None
    return result


def unquote_yaml(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def validate_project_metadata(
    report: ValidationReport,
    index: VaultIndex,
    manifest: Mapping[str, Any],
) -> None:
    projects = manifest.get("projects")
    projects_root = manifest.get("projects_root", "Notes/Projects")
    if not isinstance(projects, Mapping):
        report.add(
            Finding(
                "error",
                "project_metadata",
                "MANIFEST_PROJECTS_INVALID",
                "scripts/project-map.json",
                "manifest field 'projects' must be an object",
            )
        )
        return

    claimed_ids: dict[str, str] = {}
    expected_hubs: set[str] = set()
    for project_name in sorted(projects, key=str.casefold):
        config = projects[project_name]
        if not isinstance(config, Mapping):
            continue
        slugs = config.get("slugs") or []
        project_id = config.get("project_id")
        if not project_id and isinstance(slugs, Sequence) and not isinstance(slugs, str) and slugs:
            project_id = str(slugs[0]).rstrip("-.")
        hub_relative = (PurePosixPath(str(projects_root)) / str(project_name) / "_index.md").as_posix()
        expected_hubs.add(hub_relative)
        report.inspect("project_metadata")

        if not project_id:
            report.add(
                Finding(
                    "error",
                    "project_metadata",
                    "PROJECT_ID_MISSING",
                    hub_relative,
                    f"manifest project {project_name!r} has no project_id or primary slug",
                )
            )
            continue
        project_id = str(project_id)
        if project_id in claimed_ids:
            report.add(
                Finding(
                    "error",
                    "project_metadata",
                    "PROJECT_ID_DUPLICATE",
                    hub_relative,
                    f"project id {project_id!r} is also claimed by {claimed_ids[project_id]!r}",
                )
            )
        else:
            claimed_ids[project_id] = str(project_name)

        hub_path = index.files.get(hub_relative)
        if hub_path is None:
            report.add(
                Finding(
                    "error",
                    "project_metadata",
                    "PROJECT_HUB_MISSING",
                    hub_relative,
                    f"manifest project {project_name!r} has no generated hub in the presentation vault",
                )
            )
            continue
        try:
            text = hub_path.read_text(encoding="utf-8")
        except OSError as exc:
            report.add(
                Finding(
                    "error",
                    "project_metadata",
                    "PROJECT_HUB_UNREADABLE",
                    hub_relative,
                    str(exc),
                )
            )
            continue
        frontmatter = parse_frontmatter(text)
        tags_value = frontmatter.get("tags", [])
        tags = [str(tags_value)] if isinstance(tags_value, str) else [str(tag) for tag in tags_value]
        expected_values = {
            "type": "project-hub",
            "project": project_id,
        }
        for key, expected in expected_values.items():
            actual = frontmatter.get(key)
            if actual != expected:
                report.add(
                    Finding(
                        "error",
                        "project_metadata",
                        "PROJECT_HUB_METADATA_MISMATCH",
                        hub_relative,
                        f"frontmatter {key!r} is {actual!r}; expected {expected!r}",
                    )
                )
        expected_tag = f"project/{project_id}"
        if expected_tag not in tags:
            report.add(
                Finding(
                    "error",
                    "project_metadata",
                    "PROJECT_HUB_TAG_MISMATCH",
                    hub_relative,
                    f"frontmatter tags omit {expected_tag!r}",
                )
            )
        expected_heading = f"# {project_name} — project hub"
        if expected_heading not in text.splitlines():
            report.add(
                Finding(
                    "error",
                    "project_metadata",
                    "PROJECT_HUB_HEADING_MISMATCH",
                    hub_relative,
                    f"hub heading does not match manifest project name {project_name!r}",
                )
            )
        if "Auto-generated by `scripts/project-linker.py`" not in text:
            report.add(
                Finding(
                    "error",
                    "project_metadata",
                    "PROJECT_HUB_GENERATOR_MARKER_MISSING",
                    hub_relative,
                    "hub lacks the project-linker generated-file marker",
                )
            )

    hub_prefix = str(PurePosixPath(str(projects_root))) + "/"
    actual_hubs = {
        relative
        for relative in index.files
        if relative.startswith(hub_prefix) and relative.endswith("/_index.md")
    }
    for unexpected in sorted(actual_hubs - expected_hubs):
        report.inspect("project_metadata")
        report.add(
            Finding(
                "warning",
                "project_metadata",
                "PROJECT_HUB_NOT_IN_MANIFEST",
                unexpected,
                "generated project hub has no matching manifest entry",
            )
        )


def validate_hub_links(report: ValidationReport, index: VaultIndex) -> None:
    hub_paths = sorted(
        relative
        for relative in index.files
        if relative.startswith("Notes/Projects/") and relative.endswith("/_index.md")
    )
    for hub_relative in hub_paths:
        path = index.files[hub_relative]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            report.add(Finding("error", "hub_links", "HUB_UNREADABLE", hub_relative, str(exc)))
            continue
        links: list[tuple[str, bool]] = []
        links.extend((match.group(1), False) for match in MARKDOWN_LINK_RE.finditer(text))
        links.extend((match.group(1), True) for match in WIKILINK_RE.finditer(text))
        for raw_target, wiki in links:
            report.inspect("hub_links")
            _resolved, error = index.resolve_link(hub_relative, raw_target, wiki=wiki)
            if error:
                report.add(
                    Finding(
                        "error",
                        "hub_links",
                        "HUB_LINK_UNRESOLVED",
                        hub_relative,
                        error,
                        target=clean_link_target(raw_target, wiki=wiki),
                    )
                )


def validate_canvas_nodes(report: ValidationReport, index: VaultIndex) -> None:
    for canvas_relative in sorted(path for path in index.files if path.lower().endswith(".canvas")):
        path = index.files[canvas_relative]
        try:
            payload = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            report.add(
                Finding("error", "canvas_nodes", "CANVAS_INVALID", canvas_relative, str(exc))
            )
            continue
        nodes = payload.get("nodes", [])
        if not isinstance(nodes, list):
            report.add(
                Finding(
                    "error",
                    "canvas_nodes",
                    "CANVAS_NODES_INVALID",
                    canvas_relative,
                    "Canvas field 'nodes' must be an array",
                )
            )
            continue
        for node in nodes:
            if not isinstance(node, Mapping) or node.get("type") != "file":
                continue
            report.inspect("canvas_nodes")
            raw_target = node.get("file")
            node_id = str(node.get("id", "<missing-id>"))
            if not isinstance(raw_target, str) or not raw_target.strip():
                report.add(
                    Finding(
                        "error",
                        "canvas_nodes",
                        "CANVAS_FILE_TARGET_MISSING",
                        canvas_relative,
                        f"file node {node_id!r} has no target",
                    )
                )
                continue
            _resolved, error = index.resolve_link(
                canvas_relative,
                raw_target,
                root_relative=True,
            )
            if error:
                report.add(
                    Finding(
                        "error",
                        "canvas_nodes",
                        "CANVAS_FILE_UNRESOLVED",
                        canvas_relative,
                        f"file node {node_id!r}: {error}",
                        target=raw_target,
                    )
                )


def parse_excalidraw(text: str) -> Mapping[str, Any]:
    match = DRAWING_RE.search(text)
    if match:
        payload = json.loads(match.group("payload"))
        if not isinstance(payload, Mapping):
            raise ValueError("Excalidraw Drawing payload must be an object")
        return payload
    # The Obsidian Excalidraw plugin legitimately rewrites opened drawings as LZ-compressed JSON.
    # Link validation does not need to decompress the whole scene: the plugin keeps a canonical
    # `## Element Links` projection specifically for indexed navigation.
    if "```compressed-json" in text:
        links = re.search(r"^## Element Links\s*$\n(?P<body>.*?)(?:^%%\s*$)", text, re.M | re.S)
        if not links:
            raise ValueError("compressed Excalidraw has no Element Links projection")
        elements = []
        for line in links.group("body").splitlines():
            item = re.match(r"^([^:\s]+):\s+(\S.*?)\s*$", line)
            if item:
                elements.append({"id": item.group(1), "link": item.group(2), "isDeleted": False})
        return {"elements": elements, "compressed": True}
    raise ValueError("generated Excalidraw has neither JSON Drawing nor compressed Element Links")


def validate_excalidraw_links(report: ValidationReport, index: VaultIndex) -> None:
    drawings = sorted(
        relative for relative in index.files if relative.lower().endswith(".excalidraw.md")
    )
    for drawing_relative in drawings:
        path = index.files[drawing_relative]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            report.add(
                Finding("error", "excalidraw_links", "EXCALIDRAW_UNREADABLE", drawing_relative, str(exc))
            )
            continue
        # Hand-authored drawings may intentionally link to the web.  This
        # contract is for deterministic roadmap-status projections only.
        if "type/roadmap-status" not in text and '"source": "workspace-roadmap-status"' not in text:
            continue
        try:
            drawing = parse_excalidraw(text)
        except (ValueError, json.JSONDecodeError) as exc:
            report.add(
                Finding(
                    "error",
                    "excalidraw_links",
                    "EXCALIDRAW_GENERATED_PAYLOAD_INVALID",
                    drawing_relative,
                    str(exc),
                )
            )
            continue
        elements = drawing.get("elements", [])
        if not isinstance(elements, list):
            report.add(
                Finding(
                    "error",
                    "excalidraw_links",
                    "EXCALIDRAW_ELEMENTS_INVALID",
                    drawing_relative,
                    "Drawing field 'elements' must be an array",
                )
            )
            continue
        for element in elements:
            if not isinstance(element, Mapping) or element.get("isDeleted"):
                continue
            link = element.get("link")
            if not isinstance(link, str) or not link.strip():
                continue
            report.inspect("excalidraw_links")
            validate_excalidraw_link(
                report,
                index,
                drawing_relative,
                str(element.get("id", "<missing-id>")),
                link.strip(),
            )


def validate_excalidraw_link(
    report: ValidationReport,
    index: VaultIndex,
    drawing_relative: str,
    element_id: str,
    link: str,
) -> None:
    parsed = urllib.parse.urlparse(link)
    if parsed.scheme in {"http", "https", "mailto", "file"}:
        report.add(
            Finding(
                "error",
                "excalidraw_links",
                "EXCALIDRAW_LINK_NOT_INTERNAL",
                drawing_relative,
                f"generated element {element_id!r} uses non-internal scheme {parsed.scheme!r}",
                target=link,
            )
        )
        return
    if parsed.scheme == "obsidian":
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        path_values = query.get("path", [])
        file_values = query.get("file", [])
        if path_values:
            target = path_values[0]
            if os.path.isabs(target) or re.match(r"^[A-Za-z]:[/\\]", target):
                report.add(
                    Finding(
                        "error",
                        "excalidraw_links",
                        "EXCALIDRAW_ABSOLUTE_PATH_URI",
                        drawing_relative,
                        f"generated element {element_id!r} embeds a machine-specific absolute path",
                        target=link,
                    )
                )
                return
            raw_target = target
        elif file_values:
            raw_target = file_values[0]
            vault_values = query.get("vault", [])
            if vault_values and vault_values[0] != report.vault.name:
                report.add(
                    Finding(
                        "error",
                        "excalidraw_links",
                        "EXCALIDRAW_WRONG_VAULT",
                        drawing_relative,
                        f"generated element {element_id!r} targets vault {vault_values[0]!r}, expected {report.vault.name!r}",
                        target=link,
                    )
                )
                return
        else:
            report.add(
                Finding(
                    "error",
                    "excalidraw_links",
                    "EXCALIDRAW_URI_TARGET_MISSING",
                    drawing_relative,
                    f"generated element {element_id!r} has no 'file' target",
                    target=link,
                )
            )
            return
        _resolved, error = index.resolve_link(
            drawing_relative,
            raw_target,
            root_relative=True,
        )
        if error:
            report.add(
                Finding(
                    "error",
                    "excalidraw_links",
                    "EXCALIDRAW_LINK_UNRESOLVED",
                    drawing_relative,
                    f"generated element {element_id!r}: {error}",
                    target=link,
                )
            )
        return

    if parsed.scheme:
        report.add(
            Finding(
                "error",
                "excalidraw_links",
                "EXCALIDRAW_LINK_UNSUPPORTED",
                drawing_relative,
                f"generated element {element_id!r} uses unsupported scheme {parsed.scheme!r}",
                target=link,
            )
        )
        return

    wiki_match = re.fullmatch(r"\[\[(.+)\]\]", link)
    raw_target = wiki_match.group(1) if wiki_match else link
    _resolved, error = index.resolve_link(
        drawing_relative,
        raw_target,
        wiki=bool(wiki_match),
    )
    if error:
        report.add(
            Finding(
                "error",
                "excalidraw_links",
                "EXCALIDRAW_LINK_UNRESOLVED",
                drawing_relative,
                f"generated element {element_id!r}: {error}",
                target=link,
            )
        )


def validate_duplicate_aliases(report: ValidationReport, index: VaultIndex) -> None:
    by_realpath: dict[str, list[str]] = defaultdict(list)
    for relative, path in sorted(index.files.items()):
        if not relative.lower().endswith(".md"):
            continue
        report.inspect("duplicate_aliases")
        if path.is_symlink() and not path.exists():
            report.add(
                Finding(
                    "error",
                    "duplicate_aliases",
                    "BROKEN_MARKDOWN_ALIAS",
                    relative,
                    "Markdown symlink target does not exist",
                    target=os.readlink(path),
                )
            )
            continue
        try:
            realpath = os.path.realpath(path)
        except OSError:
            continue
        by_realpath[realpath].append(relative)

    for realpath, aliases in sorted(by_realpath.items()):
        unique_aliases = sorted(set(aliases))
        if len(unique_aliases) < 2:
            continue
        report.add(
            Finding(
                "error",
                "duplicate_aliases",
                "DUPLICATE_MARKDOWN_ALIAS",
                unique_aliases[0],
                f"{len(unique_aliases)} vault paths resolve to the same Markdown source",
                target=realpath,
                details=tuple(unique_aliases),
            )
        )


def validate(vault: Path, workspace: Path, manifest_path: Path) -> ValidationReport:
    vault = vault.expanduser().absolute()
    workspace = workspace.expanduser().absolute()
    report = ValidationReport(vault=vault, workspace=workspace)
    if not vault.is_dir():
        raise ValueError(f"presentation vault does not exist or is not a directory: {vault}")
    if not workspace.is_dir():
        raise ValueError(f"workspace does not exist or is not a directory: {workspace}")
    manifest = load_json(manifest_path)
    index = VaultIndex(vault)
    validate_project_metadata(report, index, manifest)
    validate_hub_links(report, index)
    validate_canvas_nodes(report, index)
    validate_excalidraw_links(report, index)
    validate_duplicate_aliases(report, index)
    return report


def render_human(report: ValidationReport, *, examples_per_code: int) -> str:
    status = "FAIL" if report.error_count else "PASS"
    lines = [
        f"Obsidian presentation-vault contract: {status}",
        f"vault: {report.vault}",
        f"workspace: {report.workspace}",
        "",
    ]
    findings = report.sorted_findings()
    for check in CHECK_ORDER:
        stats = report.checks[check]
        check_status = "FAIL" if stats.errors else ("WARN" if stats.warnings else "PASS")
        lines.append(
            f"[{check_status}] {check}: inspected={stats.inspected} "
            f"errors={stats.errors} warnings={stats.warnings}"
        )
        grouped: dict[str, list[Finding]] = defaultdict(list)
        for finding in findings:
            if finding.check == check:
                grouped[finding.code].append(finding)
        for code in sorted(grouped):
            items = grouped[code]
            lines.append(f"  {code}: {len(items)}")
            for item in items[:examples_per_code]:
                target = f" -> {item.target}" if item.target else ""
                lines.append(f"    - {item.source}{target}: {item.message}")
            omitted = len(items) - examples_per_code
            if omitted > 0:
                lines.append(f"    - ... {omitted} more (use --json for every finding)")
    lines.extend(
        [
            "",
            f"result: {report.error_count} errors, {report.warning_count} warnings",
            "read-only: no files were changed",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).absolute().parent
    workspace_default = script_dir.parent
    vault_default = Path(
        os.environ.get(
            "PERSONAL_VAULT_ROOT",
            os.environ.get(
                "PERSONAL_PRESENTATION_VAULT_ROOT",
                os.environ.get("PERSONAL_OBSIDIAN_VAULT", workspace_default / "Vault"),
            ),
        )
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=vault_default, help="active presentation vault")
    parser.add_argument("--workspace", type=Path, default=workspace_default, help="workspace source workspace")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="project manifest (default: <workspace>/scripts/project-map.json)",
    )
    parser.add_argument("--json", action="store_true", help="emit the complete stable JSON report")
    parser.add_argument(
        "--examples-per-code",
        type=int,
        default=3,
        help="human report examples per finding code (default: 3)",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="also return non-zero when the report contains warnings only",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.examples_per_code < 0:
        parser.error("--examples-per-code must be non-negative")
    manifest_path = args.manifest or args.workspace / "scripts" / "project-map.json"
    try:
        report = validate(args.vault, args.workspace, manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if args.json:
            print(json.dumps({"schema_version": 1, "status": "error", "message": str(exc)}, sort_keys=True))
        else:
            print(f"obsidian-vault-validate: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=False))
    else:
        print(render_human(report, examples_per_code=args.examples_per_code))
    if report.error_count or (args.fail_on_warning and report.warning_count):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

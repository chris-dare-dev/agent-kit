#!/usr/bin/env python3
"""Build one exact-span manifest for lexical and vector artifact retrieval."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import artifact_ingestion as ingestion
import artifact_runtime
import artifact_security as security


SCHEMA_VERSION = 1
PROFILE_ID = "canonical-bge384-heading-line-v1"
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
TARGET_CONTENT_TOKENS = 384
MAX_EMBEDDING_TOKENS = 510
OVERLAP_TOKENS = 64
POINT_NAMESPACE = uuid.UUID("c18e7b69-c2f4-4c7b-8ac0-8ff626602f91")
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
STRUCTURED_SUFFIXES = {".json", ".yaml", ".yml"}
LINE_SUFFIXES = STRUCTURED_SUFFIXES | {".log", ".txt"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{2,127}")


class SpanGenerationError(RuntimeError):
    """A canonical artifact could not be represented without ambiguity."""


@dataclass(frozen=True)
class ExactSpan:
    point_id: str
    span_id: str
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    line_start: int
    line_end: int
    heading: str | None
    identifiers: str
    content: str
    embedding_text: str
    span_sha256: str
    content_tokens: int
    embedding_tokens: int


class TokenCounter:
    """Count the exact wordpieces used by the pinned embedding tokenizer."""

    def __init__(self, tokenizer_file: Path):
        security.require_private_file(tokenizer_file)
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise SpanGenerationError("tokenizers dependency is unavailable") from exc
        self.tokenizer = Tokenizer.from_file(str(tokenizer_file))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()

    def __call__(self, value: str) -> int:
        return len(self.tokenizer.encode(value).ids)

    def offsets(self, value: str) -> list[tuple[int, int]]:
        return list(
            self.tokenizer.encode(
                value,
                add_special_tokens=False,
            ).offsets
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _publish_private_manifest(
    temporary: Path,
    destination: Path,
    directory: Path,
) -> None:
    """Publish one generation without ever replacing an existing pathname."""
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise SpanGenerationError(
            "exact-span manifest destination already exists; "
            "use a generation-unique path"
        ) from exc
    security.require_private_file(destination)
    temporary.unlink()
    security.fsync_directory(directory)


def _profile_digest(model_manifest_digest: str) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "profile_id": PROFILE_ID,
        "embedding_model": EMBEDDING_MODEL,
        "target_content_tokens": TARGET_CONTENT_TOKENS,
        "max_embedding_tokens": MAX_EMBEDDING_TOKENS,
        "overlap_tokens": OVERLAP_TOKENS,
        "markdown": "heading-and-line-boundary-v1",
        "structured": "strict-parse-and-line-boundary-v1",
        "plain": "line-boundary-v1",
        "identity": "revision-profile-byte-range-span-sha256-v1",
        "model_manifest_digest": model_manifest_digest,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _model_manifest(snapshot: Path) -> tuple[str, list[dict[str, Any]]]:
    try:
        return security.private_tree_manifest(snapshot)
    except security.PrivateStateError as exc:
        raise SpanGenerationError(
            f"embedding model snapshot is unsafe: {exc}"
        ) from exc


def _verified_utf8(
    workspace: Path,
    artifact: ingestion.CatalogArtifact,
) -> tuple[bytes, str]:
    relative = Path(artifact.relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SpanGenerationError("catalog path is unsafe")
    workspace = workspace.resolve(strict=True)
    path = Path(os.path.abspath(os.fspath(workspace / relative)))
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise SpanGenerationError("catalog path escapes the workspace") from exc
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SpanGenerationError("catalog path contains a symlink")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    data = bytearray()
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise SpanGenerationError("catalog source is not a regular file")
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            data.extend(block)
        after = os.fstat(handle.fileno())
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise SpanGenerationError("catalog source changed during read")
    if (
        digest.hexdigest() != artifact.content_sha256
        or after.st_size != artifact.byte_size
        or after.st_mtime_ns != artifact.mtime_ns
    ):
        raise SpanGenerationError("catalog source does not match the current revision")
    raw = bytes(data)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SpanGenerationError(f"source is not strict UTF-8: {exc}") from exc
    return raw, text


def _line_starts(text: str) -> list[int]:
    return [0] + [index + 1 for index, value in enumerate(text) if value == "\n"]


def _markdown_sections(text: str, starts: Sequence[int]) -> list[tuple[int, int, str | None]]:
    headings: list[tuple[int, str]] = []
    fence_marker: str | None = None
    for line_start in starts:
        line_end = text.find("\n", line_start)
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        fence = FENCE.match(line)
        if fence:
            marker = fence.group(1)[0]
            if fence_marker is None:
                fence_marker = marker
            elif fence_marker == marker:
                fence_marker = None
            continue
        if fence_marker is not None:
            continue
        match = HEADING.match(line)
        if match:
            headings.append((line_start, match.group(2).strip()))
    if not headings:
        return [(0, len(text), None)]
    sections: list[tuple[int, int, str | None]] = []
    if headings[0][0] > 0:
        sections.append((0, headings[0][0], None))
    for index, (start, heading) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        sections.append((start, end, heading))
    return sections


def _line_sections(text: str) -> list[tuple[int, int, str | None]]:
    return [(0, len(text), None)]


def _validate_structured(path: Path, text: str) -> None:
    if path.suffix.lower() == ".json":
        try:
            root_json = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise SpanGenerationError(f"invalid JSON: {exc}") from exc
        nodes = 0
        stack_json: list[tuple[Any, int]] = [(root_json, 1)]
        while stack_json:
            value, depth = stack_json.pop()
            nodes += 1
            if depth > 64 or nodes > 10_000:
                raise SpanGenerationError(
                    "structured source exceeds parser bounds"
                )
            if isinstance(value, dict):
                stack_json.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                stack_json.extend((item, depth + 1) for item in value)
        return
    try:
        import yaml
    except ImportError as exc:
        raise SpanGenerationError("PyYAML is unavailable") from exc
    nodes = 0
    try:
        roots = yaml.compose_all(text, Loader=yaml.SafeLoader)
        for root in roots:
            if root is None:
                continue
            stack: list[tuple[Any, int]] = [(root, 1)]
            while stack:
                node, depth = stack.pop()
                nodes += 1
                if depth > 64 or nodes > 10_000:
                    raise SpanGenerationError(
                        "structured source exceeds parser bounds"
                    )
                value = getattr(node, "value", None)
                if isinstance(value, list):
                    for child in value:
                        if isinstance(child, tuple):
                            stack.extend((item, depth + 1) for item in child)
                        else:
                            stack.append((child, depth + 1))
    except yaml.YAMLError as exc:
        raise SpanGenerationError(f"invalid YAML: {exc}") from exc


def _prefix(
    artifact: ingestion.CatalogArtifact,
    heading: str | None,
) -> str:
    values = [
        artifact.project,
        Path(artifact.relative_path).name,
    ]
    if heading:
        values.append(heading[:500])
    return "\n".join(value for value in values if value)


def _embedding_text(
    artifact: ingestion.CatalogArtifact,
    heading: str | None,
    content: str,
) -> str:
    prefix = _prefix(artifact, heading)
    return f"{prefix}\n\n{content}" if prefix else content


def _identifiers(value: str) -> str:
    found: list[str] = []
    seen: set[str] = set()
    for match in IDENTIFIER.finditer(value):
        token = match.group(0).casefold()
        if (
            token in seen
            or not any(character.isdigit() or not character.isalnum() for character in token)
        ):
            continue
        seen.add(token)
        found.append(token)
        if len(found) >= 64:
            break
    return " ".join(found)


def _candidate_boundaries(
    starts: Sequence[int],
    section_start: int,
    section_end: int,
) -> list[int]:
    left = bisect.bisect_right(starts, section_start)
    right = bisect.bisect_left(starts, section_end)
    return list(starts[left:right]) + [section_end]


def _largest_fitting_end(
    *,
    text: str,
    start: int,
    section_end: int,
    boundaries: Sequence[int],
    artifact: ingestion.CatalogArtifact,
    heading: str | None,
    counter: Callable[[str], int],
) -> int:
    candidates = [value for value in boundaries if value > start]
    low = 0
    high = len(candidates) - 1
    selected: int | None = None
    while low <= high:
        middle = (low + high) // 2
        end = candidates[middle]
        content = text[start:end]
        if (
            counter(content) <= TARGET_CONTENT_TOKENS
            and counter(_embedding_text(artifact, heading, content))
            <= MAX_EMBEDDING_TOKENS
        ):
            selected = end
            low = middle + 1
        else:
            high = middle - 1
    if selected is not None:
        return selected

    low = start + 1
    high = section_end
    selected = start
    while low <= high:
        middle = (low + high) // 2
        content = text[start:middle]
        if (
            counter(content) <= TARGET_CONTENT_TOKENS
            and counter(_embedding_text(artifact, heading, content))
            <= MAX_EMBEDDING_TOKENS
        ):
            selected = middle
            low = middle + 1
        else:
            high = middle - 1
    if selected <= start:
        raise SpanGenerationError("metadata prefix exhausts the token budget")
    return selected


def _overlap_start(
    *,
    text: str,
    prior_start: int,
    prior_end: int,
    starts: Sequence[int],
    counter: Callable[[str], int],
) -> int:
    left = bisect.bisect_right(starts, prior_start)
    right = bisect.bisect_left(starts, prior_end)
    candidates = list(starts[left:right])
    selected = prior_end
    for candidate in reversed(candidates):
        if counter(text[candidate:prior_end]) > OVERLAP_TOKENS:
            break
        selected = candidate
    if selected <= prior_start:
        return prior_end
    return selected


def _linear_end(
    *,
    text: str,
    section_start: int,
    section_end: int,
    cursor: int,
    boundaries: Sequence[int],
    token_offsets: Sequence[tuple[int, int]],
    token_ends: Sequence[int],
    artifact: ingestion.CatalogArtifact,
    heading: str | None,
    counter: TokenCounter,
) -> tuple[int, int, int]:
    relative_cursor = cursor - section_start
    start_token = bisect.bisect_right(token_ends, relative_cursor)
    if start_token >= len(token_offsets):
        return section_end, start_token, start_token
    # TARGET_CONTENT_TOKENS includes the tokenizer's two special tokens.
    end_token = min(
        len(token_offsets),
        start_token + max(1, TARGET_CONTENT_TOKENS - 2),
    )
    char_limit = (
        section_end
        if end_token == len(token_offsets)
        else section_start + token_offsets[end_token - 1][1]
    )
    boundary_index = bisect.bisect_right(boundaries, char_limit) - 1
    end = boundaries[boundary_index] if boundary_index >= 0 else char_limit
    if end <= cursor:
        end = char_limit
    if end <= cursor:
        raise SpanGenerationError("token offsets did not advance the span")
    content = text[cursor:end]
    if (
        counter(content) > TARGET_CONTENT_TOKENS
        or counter(_embedding_text(artifact, heading, content))
        > MAX_EMBEDDING_TOKENS
    ):
        end = _largest_fitting_end(
            text=text,
            start=cursor,
            section_end=end,
            boundaries=[
                value for value in boundaries if cursor < value <= end
            ],
            artifact=artifact,
            heading=heading,
            counter=counter,
        )
    actual_end_token = bisect.bisect_right(
        token_ends,
        end - section_start,
    )
    return end, start_token, actual_end_token


def _linear_overlap_start(
    *,
    section_start: int,
    prior_start: int,
    prior_end: int,
    start_token: int,
    end_token: int,
    token_offsets: Sequence[tuple[int, int]],
    line_starts: Sequence[int],
) -> int:
    if end_token <= start_token + 1:
        return prior_end
    overlap_token = max(start_token + 1, end_token - OVERLAP_TOKENS)
    if overlap_token >= len(token_offsets):
        return prior_end
    token_start = section_start + token_offsets[overlap_token][0]
    line_index = bisect.bisect_left(line_starts, token_start)
    if line_index < len(line_starts):
        line_start = line_starts[line_index]
        if prior_start < line_start < prior_end:
            return line_start
    if prior_start < token_start < prior_end:
        return token_start
    return prior_end


def _offset_bytes(text: str, positions: Iterable[int]) -> dict[int, int]:
    offsets: dict[int, int] = {}
    cursor = 0
    byte_cursor = 0
    for position in sorted(set(positions)):
        byte_cursor += len(text[cursor:position].encode("utf-8"))
        offsets[position] = byte_cursor
        cursor = position
    return offsets


def exact_spans(
    *,
    artifact: ingestion.CatalogArtifact,
    text: str,
    counter: TokenCounter,
    profile_digest: str,
    model_manifest_digest: str,
    generation: str,
) -> list[ExactSpan]:
    starts = _line_starts(text)
    suffix = Path(artifact.relative_path).suffix.lower()
    if suffix in STRUCTURED_SUFFIXES:
        _validate_structured(Path(artifact.relative_path), text)
    sections = (
        _markdown_sections(text, starts)
        if suffix == ".md"
        else _line_sections(text)
    )
    provisional: list[tuple[int, int, str | None, str, int, int]] = []
    for section_start, section_end, heading in sections:
        if section_end <= section_start or not text[section_start:section_end].strip():
            continue
        boundaries = _candidate_boundaries(starts, section_start, section_end)
        token_offsets = counter.offsets(text[section_start:section_end])
        token_ends = [value[1] for value in token_offsets]
        cursor = section_start
        while cursor < section_end:
            end, start_token, end_token = _linear_end(
                text=text,
                section_start=section_start,
                section_end=section_end,
                cursor=cursor,
                boundaries=boundaries,
                token_offsets=token_offsets,
                token_ends=token_ends,
                artifact=artifact,
                heading=heading,
                counter=counter,
            )
            content = text[cursor:end]
            embedding = _embedding_text(artifact, heading, content)
            content_token_count = counter(content)
            token_count = counter(embedding)
            if (
                content_token_count > TARGET_CONTENT_TOKENS
                or token_count > MAX_EMBEDDING_TOKENS
            ):
                raise SpanGenerationError("span exceeds the embedding token ceiling")
            provisional.append(
                (
                    cursor,
                    end,
                    heading,
                    embedding,
                    content_token_count,
                    token_count,
                )
            )
            if end >= section_end:
                break
            next_cursor = _linear_overlap_start(
                section_start=section_start,
                prior_start=cursor,
                prior_end=end,
                start_token=start_token,
                end_token=end_token,
                token_offsets=token_offsets,
                line_starts=starts,
            )
            cursor = next_cursor if cursor < next_cursor < end else end

    byte_offsets = _offset_bytes(
        text,
        (
            value
            for start, end, _heading, _embedding, _content_tokens, _tokens
            in provisional
            for value in (start, end)
        ),
    )
    spans: list[ExactSpan] = []
    for (
        start,
        end,
        heading,
        embedding,
        content_token_count,
        token_count,
    ) in provisional:
        content = text[start:end]
        span_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        byte_start = byte_offsets[start]
        byte_end = byte_offsets[end]
        span_identity = (
            f"{artifact.revision_id}\0{profile_digest}\0"
            f"{byte_start}:{byte_end}\0{span_sha}"
        )
        span_digest = hashlib.sha256(span_identity.encode("utf-8")).hexdigest()
        span_id = f"span:{span_digest}"
        point_identity = (
            f"{span_id}\0{model_manifest_digest}\0{generation}"
        )
        point_id = str(uuid.uuid5(POINT_NAMESPACE, point_identity))
        line_start = bisect.bisect_right(starts, start)
        line_end = bisect.bisect_right(starts, end - 1)
        spans.append(
            ExactSpan(
                point_id=point_id,
                span_id=span_id,
                char_start=start,
                char_end=end,
                byte_start=byte_start,
                byte_end=byte_end,
                line_start=line_start,
                line_end=line_end,
                heading=heading,
                identifiers=_identifiers(
                    f"{artifact.relative_path}\n{heading or ''}\n{content}"
                ),
                content=content,
                embedding_text=embedding,
                span_sha256=span_sha,
                content_tokens=content_token_count,
                embedding_tokens=token_count,
            )
        )
    return spans


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata (
          key TEXT PRIMARY KEY,
          value_json TEXT NOT NULL
        );
        CREATE TABLE artifacts (
          artifact_id TEXT PRIMARY KEY,
          revision_id TEXT NOT NULL UNIQUE,
          relative_path TEXT NOT NULL UNIQUE,
          content_sha256 TEXT NOT NULL,
          byte_size INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          artifact_type TEXT NOT NULL,
          authority_class TEXT NOT NULL,
          project TEXT NOT NULL,
          repository TEXT,
          source_scope TEXT NOT NULL,
          lifecycle_hints_json TEXT NOT NULL,
          representation TEXT NOT NULL,
          diagnostic TEXT,
          catalog_current INTEGER NOT NULL CHECK(catalog_current IN (0,1))
        );
        CREATE TABLE spans (
          row_id INTEGER PRIMARY KEY,
          point_id TEXT NOT NULL UNIQUE,
          span_id TEXT NOT NULL UNIQUE,
          artifact_id TEXT NOT NULL,
          revision_id TEXT NOT NULL,
          relative_path TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          span_sha256 TEXT NOT NULL,
          char_start INTEGER NOT NULL,
          char_end INTEGER NOT NULL,
          byte_start INTEGER NOT NULL,
          byte_end INTEGER NOT NULL,
          line_start INTEGER NOT NULL,
          line_end INTEGER NOT NULL,
          heading TEXT,
          identifiers TEXT NOT NULL,
          content TEXT NOT NULL,
          embedding_text TEXT NOT NULL,
          content_tokens INTEGER NOT NULL,
          embedding_tokens INTEGER NOT NULL,
          artifact_type TEXT NOT NULL,
          authority_class TEXT NOT NULL,
          lifecycle_hints_json TEXT NOT NULL,
          source_scope TEXT NOT NULL,
          repository TEXT,
          project TEXT NOT NULL,
          profile_id TEXT NOT NULL,
          profile_digest TEXT NOT NULL,
          collection_generation TEXT NOT NULL,
          ready INTEGER NOT NULL CHECK(ready IN (0,1)),
          catalog_current INTEGER NOT NULL CHECK(catalog_current IN (0,1)),
          FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id),
          UNIQUE(
            revision_id, profile_digest, byte_start, byte_end, span_sha256
          )
        );
        CREATE INDEX spans_filter_idx
          ON spans(project, artifact_type, authority_class);
        CREATE INDEX spans_revision_idx ON spans(revision_id);
        CREATE INDEX spans_hash_idx ON spans(content_sha256, span_sha256);
        CREATE VIRTUAL TABLE spans_fts USING fts5(
          identifiers,
          relative_path,
          heading,
          content,
          content='spans',
          content_rowid='row_id',
          detail=column,
          tokenize="unicode61 remove_diacritics 2 tokenchars '-_./:@'"
        );
        CREATE TRIGGER spans_ai AFTER INSERT ON spans BEGIN
          INSERT INTO spans_fts(
            rowid, identifiers, relative_path, heading, content
          ) VALUES (
            new.row_id, new.identifiers, new.relative_path,
            coalesce(new.heading, ''), new.content
          );
        END;
        CREATE TRIGGER spans_ad AFTER DELETE ON spans BEGIN
          INSERT INTO spans_fts(
            spans_fts, rowid, identifiers, relative_path, heading, content
          ) VALUES (
            'delete', old.row_id, old.identifiers, old.relative_path,
            coalesce(old.heading, ''), old.content
          );
        END;
        CREATE TRIGGER spans_au AFTER UPDATE OF
          identifiers, relative_path, heading, content ON spans BEGIN
          INSERT INTO spans_fts(
            spans_fts, rowid, identifiers, relative_path, heading, content
          ) VALUES (
            'delete', old.row_id, old.identifiers, old.relative_path,
            coalesce(old.heading, ''), old.content
          );
          INSERT INTO spans_fts(
            rowid, identifiers, relative_path, heading, content
          ) VALUES (
            new.row_id, new.identifiers, new.relative_path,
            coalesce(new.heading, ''), new.content
          );
        END;
        """
    )


def build_manifest(
    *,
    workspace: Path,
    catalog: Path,
    tokenizer_file: Path,
    destination: Path,
    generation: str,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve(strict=True)
    destination = destination.expanduser().absolute()
    directory = security.ensure_private_directory(destination.parent)
    temporary = directory / f".tmp-{destination.name}-{os.getpid()}-{uuid.uuid4().hex}"
    counter = TokenCounter(tokenizer_file)
    model_manifest_digest, model_files = _model_manifest(tokenizer_file.parent)
    profile_digest = _profile_digest(model_manifest_digest)
    catalog_run, artifacts = ingestion.load_current_artifacts(catalog)
    artifacts = sorted(artifacts, key=lambda value: value.relative_path)
    revision_set_digest = hashlib.sha256()
    for artifact in sorted(artifacts, key=lambda value: value.relative_path):
        revision_set_digest.update(
            (
                artifact.relative_path
                + "\0"
                + artifact.revision_id
                + "\0"
                + artifact.content_sha256
                + "\n"
            ).encode("utf-8")
        )
    connection = sqlite3.connect(temporary)
    security.secure_created_file(temporary)
    started = time.monotonic()
    span_count = represented = diagnostics = 0
    digest = hashlib.sha256()
    try:
        _create_schema(connection)
        for index, artifact in enumerate(artifacts, 1):
            diagnostic: str | None = None
            spans: list[ExactSpan] = []
            try:
                _raw, text = _verified_utf8(workspace, artifact)
                spans = exact_spans(
                    artifact=artifact,
                    text=text,
                    counter=counter,
                    profile_digest=profile_digest,
                    model_manifest_digest=model_manifest_digest,
                    generation=generation,
                )
                if not spans:
                    diagnostic = "empty-or-whitespace"
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                SpanGenerationError,
            ) as exc:
                diagnostic = f"{type(exc).__name__}: {exc}"[:1000]
            representation = "searchable" if spans else "metadata-only"
            represented += int(bool(spans))
            diagnostics += int(not spans)
            connection.execute(
                """
                INSERT INTO artifacts VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    artifact.artifact_id,
                    artifact.revision_id,
                    artifact.relative_path,
                    artifact.content_sha256,
                    artifact.byte_size,
                    artifact.mtime_ns,
                    artifact.artifact_type,
                    artifact.authority_class,
                    artifact.project,
                    artifact.repository,
                    artifact.source_scope,
                    json.dumps(list(artifact.lifecycle_hints), sort_keys=True),
                    representation,
                    diagnostic,
                    1,
                ),
            )
            for span in spans:
                values = (
                    span.point_id,
                    span.span_id,
                    artifact.artifact_id,
                    artifact.revision_id,
                    artifact.relative_path,
                    artifact.content_sha256,
                    span.span_sha256,
                    span.char_start,
                    span.char_end,
                    span.byte_start,
                    span.byte_end,
                    span.line_start,
                    span.line_end,
                    span.heading,
                    span.identifiers,
                    span.content,
                    span.embedding_text,
                    span.content_tokens,
                    span.embedding_tokens,
                    artifact.artifact_type,
                    artifact.authority_class,
                    json.dumps(list(artifact.lifecycle_hints), sort_keys=True),
                    artifact.source_scope,
                    artifact.repository,
                    artifact.project,
                    PROFILE_ID,
                    profile_digest,
                    generation,
                    1,
                    1,
                )
                cursor = connection.execute(
                    """
                    INSERT INTO spans(
                      point_id, span_id, artifact_id, revision_id, relative_path,
                      content_sha256, span_sha256, char_start, char_end,
                      byte_start, byte_end, line_start, line_end, heading,
                      identifiers, content, embedding_text, content_tokens,
                      embedding_tokens, artifact_type,
                      authority_class, lifecycle_hints_json, source_scope,
                      repository, project, profile_id, profile_digest,
                      collection_generation, ready, catalog_current
                    ) VALUES (
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    values,
                )
                row_id = int(cursor.lastrowid)
                if row_id <= 0:
                    raise SpanGenerationError("span insert returned no row ID")
                digest.update(
                    (
                        span.point_id
                        + "\0"
                        + span.span_id
                        + "\0"
                        + span.span_sha256
                        + "\n"
                    ).encode("utf-8")
                )
                span_count += 1
            if index % 100 == 0:
                connection.commit()
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "catalog_run_id": catalog_run,
            "catalog_status": "complete",
            "catalog_artifacts": len(artifacts),
            "catalog_revision_set_sha256": revision_set_digest.hexdigest(),
            "searchable_artifacts": represented,
            "metadata_only_artifacts": diagnostics,
            "spans": span_count,
            "span_manifest_digest": digest.hexdigest(),
            "profile_id": PROFILE_ID,
            "profile_digest": profile_digest,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_model_manifest_digest": model_manifest_digest,
            "embedding_model_files": model_files,
            "sqlite_version": sqlite3.sqlite_version,
            "target_content_tokens": TARGET_CONTENT_TOKENS,
            "max_embedding_tokens": MAX_EMBEDDING_TOKENS,
            "overlap_tokens": OVERLAP_TOKENS,
        }
        for key, value in metadata.items():
            connection.execute(
                "INSERT INTO metadata(key, value_json) VALUES (?, ?)",
                (key, json.dumps(value, sort_keys=True)),
            )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise SpanGenerationError(f"manifest integrity failed: {integrity}")
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise SpanGenerationError("manifest foreign-key check failed")
        connection.execute(
            "INSERT INTO spans_fts(spans_fts) VALUES('integrity-check')"
        )
        connection.close()
        descriptor = os.open(temporary, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _publish_private_manifest(temporary, destination, directory)
        return metadata | {
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "file_sha256": _sha256_file(destination),
            "built_unix": time.time(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    runtime = artifact_runtime.load_runtime()
    default_root = artifact_runtime.DEFAULT_DERIVED_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=runtime.workspace)
    parser.add_argument("--catalog", type=Path, default=runtime.catalog)
    parser.add_argument(
        "--tokenizer",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=default_root / "artifact-spans-g20260718v2.sqlite3",
    )
    parser.add_argument("--generation", default="g20260718v2")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    security.activate_private_umask()
    args = _parser().parse_args(argv)
    try:
        result = build_manifest(
            workspace=args.workspace,
            catalog=args.catalog,
            tokenizer_file=args.tokenizer,
            destination=args.destination,
            generation=args.generation,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

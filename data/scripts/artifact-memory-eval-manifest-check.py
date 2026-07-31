#!/usr/bin/env python3
"""Enforce the private, manifest-only Artifact Memory evaluation boundary.

Raw retrieval-gold suites contain private queries, current paths, revision/span
judgments, and hard negatives. They remain owner-local runtime evidence and
must never enter Git. The only permitted tracked entry under
``data/artifact-memory/evals/`` is a tiny, exact-schema manifest containing
version identifiers, counts, and SHA-256 attestations.

This gate intentionally uses only the Python standard library and resolves all
paths from its own location so it works from CI or any working directory.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "data" / "artifact-memory" / "evals"
EVALS_RELATIVE = PurePosixPath("data/artifact-memory/evals")
MANIFEST_SUFFIX = ".manifest.json"
MAX_MANIFEST_BYTES = 8 * 1024
MAX_TRACKED_JSON_BYTES = 8 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GOLD_VERSION = re.compile(r"^retrieval-gold-v[1-9][0-9]*$")
GENERATION = re.compile(r"^g[0-9]{8}v[1-9][0-9]*$")
SPLIT_METHODOLOGY = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
ADJUDICATION = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
GOLD_ARTIFACT_BASENAME = re.compile(
    r"^retrieval-gold-v[1-9][0-9]*(?:[._-].*)?$", re.IGNORECASE
)
RAW_RECORD_KEYS = frozenset(
    {
        "query",
        "query_id",
        "expected_relative_path",
        "expected_revision_id",
        "hard_negative_paths",
        "old_chunk_sha256",
    }
)
RAW_RECORD_MINIMUM_KEYS = 5
RAW_EVIDENCE_KEYS = frozenset(
    {
        "gold",
        "responses",
        "metrics",
        "gate",
        "collection_contract",
        "postflight_collection_contract",
    }
)
RAW_EVIDENCE_MINIMUM_KEYS = 5
RAW_RECORD_MARKERS = tuple(f'"{key}"' for key in RAW_RECORD_KEYS)
RAW_EVIDENCE_MARKERS = tuple(f'"{key}"' for key in RAW_EVIDENCE_KEYS)

TOP_LEVEL_KEYS = {
    "schema_version",
    "gold_version",
    "generation",
    "span_manifest_digest",
    "split_methodology",
    "adjudication",
    "splits",
}
SPLIT_KEYS = {"dev", "holdout"}
SPLIT_VALUE_KEYS = {"file", "sha256", "record_count"}


def problem(condition: bool, message: str, errors: list[str]) -> None:
    if condition:
        errors.append(message)


def is_allowed_identifier(key: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    validators = {
        "gold_version": GOLD_VERSION,
        "generation": GENERATION,
        "split_methodology": SPLIT_METHODOLOGY,
        "adjudication": ADJUDICATION,
    }
    return bool(validators[key].fullmatch(value))


def validate_payload(payload: Any, display_path: str) -> list[str]:
    """Return schema/privacy errors for one non-content manifest payload."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return [f"{display_path}: manifest must be a JSON object"]
    problem(
        set(payload) != TOP_LEVEL_KEYS,
        f"{display_path}: manifest keys must be exactly {sorted(TOP_LEVEL_KEYS)}",
        errors,
    )
    problem(
        payload.get("schema_version") != 1,
        f"{display_path}: schema_version must be 1",
        errors,
    )
    for key in ("gold_version", "generation", "split_methodology", "adjudication"):
        problem(
            not is_allowed_identifier(key, payload.get(key)),
            f"{display_path}: {key} has an invalid non-content identifier format",
            errors,
        )
    problem(
        not isinstance(payload.get("span_manifest_digest"), str)
        or not SHA256.fullmatch(payload["span_manifest_digest"]),
        f"{display_path}: span_manifest_digest must be a lowercase SHA-256",
        errors,
    )

    splits = payload.get("splits")
    if not isinstance(splits, dict):
        return [*errors, f"{display_path}: splits must be an object"]
    problem(
        set(splits) != SPLIT_KEYS,
        f"{display_path}: splits must be exactly {sorted(SPLIT_KEYS)}",
        errors,
    )
    gold_version = payload.get("gold_version")
    for split_name in sorted(SPLIT_KEYS):
        split = splits.get(split_name)
        if not isinstance(split, dict):
            errors.append(f"{display_path}: splits.{split_name} must be an object")
            continue
        problem(
            set(split) != SPLIT_VALUE_KEYS,
            f"{display_path}: splits.{split_name} keys must be exactly {sorted(SPLIT_VALUE_KEYS)}",
            errors,
        )
        expected_file = (
            f"{gold_version}.{split_name}.json"
            if isinstance(gold_version, str)
            else None
        )
        problem(
            split.get("file") != expected_file,
            f"{display_path}: splits.{split_name}.file must be {expected_file!r}",
            errors,
        )
        problem(
            not isinstance(split.get("sha256"), str)
            or not SHA256.fullmatch(split["sha256"]),
            f"{display_path}: splits.{split_name}.sha256 must be a lowercase SHA-256",
            errors,
        )
        problem(
            type(split.get("record_count")) is not int
            or split["record_count"] < 1
            or split["record_count"] > 1_000_000,
            f"{display_path}: splits.{split_name}.record_count must be 1..1000000",
            errors,
        )
    return errors


def validate_manifest(path: Path) -> list[str]:
    try:
        size = path.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            return [f"{path.relative_to(ROOT)}: manifest exceeds {MAX_MANIFEST_BYTES} bytes"]
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        return [f"{path.relative_to(ROOT)}: cannot read JSON manifest: {exc}"]
    errors = validate_payload(payload, str(path.relative_to(ROOT)))
    gold_version = payload.get("gold_version") if isinstance(payload, dict) else None
    expected_name = f"{gold_version}{MANIFEST_SUFFIX}" if isinstance(gold_version, str) else None
    if path.name != expected_name:
        errors.append(
            f"{path.relative_to(ROOT)}: filename must bind its gold_version as {expected_name!r}"
        )
    return errors


def parse_tracked_paths(raw: bytes) -> list[str]:
    """Decode Git's NUL-safe tracked-path stream without C quoting."""
    return [
        entry.decode("utf-8", "surrogateescape")
        for entry in raw.split(b"\0")
        if entry
    ]


def tracked_repo_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or "git ls-files -z failed")
    return parse_tracked_paths(result.stdout)


def is_eval_manifest_path(path: PurePosixPath) -> bool:
    return path.parent == EVALS_RELATIVE and path.name.endswith(MANIFEST_SUFFIX)


def is_within_evaluation_directory(path: PurePosixPath) -> bool:
    return EVALS_RELATIVE in path.parents


def is_gold_artifact_path(path: PurePosixPath) -> bool:
    return bool(GOLD_ARTIFACT_BASENAME.fullmatch(path.name))


def is_private_evaluation_payload(payload: Any) -> bool:
    """Identify the two private Artifact Memory evaluator payload shapes.

    Paths and filenames are useful first-line checks but are not a complete
    privacy boundary: a forced ``git add`` can relocate or rename JSON.  The
    gold record signature deliberately requires five specific fields so
    ordinary product JSON cannot resemble a private query/judgment corpus by
    accident.  The evidence signature similarly binds the raw response map to
    the release-gate and collection-contract data emitted by the evaluator.
    """
    pending = [payload]
    seen = 0
    while pending:
        value = pending.pop()
        seen += 1
        # A tracked JSON file should never approach this nesting/node count;
        # fail closed instead of allowing a deliberately huge wrapper to hide
        # a private payload from the finite structural walk.
        if seen > 100_000:
            return True
        if is_private_evaluation_record(value) or is_private_evaluation_evidence(value):
            return True
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def is_private_evaluation_record(value: Any) -> bool:
    return isinstance(value, dict) and (
        len(RAW_RECORD_KEYS.intersection(value)) >= RAW_RECORD_MINIMUM_KEYS
    )


def is_private_evaluation_evidence(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    gold = value.get("gold")
    return (
        isinstance(gold, dict)
        and {"path", "digest", "records"}.issubset(gold)
        and len(RAW_EVIDENCE_KEYS.intersection(value)) >= RAW_EVIDENCE_MINIMUM_KEYS
    )


def jsonish_text(raw: bytes) -> str | None:
    """Decode a JSON/JSONL-like payload after a bounded JSONC preamble.

    The evaluator writes UTF-8 JSON, but this protective gate must also catch
    a corpus saved with a UTF BOM, UTF-16, or a leading JSONC comment before
    structural and marker checks run.  Normal source code never becomes a
    candidate because the text left after this preamble must start with a JSON
    object or array delimiter.
    """
    encodings = ["utf-8-sig"]
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encodings = ["utf-32", "utf-16", "utf-8-sig"]
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16", "utf-8-sig"]
    elif b"\0" in raw[:64]:
        # UTF-16LE without a BOM decodes as syntactically valid UTF-8 with
        # embedded NULs.  Try wide encodings first so that representation
        # cannot suppress the structural and marker checks below.
        encodings = [
            "utf-32-le",
            "utf-32-be",
            "utf-16-le",
            "utf-16-be",
            "utf-8-sig",
        ]
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        text = text.lstrip("\ufeff \t\r\n")
        while True:
            if text.startswith("//"):
                newline = text.find("\n")
                if newline < 0:
                    return None
                text = text[newline + 1 :].lstrip("\ufeff \t\r\n")
                continue
            if text.startswith("/*"):
                ending = text.find("*/", 2)
                if ending < 0:
                    return None
                text = text[ending + 2 :].lstrip("\ufeff \t\r\n")
                continue
            break
        if text.startswith(("{", "[")):
            return text
    return None


class DuplicateJsonKeyError(ValueError):
    """A JSON object had two spellings that resolve to the same key."""


def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def strict_json_loads(raw: str) -> Any:
    return json.loads(raw, object_pairs_hook=reject_duplicate_json_keys)


def parse_json_values(raw: str) -> list[Any] | None:
    """Decode JSON or newline-delimited JSON without accepting partial data."""
    try:
        return [strict_json_loads(raw)]
    except DuplicateJsonKeyError:
        raise
    except json.JSONDecodeError:
        values: list[Any] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                values.append(strict_json_loads(line))
            except DuplicateJsonKeyError:
                raise
            except json.JSONDecodeError:
                return None
        return values or None


def validate_tracked_json_content(path: Path, display_path: str) -> list[str]:
    """Reject a renamed private evaluator payload without echoing it.

    The probe is deliberately content-led rather than suffix-led.  A raw
    corpus may be forced into Git as ``.JSON``, ``.jsonl``, ``.json.bak``, or
    with no suffix at all.  Only JSON-looking content is decoded, so source
    code that happens to mention these field names is never treated as data.
    """
    try:
        size = path.stat().st_size
        if size > MAX_TRACKED_JSON_BYTES:
            # There is no safe partial-read verdict: a renamed corpus can put
            # its JSON preamble or private records after an arbitrary amount
            # of padding.  Keep the repository source-sized so every tracked
            # file can receive a complete, deterministic privacy inspection.
            return [
                f"{display_path}: tracked data exceeds the private-evaluation "
                f"inspection ceiling of {MAX_TRACKED_JSON_BYTES} bytes"
            ]
        raw = path.read_bytes()
    except OSError:
        return []
    text = jsonish_text(raw)
    if text is None:
        return []
    record_marker_hits = sum(marker in text for marker in RAW_RECORD_MARKERS)
    evidence_marker_hits = sum(marker in text for marker in RAW_EVIDENCE_MARKERS)
    try:
        values = parse_json_values(text)
    except DuplicateJsonKeyError:
        return [
            f"{display_path}: JSON-like tracked data has duplicate keys and cannot be safely inspected"
        ]
    if values is not None and any(is_private_evaluation_payload(value) for value in values):
        return [
            f"{display_path}: private Artifact Memory evaluation data must never be tracked"
        ]
    if (
        record_marker_hits >= RAW_RECORD_MINIMUM_KEYS
        or evidence_marker_hits >= RAW_EVIDENCE_MINIMUM_KEYS
    ):
        return [
            f"{display_path}: private Artifact Memory evaluation data must never be tracked"
        ]
    return []


def check_paths(paths: list[str]) -> list[str]:
    """Check tracked-path policy independently of the Git transport."""
    errors: list[str] = []
    for rel in paths:
        path = PurePosixPath(rel)
        candidate = ROOT / rel
        errors.extend(validate_tracked_json_content(candidate, rel))
        if is_within_evaluation_directory(path):
            if not is_eval_manifest_path(path):
                errors.append(
                    f"{rel}: only an exact-schema manifest may be tracked in the evaluation directory"
                )
                continue
            errors.extend(validate_manifest(ROOT / rel))
            continue
        if is_gold_artifact_path(path):
            errors.append(
                f"{rel}: Artifact Memory gold artifacts may only be an exact-schema "
                "manifest directly under data/artifact-memory/evals"
            )
    return errors


def check_tracked() -> list[str]:
    try:
        return check_paths(tracked_repo_paths())
    except RuntimeError as exc:
        return [f"cannot enumerate tracked evaluation paths: {exc}"]


def self_test() -> int:
    valid = {
        "schema_version": 1,
        "gold_version": "retrieval-gold-v1",
        "generation": "g20260718v2",
        "span_manifest_digest": "a" * 64,
        "split_methodology": "fixed-balanced-nonblind-v1",
        "adjudication": "independent_exact_span_review",
        "splits": {
            "dev": {
                "file": "retrieval-gold-v1.dev.json",
                "sha256": "b" * 64,
                "record_count": 30,
            },
            "holdout": {
                "file": "retrieval-gold-v1.holdout.json",
                "sha256": "c" * 64,
                "record_count": 30,
            },
        },
    }
    assert validate_payload(valid, "valid") == []
    leaked = {**valid, "query": "private corpus text"}
    assert validate_payload(leaked, "leaked")
    wrong_file = json.loads(json.dumps(valid))
    wrong_file["splits"]["dev"]["file"] = "../private.json"
    assert validate_payload(wrong_file, "wrong_file")
    invalid_digest = json.loads(json.dumps(valid))
    invalid_digest["span_manifest_digest"] = "not-a-digest"
    assert validate_payload(invalid_digest, "invalid_digest")
    assert parse_tracked_paths(b"docs/line\nbreak\0normal.json\0") == [
        "docs/line\nbreak",
        "normal.json",
    ]
    assert is_eval_manifest_path(
        PurePosixPath("data/artifact-memory/evals/retrieval-gold-v1.manifest.json")
    )
    assert not is_eval_manifest_path(
        PurePosixPath("docs/retrieval-gold-v1.manifest.json")
    )
    assert is_within_evaluation_directory(
        PurePosixPath("data/artifact-memory/evals/nested/private.bin")
    )
    assert is_gold_artifact_path(PurePosixPath("docs/retrieval-gold-v1.dev.json"))
    assert is_gold_artifact_path(PurePosixPath("data/retrieval-gold-v2.json"))
    assert is_gold_artifact_path(PurePosixPath("retrieval-gold-v1.manifest.json"))
    relocated = check_paths(["docs/retrieval-gold-v1.holdout.json"])
    assert any("may only be" in error for error in relocated)
    assert is_gold_artifact_path(
        PurePosixPath("archive/retrieval-gold-v1.results.json")
    )
    misplaced = check_paths(["data/artifact-memory/evals/private-results.json"])
    assert any("only an exact-schema manifest" in error for error in misplaced)
    nested = check_paths(["data/artifact-memory/evals/nested/private.bin"])
    assert any("only an exact-schema manifest" in error for error in nested)
    raw_record = {
        "query": "private corpus text",
        "query_id": "dev-001",
        "expected_relative_path": "private/path",
        "expected_revision_id": "a" * 64,
        "hard_negative_paths": ["private/negative"],
        "old_chunk_sha256": "b" * 64,
    }
    assert is_private_evaluation_payload([raw_record])
    raw_results = {
        "gold": {"path": "/private/gold.json", "digest": "c" * 64, "records": 1},
        "responses": {"dev-001": {"results": []}},
        "metrics": {},
        "gate": {},
        "collection_contract": {},
        "postflight_collection_contract": {},
    }
    assert is_private_evaluation_payload(raw_results)
    assert is_private_evaluation_payload({"wrapper": raw_results})
    assert not is_private_evaluation_payload(valid)
    with tempfile.TemporaryDirectory() as directory:
        directory_path = Path(directory)
        for name, content in (
            ("private-eval.JSON", json.dumps({"records": [raw_record]})),
            ("private-eval.jsonl", json.dumps(raw_record)),
            ("private-eval.json.bak", json.dumps({"records": [raw_record]})),
            ("private-eval", json.dumps({"records": [raw_record]})),
            ("private-eval-bom", b"\xef\xbb\xbf" + json.dumps(raw_results).encode("utf-8")),
            ("private-eval-utf16", json.dumps(raw_results).encode("utf-16")),
            ("private-eval-utf16le", json.dumps(raw_results).encode("utf-16-le")),
            ("private-eval-utf32le", json.dumps(raw_results).encode("utf-32-le")),
            ("private-eval-jsonc", "// owner-local evidence\n" + json.dumps(raw_results)),
            ("private-eval-nested", json.dumps({"wrapper": raw_results})),
            (
                "private-eval-duplicate",
                '{"rec\\u006frds":['
                + json.dumps(raw_record)
                + '],"records":[]}',
            ),
            (
                "private-eval-malformed",
                "{" + ",".join(marker + ":null" for marker in RAW_RECORD_MARKERS),
            ),
        ):
            relocated_payload = directory_path / name
            if isinstance(content, bytes):
                relocated_payload.write_bytes(content)
            else:
                relocated_payload.write_text(content, encoding="utf-8")
            relocated_errors = validate_tracked_json_content(
                relocated_payload,
                f"docs/{name}",
            )
            assert any(
                "must never be tracked" in error or "duplicate keys" in error
                for error in relocated_errors
            )
        oversized = directory_path / "private-eval-oversized"
        oversized.touch()
        oversized.chmod(0o600)
        with oversized.open("r+b", encoding="utf-8") as handle:
            handle.write(b" ")
            handle.truncate(MAX_TRACKED_JSON_BYTES + 1)
        oversized_errors = validate_tracked_json_content(
            oversized,
            "docs/private-eval-oversized",
        )
        assert any("inspection ceiling" in error for error in oversized_errors)
    print("artifact-memory-eval-manifest-check self-test: OK")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--check-tracked",
        action="store_true",
        help="validate the manifest-only evaluation directory and reject raw suites repo-wide",
    )
    parser.add_argument(
        "--check-path",
        type=Path,
        action="append",
        default=[],
        help="validate a manifest path (may be untracked)",
    )
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()

    errors: list[str] = []
    if args.check_tracked or not args.check_path:
        errors.extend(check_tracked())
    for path in args.check_path:
        candidate = path if path.is_absolute() else ROOT / path
        errors.extend(validate_manifest(candidate))
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if errors:
        return 1
    print("artifact-memory-eval-manifest-check: manifest-only evaluation boundary is clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

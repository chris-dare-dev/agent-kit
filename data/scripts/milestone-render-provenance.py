#!/usr/bin/env python3
"""Emit machine-owned source/target/artifact provenance in a rendered repo.

CI/renderers call this before committing generated GitOps output. The resulting
`.workspace/source-revision.json` is read from the live rendered commit by the
milestone-pipeline v2 publication gate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProvenanceError(Exception):
    pass


def nonempty(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ProvenanceError(f"{label}: expected non-empty string")
    return value


def load_artifacts(path: Path | None, target_ids: set[str]) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"--artifacts: cannot read JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ProvenanceError("--artifacts: root must be an array")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for i, item in enumerate(value):
        label = f"--artifacts[{i}]"
        if not isinstance(item, dict) or set(item) != {"uri", "digest", "target_ids"}:
            raise ProvenanceError(f"{label}: expected exactly uri/digest/target_ids")
        uri = nonempty(str(item["uri"]), f"{label}.uri")
        digest = nonempty(str(item["digest"]), f"{label}.digest")
        if not DIGEST_RE.fullmatch(digest) or not uri.endswith(f"@{digest}"):
            raise ProvenanceError(f"{label}: URI must be qualified by its sha256 digest")
        raw_targets = item["target_ids"]
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ProvenanceError(f"{label}.target_ids: expected non-empty array")
        normalized = tuple(sorted(nonempty(str(v), f"{label}.target_ids") for v in raw_targets))
        if len(normalized) != len(set(normalized)):
            raise ProvenanceError(f"{label}.target_ids: duplicate target")
        unknown = sorted(set(normalized) - target_ids)
        if unknown:
            raise ProvenanceError(f"{label}.target_ids: outside rendered target set: {unknown}")
        key = (uri, digest, normalized)
        if key in seen:
            raise ProvenanceError(f"{label}: duplicate artifact")
        seen.add(key)
        result.append({"uri": uri, "digest": digest, "target_ids": list(normalized)})
    return sorted(result, key=lambda item: (item["uri"], item["digest"], item["target_ids"]))


def build(source_repo: str, source_commit: str, targets: list[str], artifacts: Path | None) -> dict[str, Any]:
    repo = nonempty(source_repo, "--source-repo")
    commit = nonempty(source_commit, "--source-commit").lower()
    if not COMMIT_RE.fullmatch(commit):
        raise ProvenanceError("--source-commit: expected full/abbreviated hexadecimal commit")
    target_ids = sorted({nonempty(value, "--target-id") for value in targets})
    if not target_ids:
        raise ProvenanceError("--target-id: provide at least one deployment target")
    return {
        "source_repo": repo,
        "source_commit": commit,
        "target_ids": target_ids,
        "artifacts": load_artifacts(artifacts, set(target_ids)),
    }


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def self_test() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        digest = "sha256:" + "a" * 64
        artifacts = root / "artifacts.json"
        artifacts.write_text(json.dumps([{
            "uri": f"registry.example/app@{digest}", "digest": digest,
            "target_ids": ["dev/app"],
        }], encoding="utf-8"), encoding="utf-8")
        value = build("repo", "b" * 40, ["dev/app"], artifacts)
        output = root / ".workspace/source-revision.json"
        write_atomic(output, value)
        assert json.loads(output.read_text(encoding="utf-8")) == value
    print("milestone-render-provenance self-test: OK")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return self_test()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--target-id", action="append", default=[], required=True)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".workspace/source-revision.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        value = build(args.source_repo, args.source_commit, args.target_id, args.artifacts)
        if args.check:
            current = json.loads(args.output.read_text(encoding="utf-8"))
            if current != value:
                raise ProvenanceError(f"{args.output}: stale provenance")
        else:
            write_atomic(args.output, value)
    except (ProvenanceError, OSError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

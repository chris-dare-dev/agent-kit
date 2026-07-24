#!/usr/bin/env python3
"""Validate the per-provider model resolution maps in data/model-policy.json.

Every semantic class must resolve to a concrete model for all three providers
(claude / codex / opencode), so the adapter packs (S3.4/S3.5) and any runtime
can stamp the right model per provider WITHOUT a raw alias leaking into a
consumer. Model-only is required (effort is per-provider and added as the packs
consume these); claude entries additionally carry effort, mirroring the class.

Stdlib only. CWD-independent. Exit 0 clean | 1 incomplete/malformed.

Usage: python3 data/scripts/model-policy-providers-check.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "data" / "model-policy.json"
REQUIRED_PROVIDERS = ("claude", "codex", "opencode")


def validate(classes: dict) -> list[str]:
    """Return a list of failure strings ([] = clean)."""
    failures: list[str] = []
    for name, cls in classes.items():
        providers = cls.get("providers")
        if not isinstance(providers, dict):
            failures.append(f"class '{name}' has no providers map")
            continue
        for prov in REQUIRED_PROVIDERS:
            entry = providers.get(prov)
            if not isinstance(entry, dict) or not entry.get("model"):
                failures.append(f"class '{name}' provider '{prov}' missing a model")
        # providers.claude MUST equal the class's own model/effort — the class is
        # the single source of truth; a stale providers.claude would make the
        # Claude adapter pack stamp a different model than the live fleet runs.
        claude = providers.get("claude")
        if isinstance(claude, dict):
            if claude.get("model") != cls.get("model"):
                failures.append(
                    f"class '{name}' providers.claude.model={claude.get('model')!r} "
                    f"!= class model {cls.get('model')!r}"
                )
            if claude.get("effort") != cls.get("effort"):
                failures.append(
                    f"class '{name}' providers.claude.effort={claude.get('effort')!r} "
                    f"!= class effort {cls.get('effort')!r}"
                )
    return failures


def self_test() -> int:
    good = {"c": {"model": "sonnet", "effort": "high",
                  "providers": {"claude": {"model": "sonnet", "effort": "high"},
                                "codex": {"model": "gpt-5.5"},
                                "opencode": {"model": "x/y"}}}}
    assert validate(good) == [], validate(good)
    # stale providers.claude must be caught
    bad = {"c": {"model": "fable", "effort": "high",
                 "providers": {"claude": {"model": "sonnet", "effort": "high"},
                               "codex": {"model": "g"}, "opencode": {"model": "x"}}}}
    assert any("providers.claude.model" in f for f in validate(bad)), "equality check missed drift"
    # missing provider must be caught
    miss = {"c": {"model": "haiku", "providers": {"claude": {"model": "haiku"}}}}
    assert any("codex" in f for f in validate(miss)), "presence check missed a gap"
    print("model-policy-providers-check self-test: OK")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    try:
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: cannot read {POLICY}: {exc}", file=sys.stderr)
        return 1

    classes = policy.get("classes", {})
    if not classes:
        print("FAIL: no classes in model-policy.json", file=sys.stderr)
        return 1

    failures = validate(classes)
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        print(f"{len(failures)} provider-map failure(s)", file=sys.stderr)
        return 1
    print(f"model-policy providers: all {len(classes)} classes resolve for {', '.join(REQUIRED_PROVIDERS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

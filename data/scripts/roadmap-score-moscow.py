#!/usr/bin/env python3
"""Validate MoSCoW bucketing — Musts must not exceed 60% of total epics.

Usage:
  score-moscow.py <input.json>          # validate from a file
  score-moscow.py -                      # validate from stdin
  score-moscow.py --example              # print an example input + validation

Input JSON: a list of objects, each with:
  {
    "id": "E1",
    "title": "...",          # optional
    "bucket": "Must" | "Should" | "Could" | "Won't"
  }

Validation rules:
  1. Must bucket ≤ 60% of total items.
  2. Total Won't items NOT counted toward the denominator (they're cut from this horizon).
  3. Every item must have a valid bucket.

Exit code 0 if valid; 1 with warnings if Must cap violated; 2 on input error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from collections import Counter


VALID_BUCKETS = {"Must", "Should", "Could", "Won't"}
MUST_CAP = 0.60

EXAMPLE = [
    {"id": "E1", "title": "Add Kiali multi-cluster secret", "bucket": "Must"},
    {"id": "E2", "title": "Migrate Keycloak v26", "bucket": "Must"},
    {"id": "E3", "title": "Add per-namespace cost dashboard", "bucket": "Should"},
    {"id": "E4", "title": "IRSA scaffolding", "bucket": "Should"},
    {"id": "E5", "title": "Update docs", "bucket": "Could"},
    {"id": "E6", "title": "Per-pod cost", "bucket": "Won't"},
]


def validate_buckets(items: list[dict]) -> None:
    seen = set()
    for i, it in enumerate(items):
        if "id" not in it:
            sys.exit(f"item {i}: missing 'id'")
        if it["id"] in seen:
            sys.exit(f"duplicate id: {it['id']}")
        seen.add(it["id"])
        if "bucket" not in it:
            sys.exit(f"item {it['id']}: missing 'bucket'")
        if it["bucket"] not in VALID_BUCKETS:
            sys.exit(
                f"item {it['id']}: bucket '{it['bucket']}' invalid. "
                f"Valid: {', '.join(sorted(VALID_BUCKETS))}"
            )


def report(items: list[dict]) -> int:
    counts = Counter(it["bucket"] for it in items)
    total = len(items)
    wont_key = "Won't"
    n_must = counts["Must"]
    n_should = counts["Should"]
    n_could = counts["Could"]
    n_wont = counts[wont_key]
    in_scope = total - n_wont

    must_pct = (n_must / in_scope * 100) if in_scope else 0
    should_pct = (n_should / in_scope * 100) if in_scope else 0
    could_pct = (n_could / in_scope * 100) if in_scope else 0

    print(f"Total items: {total} ({in_scope} in scope, {n_wont} cut)")
    print()
    print(f"  Must:    {n_must:>3}  ({must_pct:>5.1f}% of in-scope)")
    print(f"  Should:  {n_should:>3}  ({should_pct:>5.1f}% of in-scope)")
    print(f"  Could:   {n_could:>3}  ({could_pct:>5.1f}% of in-scope)")
    print(f"  Won't:   {n_wont:>3}  (cut from this horizon)")
    print()

    must_ratio = n_must / in_scope if in_scope else 0
    if must_ratio > MUST_CAP:
        print(
            f"VIOLATION: Musts at {must_ratio*100:.1f}% exceed cap of {MUST_CAP*100:.0f}%."
        )
        print("  Re-bucket. For each Must, ask: 'if we shipped without this,")
        print("  would the goal fail?' If no -> Should.")
        print()
        print("Musts to re-evaluate:")
        for it in items:
            if it["bucket"] == "Must":
                print(f"  {it['id']}: {it.get('title','')}")
        return 1
    else:
        print(
            f"OK: Musts at {must_ratio*100:.1f}% within cap ({MUST_CAP*100:.0f}%)."
        )
        return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    if argv[1] == "--example":
        print("Example input JSON:")
        print(json.dumps(EXAMPLE, indent=2))
        print()
        print("Validation:")
        return report(EXAMPLE)

    if argv[1] == "-":
        raw = sys.stdin.read()
    else:
        path = Path(argv[1])
        if not path.exists():
            print(f"file not found: {path}", file=sys.stderr)
            return 2
        raw = path.read_text(encoding="utf-8")

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"invalid JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(items, list):
        print("input must be a JSON array", file=sys.stderr)
        return 2

    validate_buckets(items)
    return report(items)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

#!/usr/bin/env python3
"""Rank epics by RICE score.

Usage:
  score-rice.py <input.json>          # rank from a file
  score-rice.py -                      # rank from stdin
  score-rice.py --example              # print an example input + ranked output

Input JSON: a list of objects, each with:
  {
    "id": "E1",                  # required, string
    "title": "...",              # optional, for display
    "reach": 1-10,               # required, int
    "impact": 1-10,              # required, int
    "confidence": 1-10,          # required, int. Default to 5 (50%) without evidence.
    "effort": 1-10               # required, int. 1 = ≤ 1 week, 5 = ~1 month, 10 = ≤ 1 quarter.
  }

RICE = (reach * impact * confidence) / effort. Higher = higher priority.

Output: a ranked table (text) printed to stdout, plus a JSON dump (one per line)
of the ranked items at the end for machine consumption.

Confidence calibration warning: if any item has confidence > 7, prints a
reminder that confidence > 7 should be backed by evidence.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


EXAMPLE = [
    {"id": "E1", "title": "Add Kiali multi-cluster secret for tenant-acme",
     "reach": 8, "impact": 7, "confidence": 6, "effort": 3},
    {"id": "E2", "title": "Migrate Keycloak realm-import to v26 schema",
     "reach": 9, "impact": 9, "confidence": 4, "effort": 6},
    {"id": "E3", "title": "Add per-namespace cost dashboard",
     "reach": 6, "impact": 8, "confidence": 7, "effort": 4},
    {"id": "E4", "title": "Establish IRSA scaffolding for cost-visibility-l3",
     "reach": 4, "impact": 5, "confidence": 8, "effort": 2},
]


def validate(items: list[dict]) -> None:
    seen = set()
    for i, it in enumerate(items):
        if "id" not in it:
            sys.exit(f"item {i}: missing 'id'")
        if it["id"] in seen:
            sys.exit(f"duplicate id: {it['id']}")
        seen.add(it["id"])
        for f in ("reach", "impact", "confidence", "effort"):
            if f not in it:
                sys.exit(f"item {it['id']}: missing '{f}'")
            v = it[f]
            if not isinstance(v, (int, float)) or v < 1 or v > 10:
                sys.exit(f"item {it['id']}: '{f}' must be a number in [1, 10]")
        if it["effort"] == 0:
            sys.exit(f"item {it['id']}: effort cannot be 0")


def score(item: dict) -> float:
    return (item["reach"] * item["impact"] * item["confidence"]) / item["effort"]


def rank(items: list[dict]) -> list[dict]:
    for it in items:
        it["rice"] = round(score(it), 2)
    return sorted(items, key=lambda x: x["rice"], reverse=True)


def print_table(items: list[dict]) -> None:
    print(
        f"{'rank':<5} {'id':<8} {'R':>3} {'I':>3} {'C':>3} {'E':>3} {'RICE':>8}  title"
    )
    print("-" * 80)
    for rank_, it in enumerate(items, start=1):
        title = it.get("title", "")
        if len(title) > 48:
            title = title[:45] + "..."
        print(
            f"{rank_:<5} {it['id']:<8} {it['reach']:>3} {it['impact']:>3} "
            f"{it['confidence']:>3} {it['effort']:>3} {it['rice']:>8.2f}  {title}"
        )


def warn_confidence(items: list[dict]) -> None:
    high_conf = [it for it in items if it["confidence"] > 7]
    if high_conf:
        print()
        print(
            f"Note: {len(high_conf)} item(s) have confidence > 7. "
            "Confidence > 7 should be backed by evidence (data, prior incident, "
            "explicit prior validation). Otherwise default to 5 (50%)."
        )
        for it in high_conf:
            print(f"  {it['id']} (confidence={it['confidence']}): {it.get('title','')}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    if argv[1] == "--example":
        print("Example input JSON:")
        print(json.dumps(EXAMPLE, indent=2))
        print()
        print("Ranked output:")
        ranked = rank(EXAMPLE.copy())
        print_table(ranked)
        warn_confidence(ranked)
        return 0

    if argv[1] == "-":
        raw = sys.stdin.read()
    else:
        path = Path(argv[1])
        if not path.exists():
            print(f"file not found: {path}", file=sys.stderr)
            return 1
        raw = path.read_text(encoding="utf-8")

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"invalid JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(items, list):
        print("input must be a JSON array", file=sys.stderr)
        return 1

    validate(items)
    ranked = rank(items)
    print_table(ranked)
    warn_confidence(ranked)

    # Machine-consumption tail: one JSON per line, ranked order.
    print()
    print("--- JSON (ranked) ---")
    for it in ranked:
        print(json.dumps(it))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

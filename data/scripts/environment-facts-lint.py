#!/usr/bin/env python3
"""Drift guard between the structured platform facts and their reference docs.

`get_environment_map` / `get_platform_architecture` now read the structured
`data/facts/*.json` files (not TypeScript string literals), so the tools can no
longer silently lag `data/references/environment-map.md`. This lint makes that
guarantee *enforceable*: it fails the `data-lint` CI job when the JSON facts and
the reference doc disagree on the hard, unambiguous values, and when a
previously-fixed stale fact (prod sync "Manual", `eks-*` IRSA pattern, `-dev`
dev contexts) regresses.

Stdlib only (runs in the python:3.12-slim data-lint image). CWD-independent:
paths resolve relative to this file. Exit 0 = clean, 1 = drift/regression.

Usage: python3 data/scripts/environment-facts-lint.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # data/scripts/x.py -> repo root
ENV_JSON = ROOT / "data" / "facts" / "environment-map.json"
ARCH_JSON = ROOT / "data" / "facts" / "platform-architecture.json"
ENV_MD = ROOT / "data" / "references" / "environment-map.md"


def main() -> int:
    failures: list[str] = []

    try:
        env = json.loads(ENV_JSON.read_text(encoding="utf-8"))
        arch = json.loads(ARCH_JSON.read_text(encoding="utf-8"))
        md = ENV_MD.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: cannot read facts/reference: {exc}", file=sys.stderr)
        return 1

    environments = env.get("environments", {})
    clusters = env.get("clusters", {})

    # 1. Every account ID and domain in the JSON must appear verbatim in the doc.
    for name, info in environments.items():
        for field in ("accountId", "domain"):
            val = info.get(field, "")
            if val and val not in md:
                failures.append(
                    f"environments.{name}.{field}='{val}' is not present in "
                    f"environment-map.md (JSON and reference disagree)"
                )

    # 2. Prod sync must be Automated, never the old stale "Manual".
    prod_sync = environments.get("prod", {}).get("syncPolicy", "")
    if "Automated" not in prod_sync:
        failures.append(
            f"environments.prod.syncPolicy='{prod_sync}' must be Automated "
            f"(prod auto-deploys — regressing to 'Manual' is the stale fact this fixed)"
        )
    if prod_sync.strip().lower() == "manual":
        failures.append("environments.prod.syncPolicy regressed to 'Manual'")

    # 3. dev contexts are `-commercial`, never the old stale `-dev` aliases.
    for c in clusters.get("dev", []):
        ctx = c.get("context", "")
        if not ctx.endswith("-commercial"):
            failures.append(
                f"clusters.dev context '{ctx}' must end with '-commercial' "
                f"(the '-dev' alias form is stale)"
            )

    # 4. Deploy branch must be a branch ArgoCD actually watches (dev/stage/main).
    #    A stale env-named branch (e.g. test's old "test") is a silent no-op on
    #    the promotion path — the reference doc gives test's deploy branch as dev.
    valid_branches = {"dev", "stage", "main"}
    for name, info in environments.items():
        br = info.get("branch", "")
        if br not in valid_branches:
            failures.append(
                f"environments.{name}.branch='{br}' is not an ArgoCD-watched "
                f"branch (dev/stage/main) — a stale env-named branch renders nothing"
            )

    # 5. IRSA convention must be the current `platform-*` pattern, never `eks-*`.
    irsa = next(
        (k for k in arch.get("keyConventions", []) if "IRSA" in k or "irsa" in k), ""
    )
    if "platform-" not in irsa:
        failures.append(
            f"platform-architecture IRSA convention '{irsa}' must use the "
            f"'platform-{{clusterShort}}-{{service}}-role-{{env}}' pattern"
        )
    if "eks-{service}" in irsa or "`eks-" in irsa:
        failures.append("platform-architecture IRSA convention regressed to the stale 'eks-*' form")

    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        print(f"{len(failures)} drift/regression failure(s)", file=sys.stderr)
        return 1
    print("environment-facts-lint: facts and references agree (clean)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

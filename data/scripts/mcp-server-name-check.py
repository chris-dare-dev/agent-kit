#!/usr/bin/env python3
"""mcp-server-name-check.py -- every mcp__<server>__ grant must name a real server.

The shipped registration templates used to register this kit as
``workspace-platform`` at a pre-genericization employer path, while the README
registered it as ``agent-kit`` and 28 tool grants across data/ named
``mcp__agent-kit__*``. ``git grep mcp__workspace-platform`` returned nothing, so
every one of those grants referred to a server no template created: the packs
shipped granting tools that could never resolve, and nothing noticed.

This gate closes that loop in both directions:

  * every ``mcp__<server>__`` prefix in data/**/*.md must be a server key
    present in EVERY shipped registration template, and
  * this kit's own server must be registered in every template.

It is also how a rename is made safe. If the naming decision changes the server
key, this exits non-zero and names each file still carrying the old grant.

Usage:
    python3 data/scripts/mcp-server-name-check.py --check
    python3 data/scripts/mcp-server-name-check.py --check --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"

GRANT_RE = re.compile(r"mcp__([A-Za-z0-9][A-Za-z0-9._-]*)__")

# Grants known to resolve to no shipped template, each with the issue that
# removes it. These are reported as WARN, not FAIL, so this gate can enter CI
# green while the debt stays visible and attributed. Adding an entry here is a
# deliberate act with an owner — it is not a way to silence the check.
KNOWN_UNRESOLVED = {
    "GitLab": (
        "employer GitLab MCP server; the fleet tracks work on GitHub now. "
        "Removed by issue #37 (purge employer identifiers from shipped content)."
    ),
}

# Every template that registers MCP servers, and how to read its server keys.
TEMPLATES = {
    "data/scripts/template-mcp.json": ("json", ("mcpServers",)),
    "data/scripts/template-opencode.json": ("json", ("mcp",)),
    "data/scripts/template-codex-config.toml": ("toml", ("mcp_servers",)),
}


def server_keys(path: Path, kind: str, section: tuple[str, ...]) -> set[str]:
    if kind == "json":
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    for key in section:
        payload = payload.get(key, {}) if isinstance(payload, dict) else {}
    return {k for k in payload} if isinstance(payload, dict) else set()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit non-zero on any mismatch")
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args(argv)

    problems: list[str] = []
    per_template: dict[str, set[str]] = {}

    for rel, (kind, section) in TEMPLATES.items():
        path = REPO_ROOT / rel
        if not path.exists():
            problems.append(f"{rel}: template is missing")
            continue
        try:
            per_template[rel] = server_keys(path, kind, section)
        except Exception as exc:  # noqa: BLE001 - report, never crash the gate
            problems.append(f"{rel}: could not be parsed ({exc})")

    if not per_template:
        print("FAIL: no registration templates could be read", file=sys.stderr)
        return 2

    # A grant is only satisfiable if EVERY template registers that server —
    # a grant that resolves under Claude but not Codex is still a broken pack.
    common = set.intersection(*per_template.values()) if per_template else set()

    grants: dict[str, list[str]] = {}
    for md in sorted(DATA_DIR.rglob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        for server in GRANT_RE.findall(text):
            grants.setdefault(server, []).append(str(md.relative_to(REPO_ROOT)).replace("\\", "/"))

    warnings: list[str] = []
    for server, files in sorted(grants.items()):
        if server in common:
            continue
        where = [rel for rel, keys in per_template.items() if server in keys]
        detail = f"registered only in {', '.join(where)}" if where else "registered in no template"
        message = (
            f"grant mcp__{server}__ is {detail}; used in "
            f"{len(set(files))} file(s): " + ", ".join(sorted(set(files))[:4])
        )
        if server in KNOWN_UNRESOLVED:
            warnings.append(f"{message}\n         known: {KNOWN_UNRESOLVED[server]}")
        else:
            problems.append(message)

    for rel, keys in per_template.items():
        if not keys:
            problems.append(f"{rel}: registers no MCP server at all")

    if args.as_json:
        print(json.dumps(
            {"templates": {k: sorted(v) for k, v in per_template.items()},
             "grants": {k: sorted(set(v)) for k, v in grants.items()},
             "problems": problems},
            indent=2))
    else:
        for rel, keys in per_template.items():
            print(f"  {rel}: {', '.join(sorted(keys)) or '(none)'}")
        print(f"  grants found: {', '.join(f'mcp__{g}__' for g in sorted(grants)) or '(none)'}")
        for warning in warnings:
            print(f"  WARN: {warning}")
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)

    if problems:
        print(f"\n{len(problems)} problem(s)", file=sys.stderr)
        return 2 if args.check else 0
    suffix = f" ({len(warnings)} known-unresolved grant(s) warned)" if warnings else ""
    print(f"\nOK: every grant resolves to a server registered in every template{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

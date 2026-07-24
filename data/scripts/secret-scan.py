#!/usr/bin/env python3
"""secret-scan.py — repo-wide plaintext-credential scan (S1.3, Gate 1n + pre-push).

Stdlib-only (re, json, subprocess, argparse) so it runs in the bare data-lint
job before any pip install. Scans every git-TRACKED text file (git ls-files —
gitignored tiers like .claude/notes/ and plans/ are out of scope by
construction) with the two heuristics proven in the PreToolUse guard:

  1. Known credential token shapes (Atlassian ATATT, GitHub ghp_/gho_/
     github_pat_, GitLab glpat-, AWS AKIA, Slack xox*, OpenAI/Anthropic sk-*,
     PEM private-key header) — applied to ALL tracked text files.
  2. A secret-named key (token|secret|password|api_key|access_key) assigned a
     NON-indirection literal >=12 chars — applied only to CONFIG-SHAPED files
     (.json/.toml/.yaml/.yml/.env/.ini/.properties/.tfvars): that is where
     credentials get planted, and prose/code mentioning "token: <value>"
     placeholders would otherwise produce a false-positive storm.

PATTERN-DRIFT NOTE: these regexes are a Python port of
data/hooks/block-plaintext-secret-write.sh (the live PreToolUse guard). Edit a
pattern THERE -> edit it HERE too, or the live guard and the CI scan silently
diverge. Full single-source unification is a flagged Next-lane cleanup.

Allowlist: data/scripts/secret-scan-allowlist.json — a git-tracked, reviewed
list of {path, snippet, reason} entries (fingerprint-style, modeled on
.gitleaksignore). An entry suppresses a finding only when BOTH the path matches
AND the literal snippet occurs in the flagged line — so a real leak landing in
an allowlisted FILE is still caught unless its content matches too. The
allowlist file itself is skipped for NORMAL findings (its snippets ARE the fake
fixtures) but still token-shape-scanned: any token shape on one of its lines
that is NOT among its own registered snippets hard-fails (V-M4 — the exception
registry must not become the one tracked file a real credential can hide in).

A finding means a HUMAN must rotate the credential — this script never will.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_REL = "data/scripts/secret-scan-allowlist.json"

# Heuristic 1 — mirror of the guard's token-shape grep (keep in sync).
TOKEN_SHAPES = re.compile(
    r"ATATT3xFfGF0|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|sk-(?:ant|proj)-[A-Za-z0-9-]{20,}|sk-[A-Za-z0-9]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)

# Heuristic 2 — mirror of the guard's secret-named-key grep (keep in sync).
# First value char may not be `$` or `{` so `${VAR}` indirection stays legal.
KEY_ASSIGN = re.compile(
    r'(?i)(?:token|secret|password|api[_-]?key|access[_-]?key)"?\s*[:=]\s*"?'
    r'[^"$\s{][^"\s]{11,}'
)

# DELIBERATE SCOPE DECISION (implementation deviation #3, upheld at
# rectification — V-M4): heuristic 2 (secret-named key = literal) runs ONLY on
# these config-shaped suffixes. Running it on prose/code (.md/.sh/.py/.ts)
# produces a false-positive storm on docs that merely MENTION `token: <value>`.
# The repo-wide backstop for non-config files is heuristic 1 (known vendor
# token shapes); a NON-vendor-shaped secret in a non-config tracked file is a
# known, accepted boundary of this scan — not an oversight.
CONFIG_SUFFIXES = {
    ".json", ".toml", ".yaml", ".yml", ".env", ".ini", ".properties", ".tfvars",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".gz",
    ".tgz", ".tar", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".lockb",
    ".node", ".wasm", ".mp4", ".mp3",
}


def load_allowlist(root: Path):
    path = root / ALLOWLIST_REL
    if not path.is_file():
        return []
    entries = json.loads(path.read_text(encoding="utf-8"))
    for e in entries:
        if not isinstance(e, dict) or "path" not in e or "snippet" not in e:
            sys.exit(f"FAIL: malformed allowlist entry (need path+snippet): {e!r}")
    return entries


def allowlisted(entries, rel_path: str, line: str) -> bool:
    return any(e["path"] == rel_path and e["snippet"] in line for e in entries)


def mask(match_text: str) -> str:
    """First 6 chars + length ONLY (L2): a finding must never re-print the full
    credential — CI job logs have their own retention and outlive a git-history
    purge, so a fully-printed short token (glpat-/AKIA fit in 60 chars) would
    survive remediation."""
    return f"{match_text[:6]}…({len(match_text)} chars)"


def scan_text(rel_path: str, text: str, entries) -> list[tuple[int, str, str]]:
    """Return (lineno, heuristic, matched_span) findings for one file's
    content. The third element is the MATCH (not the whole line) so the
    printer can mask it — see mask()."""
    findings = []
    is_config = Path(rel_path).suffix.lower() in CONFIG_SUFFIXES
    for lineno, line in enumerate(text.splitlines(), 1):
        hit = None
        m = TOKEN_SHAPES.search(line)
        if m:
            hit = "token-shape"
        elif is_config:
            m = KEY_ASSIGN.search(line)
            if m:
                hit = "secret-named-key"
        if hit and not allowlisted(entries, rel_path, line):
            findings.append((lineno, hit, m.group(0)))
    return findings


def scan_allowlist_file(text: str, entries) -> list[tuple[int, str, str]]:
    """Token-shape scan of the allowlist file ITSELF (V-M4). It is skipped for
    normal findings (its snippets are reviewed fake fixtures), but a token
    shape on any line that is NOT one of its own registered snippets is a
    hard-fail — an unregistered live-shaped token must not hide in the one
    file the scanner otherwise never reads."""
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        m = TOKEN_SHAPES.search(line)
        if m and not any(e["snippet"] in line for e in entries):
            findings.append((lineno, "unregistered-token-in-allowlist", m.group(0)))
    return findings


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.split("\0") if f]


def check(root: Path) -> int:
    entries = load_allowlist(root)
    findings = []
    for rel in tracked_files(root):
        if rel == ALLOWLIST_REL:
            # The reviewed exception registry: skipped for normal findings (its
            # snippets ARE fake fixtures), but token-shape-scanned for any
            # UNREGISTERED shape so a real credential cannot hide here (V-M4).
            try:
                text = (root / rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, heuristic, match_text in scan_allowlist_file(text, entries):
                findings.append((rel, lineno, heuristic, match_text))
            continue
        p = root / rel
        if p.suffix.lower() in BINARY_SUFFIXES or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable — token shapes are ASCII text
        for lineno, heuristic, match_text in scan_text(rel, text, entries):
            findings.append((rel, lineno, heuristic, match_text))

    if not findings:
        print(f"secret-scan: {len(tracked_files(root))} tracked files clean ✓")
        return 0

    print(f"✗ secret-scan: {len(findings)} potential plaintext credential(s) in tracked files:",
          file=sys.stderr)
    for rel, lineno, heuristic, match_text in findings:
        # Masked span only (L2) — file:line locates it; the log never carries
        # the full literal (short tokens previously fit inside the old 60-char
        # line truncation).
        print(f"    {rel}:{lineno}: [{heuristic}] {mask(match_text)}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  If any finding is a REAL credential: rotating it is a HUMAN action —", file=sys.stderr)
    print("  no automation (CI, agent, or script) may rotate it for you. Follow", file=sys.stderr)
    print("  README.md 'Rotating an exposed credential', purge the literal from the", file=sys.stderr)
    print("  file, and keep the secret only in ~/.config/workspace/secrets.env (600).", file=sys.stderr)
    print(f"  If it is a deliberate FAKE fixture: add a path+snippet entry to {ALLOWLIST_REL}.", file=sys.stderr)
    return 1


def self_test() -> int:
    fails = []

    def ck(label, cond):
        print(("ok   " if cond else "FAIL ") + label)
        if not cond:
            fails.append(label)

# Fixtures are CONCATENATED so this source file never contains a contiguous
    # token-shaped literal (the scanner would self-flag its own self-test).
    fake_glpat = "glpat-" + "A" * 24
    fake_atatt = "ATATT3xFfGF" + "0abcdef1234567890ABCDEF"
    fake_skant = "sk-ant-api03-" + "A" * 24
    fake_ghp = "ghp_" + "a" * 24
    fake_akia = "AKIA" + "ABCDEFGHIJKLMNOP"
    fake_xoxb = "xoxb-" + "1234567890-abc"
    pem = "-----BEGIN PRIVATE" + " KEY-----"

    # Heuristic 1 — all file types.
    for label, val in [("glpat", fake_glpat), ("atatt", fake_atatt),
                       ("sk-ant", fake_skant), ("ghp", fake_ghp),
                       ("akia", fake_akia), ("xoxb", fake_xoxb), ("pem", pem)]:
        ck(f"token-shape catches {label}", bool(scan_text("x.ts", f"const t = '{val}'", [])))

    # Heuristic 2 — config-shaped files only.
    ck("key-assign catches literal in .json",
       bool(scan_text("cfg.json", '"api_key": "sup3rSecretValue123"', [])))
    ck("key-assign catches literal in .toml",
       bool(scan_text("cfg.toml", "CONFLUENCE_API_TOKEN = hunter2hunter2hunter2", [])))
    ck("key-assign scoped OUT of .md prose",
       not scan_text("doc.md", "set token: examplelongvalue123 in your shell", []))
    ck("${VAR} indirection allowed",
       not scan_text("cfg.json", '"CONFLUENCE_API_TOKEN": "${ATLASSIAN_API_TOKEN}"', []))
    ck("secrets.env wrapper line allowed",
       not scan_text("cfg.json",
                     '"args": ["-c", "set -a; . \\"$HOME/.config/workspace/secrets.env\\"; set +a; exec uvx mcp-atlassian"]',
                     []))

    # Allowlist mechanics: exact path+snippet suppresses; different content does not.
    entries = [{"path": "t.sh", "snippet": fake_glpat, "reason": "fixture"}]
    ck("allowlist suppresses exact path+snippet",
       not scan_text("t.sh", f'GLPAT="{fake_glpat}"', entries))
    ck("allowlist misses different content in same file",
       bool(scan_text("t.sh", f'GLPAT="{fake_ghp}"', entries)))
    ck("allowlist misses same snippet in other file",
       bool(scan_text("other.sh", f'GLPAT="{fake_glpat}"', entries)))

    # Allowlist SELF-scan (V-M4): the registry's own registered snippets pass,
    # but an unregistered token shape planted in the file hard-fails.
    ck("allowlist self-scan passes registered snippet",
       not scan_allowlist_file(f'  "snippet": "{fake_glpat}",', entries))
    ck("allowlist self-scan flags unregistered token",
       bool(scan_allowlist_file(f'  "snippet": "{fake_ghp}",', entries)))

    # Finding printer masking (L2): the printed form must never contain the
    # full token — first 6 chars + length only.
    masked = mask(fake_glpat)
    ck("mask never re-prints the full token",
       fake_glpat not in masked and masked.startswith(fake_glpat[:6]))
    findings = scan_text("x.ts", f"const t = '{fake_glpat}'", [])
    ck("scan_text returns the matched span (maskable), not the line",
       findings and findings[0][2] == fake_glpat)

    if fails:
        print(f"{len(fails)} self-test case(s) failed", file=sys.stderr)
        return 1
    print("all secret-scan self-test cases passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="scan tracked files; exit 1 on findings")
    mode.add_argument("--self-test", action="store_true", help="run fixture-based self-test")
    args = ap.parse_args()
    return self_test() if args.self_test else check(REPO_ROOT)


if __name__ == "__main__":
    sys.exit(main())

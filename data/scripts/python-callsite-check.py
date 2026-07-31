#!/usr/bin/env python3
"""Static call-site gate for the mechanical-sweep corruption class.

A repo-wide "always pass encoding='utf-8'" sweep put `encoding=` onto 34 call
sites that do not accept it — `json.dumps`, a binary-mode `Path.open`, and a
locally defined `milestone()` test helper — and added one repeated keyword that
does not compile. None of it was caught, for two reasons worth stating because
this gate exists to close both:

  `ast.parse` is not `compile`. `ast.parse` happily accepts
  `f(encoding=1, encoding=2)` and returns a tree; only the compile step rejects
  it. A verification pass built on `ast.parse` reported the file as fine. This
  gate compiles.

  Nothing executed the damaged paths. The bad calls sit in branches no gate
  reached, so eight self-tests were red for days without anyone learning it.
  Runtime coverage is the real fix, but it is platform-dependent — half these
  scripts import `fcntl` and cannot run on Windows at all. So this gate is
  STATIC: it catches the whole class on every host, including the one where the
  code under test cannot be imported.

Three checks, all static, no imports of the code under test:

  1. compile()      — every file must compile, not merely parse.
  2. local defs     — keyword args at a call site must exist in the signature of
                      the function that file defines under that name.
  3. encoding=      — only text APIs known to accept `encoding` may be passed it,
                      and never together with a binary mode.

Check 3 is an allowlist, deliberately. Adding a name to TEXT_ENCODING_APIS is a
one-line, reviewable act; leaving unknown callables unflagged would have let all
34 sites through, which is the entire failure this file is here to prevent.

Stdlib only. CWD-independent. Exit 0 clean | 1 bad call site(s). `--self-test`
self-checks.

Usage: python3 data/scripts/python-callsite-check.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # data/scripts/x.py -> repo root

SOURCE_ROOTS = ("data/scripts", "workspace-tooling", "scripts")
PRUNED = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

# Callables that genuinely take `encoding=`. Matched on the bare name, since a
# static pass cannot resolve `p.open` to `Path.open` — an over-broad name here
# costs one missed call site, an over-narrow one costs a false failure that a
# reviewer resolves by appending to this list.
TEXT_ENCODING_APIS = frozenset({
    # text file IO
    "open", "read_text", "write_text", "TextIOWrapper", "fdopen",
    # codecs
    "decode", "encode",
    # already-open streams
    "reconfigure",
    # subprocess, in text mode
    "run", "check_output", "check_call", "call", "Popen", "communicate",
    # tempfiles
    "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile",
    # xml serialisation
    "tostring", "tostringlist",
})

# Dotted names that never take `encoding=`, checked BEFORE the bare-name
# allowlist. `os.open` is the reason this exists: it returns a raw file
# descriptor and takes no encoding, but its bare name collides with the
# builtin `open` that does.
NEVER_ENCODING = frozenset({
    "os.open", "os.pread", "os.pwrite",
    "json.dump", "json.dumps", "json.load", "json.loads",
})


def _iter_sources() -> list[Path]:
    """Every .py file under the source roots, pruned dirs excluded."""
    out: list[Path] = []
    for rel in SOURCE_ROOTS:
        root = ROOT / rel
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if PRUNED.isdisjoint(path.relative_to(ROOT).parts):
                out.append(path)
    return out


def _call_name(node: ast.Call) -> str:
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""


def _qualified_name(node: ast.Call) -> str:
    """Dotted name of the callee (`os.open`), or "" if the chain is not static."""
    parts, cur = [], node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return ""
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _mode_args(node: ast.Call) -> list[ast.expr]:
    """Positional/keyword mode arguments of an `open`-shaped call."""
    fn, modes = node.func, []
    if _call_name(node) == "open":
        if isinstance(fn, ast.Attribute) and node.args:
            modes.append(node.args[0])          # Path.open(mode, ...)
        elif isinstance(fn, ast.Name) and len(node.args) > 1:
            modes.append(node.args[1])          # open(path, mode, ...)
    for kw in node.keywords:
        if kw.arg == "mode":
            modes.append(kw.value)
    return modes


def _local_signatures(tree: ast.AST) -> dict[str, set[str] | None]:
    """Map each locally defined function name to its accepted keyword names.

    A name defined more than once maps to None and is never checked: a static
    pass cannot tell which definition is in scope at a given call site, and a
    gate that guesses is a gate that cries wolf. `**kwargs` maps to None too,
    since it accepts anything.
    """
    sigs: dict[str, set[str] | None] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in sigs:
            sigs[node.name] = None              # ambiguous: two defs, one name
            continue
        a = node.args
        if a.kwarg is not None:
            sigs[node.name] = None              # **kwargs takes every keyword
            continue
        sigs[node.name] = {p.arg for p in (*a.args, *a.kwonlyargs)}
    return sigs


def bad_call_sites(source: str, label: str = "<str>") -> list[str]:
    """Every offending call site in `source`, as human-readable findings."""
    try:
        compile(source, label, "exec")
    except SyntaxError as exc:
        return [f"{label}:{exc.lineno}: does not compile: {exc.msg}"]

    tree = ast.parse(source)
    sigs = _local_signatures(tree)
    findings: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        keywords = {kw.arg for kw in node.keywords if kw.arg}

        # 2. keyword must exist in the signature this file defines.
        if isinstance(node.func, ast.Name) and sigs.get(name) is not None:
            for unknown in sorted(keywords - (sigs[name] or set())):
                findings.append(
                    f"{label}:{node.lineno}: {name}() has no parameter {unknown!r} "
                    f"(defined in this file)"
                )

        if "encoding" not in keywords:
            continue

        # Checks 3a/3b reason about stdlib text APIs. A function this file
        # defines is not one, however it is named: check 2 already validated it
        # against the real signature, and re-checking here would flag a local
        # helper that legitimately takes an `encoding` parameter.
        if isinstance(node.func, ast.Name) and name in sigs:
            continue

        # 3a. encoding= only on known text APIs. A dotted name on the deny list
        # loses regardless of what its last segment happens to be called.
        if _qualified_name(node) in NEVER_ENCODING or name not in TEXT_ENCODING_APIS:
            findings.append(
                f"{label}:{node.lineno}: {name}(encoding=...) -- not a text API; "
                f"add to TEXT_ENCODING_APIS if this is wrong"
            )
            continue

        # 3b. never encoding= together with a binary mode.
        for mode in _mode_args(node):
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
                findings.append(
                    f"{label}:{node.lineno}: {name}(mode={mode.value!r}) with encoding= "
                    f"-- binary mode takes no encoding"
                )

    return findings


def _self_test() -> int:
    # compile(), not ast.parse(): the repeated keyword that started this.
    dup = bad_call_sites("f(encoding='utf-8', encoding='utf-8')")
    assert len(dup) == 1 and "does not compile" in dup[0], dup
    assert ast.parse("f(encoding=1, encoding=2)"), "ast.parse tolerates it — the whole point"

    # encoding= on a non-text API.
    assert bad_call_sites("import json\njson.dumps(x, encoding='utf-8')"), "json.dumps not caught"
    assert bad_call_sites("os.open(p, flags, encoding='utf-8')"), "os.open not caught"

    # encoding= on a real text API stays quiet.
    assert bad_call_sites("open(p, encoding='utf-8')") == []
    assert bad_call_sites("p.read_text(encoding='utf-8')") == []
    assert bad_call_sites("sys.stdout.reconfigure(encoding='utf-8')") == []
    assert bad_call_sites("subprocess.run(cmd, encoding='utf-8')") == []

    # binary mode + encoding, both call shapes.
    assert bad_call_sites("p.open('r+b', encoding='utf-8')"), "Path.open binary not caught"
    assert bad_call_sites("open(p, 'rb', encoding='utf-8')"), "open binary not caught"
    assert bad_call_sites("open(p, mode='wb', encoding='utf-8')"), "keyword binary not caught"

    # locally defined helper given a parameter it does not have.
    assert bad_call_sites("def milestone(a, b):\n    pass\nmilestone(1, 2, encoding='utf-8')")
    assert bad_call_sites("def milestone(a, *, encoding=None):\n    pass\nmilestone(1, encoding='x')") == []
    # **kwargs and duplicate names are deliberately not checked.
    assert bad_call_sites("def f(**kw):\n    pass\nf(anything=1)") == []
    assert bad_call_sites("def f(a):\n    pass\ndef f(b):\n    pass\nf(b=1)") == []

    print("python-callsite-check self-test: OK")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return _self_test()

    sources = _iter_sources()
    findings: list[str] = []
    for path in sources:
        rel = path.relative_to(ROOT).as_posix()
        findings += bad_call_sites(path.read_text(encoding="utf-8"), rel)

    for finding in findings:
        print(f"FAIL: {finding}", file=sys.stderr)
    if findings:
        print(
            f"\n{len(findings)} bad call site(s) across "
            f"{len({f.split(':')[0] for f in findings})} file(s) of {len(sources)} checked",
            file=sys.stderr,
        )
        return 1
    print(f"python-callsite-check: {len(sources)} file(s) compile; no bad call sites")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

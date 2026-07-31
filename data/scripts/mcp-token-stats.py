#!/usr/bin/env python3
"""MCP Token Usage Dashboard.

Reads the workspace-mcp JSONL token log and renders a color-coded terminal
dashboard showing per-tool call counts, estimated token volumes, call
latencies, and a 7-day trend.  The "output est." column is the one to
watch — those tokens go directly into Claude's context window on every call.

Usage:
  python3 mcp-token-stats.py [LOG_PATH]
  TOKEN_LOG_PATH=/path/to/log python3 mcp-token-stats.py

LOG_PATH resolution (first match wins):
  1. Positional argument
  2. TOKEN_LOG_PATH env var
  3. ~/.claude/mcp-token-log.jsonl  (default local dev path)

Set NO_COLOR=1 to strip ANSI escape codes (e.g. for piping to a file).
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ─── ANSI colour palette ─────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

def _ansi(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def bold(t):         return _ansi("1",    t)
def dim(t):          return _ansi("2",    t)
def cyan(t):         return _ansi("96",   t)
def green(t):        return _ansi("92",   t)
def yellow(t):       return _ansi("93",   t)
def red(t):          return _ansi("91",   t)
def magenta(t):      return _ansi("95",   t)
def blue(t):         return _ansi("94",   t)
def white(t):        return _ansi("97",   t)

def heat(value: int, peak: int) -> str:
    """Colour a token count green → yellow → red based on share of peak."""
    if peak == 0 or value == 0:
        return dim
    ratio = value / peak
    if ratio > 0.65:
        return red
    if ratio > 0.35:
        return yellow
    return green

# ─── Unicode bar chart ────────────────────────────────────────────────────────

_BLOCKS = " ▏▎▍▌▋▊▉█"

def bar(value: int, peak: int, width: int = 24) -> str:
    """Render a proportional unicode block bar of the given character width."""
    if peak == 0:
        return " " * width
    filled = (value / peak) * width
    full  = int(filled)
    frac  = filled - full
    frac_char = _BLOCKS[int(frac * (len(_BLOCKS) - 1))]
    result = "█" * full + (frac_char if full < width else "") + " " * max(0, width - full - 1)
    return result[:width]

# ─── Data loading ─────────────────────────────────────────────────────────────

def load_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError:
                print(f"  {yellow('⚠')} skipped malformed line {lineno}", file=sys.stderr)
    return entries

def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None

# ─── Formatting helpers ───────────────────────────────────────────────────────

def fmt_k(n: int) -> str:
    """Format token count with K suffix above 999."""
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)

def fmt_ts(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "?"

# ─── Aggregation ──────────────────────────────────────────────────────────────

def aggregate(entries: list[dict]) -> dict:
    by_tool: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "input": 0, "output": 0, "ms": 0, "errors": 0}
    )
    for e in entries:
        t = by_tool[e.get("tool", "unknown")]
        t["calls"]  += 1
        t["input"]  += e.get("inputEst",  0)
        t["output"] += e.get("outputEst", 0)
        t["ms"]     += e.get("ms",        0)
        if e.get("error"):
            t["errors"] += 1
    return dict(by_tool)

# ─── Print sections ───────────────────────────────────────────────────────────

W = 70   # display width

def rule(char="═"):
    print(cyan(bold(char * W)))

def section(title: str):
    print()
    print(blue(bold(f"  ── {title} " + "─" * max(0, W - 6 - len(title)))))
    print()

def print_header():
    print()
    rule("═")
    print(cyan(bold(f"  MCP Token Usage Dashboard  ·  @chris-dare-dev/agent-kit")))
    rule("═")

def print_summary(entries: list[dict], log_path: Path):
    now = datetime.now(timezone.utc)
    total_in  = sum(e.get("inputEst",  0) for e in entries)
    total_out = sum(e.get("outputEst", 0) for e in entries)
    total_err = sum(1 for e in entries if e.get("error"))

    timestamps = [parse_ts(e.get("ts")) for e in entries]
    timestamps = [t for t in timestamps if t]
    first = min(timestamps) if timestamps else None
    last  = max(timestamps) if timestamps else None

    today_out = sum(
        e.get("outputEst", 0) for e, t in zip(entries, timestamps)
        if t and (now - t).days < 1
    )
    today_calls = sum(
        1 for t in timestamps if t and (now - t).days < 1
    )

    section("Summary")
    rows = [
        ("Total calls",  white(bold(f"{len(entries):,}"))),
        ("Input  est.",  green(f"~{fmt_k(total_in)} tok")),
        ("Output est.",  yellow(f"~{fmt_k(total_out)} tok")
         + dim("  ← goes into context on every call")),
        ("Net est.",     cyan(f"~{fmt_k(total_in + total_out)} tok")),
    ]
    if total_err:
        rows.append(("Errors", red(str(total_err))))
    if today_calls:
        rows.append(("Today", f"{today_calls} calls  ~{fmt_k(today_out)} tok out"))
    if first and last:
        rows.append(("Period", dim(f"{fmt_ts(first)}  →  {fmt_ts(last)} UTC")))
    rows.append(("Log", dim(str(log_path))))

    label_w = max(len(r[0]) for r in rows)
    for label, value in rows:
        print(f"  {dim((label + ':').ljust(label_w + 1))}  {value}")

def print_tool_table(by_tool: dict):
    if not by_tool:
        return
    section("Per-Tool Breakdown  (sorted by output tokens)")

    peak = max((v["output"] for v in by_tool.values()), default=1)
    sorted_rows = sorted(by_tool.items(), key=lambda x: x[1]["output"], reverse=True)
    name_w = max(len(k) for k in by_tool)

    # Header
    hdr = (
        f"  {'Tool'.ljust(name_w)}  {'Calls':>5}  "
        f"{'In tok':>7}  {'Out tok':>8}  {'AvgMs':>6}  Output bar"
    )
    print(dim(hdr))
    print(dim(f"  {'─'*name_w}  {'─'*5}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*24}"))

    for tool, s in sorted_rows:
        calls  = s["calls"]
        inp    = s["input"]
        out    = s["output"]
        avg_ms = round(s["ms"] / calls) if calls else 0
        errs   = s["errors"]
        color  = heat(out, peak)

        err_tag = f"  {red(f'✗ {errs} err')}" if errs else ""
        bar_str = color(bar(out, peak, width=24))

        print(
            f"  {white(tool.ljust(name_w))}"
            f"  {calls:>5}"
            f"  {green(('~' + fmt_k(inp)).rjust(7))}"
            f"  {color(('~' + fmt_k(out)).rjust(8))}"
            f"  {avg_ms:>5}ms"
            f"  {bar_str}"
            f"{err_tag}"
        )

def print_heaviest(entries: list[dict], n: int = 5):
    top = sorted(entries, key=lambda e: e.get("outputEst", 0), reverse=True)[:n]
    if not top:
        return
    section(f"Top {n} Heaviest Calls  (by output tokens)")

    for i, e in enumerate(top, 1):
        tool    = e.get("tool", "unknown")
        out_tok = e.get("outputEst", 0)
        ms      = e.get("ms", 0)
        ts      = parse_ts(e.get("ts"))
        ts_str  = ts.strftime("%m-%d %H:%M") if ts else "?"
        err_str = f"  {red('✗ ' + str(e['error'])[:50])}" if e.get("error") else ""

        print(
            f"  {dim(str(i) + '.')}  {white(tool):<{30}}"
            f"  {yellow('~' + fmt_k(out_tok) + ' tok out'):<18}"
            f"  {dim(str(ms) + 'ms'):<8}"
            f"  {dim(ts_str)}"
            f"{err_str}"
        )

def print_daily_trend(entries: list[dict], days: int = 7):
    now = datetime.now(timezone.utc)
    buckets: dict[str, dict] = defaultdict(lambda: {"calls": 0, "output": 0})

    for e in entries:
        ts = parse_ts(e.get("ts"))
        if not ts:
            continue
        age = (now - ts).days
        if age >= days:
            continue
        key = ts.strftime("%a %m/%d")
        buckets[key]["calls"]  += 1
        buckets[key]["output"] += e.get("outputEst", 0)

    if not buckets:
        return
    section(f"Last {days} Days  (output tokens)")

    peak = max(v["output"] for v in buckets.values())
    for day, stats in sorted(buckets.items()):
        color  = heat(stats["output"], peak)
        bar_str = color(bar(stats["output"], peak, width=30))
        calls_str = f"{stats['calls']:>3} calls"
        out_str   = dim(f"~{fmt_k(stats['output'])} tok")
        print(f"  {dim(day)}  {bar_str}  {calls_str}  {out_str}")

# ─── Main ─────────────────────────────────────────────────────────────────────

LOG_FILENAME = "mcp-token-log.jsonl"


def resolve_log_path(explicit: str | None) -> Path:
    """Return the first candidate log path that contains data.

    Priority:
      1. Explicit CLI argument or TOKEN_LOG_PATH env var (must exist even if empty)
      2. ~/.claude/mcp-token-log.jsonl  (macOS / local default)
      3. ~/Gitlab/platform/.claude/mcp-token-log.jsonl  (sandbox default — WORKSPACE_ROOT
         equals PLATFORM_ROOT so the MCP server writes here, not ~/.claude/)
      4. Any mcp-token-log.jsonl found under ~/Gitlab/ (discovery fallback)
    """
    home = Path.home()

    if explicit:
        return Path(explicit)

    candidates = [
        home / ".claude" / LOG_FILENAME,
        home / "Gitlab" / "platform" / ".claude" / LOG_FILENAME,
    ]

    # Check fixed candidates first — return the first one that has content.
    for p in candidates:
        if p.exists() and p.stat().st_size > 0:
            return p

    # Discovery fallback: search under ~/Gitlab/ up to depth 5.
    try:
        import glob as _glob
        found = _glob.glob(str(home / "Gitlab" / "**" / LOG_FILENAME), recursive=True)
        if found:
            # Prefer the most recently modified.
            return Path(max(found, key=lambda f: Path(f).stat().st_mtime))
    except Exception:
        pass

    # Nothing found — return the primary default so the error message is useful.
    return candidates[0]


def main():
    explicit: str | None = None
    if len(sys.argv) > 1:
        explicit = sys.argv[1]
    elif os.environ.get("TOKEN_LOG_PATH"):
        explicit = os.environ["TOKEN_LOG_PATH"]

    log_path = resolve_log_path(explicit)
    entries = load_log(log_path)

    if not entries:
        print(f"\n  {yellow('No log entries found at:')} {log_path}")
        print(f"  {dim('Make some MCP tool calls first, or point TOKEN_LOG_PATH at your log.')}\n")
        sys.exit(0)

    by_tool = aggregate(entries)

    print_header()
    print_summary(entries, log_path)
    print_tool_table(by_tool)
    print_heaviest(entries, n=5)
    print_daily_trend(entries, days=7)

    print()
    print(dim("  " + "─" * (W - 2)))
    print()

if __name__ == "__main__":
    main()

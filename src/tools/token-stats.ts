import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import type { TokenLogger, TokenLogEntry } from "../logging/token-logger.js";
import { redactArgsForProfile } from "../logging/token-logger.js";
import type { ServerProfile } from "../types.js";

/** Tools served straight from in-memory data (no caching pressure). */
const CACHE_EXEMPT_TOOLS = new Set<string>([
  "get_token_stats",
  "list_skills",
  "list_references",
  "list_context_guides",
  "list_agents",
]);

const CHUNKABLE_TOOLS = new Set<string>([
  "get_context_guide",
  "get_skill",
  "get_agent",
]);

/**
 * Creates the get_token_stats MCP tool handler.
 *
 * Two modes:
 *   - "summary" (default) — current session totals + per-tool breakdown +
 *     top-5 largest historical calls.  ~250-400 tokens.  Cheap to call often.
 *   - "detail" — additionally re-reads the JSONL log to surface hot queries,
 *     content-hash dupes, chunking adoption, and cache-bypass usage.  Mirrors
 *     the standalone scripts/analyze-mcp-log.py without leaving the session.
 *     ~1-1.5 KB depending on log size.
 *
 * The tool is `cache-exempt` and not itself token-logged so calling it
 * repeatedly never inflates the very metrics it reports.
 */
export function createTokenStatsTools(logger: TokenLogger) {
  return {
    get_token_stats: async (
      args: { mode?: "summary" | "detail" } = {},
    ): Promise<string> => {
      const mode = args.mode === "detail" ? "detail" : "summary";
      const stats = await logger.getFullStats();
      const fmt = (n: number) => n.toLocaleString("en-US");
      const lines: string[] = [];

      lines.push(`## MCP Token Usage (mode: ${mode})`);
      lines.push("");
      lines.push(
        `Session: ${stats.calls} calls | input ~${fmt(stats.inputEst)} tok | output ~${fmt(stats.outputEst)} tok | net ~${fmt(stats.inputEst + stats.outputEst)} tok`,
      );
      if (stats.sessionCacheHits > 0) {
        lines.push(
          `  this session cache: ${stats.sessionCacheHits} hits saved ~${fmt(stats.sessionTokensSaved)} tok`,
        );
      }

      if (Object.keys(stats.byTool).length > 0) {
        lines.push("");
        lines.push("By tool (sorted by output size):");

        const sorted = Object.entries(stats.byTool).sort(
          ([, a], [, b]) => b.outputEst - a.outputEst,
        );
        const maxNameLen = Math.max(...sorted.map(([name]) => name.length));

        for (const [tool, s] of sorted) {
          const callLabel = s.calls === 1 ? "call " : "calls";
          lines.push(
            `  ${tool.padEnd(maxNameLen)}  ${String(s.calls).padStart(2)} ${callLabel}` +
              `  in: ${String(s.inputEst).padStart(6)} tok` +
              `  out: ${String(s.outputEst).padStart(7)} tok` +
              `  avg ${s.avgMs}ms`,
          );
        }
      }

      if (stats.historicalCalls > 0) {
        const histNet = stats.historicalInputEst + stats.historicalOutputEst;
        lines.push("");
        lines.push(
          `All sessions: ${stats.historicalCalls} calls across ${stats.historicalSessions} session(s)` +
            ` | net ~${fmt(histNet)} tok total`,
        );
        if (stats.historicalCacheHits > 0) {
          lines.push(
            `Cache: ${stats.historicalCacheHits} hits | ~${fmt(stats.historicalTokensSaved)} tok saved`,
          );
        }
      }

      if (stats.topLargestCalls.length > 0) {
        lines.push("");
        lines.push("Top 5 largest historical calls (by output tokens):");
        for (const entry of stats.topLargestCalls) {
          const argHint = entry.args ? `  args=${entry.args}` : "";
          lines.push(
            `  ${entry.ts.slice(0, 19).replace("T", " ")}  ` +
              `${entry.tool.padEnd(28)}  ` +
              `out: ${String(entry.outputEst).padStart(7)} tok` +
              argHint,
          );
        }
      }

      // --- detail-mode extension ---------------------------------------------
      if (mode === "detail" && stats.logPath && existsSync(stats.logPath)) {
        const detail = await buildDetailReport(stats.logPath, fmt, logger.getServerProfile());
        if (detail) {
          lines.push("");
          lines.push(detail);
        }
      }

      lines.push("");
      lines.push(`Session ID: ${stats.sessionId}`);
      if (stats.logPath) {
        lines.push(`Log: ${stats.logPath}`);
      } else {
        lines.push("Log: disabled (set TOKEN_LOG_PATH env var to enable persistent history)");
      }
      if (mode === "summary") {
        lines.push(
          `Tip: pass {"mode":"detail"} for hot queries, content dupes, and chunking adoption.`,
        );
      }
      lines.push("Note: ~4 chars/token estimate — actual counts vary by content and model.");

      return lines.join("\n");
    },
  };
}

// ---------------------------------------------------------------------------
// Detail report — minimal in-process port of scripts/analyze-mcp-log.py
// ---------------------------------------------------------------------------

interface QueryAgg {
  calls: number;
  out: number;
  saved: number;
  errors: number;
}

interface HashAgg {
  paths: Map<string, number>; // canonical "tool args" -> count
  size: number;
}

async function buildDetailReport(
  logPath: string,
  fmt: (n: number) => string,
  serverProfile: ServerProfile,
): Promise<string | null> {
  let raw: string;
  try {
    raw = await readFile(logPath, "utf-8");
  } catch {
    return null;
  }

  const entries: TokenLogEntry[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      entries.push(JSON.parse(trimmed) as TokenLogEntry);
    } catch {
      // skip malformed lines
    }
  }
  if (entries.length === 0) return null;

  const out: string[] = [];

  // --- Hot queries (top 5 by would-have-shipped tokens) ------------------
  const byQuery = new Map<string, QueryAgg>();
  for (const e of entries) {
    const key = `${e.tool} ${redactArgsForProfile(e.args, serverProfile) ?? ""}`;
    const agg = byQuery.get(key) ?? { calls: 0, out: 0, saved: 0, errors: 0 };
    agg.calls += 1;
    agg.out += e.outputEst ?? 0;
    agg.saved += e.outputSavedEst ?? 0;
    if (e.isError || e.error) agg.errors += 1;
    byQuery.set(key, agg);
  }
  const hot = [...byQuery.entries()]
    .sort(([, a], [, b]) => b.out + b.saved - (a.out + a.saved))
    .slice(0, 5);
  out.push("Hot queries (top 5, by would-have-shipped tokens):");
  for (const [key, s] of hot) {
    out.push(
      `  shipped ${fmt(s.out).padStart(8)}  +saved ${fmt(s.saved).padStart(7)}  ${String(s.calls).padStart(3)} calls  ${s.errors > 0 ? `${s.errors}err  ` : ""}${key.slice(0, 110)}`,
    );
  }

  // --- Content-hash dupes ------------------------------------------------
  const byHash = new Map<string, HashAgg>();
  for (const e of entries) {
    const h = e.outputHash;
    if (!h) continue;
    const key = `${e.tool} ${redactArgsForProfile(e.args, serverProfile) ?? ""}`;
    const agg = byHash.get(h) ?? {
      paths: new Map<string, number>(),
      size: e.outputEst ?? 0,
    };
    agg.paths.set(key, (agg.paths.get(key) ?? 0) + 1);
    byHash.set(h, agg);
  }
  const dupes = [...byHash.entries()].filter(([, v]) => v.paths.size > 1);
  if (dupes.length > 0) {
    dupes.sort(
      ([, a], [, b]) =>
        sumValues(b.paths) - sumValues(a.paths) || b.size - a.size,
    );
    out.push("");
    out.push("Content-hash dupes (same content, different paths):");
    for (const [h, agg] of dupes.slice(0, 3)) {
      out.push(
        `  hash ${h} (~${agg.size} tok, ${sumValues(agg.paths)} calls):`,
      );
      const sortedPaths = [...agg.paths.entries()].sort(
        ([, a], [, b]) => b - a,
      );
      for (const [path, count] of sortedPaths.slice(0, 4)) {
        out.push(`    ${String(count).padStart(3)}× ${path.slice(0, 110)}`);
      }
    }
  }

  // --- Chunking adoption -------------------------------------------------
  const chunkAgg = new Map<string, { chunked: number; full: number }>();
  for (const e of entries) {
    if (!CHUNKABLE_TOOLS.has(e.tool)) continue;
    const a = chunkAgg.get(e.tool) ?? { chunked: 0, full: 0 };
    if (e.chunked) a.chunked += 1;
    else a.full += 1;
    chunkAgg.set(e.tool, a);
  }
  if (chunkAgg.size > 0) {
    out.push("");
    out.push("Chunking adoption:");
    for (const [tool, a] of [...chunkAgg.entries()].sort()) {
      const total = a.chunked + a.full;
      const pct = total > 0 ? Math.round((a.chunked / total) * 100) : 0;
      out.push(
        `  ${tool.padEnd(28)} ${a.chunked}/${total} chunked (${pct}%)`,
      );
    }
  }

  // --- Cache-bypass (force) usage ---------------------------------------
  const cacheable = entries.filter((e) => !CACHE_EXEMPT_TOOLS.has(e.tool));
  const forcedCount = cacheable.filter((e) => e.forced).length;
  if (cacheable.length > 0) {
    const pct = ((forcedCount / cacheable.length) * 100).toFixed(1);
    out.push("");
    out.push(
      `Cache-bypass: ${forcedCount}/${cacheable.length} cacheable calls (${pct}%) used force=true`,
    );
  }

  return out.join("\n");
}

function sumValues(m: Map<string, number>): number {
  let total = 0;
  for (const v of m.values()) total += v;
  return total;
}

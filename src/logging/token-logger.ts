/**
 * Token usage logger for the MCP server.
 *
 * Estimates token counts from text length (~4 chars per token — standard
 * GPT/Claude approximation) and tracks them per-tool, per-session, and
 * historically via an append-only JSONL log file.
 *
 * Design goals:
 *   - Zero blocking: all file I/O is async and fire-and-forget.
 *   - Zero throws: errors in file I/O are silently swallowed.
 *   - Minimal footprint: no external dependencies beyond Node built-ins.
 */

import { appendFile, readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import type { ServerProfile } from "../types.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** One entry persisted to the JSONL log file per tool call. */
export interface TokenLogEntry {
  ts: string;
  /** Short random ID generated at server startup — groups calls by session. */
  sessionId: string;
  tool: string;
  inputEst: number;
  outputEst: number;
  ms: number;
  /** Args summary. personal: ≤ARGS_TRUNCATE_LEN-char raw preview; shared: an
   * 8-hex sha256 of the full raw args, salted per-process (see hashArgs) — the
   * raw query is deliberately NOT recoverable under the shared profile. */
  args?: string;
  /**
   * First 8 hex chars of sha256(outputText).  Lets the analyzer detect when
   * different (tool, args) pairs ship identical content — e.g. a TOC and
   * `section: "list"` returning byte-identical output.
   */
  outputHash?: string;
  /**
   * Section slug pulled out of `args.section` (denormalized so analyzers
   * don't have to parse the args JSON to aggregate by section).
   */
  section?: string;
  /** True when `args.force === true` — caller explicitly bypassed cache. */
  forced?: boolean;
  /**
   * True when the response was a chunked / TOC / single-section view rather
   * than the full source body.  Tracks whether chunking is actually being
   * used.
   */
  chunked?: boolean;
  /**
   * Running output-token total for this session at the time of this call
   * (post-increment).  Lets the analyzer find runaway sessions without
   * joining across rows.
   */
  sessionTok?: number;
  /** True when the tool returned `isError: true` (semantic error, not an exception). */
  isError?: boolean;
  /** True when outputEst exceeded LARGE_OUTPUT_THRESHOLD — flag for review. */
  large?: boolean;
  /** True when this call was served from the in-process response cache (stub returned). */
  cached?: boolean;
  /** Tokens that would have been re-shipped without the cache. */
  outputSavedEst?: number;
  error?: string;
}

/** Output sizes above this trigger the `large` flag in the log entry. */
const LARGE_OUTPUT_THRESHOLD = 5000;
/** Output sizes above this also emit a stderr warning so engineers see them. */
const STDERR_WARN_THRESHOLD = 10000;
/** Args summaries are truncated to this many chars to keep the log compact. */
const ARGS_TRUNCATE_LEN = 120;

// --- Layer 3 — periodic stderr digest + session budget warning ---
/** Emit a digest every N successful calls or every M ms, whichever first. */
const DIGEST_EVERY_N_CALLS = 100;
const DIGEST_EVERY_MS = 5 * 60 * 1000;
/**
 * Warn once per session when this output-token threshold is crossed.  Catches
 * runaway sessions (model loops, accidental fan-outs) in real time without
 * waiting for the engineer to run the analyzer.
 */
const SESSION_BUDGET_WARN_TOK = 30000;

export interface ToolTokenStats {
  calls: number;
  inputEst: number;
  outputEst: number;
  /** Average call duration in milliseconds. */
  avgMs: number;
}

export interface SessionStats {
  calls: number;
  inputEst: number;
  outputEst: number;
  byTool: Record<string, ToolTokenStats>;
  /** Cache hits served in this session (live count, no log scan). */
  sessionCacheHits: number;
  /** Tokens saved by the cache in this session. */
  sessionTokensSaved: number;
}

/** Compact representation of a single log entry — used in top-N reports. */
export interface LogEntrySummary {
  ts: string;
  tool: string;
  inputEst: number;
  outputEst: number;
  args?: string;
}

export interface FullStats extends SessionStats {
  historicalCalls: number;
  historicalInputEst: number;
  historicalOutputEst: number;
  /** Top-5 largest historical calls by outputEst. */
  topLargestCalls: LogEntrySummary[];
  /** Number of distinct sessions found in the log. */
  historicalSessions: number;
  /** Number of historical calls served from the response cache. */
  historicalCacheHits: number;
  /** Total tokens saved by the response cache across all sessions. */
  historicalTokensSaved: number;
  /** Current process session ID (matches sessionId field on log entries). */
  sessionId: string;
  logPath: string | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Rough token count: 4 characters ≈ 1 token.
 * This matches the well-known rule of thumb for English prose and code.
 * Actual counts depend on the model's tokenizer and language.
 */
function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

/**
 * Build a compact, single-line preview of the JSON-stringified args.
 * Returns undefined for empty/`{}` args so the log entry stays minimal.
 */
function summarizeArgs(inputText: string): string | undefined {
  if (!inputText) return undefined;
  // Collapse whitespace and strip common JSON noise so 120 chars goes further.
  const collapsed = inputText.replace(/\s+/g, " ").trim();
  if (!collapsed || collapsed === "{}") return undefined;
  if (collapsed.length <= ARGS_TRUNCATE_LEN) return collapsed;
  return collapsed.slice(0, ARGS_TRUNCATE_LEN - 1) + "…";
}

/**
 * Hash the FULL raw args (sha256, 8 hex) instead of storing a preview — used
 * under SERVER_PROFILE=shared so a shared deployment's token log never persists
 * raw tool-call arguments (which can carry sensitive query content). Mirrors the
 * existing outputHash pattern; hashes the whole string so no prefix survives.
 */
function hashArgs(inputText: string, salt: string): string | undefined {
  if (!inputText) return undefined;
  const collapsed = inputText.replace(/\s+/g, " ").trim();
  if (!collapsed || collapsed === "{}") return undefined;
  // Salt with the per-process session id so the digest still groups identical
  // args WITHIN a session (dedup analytics survive) but cannot be used for
  // cross-session/cross-tenant dictionary confirmation of a guessed args string.
  return createHash("sha256").update(salt).update(inputText).digest("hex").slice(0, 8);
}

/**
 * Redact a persisted args value under the shared profile: any value that is NOT
 * already an 8-hex hash (i.e. a raw preview left by a personal-profile write, or
 * an explicitly-shared log file) is replaced, so get_token_stats never re-serves
 * raw args on a shared server. The write side (hashArgs) covers new entries; this
 * covers the READ side of pre-existing/foreign entries.
 */
export function redactArgsForProfile(
  args: string | undefined,
  serverProfile: ServerProfile,
): string | undefined {
  if (args === undefined) return undefined;
  if (serverProfile === "shared" && !/^[0-9a-f]{8}$/.test(args)) return "[redacted]";
  return args;
}

// ---------------------------------------------------------------------------
// TokenLogger
// ---------------------------------------------------------------------------

export class TokenLogger {
  private readonly logPath: string | null;
  private readonly serverProfile: ServerProfile;
  private readonly sessionId: string;

  // In-memory session accumulators (reset on process restart)
  private readonly byTool = new Map<
    string,
    { calls: number; inputEst: number; outputEst: number; totalMs: number }
  >();
  private totalCalls = 0;
  private totalInputEst = 0;
  private totalOutputEst = 0;
  // Layer 3 — session-level cache hit tracking, used by both the periodic
  // digest and `get_token_stats` summary so we don't have to re-scan the log.
  private sessionCacheHits = 0;
  private sessionTokensSaved = 0;
  // Digest emission state.
  private lastDigestAt: number;
  private callsSinceLastDigest = 0;
  // Once-per-session flag so the budget warning fires exactly once.
  private sessionBudgetWarned = false;

  constructor(logPath: string | null, serverProfile: ServerProfile = "personal") {
    this.logPath = logPath;
    this.serverProfile = serverProfile;
    // 8-char hex random ID; collision-free enough for session grouping.
    this.sessionId = Math.random().toString(36).slice(2, 10);
    this.lastDigestAt = Date.now();

    if (logPath) {
      // Ensure the parent directory exists — best-effort, silent on failure.
      try {
        mkdirSync(path.dirname(logPath), { recursive: true });
      } catch {
        // Carry on; subsequent appendFile calls will also fail silently.
      }
    }
  }

  /** Returns this logger's session ID — useful for filtering log entries. */
  getSessionId(): string {
    return this.sessionId;
  }

  /** The active trust profile — used by get_token_stats to redact raw args
   * from the log's detail report under shared. */
  getServerProfile(): ServerProfile {
    return this.serverProfile;
  }

  /**
   * Record a single tool call.
   *
   * Updates in-memory session totals synchronously, then fire-and-forgets a
   * JSONL append to disk.  Never throws and never blocks the caller.
   */
  logCall(
    tool: string,
    inputText: string,
    outputText: string,
    durationMs: number,
    opts: {
      error?: string;
      cached?: boolean;
      outputSavedEst?: number;
      /** Pre-parsed args object — used to extract section/force flags. */
      args?: Record<string, unknown>;
      /** True when the tool's structured response carried isError. */
      isError?: boolean;
      /** True when chunkByPolicy returned a reduced view of the source. */
      chunked?: boolean;
    } = {},
  ): void {
    const { error, cached, outputSavedEst, args, isError, chunked } = opts;
    const inputEst = estimateTokens(inputText);
    const outputEst = estimateTokens(outputText);

    // --- Session accumulators ---
    if (!this.byTool.has(tool)) {
      this.byTool.set(tool, { calls: 0, inputEst: 0, outputEst: 0, totalMs: 0 });
    }
    const s = this.byTool.get(tool)!;
    s.calls++;
    s.inputEst += inputEst;
    s.outputEst += outputEst;
    s.totalMs += durationMs;

    this.totalCalls++;
    this.totalInputEst += inputEst;
    this.totalOutputEst += outputEst;
    if (cached) {
      this.sessionCacheHits++;
      if (outputSavedEst) this.sessionTokensSaved += outputSavedEst;
    }

    const isLarge = outputEst > LARGE_OUTPUT_THRESHOLD;
    // Under the shared profile, hash the args instead of storing a raw preview —
    // a shared deployment's log must never persist raw tool-call arguments.
    const argsSummary =
      this.serverProfile === "shared"
        ? hashArgs(inputText, this.sessionId)
        : summarizeArgs(inputText);
    const section =
      args && typeof args.section === "string" ? args.section : undefined;
    const forced = args?.force === true ? true : undefined;
    // Hash the actual output bytes — covers fresh AND cached calls so the
    // analyzer can group identical content across paths. Skip for empty
    // outputs (errors that returned no text).
    const outputHash =
      outputText.length > 0
        ? createHash("sha256").update(outputText).digest("hex").slice(0, 8)
        : undefined;

    // --- stderr warning for very large outputs (visible in MCP server logs) ---
    if (outputEst > STDERR_WARN_THRESHOLD) {
      const argHint = argsSummary ? ` args=${argsSummary}` : "";
      process.stderr.write(
        `[workspace-mcp] WARNING: ${tool} returned ~${outputEst.toLocaleString("en-US")} tokens${argHint}\n`,
      );
    }

    // --- Persistent log (async, fire-and-forget) ---
    if (this.logPath) {
      const entry: TokenLogEntry = {
        ts: new Date().toISOString(),
        sessionId: this.sessionId,
        tool,
        inputEst,
        outputEst,
        ms: durationMs,
        ...(argsSummary ? { args: argsSummary } : {}),
        ...(outputHash ? { outputHash } : {}),
        // section is a caller-chosen arg fragment — omit it under shared so no
        // raw arg content survives (the args field is already hashed there).
        ...(section && this.serverProfile !== "shared" ? { section } : {}),
        ...(forced ? { forced } : {}),
        ...(chunked ? { chunked: true } : {}),
        sessionTok: this.totalOutputEst,
        ...(isError ? { isError: true } : {}),
        ...(isLarge ? { large: true } : {}),
        ...(cached ? { cached: true } : {}),
        ...(outputSavedEst ? { outputSavedEst } : {}),
        ...(error ? { error } : {}),
      };
      appendFile(this.logPath, JSON.stringify(entry) + "\n").catch(() => {});
    }

    // --- Layer 3 — session budget warning + periodic digest ---
    this.maybeWarnSessionBudget();
    this.callsSinceLastDigest++;
    this.maybeEmitDigest();
  }

  /**
   * Emit a one-line digest to stderr if either a call-count or wall-clock
   * threshold has been crossed since the previous digest.  Cheap continuous
   * awareness — visible in the MCP server logs without running the analyzer.
   */
  private maybeEmitDigest(): void {
    const now = Date.now();
    const overTime = now - this.lastDigestAt >= DIGEST_EVERY_MS;
    const overCount = this.callsSinceLastDigest >= DIGEST_EVERY_N_CALLS;
    if (!overTime && !overCount) return;

    let topTool = "?";
    let topOut = 0;
    for (const [tool, s] of this.byTool) {
      if (s.outputEst > topOut) {
        topOut = s.outputEst;
        topTool = tool;
      }
    }
    const fmt = (n: number) => n.toLocaleString("en-US");
    const cacheNote =
      this.sessionCacheHits > 0
        ? ` | ${this.sessionCacheHits} cache hits saved ${fmt(this.sessionTokensSaved)} tok`
        : "";
    process.stderr.write(
      `[workspace-mcp] Digest: session ${this.sessionId} ` +
        `${this.totalCalls} calls | shipped ${fmt(this.totalOutputEst)} tok` +
        cacheNote +
        ` | top: ${topTool}\n`,
    );
    this.lastDigestAt = now;
    this.callsSinceLastDigest = 0;
  }

  /**
   * Emit a stderr warning the first time this session crosses the runaway-
   * session output threshold.  Single-shot to avoid log spam.
   */
  private maybeWarnSessionBudget(): void {
    if (this.sessionBudgetWarned) return;
    if (this.totalOutputEst < SESSION_BUDGET_WARN_TOK) return;
    this.sessionBudgetWarned = true;
    const fmt = (n: number) => n.toLocaleString("en-US");
    process.stderr.write(
      `[workspace-mcp] WARNING: session ${this.sessionId} crossed ` +
        `${fmt(SESSION_BUDGET_WARN_TOK)} output tokens ` +
        `(now ${fmt(this.totalOutputEst)}). ` +
        `Inspect with scripts/analyze-mcp-log.py or get_token_stats {"mode":"detail"}.\n`,
    );
  }

  /** Synchronous snapshot of the current process session. */
  getSessionStats(): SessionStats {
    const byTool: Record<string, ToolTokenStats> = {};
    for (const [tool, s] of this.byTool.entries()) {
      byTool[tool] = {
        calls: s.calls,
        inputEst: s.inputEst,
        outputEst: s.outputEst,
        avgMs: s.calls > 0 ? Math.round(s.totalMs / s.calls) : 0,
      };
    }
    return {
      calls: this.totalCalls,
      inputEst: this.totalInputEst,
      outputEst: this.totalOutputEst,
      byTool,
      sessionCacheHits: this.sessionCacheHits,
      sessionTokensSaved: this.sessionTokensSaved,
    };
  }

  /**
   * Async read of the JSONL log file to compute all-time historical totals,
   * merged with the current session stats.
   *
   * Also surfaces the top-5 largest individual calls (by output size) so the
   * caller can see *which* queries are the biggest token consumers.
   */
  async getFullStats(): Promise<FullStats> {
    const session = this.getSessionStats();
    let historicalCalls = 0;
    let historicalInputEst = 0;
    let historicalOutputEst = 0;
    const sessionsSeen = new Set<string>();
    const top: LogEntrySummary[] = [];
    const TOP_N = 5;
    let historicalCacheHits = 0;
    let historicalTokensSaved = 0;

    if (this.logPath && existsSync(this.logPath)) {
      try {
        const raw = await readFile(this.logPath, "utf-8");
        for (const line of raw.split("\n")) {
          const trimmed = line.trim();
          if (!trimmed) continue;
          try {
            const e = JSON.parse(trimmed) as TokenLogEntry;
            historicalCalls++;
            historicalInputEst += e.inputEst ?? 0;
            historicalOutputEst += e.outputEst ?? 0;
            if (e.sessionId) sessionsSeen.add(e.sessionId);
            if (e.cached) historicalCacheHits++;
            if (e.outputSavedEst) historicalTokensSaved += e.outputSavedEst;

            // Maintain top-N by outputEst. With N=5 a linear scan + insertion
            // is O(N) per entry — fine for log files up to ~100k entries.
            // Redact any raw (non-hash) args under shared — the log may carry
            // personal-profile entries this shared reader must not re-serve.
            const summaryArgs = redactArgsForProfile(e.args, this.serverProfile);
            const summary: LogEntrySummary = {
              ts: e.ts,
              tool: e.tool,
              inputEst: e.inputEst ?? 0,
              outputEst: e.outputEst ?? 0,
              ...(summaryArgs ? { args: summaryArgs } : {}),
            };
            if (top.length < TOP_N) {
              top.push(summary);
              top.sort((a, b) => b.outputEst - a.outputEst);
            } else if (summary.outputEst > top[top.length - 1].outputEst) {
              top[top.length - 1] = summary;
              top.sort((a, b) => b.outputEst - a.outputEst);
            }
          } catch {
            // Malformed line — skip silently.
          }
        }
      } catch {
        // Log file unreadable — return session-only stats.
      }
    }

    return {
      ...session,
      historicalCalls,
      historicalInputEst,
      historicalOutputEst,
      topLargestCalls: top,
      historicalSessions: sessionsSeen.size,
      historicalCacheHits,
      historicalTokensSaved,
      sessionId: this.sessionId,
      logPath: this.logPath,
    };
  }
}

/**
 * In-process response cache for MCP tool calls.
 *
 * The goal is NOT to save CPU — it is to avoid re-shipping the same multi-KB
 * response to the model when it calls the same tool with the same args twice
 * within a short window.  Anthropic's prompt cache makes re-reading conversation
 * history cheap, but each fresh tool_result block adds new tokens to the
 * context window.  Returning a small stub on repeat saves those tokens.
 *
 * Design choices:
 *   - Memory only.  A new server process starts with an empty cache.  This is
 *     intentional: cache hits are only useful within a live conversation, and
 *     a server restart usually coincides with a new session anyway.
 *   - TTL is per-tool.  Tools whose underlying source is loaded once at server
 *     startup (skills, agents, references, context guides) safely use a longer
 *     TTL than tools that read live state on each request.
 *   - Skip dedup for small responses.  No point spending complexity on tools
 *     that already return < 1K tokens; they're a rounding error.
 *   - Escape hatch via `force: true` on the args object.  The model can bypass
 *     the cache when it deliberately wants a fresh fetch.
 */

import { createHash } from "node:crypto";

/** Metadata for a cached response — body is intentionally not retained. */
export interface CachedResponse {
  /** Wall-clock time the response was first seen. */
  ts: number;
  /** First 8 hex chars of sha256(content) — lets the model fingerprint reuse. */
  hashHex: string;
  /** Estimated tokens in the original output. */
  outputEst: number;
  /** Original tool name. */
  tool: string;
  /** Canonical JSON-encoded args. */
  argsKey: string;
  /**
   * Opaque caller-supplied version tag (e.g. a directory fingerprint).
   * If set, a cache hit also requires the request to supply the same tag.
   * Lets file-backed tools invalidate their cache when source changes.
   */
  versionTag?: string;
}

/** Result of a cache lookup. */
export interface CacheHit {
  cached: CachedResponse;
  /** A short text stub to return in lieu of the full response. */
  stub: string;
  /** Tokens that would have been re-shipped without the cache. */
  outputSavedEst: number;
}

const DEFAULT_TTL_MS = 5 * 60 * 1000;
const DEFAULT_MIN_SIZE = 1000;

export interface ResponseCacheOptions {
  /** Default TTL applied to any tool not in `perToolTtlMs`. */
  ttlMs?: number;
  /**
   * Per-tool TTL overrides.  Tools whose underlying source is loaded once at
   * server startup (skills, agents, references, context guides) can safely
   * use a longer TTL than tools that read live state.
   */
  perToolTtlMs?: Record<string, number>;
  /** Default token threshold below which responses are not cached. */
  minSizeToDedupeTokens?: number;
  /**
   * Per-tool min-size overrides.  Tools whose responses are typically small
   * (e.g. get_app_context returns 200-700 tok) can opt into caching at a
   * lower threshold than the global default.
   */
  perToolMinSizeTokens?: Record<string, number>;
}

export class ResponseCache {
  private readonly entries = new Map<string, CachedResponse>();
  private readonly defaultTtlMs: number;
  private readonly perToolTtlMs: Record<string, number>;
  private readonly defaultMinSize: number;
  private readonly perToolMinSize: Record<string, number>;

  constructor(opts: ResponseCacheOptions = {}) {
    this.defaultTtlMs = opts.ttlMs ?? DEFAULT_TTL_MS;
    this.perToolTtlMs = opts.perToolTtlMs ?? {};
    this.defaultMinSize = opts.minSizeToDedupeTokens ?? DEFAULT_MIN_SIZE;
    this.perToolMinSize = opts.perToolMinSizeTokens ?? {};
  }

  /** Resolve the effective TTL for a given tool. */
  private ttlForTool(tool: string): number {
    return this.perToolTtlMs[tool] ?? this.defaultTtlMs;
  }

  /** Resolve the effective min-size threshold for a given tool. */
  private minSizeForTool(tool: string): number {
    return this.perToolMinSize[tool] ?? this.defaultMinSize;
  }

  /**
   * Build a stable cache key from a tool name and args object.
   *
   * We strip the `force` flag (it's a cache-bypass signal, not part of the
   * logical query) and sort top-level keys to ensure stability across
   * different argument orderings.
   */
  static makeKey(tool: string, args: Record<string, unknown>): string {
    const filtered: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(args)) {
      if (k === "force") continue;
      filtered[k] = v;
    }
    const sortedKeys = Object.keys(filtered).sort();
    const canonical: Record<string, unknown> = {};
    for (const k of sortedKeys) canonical[k] = filtered[k];
    return `${tool} ${JSON.stringify(canonical)}`;
  }

  /**
   * Look up a recent matching response.  Returns null on miss or expiry.
   * Side effect: removes expired or version-mismatched entries lazily.
   *
   * @param opts.versionTag  When supplied, the cache entry must carry the
   *   same tag to be a hit.  Lets file-backed tools (get_app_context)
   *   invalidate when the source directory's fingerprint changes.
   */
  check(
    tool: string,
    args: Record<string, unknown>,
    opts: { versionTag?: string } = {},
  ): CacheHit | null {
    const key = ResponseCache.makeKey(tool, args);
    const cached = this.entries.get(key);
    if (!cached) return null;

    const ttlMs = this.ttlForTool(tool);
    const ageMs = Date.now() - cached.ts;
    if (ageMs > ttlMs) {
      this.entries.delete(key);
      return null;
    }

    // Version-tag mismatch counts as a miss and evicts the stale entry.
    // Treat undefined as "no tag" — only reject when the tags differ AND
    // at least one was set, so callers that don't supply tags don't pay
    // a penalty.
    if (
      (cached.versionTag !== undefined || opts.versionTag !== undefined) &&
      cached.versionTag !== opts.versionTag
    ) {
      this.entries.delete(key);
      return null;
    }

    return {
      cached,
      stub: this.formatStub(cached, ageMs, ttlMs),
      outputSavedEst: cached.outputEst,
    };
  }

  /**
   * Record a successful response so future repeat calls can be deduped.
   * No-op for responses below `minSizeToDedupeTokens` — small outputs aren't
   * worth the overhead.
   */
  record(
    tool: string,
    args: Record<string, unknown>,
    content: string,
    opts: { versionTag?: string } = {},
  ): CachedResponse | null {
    const outputEst = Math.ceil(content.length / 4);
    if (outputEst < this.minSizeForTool(tool)) return null;

    const argsKey = ResponseCache.makeKey(tool, args);
    const hashHex = createHash("sha256").update(content).digest("hex").slice(0, 8);
    const entry: CachedResponse = {
      ts: Date.now(),
      hashHex,
      outputEst,
      tool,
      argsKey,
      ...(opts.versionTag !== undefined ? { versionTag: opts.versionTag } : {}),
    };
    this.entries.set(argsKey, entry);
    return entry;
  }

  /** Number of live entries.  Used for diagnostics, not flow control. */
  size(): number {
    return this.entries.size;
  }

  /**
   * Serialize all live entries for persistence.  Caller is responsible for
   * actually writing the result to disk (or wherever).  No filtering — the
   * counterpart `loadEntries` is responsible for deciding what to restore.
   */
  serialize(): CachedResponse[] {
    return Array.from(this.entries.values());
  }

  /**
   * Restore previously-serialized entries.  Drops anything older than
   * `maxAgeMs` so we don't restore stubs from a prior, possibly unrelated
   * conversation.
   *
   * Returns the number of entries actually restored (after filtering).
   */
  loadEntries(entries: CachedResponse[], maxAgeMs: number): number {
    const now = Date.now();
    let restored = 0;
    for (const e of entries) {
      if (!e || typeof e.tool !== "string" || typeof e.argsKey !== "string") {
        continue;
      }
      if (typeof e.ts !== "number" || now - e.ts > maxAgeMs) continue;
      this.entries.set(e.argsKey, e);
      restored++;
    }
    return restored;
  }

  /**
   * Render the small text stub returned to the model on a cache hit.
   * Includes everything the model needs to either trust the prior result
   * or re-fetch with `force: true`.
   */
  private formatStub(
    cached: CachedResponse,
    ageMs: number,
    ttlMs: number,
  ): string {
    const ageHuman = formatAge(ageMs);
    const ttlHuman = formatAge(ttlMs);
    const argsDisplay = cached.argsKey.split(" ")[1] ?? "{}";
    return [
      "[MCP response cache] Identical output already returned in this session.",
      `- Tool: ${cached.tool}`,
      `- Args: ${argsDisplay}`,
      `- When: ${new Date(cached.ts).toISOString()} (${ageHuman} ago, TTL ${ttlHuman})`,
      `- Hash: ${cached.hashHex}`,
      `- Size: ~${cached.outputEst.toLocaleString("en-US")} tokens`,
      "",
      "Reuse the prior tool_result with this hash.",
      "If the prior result is no longer in context or you need fresh data, " +
        'call again with `"force": true` to bypass the cache.',
    ].join("\n");
  }
}

function formatAge(ms: number): string {
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const remSec = sec % 60;
  return remSec > 0 ? `${min}m ${remSec}s` : `${min}m`;
}

#!/usr/bin/env node

/**
 * agent-kit MCP Server
 *
 * Exposes the workspace knowledge base — skills, agents, references, context
 * guides, CLAUDE.md files, and the artifact-memory substrate — as MCP tools
 * over stdio transport.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import fs from "node:fs/promises";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import type { CachedResponse } from "./cache/response-cache.js";

import { loadConfig } from "./config.js";
import {
  discoverSkillsMerged,
  discoverAgentsMerged,
  discoverReferencesMerged,
  discoverContextGuides,
  discoverMemory,
  discoverClaudeMdFilesMerged,
} from "./discovery/index.js";
import { buildIndex, search } from "./search/index.js";
import type { SearchDocument } from "./search/index.js";
import { createSkillTools } from "./tools/skills.js";
import { createReferenceTools } from "./tools/references.js";
import { createMemoryTools } from "./tools/memory.js";
import { createContextGuideTools } from "./tools/context-guides.js";
import { createSearchTools } from "./tools/search.js";
import { createAgentsRegistryTools } from "./tools/agents-registry.js";
import {
  createArtifactMemoryTools,
  probeArtifactMemory,
} from "./tools/artifact-memory.js";
import { TokenLogger } from "./logging/token-logger.js";
import { createTokenStatsTools } from "./tools/token-stats.js";
import { ResponseCache } from "./cache/response-cache.js";
import { guardToolInput, resolveGuardMode, type GuardMode } from "./security/guard.js";

// ---------------------------------------------------------------------------
// Startup — discover content and build search index
// ---------------------------------------------------------------------------

const config = loadConfig();

const log = (msg: string) => process.stderr.write(`[agent-kit-mcp] ${msg}\n`);

log("Discovering workspace knowledge...");

const [skills, agents, references, contextGuides, memories, claudeMdFiles] =
  await Promise.all([
    discoverSkillsMerged(config.dataSkillsDir, config.skillsDir),
    discoverAgentsMerged(config.dataAgentsDir, config.agentsDir),
    discoverReferencesMerged(config.dataReferencesDir, config.referencesDir),
    discoverContextGuides(config.contextGuidesDir),
    discoverMemory(config.memoryDir),
    discoverClaudeMdFilesMerged(
      config.platformRoot,
      config.workspaceRoot,
      config.dataClaudeMdDir,
      config.claudeMdGlobs,
    ),
  ]);

log(`  Skills: ${skills.length}`);
log(`  Agents: ${agents.length}`);
log(`  References: ${references.length}`);
log(`  Memory (local/private): ${memories.length}`);
log(`  Context guides: ${contextGuides.length}`);
log(`  CLAUDE.md files: ${claudeMdFiles.length}`);

// Trust-profile gate (SERVER_PROFILE=shared): the personal-memory tier is
// excluded from BOTH the search index AND the memory tools. list_memory /
// get_memory read this array directly (bypassing the index), so gating at this
// single upstream variable closes that side door too — a shared deployment must
// not leak personal memory through ANY tool, not just search.
const memoryVisible = config.serverProfile === "personal" ? memories : [];
if (config.serverProfile === "shared" && memories.length > 0) {
  log(`  Memory tier EXCLUDED (shared profile): ${memories.length} entries hidden`);
}

// State the memory tier explicitly. "Disabled because nothing resolved" and
// "enabled but empty" are the same silent `0` in the count above, so an
// operator could not tell a misconfiguration from an empty corpus. The enabled
// line always carries the path — that presence is the discriminator.
if (config.serverProfile === "personal") {
  if (config.memoryDir === "") {
    log(
      process.env.MEMORY_ROOT === ""
        ? "memory tier disabled: MEMORY_ROOT is set to the empty string"
        : "memory tier disabled: no MEMORY_ROOT, and no resolvable home directory or WORKSPACE_ROOT",
    );
  } else {
    log(
      `memory tier enabled, ${memories.length} entries at ${config.memoryDir}`,
    );
  }
}

// Build search index from all content sources
const searchDocuments: SearchDocument[] = [
  ...skills.map((s) => ({
    path: `skills/${s.name}`,
    title: s.name,
    content: s.content,
    type: "skill" as const,
  })),
  ...agents.map((a) => ({
    path: `agents/${a.name}`,
    title: a.name,
    content: a.content,
    type: "agent" as const,
  })),
  ...references.map((r) => ({
    path: r.path,
    title: r.name,
    content: r.content,
    type: "reference" as const,
  })),
  // Personal-memory tier — LOCAL + PRIVATE. Indexed (personal profile only) so
  // the engineer's own lessons are findable on their own machine
  // (search_platform_knowledge + the memory-scoped search_memory). Never shared /
  // pushed / promoted; excluded entirely under SERVER_PROFILE=shared (memoryVisible).
  ...memoryVisible.map((m) => ({
    path: m.path,
    title: m.name,
    content: m.content,
    type: "memory" as const,
  })),
  ...contextGuides.map((g) => ({
    path: g.path,
    title: g.topic,
    content: g.content,
    type: "context-guide" as const,
  })),
  ...claudeMdFiles.map((c) => ({
    path: c.path,
    title: c.name,
    content: c.content,
    type: "claude-md" as const,
  })),
];

const searchIndex = buildIndex(searchDocuments);
log(`  Search index: ${searchDocuments.length} documents indexed`);

/**
 * Render the `(e.g., …)` fragment of a tool's argument description from names
 * this server actually discovered.
 *
 * The examples used to be hardcoded: one Kubernetes-tooling skill name for
 * get_skill and three platform agent names for get_agent. None of the four
 * survived the genericization fork, and because argument descriptions live
 * inside `inputSchema`, `tests/fixtures/tools-list.golden.json` froze the stale
 * text into a *passing* contract test — the gate certified the drift instead of
 * catching it. Deriving from `skills` / `agents` / `references` means the
 * examples cannot outlive the content again (M2, gates-green-t-tool-example-names).
 *
 * Only the bundled `data/` tiers are used. Context guides and personal memory
 * resolve from host-dependent directories, so deriving examples from those
 * would make the golden fixture differ per machine.
 */
function derivedExamples(names: readonly string[], limit: number): string {
  const picked = [...names].sort((a, b) => a.localeCompare(b)).slice(0, limit);
  return picked.length === 0 ? "" : ` (e.g., ${picked.join(", ")})`;
}

const SKILL_NAME_EXAMPLES = derivedExamples(skills.map((s) => s.name), 1);
const AGENT_NAME_EXAMPLES = derivedExamples(agents.map((a) => a.name), 3);
// A reference's `name` is its filename stem with hyphens turned into spaces for
// display. get_reference canonicalizes both, but the hyphenated stem is the form
// a caller reads off `ls data/references/`, so show that.
const REFERENCE_NAME_EXAMPLES = derivedExamples(
  references.map((r) => path.basename(r.path, ".md")),
  2,
);

// ---------------------------------------------------------------------------
// Create tool handlers
// ---------------------------------------------------------------------------

const skillTools = createSkillTools(skills);
const referenceTools = createReferenceTools(references);
// search_memory reuses the single shared index but post-filters to the memory
// tier. Over-fetch (×4) before filtering so a memory hit ranked below
// non-memory docs still surfaces within the caller's requested cap.
const memoryTools = createMemoryTools(memoryVisible, (query, maxResults) => {
  const cap = maxResults ?? 10;
  return search(searchIndex, query, cap * 4)
    .filter((r) => r.type === "memory")
    .slice(0, cap);
});
const contextGuideTools = createContextGuideTools(contextGuides);
const searchTools = createSearchTools((query, maxResults) =>
  search(searchIndex, query, maxResults),
);
const agentsRegistryTools = createAgentsRegistryTools(agents);
const ARTIFACT_MEMORY_TOOL_NAMES = new Set([
  "artifact_memory_status",
  "search_artifacts",
  "get_artifact",
  "query_temporal_facts",
]);
// Registration is decided by a capability PROBE, not by the profile alone: an
// unusable transport is a state to report, not an exception to die on. Before
// this, createArtifactMemoryTools() ran unguarded at module scope and threw on
// any host without AF_UNIX, killing the 13 tools that never touch the substrate.
//
// AGENT_KIT_PROBE_PLATFORM is a test-only override (mirroring the socketPath
// override on ArtifactMemoryConfig) so the win32 branch is reachable from a
// POSIX developer machine and from a Linux CI runner.
const artifactMemoryProbe = probeArtifactMemory({
  serverProfile: config.serverProfile,
  // loadConfig() owns the three-step resolution (ARTIFACT_MEMORY_SOCKET, then
  // service.socket_path from the runtime config, then the profile-aware
  // default). Without passing it, the adapter falls back to its OWN default and
  // AGENT_KIT_PROFILE is ignored — every profile dials the same socket.
  socketPath: config.artifactMemorySocketPath,
  platform: (process.env.AGENT_KIT_PROBE_PLATFORM as NodeJS.Platform) || undefined,
});

let artifactMemoryTools: ReturnType<typeof createArtifactMemoryTools> | undefined;
/** Set only when the group is unavailable through FAILURE, never through policy. */
let artifactMemoryUnavailable: string | undefined;

if (artifactMemoryProbe.available) {
  try {
    artifactMemoryTools = createArtifactMemoryTools({
      socketPath: artifactMemoryProbe.socketPath,
    });
  } catch (err) {
    // Belt-and-braces: the probe said yes, construction disagreed. Degrade
    // rather than exit — a stale socket must not cost the operator every tool.
    artifactMemoryUnavailable = `${
      err instanceof Error ? err.message : String(err)
    } (socket: ${artifactMemoryProbe.socketPath})`;
    log(`artifact-memory tools unavailable: ${artifactMemoryUnavailable}`);
  }
} else if (artifactMemoryProbe.reason === "disabled-by-profile") {
  log("  Artifact-memory tools EXCLUDED (shared profile)");
} else {
  artifactMemoryUnavailable =
    `${artifactMemoryProbe.reason}: ${artifactMemoryProbe.detail} ` +
    `[socket path from: ${config.artifactMemorySocketSource}]`;
  log(`artifact-memory tools unavailable: ${artifactMemoryUnavailable}`);
}

const artifactMemoryEnabled = artifactMemoryTools !== undefined;

const tokenLogger = new TokenLogger(config.tokenLogPath, config.serverProfile);
const tokenStatsTools = createTokenStatsTools(tokenLogger);



// AIDefence tool-input guard mode (E2 wiring — ruflo-security-salvage-m2).
// "enforce" blocks injection/traversal/command-injection tool inputs at dispatch
// (returns an isError with the matched rule id + class); "log" (DEFAULT) emits a
// structured stderr line and lets the call through. The default is log-only by
// design — this is a production read-only knowledge server, so we soak the FP
// rate on real traffic (L-6 ramp; M1 measured FP 0% on 984 real entries) before
// flipping to enforce. Flipping is env-only: set AIDEFENCE_GUARD_MODE=enforce.
// Any value other than the literal "enforce" (unset, typo, empty) → "log".
// CAVEAT (known, intentional): enforce mode WILL block tool inputs that merely
// QUOTE attack syntax in a benign context — e.g. a search_platform_knowledge
// query like "what does $(git rev-parse) do in helm values" trips CI-01, and a
// query about "ignore previous instructions" / "| sh" / "; rm" trips a PI-*/CI-*
// rule. This is a knowledge server, so engineers legitimately ask ABOUT those
// topics; the M1 0%-FP figure was measured on captured tool ARGUMENTS, not such
// free-text questions. The log default exists to measure that FP rate first — do
// NOT set AIDEFENCE_GUARD_MODE=enforce in any env (incl. chart values) without
// first reviewing the log-mode FP soak. Tightening CI-01 is out of scope (it
// would re-open command-injection FNs — see aidefence-rules.ts).
// EXCEPTION — SERVER_PROFILE=shared HARD-FORCES enforce (not overridable by
// AIDEFENCE_GUARD_MODE): a shared-trust surface must fail closed, and the
// benign-quote FP class above is an accepted tradeoff there. The log-mode soak
// applies only to the default (personal) single-trust-domain deployment.
const guardMode: GuardMode = resolveGuardMode(
  config.serverProfile,
  process.env.AIDEFENCE_GUARD_MODE,
);
log(`AIDefence tool-input guard mode: ${guardMode} (profile: ${config.serverProfile})`);

// Per-tool cache TTL.  Tools whose content is loaded once at server startup
// (skills, agents, references, context guides) safely use a longer TTL than
// tools that read live state on each request.
const MIN = 60 * 1000;
const responseCache = new ResponseCache({
  perToolTtlMs: {
    get_context_guide: 30 * MIN,
    get_skill: 30 * MIN,
    get_agent: 30 * MIN,
    get_reference: 30 * MIN,
    get_memory: 30 * MIN, // memory tree is loaded once at startup
    // search_memory uses the 5-min default (query-dependent)
    // search_platform_knowledge uses the 5-min default (query-dependent)
  },
});
log(
  `  Token log: ${config.tokenLogPath ?? "disabled"} (set TOKEN_LOG_PATH to override)`,
);
log(`  Session ID: ${tokenLogger.getSessionId()}`);
log(`  Response cache: enabled (5m default TTL, 30-60m for static content)`);

// --- Cache persistence -----------------------------------------------------
// Restore recent entries from the snapshot file so cache hits survive a
// server restart (e.g. when Claude Code respawns the MCP server but keeps
// the conversation alive).  We aggressively prune anything older than 5 min
// so we never restore stubs from an unrelated prior session — the model
// might not have those tool_results in its current context.
//
// Set CACHE_SNAPSHOT_PATH="" to disable.
const RESTORE_MAX_AGE_MS = 5 * MIN;
if (config.cacheSnapshotPath && existsSync(config.cacheSnapshotPath)) {
  try {
    const raw = await fs.readFile(config.cacheSnapshotPath, "utf-8");
    const parsed: CachedResponse[] = [];
    for (const line of raw.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        parsed.push(JSON.parse(trimmed) as CachedResponse);
      } catch {
        /* malformed line — skip */
      }
    }
    const restored = responseCache.loadEntries(parsed, RESTORE_MAX_AGE_MS);
    log(
      `  Cache restored: ${restored} entries from ${config.cacheSnapshotPath} ` +
        `(filtered to entries < ${RESTORE_MAX_AGE_MS / MIN} min old)`,
    );
  } catch {
    log("  Cache restore: snapshot unreadable — starting empty");
  }
}

/**
 * Write the cache to disk.  Used by both the periodic timer and the exit
 * handlers below.  Best-effort; errors are swallowed.
 *
 * Sync writes are used in exit handlers because Node's process.exit() does
 * not await pending Promises.  The async path runs from a setInterval that
 * keeps the file fresh between server restarts.
 */
async function writeCacheSnapshotAsync(): Promise<void> {
  if (!config.cacheSnapshotPath) return;
  try {
    mkdirSync(path.dirname(config.cacheSnapshotPath), { recursive: true });
    const lines = responseCache
      .serialize()
      .map((e) => JSON.stringify(e))
      .join("\n");
    await fs.writeFile(config.cacheSnapshotPath, lines + (lines ? "\n" : ""));
  } catch {
    /* best effort */
  }
}

function writeCacheSnapshotSync(): void {
  if (!config.cacheSnapshotPath) return;
  try {
    mkdirSync(path.dirname(config.cacheSnapshotPath), { recursive: true });
    const lines = responseCache
      .serialize()
      .map((e) => JSON.stringify(e))
      .join("\n");
    writeFileSync(config.cacheSnapshotPath, lines + (lines ? "\n" : ""));
  } catch {
    /* best effort */
  }
}

// Periodic snapshot — guards against hard kills that bypass exit handlers.
const SNAPSHOT_INTERVAL_MS = 30 * 1000;
const snapshotTimer = setInterval(() => {
  void writeCacheSnapshotAsync();
}, SNAPSHOT_INTERVAL_MS);
snapshotTimer.unref(); // don't keep the process alive just for this

// Exit-time snapshots.  beforeExit fires on natural drain; SIGTERM/SIGINT
// when the parent kills us (e.g. Claude Code shutting down the server).
process.on("beforeExit", () => {
  writeCacheSnapshotSync();
});
for (const sig of ["SIGTERM", "SIGINT"] as const) {
  process.on(sig, () => {
    writeCacheSnapshotSync();
    process.exit(0);
  });
}

if (config.cacheSnapshotPath) {
  log(
    `  Cache snapshot: ${config.cacheSnapshotPath} (writes every 30s + on exit)`,
  );
}

// ---------------------------------------------------------------------------
// MCP Server setup
// ---------------------------------------------------------------------------

const server = new Server(
  { name: "@chris-dare-dev/agent-kit", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

// Register tool definitions
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "artifact_memory_status",
      description:
        "Read-only status for the local artifact catalog, Qdrant index, Graphiti groups, and incremental skill receipts.",
      inputSchema: { type: "object" as const, properties: {} },
    },
    {
      name: "search_artifacts",
      description:
        "Semantic search across current cataloged workspace artifacts in local Qdrant. Historical snippets are disabled; results are discovery hints and should be verified with get_artifact.",
      inputSchema: {
        type: "object" as const,
        properties: {
          query: { type: "string", description: "Semantic search query" },
          max_results: {
            type: "number",
            description: "Maximum results, 1-25 (default 8)",
          },
          project: { type: "string" },
          artifact_type: { type: "string" },
          authority_class: { type: "string" },
          repository: { type: "string" },
          lifecycle_hint: { type: "string" },
        },
        required: ["query"],
      },
    },
    {
      name: "get_artifact",
      description:
        "Read one current canonical artifact by workspace-relative path. The backend verifies its catalog hash, size, mtime, path containment, and symlink safety before returning content.",
      inputSchema: {
        type: "object" as const,
        properties: {
          relative_path: {
            type: "string",
            description: "Path relative to the workspace",
          },
          max_chars: {
            type: "number",
            description: "Maximum content characters, 256-50000 (default 12000)",
          },
        },
        required: ["relative_path"],
      },
    },
    {
      name: "query_temporal_facts",
      description:
        "Read temporal facts from approved local Graphiti groups. Unapproved pilot access is disabled; this tool never writes Graphiti.",
      inputSchema: {
        type: "object" as const,
        properties: {
          query: { type: "string", description: "Fact text to match" },
          max_results: {
            type: "number",
            description: "Maximum results, 1-25 (default 8)",
          },
          group_id: {
            type: "string",
            description: "Optional exact Graphiti group",
          },
        },
        required: ["query"],
      },
    },
    {
      name: "list_skills",
      description:
        "List all available platform skills with name and description",
      inputSchema: { type: "object" as const, properties: {} },
    },
    {
      name: "get_skill",
      description:
        'Get a skill\'s SKILL.md content. Supports section chunking: omit `section` for full content (small skills) or auto-summary (large skills); pass `section: "list"` for a TOC of headings; pass `section: "<slug>"` for one section; pass `section: "all"` to force full content.',
      inputSchema: {
        type: "object" as const,
        properties: {
          name: {
            type: "string",
            description: `Skill name${SKILL_NAME_EXAMPLES}`,
          },
          section: {
            type: "string",
            description: "Optional section slug, or 'list'/'all'.",
          },
        },
        required: ["name"],
      },
    },
    {
      name: "list_references",
      description:
        "List all shared reference files (name + description). Use this first, then get_reference for the full content of one.",
      inputSchema: { type: "object" as const, properties: {} },
    },
    {
      name: "get_reference",
      description: "Get the full content of a shared reference file",
      inputSchema: {
        type: "object" as const,
        properties: {
          name: {
            type: "string",
            description: `Reference name${REFERENCE_NAME_EXAMPLES}`,
          },
        },
        required: ["name"],
      },
    },
    {
      name: "list_memory",
      description:
        "List this engineer's LOCAL/PRIVATE personal-memory entries (name + category + one-line summary). Memory is machine-specific and gitignored — never shared. Use get_memory to fetch one, search_memory to find by keyword.",
      inputSchema: { type: "object" as const, properties: {} },
    },
    {
      name: "get_memory",
      description:
        "Get the full content of one personal-memory file by name (the on-disk filename stem). Accepts a unique substring. Call list_memory for the available stems. LOCAL/PRIVATE tier.",
      inputSchema: {
        type: "object" as const,
        properties: {
          name: {
            type: "string",
            description:
              "Memory name (filename stem) or a unique substring of it",
          },
        },
        required: ["name"],
      },
    },
    {
      name: "search_memory",
      description:
        "Full-text search restricted to this engineer's LOCAL/PRIVATE personal-memory tree (project/feedback/reference/user notes). Use when you recall having a personal lesson but not its exact name. For shared platform knowledge use search_platform_knowledge instead.",
      inputSchema: {
        type: "object" as const,
        properties: {
          query: { type: "string", description: "Search query" },
          max_results: {
            type: "number",
            description: "Maximum results to return (default: 10)",
          },
        },
        required: ["query"],
      },
    },
    {
      name: "list_context_guides",
      description:
        "List all topical context guides (service-mesh, monitoring, networking, etc.)",
      inputSchema: { type: "object" as const, properties: {} },
    },
    {
      name: "get_context_guide",
      description:
        'Get a topical context guide. Topic supports partial matching, so a substring of the topic resolves it; call list_context_guides for the available topics. Large guides default to a chunked summary (intro + TOC); pass `section: "<slug>"` for one section, `section: "list"` for TOC only, or `section: "all"` for full content. Use chunking to save tokens when you only need part of the guide.',
      inputSchema: {
        type: "object" as const,
        properties: {
          topic: { type: "string", description: "Topic name or partial match" },
          section: {
            type: "string",
            description: "Optional section slug, or 'list'/'all'.",
          },
        },
        required: ["topic"],
      },
    },
    {
      name: "search_platform_knowledge",
      description:
        "Full-text search across all platform knowledge: skills, agents, references, context guides, and CLAUDE.md files",
      inputSchema: {
        type: "object" as const,
        properties: {
          query: { type: "string", description: "Search query" },
          max_results: {
            type: "number",
            description: "Maximum results to return (default: 10)",
          },
        },
        required: ["query"],
      },
    },
    {
      name: "list_agents",
      description:
        "List all available platform agents with name, description, and model. Use this first; then call get_agent for full details on a specific agent.",
      inputSchema: { type: "object" as const, properties: {} },
    },
    {
      name: "get_agent",
      description:
        'Get the definition for one named platform agent. Supports section chunking: omit `section` for full content (small agents) or auto-summary (large agents); pass `section: "list"` for headings, `section: "<slug>"` for one section, or `section: "all"` for full content.',
      inputSchema: {
        type: "object" as const,
        properties: {
          name: {
            type: "string",
            description: `Agent name${AGENT_NAME_EXAMPLES}`,
          },
          section: {
            type: "string",
            description: "Optional section slug, or 'list'/'all'.",
          },
        },
        required: ["name"],
      },
    },
    {
      name: "get_token_stats",
      description:
        'Show estimated MCP token usage. Default `mode: "summary"` returns per-tool counts, session totals, and top-5 historical large calls (~250-400 tok). `mode: "detail"` adds hot queries, content-hash dupes, chunking adoption, and cache-bypass rate (~1-1.5K tok). Itself cache-exempt and not token-logged.',
      inputSchema: {
        type: "object" as const,
        properties: {
          mode: {
            type: "string",
            enum: ["summary", "detail"],
            description: "summary (default) or detail",
          },
        },
      },
    },
  ].filter(
    (tool) =>
      artifactMemoryEnabled || !ARTIFACT_MEMORY_TOOL_NAMES.has(tool.name),
  ),
}));

// Tools that should never be deduped — they're either cheap (list/stats) or
// expected to return fresh state each call (token-stats, artifact memory).
const CACHE_EXEMPT_TOOLS = new Set<string>([
  "artifact_memory_status",
  "search_artifacts",
  "get_artifact",
  "query_temporal_facts",
  "get_token_stats",
  "list_skills",
  "list_references",
  "list_memory",
  "list_context_guides",
  "list_agents",
]);

// Tools that route their output through `chunkByPolicy`.  Used to detect
// when a response was a TOC / single-section view rather than the full body
// — the analyzer reports on whether chunking is actually being used.
const CHUNKABLE_TOOLS = new Set<string>([
  "get_context_guide",
  "get_skill",
  "get_agent",
]);

/**
 * Heuristic chunked-response detector.  Looks at the response text plus the
 * caller's `section` arg to decide whether the model received a reduced
 * view of the source.  Used purely for analytics — never gates behavior.
 */
function detectChunked(
  toolName: string,
  args: Record<string, unknown>,
  output: string,
): boolean {
  if (!CHUNKABLE_TOOLS.has(toolName)) return false;
  const section = String(args.section ?? "").toLowerCase();
  if (section === "all") return false;
  if (section === "list" || section === "toc") return true;
  if (section) return true; // explicit named section
  // No section arg: chunkByPolicy auto-summarises only for large docs.  The
  // summary header is a stable marker.
  return output.includes("**Chunked response.**");
}

// Handle tool calls
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const argObj = (args ?? {}) as Record<string, unknown>;

  // Defense in depth: shared-profile clients cannot invoke artifact-memory
  // handlers even if they retained a stale tool schema from a personal server.
  //
  // Excluded BY POLICY and unavailable BY FAILURE are answered differently on
  // purpose. A shared deployment must not confirm the group even exists, so it
  // keeps the flat "Unknown tool". A personal deployment whose transport is
  // unusable should say so — the operator needs to know it is a host problem,
  // not a missing feature.
  if (!artifactMemoryEnabled && ARTIFACT_MEMORY_TOOL_NAMES.has(name)) {
    if (artifactMemoryUnavailable === undefined) {
      return {
        content: [{ type: "text", text: `Unknown tool: ${name}` }],
        isError: true,
      };
    }
    return {
      content: [
        {
          type: "text",
          text:
            `Tool ${name} is unavailable on this host — ${artifactMemoryUnavailable}. ` +
            "Every other tool is unaffected.",
        },
      ],
      isError: true,
    };
  }

  // Capture input size and start time for token logging.
  // get_token_stats is excluded from logging to avoid recursive noise.
  const shouldLog = name !== "get_token_stats";
  const inputText = shouldLog ? JSON.stringify(argObj) : "";
  const startMs = Date.now();
  const forceFresh = argObj.force === true;

  // --- AIDefence tool-input guard -----------------------------------------
  // Scan the serialized tool input for prompt-injection / path-traversal /
  // command-injection BEFORE the cache short-circuit, so a poisoned argObj is
  // evaluated on every call regardless of cache state and is never served from a
  // stub. The scan string is computed independently of `shouldLog` because
  // get_token_stats's `inputText` is "" (but its args carry no attack surface
  // anyway). This server has no external-write tool, so there is no PII egress
  // path to gate here.
  // Reuse the already-computed serialized args when available (every tool except
  // get_token_stats); recompute only for the one tool whose inputText is "" — so
  // get_token_stats stays scanned without a second stringify on the hot path.
  const guardInput = inputText !== "" ? inputText : JSON.stringify(argObj);
  const guardDecision = guardToolInput(guardInput, guardMode);
  if (guardDecision.blocked) {
    // enforce mode + a SAFETY match: return EARLY with the existing isError
    // content shape, before the cache check, the dispatch switch, and any
    // cache.record — so a block never poisons the dedup cache.
    if (shouldLog) {
      tokenLogger.logCall(name, inputText, "", Date.now() - startMs, {
        args: argObj,
        isError: true,
        error: `aidefence-blocked:${guardDecision.ruleId}`,
      });
    }
    // Symmetric with the log-mode line below, so a stderr tail shows BOTH
    // log-mode matches and enforce-mode blocks (channel-consistent soak/ops view).
    process.stderr.write(
      `[workspace-mcp] AIDEFENCE BLOCKED: tool=${name} rule=${guardDecision.ruleId} ` +
        `class=${guardDecision.cls} severity=${guardDecision.severity}\n`,
    );
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify({
            error: "Input blocked by AIDefence tool-input guard.",
            rule_id: guardDecision.ruleId,
            class: guardDecision.cls,
            severity: guardDecision.severity,
            mode: guardMode,
          }),
        },
      ],
      isError: true,
    };
  }
  if (guardDecision.matched) {
    // log mode (default): a rule fired but we are NOT enforcing. Emit ONE
    // structured stderr line and fall through to normal dispatch — do not block,
    // do not alter the result. Soak data accumulates here; flip to enforce via
    // AIDEFENCE_GUARD_MODE=enforce once the FP rate is confirmed 0 on real traffic.
    process.stderr.write(
      `[workspace-mcp] AIDEFENCE log-only: tool=${name} rule=${guardDecision.ruleId} ` +
        `class=${guardDecision.cls} severity=${guardDecision.severity} ` +
        `(not blocked; set AIDEFENCE_GUARD_MODE=enforce to block)\n`,
    );
  }

  // --- Response cache: short-circuit identical recent calls ---------------
  // No tool here reads live filesystem state per call, so nothing needs a
  // versionTag; all served content is loaded once at startup.
  const versionTag: string | undefined = undefined;
  // The model has the original tool_result in conversation context, so we
  // return a tiny stub instead of re-shipping the same multi-KB payload.
  if (!CACHE_EXEMPT_TOOLS.has(name) && !forceFresh) {
    const hit = responseCache.check(name, argObj, { versionTag });
    if (hit) {
      const result = { content: [{ type: "text", text: hit.stub }] };
      if (shouldLog) {
        tokenLogger.logCall(name, inputText, hit.stub, Date.now() - startMs, {
          cached: true,
          outputSavedEst: hit.outputSavedEst,
          args: argObj,
        });
      }
      return result;
    }
  }

  try {
    // Tool handlers may return a plain string or an MCP content object.
    // Normalize to MCP content format.
    type McpContent = {
      content: { type: string; text: string }[];
      isError?: boolean;
    };
    let raw: string | McpContent;

    switch (name) {
      // One guarded branch for the whole group: the dispatch guard above already
      // rejects these names when the group is unregistered, so reaching here
      // with no tools would be a bug — answer it as an error, never a crash.
      case "artifact_memory_status":
      case "search_artifacts":
      case "get_artifact":
      case "query_temporal_facts": {
        const tools = artifactMemoryTools;
        if (!tools) {
          raw = {
            content: [
              {
                type: "text",
                text: `Tool ${name} is unavailable — ${
                  artifactMemoryUnavailable ?? "the tool group is not registered"
                }.`,
              },
            ],
            isError: true,
          };
          break;
        }
        switch (name) {
          case "artifact_memory_status":
            raw = await tools.artifact_memory_status();
            break;
          case "search_artifacts":
            raw = await tools.search_artifacts(argObj);
            break;
          case "get_artifact":
            raw = await tools.get_artifact(argObj);
            break;
          default:
            raw = await tools.query_temporal_facts(argObj);
            break;
        }
        break;
      }
      case "list_skills":
        raw = await skillTools.list_skills();
        break;
      case "get_skill":
        raw = await skillTools.get_skill(
          argObj as { name: string; section?: string },
        );
        break;
      case "list_references":
        raw = await referenceTools.list_references();
        break;
      case "get_reference":
        raw = await referenceTools.get_reference(argObj as { name: string });
        break;
      case "list_memory":
        raw = await memoryTools.list_memory();
        break;
      case "get_memory":
        raw = await memoryTools.get_memory(argObj as { name: string });
        break;
      case "search_memory":
        raw = await memoryTools.search_memory(
          argObj as { query: string; max_results?: number },
        );
        break;
      case "list_context_guides":
        raw = await contextGuideTools.list_context_guides();
        break;
      case "get_context_guide":
        raw = await contextGuideTools.get_context_guide(
          argObj as { topic: string; section?: string },
        );
        break;
      case "search_platform_knowledge":
        raw = await searchTools.search_platform_knowledge(
          argObj as { query: string; max_results?: number },
        );
        break;
      case "list_agents":
        raw = await agentsRegistryTools.list_agents();
        break;
      case "get_agent":
        raw = await agentsRegistryTools.get_agent(
          argObj as { name: string; section?: string },
        );
        break;
      case "get_token_stats":
        raw = await tokenStatsTools.get_token_stats(
          argObj as { mode?: "summary" | "detail" },
        );
        break;
      default:
        return {
          content: [{ type: "text", text: `Unknown tool: ${name}` }],
          isError: true,
        };
    }

    // Normalize to MCP content format.
    let result: McpContent;
    if (typeof raw === "object" && raw !== null && "content" in raw) {
      result = raw;
    } else {
      const text = typeof raw === "string" ? raw : JSON.stringify(raw);
      result = { content: [{ type: "text", text }] };
    }

    const outputText = result.content.map((c) => c.text).join("");

    // Record successful (non-error) responses in the dedup cache so future
    // identical calls within the TTL get a stub.  versionTag is forwarded
    // so file-backed entries can self-invalidate on source change.
    if (!CACHE_EXEMPT_TOOLS.has(name) && !result.isError) {
      responseCache.record(name, argObj, outputText, { versionTag });
    }

    // Log token usage after normalization so we capture the actual output text.
    if (shouldLog) {
      tokenLogger.logCall(name, inputText, outputText, Date.now() - startMs, {
        args: argObj,
        isError: result.isError === true,
        chunked: detectChunked(name, argObj, outputText),
      });
    }

    return result;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (shouldLog) {
      tokenLogger.logCall(name, inputText, "", Date.now() - startMs, {
        error: message,
        args: argObj,
      });
    }
    return {
      content: [{ type: "text", text: `Error: ${message}` }],
      isError: true,
    };
  }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const transport = new StdioServerTransport();
await server.connect(transport);

log("Server started — listening on stdio");

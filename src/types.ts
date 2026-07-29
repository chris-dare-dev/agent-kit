/**
 * Shared type definitions for the platform MCP server.
 */

/** A Claude Code skill parsed from a SKILL.md file with YAML frontmatter. */
export interface SkillDefinition {
  /** Skill name (from frontmatter `name` field). */
  name: string;
  /** Human-readable description (from frontmatter `description` field). */
  description: string;
  /** Tools the skill is allowed to invoke (from frontmatter `allowed-tools`). */
  allowedTools: string[];
  /** Full markdown content of the skill file (body after frontmatter). */
  content: string;
  /** Absolute path to the skill's directory (may contain additional assets). */
  assetsPath: string;
}

/** A Claude Code agent parsed from an agent definition markdown file. */
export interface AgentDefinition {
  /** Agent name. */
  name: string;
  /** Human-readable description. */
  description: string;
  /** Tools the agent is allowed to invoke. */
  tools: string[];
  /** Model the agent should use (if specified). */
  model: string | undefined;
  /** Full markdown content of the agent definition. */
  content: string;
}

/** A reference file from .claude/references/ — quick-lookup structured data. */
export interface ReferenceFile {
  /** Display name derived from the filename (e.g. "environment-map"). */
  name: string;
  /** Absolute path to the reference file. */
  path: string;
  /** Full markdown content. */
  content: string;
}

/**
 * A personal-memory file from the per-engineer auto-memory tree
 * (`~/.claude/projects/<slug>/memory/*.md`). LOCAL + PRIVATE — gitignored,
 * machine-specific, never bundled in data/ and never shared.
 */
export interface MemoryFile {
  /** Display name derived from the filename (e.g. "reference_pqc_envoyfilter_constraints"). */
  name: string;
  /** Absolute path to the memory file. */
  path: string;
  /** Full markdown content (frontmatter included). */
  content: string;
  /** One-line summary from the frontmatter `description:` field ("" if absent). */
  description: string;
  /** Memory class from frontmatter `metadata.type:` — user | feedback | project | reference ("" if absent). */
  category: string;
}

/** A context guide from sandbox/context/ — topical deep-dive documentation. */
export interface ContextGuide {
  /** Display name derived from the filename (e.g. "networking"). */
  name: string;
  /** Absolute path to the context guide file. */
  path: string;
  /** Full markdown content. */
  content: string;
  /** Topic slug used for lookup (same as name, kept for query filtering). */
  topic: string;
}

/** A search result returned by the knowledge search tool. */
export interface SearchResult {
  /** Absolute path to the matching file. */
  path: string;
  /** Title extracted from the first H1 heading or the filename. */
  title: string;
  /** Text snippet around the match (with context lines). */
  snippet: string;
  /** Relevance score (higher is better, 0-1 range). */
  score: number;
  /** Content category. */
  type: "skill" | "agent" | "reference" | "context-guide" | "claude-md" | "memory";
}

/**
 * Server trust profile (SERVER_PROFILE env var).
 *  - "personal" (default): single-engineer local deployment — the personal-memory
 *    tier is indexed and served, AIDefence guard soaks in log mode, token-log args
 *    keep their compact preview.
 *  - "shared": a multi-tenant / shared-trust deployment — the personal-memory tier
 *    is excluded from the index AND the memory tools, the guard runs enforce
 *    (fail-closed), and token-log args are hashed. Precondition for any HTTP transport.
 */
export type ServerProfile = "personal" | "shared";

/** Resolved platform configuration — all paths derived from env vars. */
export type ArtifactMemorySocketSource =
  | "ARTIFACT_MEMORY_SOCKET"
  | "artifact-memory-runtime.json"
  | "default";

export interface PlatformConfig {
  /** Root of the platform monorepo (PLATFORM_ROOT env var). */
  platformRoot: string;
  /** Root of the workspace (WORKSPACE_ROOT env var). */
  workspaceRoot: string;
  /** Server trust profile (SERVER_PROFILE env var; default "personal"). */
  serverProfile: ServerProfile;
  /** Path to .claude/skills/ directory. */
  skillsDir: string;
  /** Path to .claude/agents/ directory. */
  agentsDir: string;
  /** Path to .claude/references/ directory. */
  referencesDir: string;
  /** Path to .claude/scripts/ directory. */
  scriptsDir: string;
  /** Path to sandbox/context/ directory (context guides). */
  contextGuidesDir: string;
  /** Path to the platform-level CLAUDE.md file. */
  platformClaudeMd: string;
  /** Glob patterns for discovering per-app CLAUDE.md files. */
  claudeMdGlobs: string[];
  /** Root for derived state; mirrors artifact_runtime.derived_root(). */
  artifactMemoryDerivedRoot: string;
  /** Resolved artifact-memory service socket. */
  artifactMemorySocketPath: string;
  /** Which resolution step supplied artifactMemorySocketPath. */
  artifactMemorySocketSource: ArtifactMemorySocketSource;
  /** Bundled data directory root (data/ in MCP server repo). */
  dataDir: string;
  /** Bundled skills directory (data/skills/). */
  dataSkillsDir: string;
  /** Bundled agents directory (data/agents/). */
  dataAgentsDir: string;
  /** Bundled references directory (data/references/). */
  dataReferencesDir: string;
  /** Bundled CLAUDE.md files directory (data/claude-md/). */
  dataClaudeMdDir: string;
  /**
   * Path to the per-engineer personal-memory tree
   * (`~/.claude/projects/<slug>/memory/`). LOCAL + PRIVATE; "" disables it.
   */
  memoryDir: string;
  /**
   * Absolute path for the JSONL token usage log.
   * null means logging is disabled (TOKEN_LOG_PATH="" or sandbox with no WORKSPACE_ROOT).
   */
  tokenLogPath: string | null;
  /**
   * Absolute path for the response-cache snapshot file (JSONL).
   * null means persistence is disabled — the cache lives only in memory.
   */
  cacheSnapshotPath: string | null;
}

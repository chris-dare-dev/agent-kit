/**
 * Configuration loader.
 *
 * Resolves all directory paths from PLATFORM_ROOT and WORKSPACE_ROOT
 * environment variables.  Missing directories are tolerated — callers
 * should check for existence before scanning.
 */

import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { PlatformConfig, ServerProfile } from "./types.js";

/**
 * Build a PlatformConfig from environment variables.
 *
 * PLATFORM_ROOT is required and must point to the platform monorepo root.
 *
 * WORKSPACE_ROOT is optional.  When absent (e.g. inside the sandbox
 * container where the workspace hierarchy does not exist),
 * it defaults to PLATFORM_ROOT and a warning is emitted to stderr.
 * All .claude/ directory paths are resolved relative to the effective
 * workspace root.
 *
 * @throws if PLATFORM_ROOT is not set.
 */
export function loadConfig(): PlatformConfig {
  const platformRoot = process.env.PLATFORM_ROOT;
  const workspaceRootEnv = process.env.WORKSPACE_ROOT;

  if (!platformRoot) {
    throw new Error(
      "PLATFORM_ROOT environment variable is required. " +
        "Set it to the platform monorepo root, e.g. " +
        "/Users/you/Work/workspace/platform",
    );
  }

  let workspaceRoot: string;
  if (workspaceRootEnv) {
    workspaceRoot = workspaceRootEnv;
  } else {
    process.stderr.write(
      "[claude-mcp-server] WORKSPACE_ROOT is not set; " +
        "defaulting to PLATFORM_ROOT (" +
        platformRoot +
        "). " +
        ".claude/ directories will be resolved relative to the platform root.\n",
    );
    workspaceRoot = platformRoot;
  }

  // Server trust profile. Any value other than the literal "shared" (unset, typo,
  // empty) resolves to "personal" — the safe default that preserves today's
  // single-engineer behavior (the NO-BREAK requirement). Resolved ONCE here; no
  // other module reads process.env.SERVER_PROFILE.
  const serverProfile: ServerProfile =
    process.env.SERVER_PROFILE === "shared" ? "shared" : "personal";

  const skillsDir = resolve(workspaceRoot, ".claude", "skills");
  const agentsDir = resolve(workspaceRoot, ".claude", "agents");
  const referencesDir = resolve(workspaceRoot, ".claude", "references");
  const scriptsDir = resolve(workspaceRoot, ".claude", "scripts");
  const contextGuidesDir = resolve(platformRoot, "sandbox", "context");

  // The platform-level CLAUDE.md may live at the repo root or under sandbox/.
  // Prefer the repo root; fall back to sandbox/CLAUDE.md.
  const rootClaudeMd = resolve(platformRoot, "CLAUDE.md");
  const sandboxClaudeMd = resolve(platformRoot, "sandbox", "CLAUDE.md");
  const platformClaudeMd = existsSync(rootClaudeMd)
    ? rootClaudeMd
    : sandboxClaudeMd;

  // Bundled data directory — lives at package root (one level above src/).
  // Works for both the source tree (src/config.ts → ../data/) and the compiled
  // output (dist/config.js → ../data/) because data/ sits at the package root
  // in both cases.
  const currentFile = fileURLToPath(import.meta.url);
  const dataDir = resolve(dirname(currentFile), "..", "data");
  const dataSkillsDir = resolve(dataDir, "skills");
  const dataAgentsDir = resolve(dataDir, "agents");
  const dataReferencesDir = resolve(dataDir, "references");
  const dataClaudeMdDir = resolve(dataDir, "claude-md");

  // Glob patterns that discover per-app CLAUDE.md files.
  const claudeMdGlobs = [
    // Helm charts
    resolve(platformRoot, "charts", "*", "CLAUDE.md"),
    // Custom app source
    resolve(platformRoot, "source", "*", "CLAUDE.md"),
    // Infrastructure
    resolve(platformRoot, "infra", "*", "CLAUDE.md"),
    // CI templates
    resolve(platformRoot, "ci-cd-templates", "CLAUDE.md"),
  ];

  // ---------------------------------------------------------------------------
  // Token log path — controls where per-call token estimates are written.
  //
  // Priority:
  //   1. TOKEN_LOG_PATH env var (explicit path, or "" to disable)
  //   2. Default: WORKSPACE_ROOT/.claude/mcp-token-log.jsonl  (local dev)
  //   3. null (disabled) when WORKSPACE_ROOT was not set (sandbox context)
  // ---------------------------------------------------------------------------
  let tokenLogPath: string | null;
  if (process.env.TOKEN_LOG_PATH !== undefined) {
    // Explicit override: empty string disables, any other value is the path.
    tokenLogPath = process.env.TOKEN_LOG_PATH || null;
  } else if (workspaceRootEnv) {
    // Local dev: log alongside other .claude/ artefacts. The shared profile uses
    // a distinct default file so it never reads/writes the same log a personal
    // server filled with raw-preview args (get_token_stats would re-serve them).
    tokenLogPath = resolve(
      workspaceRoot,
      ".claude",
      serverProfile === "shared" ? "mcp-token-log.shared.jsonl" : "mcp-token-log.jsonl",
    );
  } else {
    // Sandbox / CI: no workspace root → disable by default.
    tokenLogPath = null;
  }

  // Cache snapshot file — same priority rules as the token log.
  // Set CACHE_SNAPSHOT_PATH="" to disable.
  let cacheSnapshotPath: string | null;
  if (process.env.CACHE_SNAPSHOT_PATH !== undefined) {
    cacheSnapshotPath = process.env.CACHE_SNAPSHOT_PATH || null;
  } else if (workspaceRootEnv) {
    // Distinct default under shared (same reason as the token log above — the
    // snapshot stores raw argsKeys + acts as an existence/size oracle).
    cacheSnapshotPath = resolve(
      workspaceRoot,
      ".claude",
      serverProfile === "shared"
        ? "mcp-cache-snapshot.shared.jsonl"
        : "mcp-cache-snapshot.jsonl",
    );
  } else {
    cacheSnapshotPath = null;
  }

  // ---------------------------------------------------------------------------
  // Personal-memory tree path — the per-engineer auto-memory store. LOCAL and
  // PRIVATE: gitignored, machine-specific, never bundled in data/.
  //
  // Priority:
  //   1. MEMORY_ROOT env var (explicit absolute path, or "" to disable)
  //   2. Derived: <home>/.claude/projects/<workspace-slug>/memory, where
  //      <workspace-slug> is the workspace root with every "/" replaced by "-"
  //      (matches Claude Code's project-directory naming convention).
  //   3. "" (disabled) when the home directory or WORKSPACE_ROOT cannot be
  //      resolved (sandbox / CI). A missing or "" dir makes discoverMemory
  //      return [].
  //
  // Home comes from os.homedir(), NOT process.env.HOME. Under native PowerShell
  // — how Claude Code actually launches on Windows — HOME is undefined while
  // USERPROFILE is set, so keying on HOME silently disabled the entire tier and
  // list/get/search_memory returned empty results indistinguishable from "this
  // user has no memory files". homedir() consults USERPROFILE on Windows and
  // still honours HOME on POSIX.
  // ---------------------------------------------------------------------------
  const home = homedir();
  let memoryDir: string;
  if (process.env.MEMORY_ROOT !== undefined) {
    memoryDir = process.env.MEMORY_ROOT;
  } else if (home && workspaceRootEnv) {
    // Claude Code's project-dir slug replaces EVERY non-alphanumeric character
    // (path separators AND dots, e.g. "chris.dare" → "chris-dare") with "-".
    const slug = workspaceRoot.replace(/[^a-zA-Z0-9]/g, "-");
    memoryDir = resolve(home, ".claude", "projects", slug, "memory");
  } else {
    memoryDir = "";
  }

  return {
    platformRoot,
    workspaceRoot,
    serverProfile,
    skillsDir,
    agentsDir,
    referencesDir,
    scriptsDir,
    contextGuidesDir,
    platformClaudeMd,
    claudeMdGlobs,
    dataDir,
    dataSkillsDir,
    dataAgentsDir,
    dataReferencesDir,
    dataClaudeMdDir,
    memoryDir,
    tokenLogPath,
    cacheSnapshotPath,
  };
}

/**
 * Check whether a directory exists on disk.
 * Used by discovery code to skip missing optional directories.
 */
export function directoryExists(dirPath: string): boolean {
  try {
    return existsSync(dirPath);
  } catch {
    return false;
  }
}

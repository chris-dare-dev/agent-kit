/**
 * Configuration loader.
 *
 * Resolves all directory paths from PLATFORM_ROOT and WORKSPACE_ROOT
 * environment variables.  Missing directories are tolerated — callers
 * should check for existence before scanning.
 */

import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DEFAULT_CLAUDE_MD_GLOBS } from "./discovery/claude-md.js";
import type {
  ArtifactMemorySocketSource,
  PlatformConfig,
  ServerProfile,
} from "./types.js";

/**
 * Build a PlatformConfig from environment variables.
 *
 * Every variable is OPTIONAL — a bare `node dist/index.js` with an empty
 * environment starts and serves the bundled content. Overrides:
 *
 *   PLATFORM_ROOT      extra content root (default: the package root)
 *   WORKSPACE_ROOT     root for .claude/ lookups (default: PLATFORM_ROOT)
 *   CONTEXT_GUIDES_DIR context-guide directory
 *   CLAUDE_MD_GLOBS    comma-separated CLAUDE.md patterns, relative to each root
 *   MEMORY_ROOT        personal-memory directory ("" disables the tier)
 *   SERVER_PROFILE     "shared" to exclude the personal tiers
 *   TOKEN_LOG_PATH / CACHE_SNAPSHOT_PATH   ("" disables)
 *
 * @throws only if an explicitly supplied PLATFORM_ROOT does not exist — a bad
 *   value the operator typed must fail loudly rather than silently fall back.
 */
export function loadConfig(): PlatformConfig {
  const platformRootEnv = process.env.PLATFORM_ROOT;
  const workspaceRootEnv = process.env.WORKSPACE_ROOT;

  // The package root — the directory containing data/ — for both the source
  // tree (src/config.ts -> ../) and the compiled output (dist/config.js -> ../).
  const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

  // PLATFORM_ROOT used to be mandatory, and its error told the reader to point
  // the tool at a "platform monorepo" that has nothing to do with this
  // repository — so the documented Quick Start died on the first command. It is
  // now an optional override that defaults to the package root, which is where
  // a plain clone's content actually lives.
  //
  // An explicitly supplied value is still validated: silently falling back
  // would leave the operator staring at empty discovery counts, wondering why
  // the path they set had no effect.
  if (platformRootEnv !== undefined && !existsSync(platformRootEnv)) {
    throw new Error(
      `PLATFORM_ROOT was set to "${platformRootEnv}", which does not exist. ` +
        "Unset it to use the packaged content, or point it at a directory " +
        "that does.",
    );
  }
  const platformRoot = platformRootEnv || packageRoot;

  let workspaceRoot: string;
  if (workspaceRootEnv) {
    workspaceRoot = workspaceRootEnv;
  } else {
    process.stderr.write(
      "[agent-kit-mcp] WORKSPACE_ROOT is not set; " +
        "defaulting to " +
        platformRoot +
        ". " +
        ".claude/ directories will be resolved relative to that root.\n",
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

  // ---------------------------------------------------------------------------
  // Discovery block — where this server looks for content that is NOT bundled.
  // Every entry is overridable, because the built-in defaults cannot be right
  // for every layout and the previous hardcoded ones were right for exactly one.
  // ---------------------------------------------------------------------------

  // Context guides. `.claude/context` is the agent-kit-native location; the
  // legacy `sandbox/context` is still honoured when present so existing trees
  // (and the contract fixture) keep working.
  const workspaceContextDir = resolve(workspaceRoot, ".claude", "context");
  const legacyContextDir = resolve(platformRoot, "sandbox", "context");
  const contextGuidesDir =
    process.env.CONTEXT_GUIDES_DIR ||
    (existsSync(workspaceContextDir) ? workspaceContextDir : legacyContextDir);

  // The platform-level CLAUDE.md may live at the repo root or under sandbox/.
  // Prefer the repo root; fall back to sandbox/CLAUDE.md.
  const rootClaudeMd = resolve(platformRoot, "CLAUDE.md");
  const sandboxClaudeMd = resolve(platformRoot, "sandbox", "CLAUDE.md");
  const platformClaudeMd = existsSync(rootClaudeMd)
    ? rootClaudeMd
    : sandboxClaudeMd;

  // Bundled data directory — lives at the package root, for both the source
  // tree (src/config.ts → ../data/) and the compiled output (dist/config.js →
  // ../data/), because data/ sits at the package root in both cases.
  const dataDir = resolve(packageRoot, "data");
  const dataSkillsDir = resolve(dataDir, "skills");
  const dataAgentsDir = resolve(dataDir, "agents");
  const dataReferencesDir = resolve(dataDir, "references");
  const dataClaudeMdDir = resolve(dataDir, "claude-md");

  // Glob patterns that discover per-app CLAUDE.md files, RELATIVE to each scan
  // root (the platform root and, when different, the workspace root).
  //
  // These were four absolute employer-monorepo paths that were also never read:
  // discoverClaudeMdFilesMerged() ignored this field and used its own hardcoded
  // copy. Now this is the single source of truth and it is actually passed in.
  // Override with CLAUDE_MD_GLOBS as a comma-separated list.
  const claudeMdGlobs = (process.env.CLAUDE_MD_GLOBS
    ? process.env.CLAUDE_MD_GLOBS.split(",")
    : DEFAULT_CLAUDE_MD_GLOBS
  )
    .map((pattern) => pattern.trim())
    .filter((pattern) => pattern.length > 0);

  // ---------------------------------------------------------------------------
  // Derived-state root and the artifact-memory socket.
  //
  // This MIRRORS workspace-tooling/artifact_runtime.derived_root(); the two are
  // kept honest by the cross-language drift test in the substrate suite. Before
  // that test was revived it had been silently skipping, and the two halves had
  // drifted: this adapter dialled ~/.local/share/workspace-artifacts/... while
  // the provisioner bound ~/.local/share/personal-artifacts/..., so all four
  // artifact-memory tools were dead on arrival on every clean install.
  // ---------------------------------------------------------------------------
  const derivedRoot = (): string => {
    const override = process.env.AGENT_KIT_DERIVED_ROOT;
    if (override) return resolve(override);
    const xdg = process.env.XDG_DATA_HOME;
    if (xdg) return resolve(xdg, "agent-kit");
    if (process.platform === "win32") {
      const local = process.env.LOCALAPPDATA || resolve(homedir(), "AppData", "Local");
      return resolve(local, "agent-kit");
    }
    if (process.platform === "darwin") {
      return resolve(homedir(), "Library", "Application Support", "agent-kit");
    }
    return resolve(homedir(), ".local", "share", "agent-kit");
  };

  const artifactMemoryDerivedRoot = derivedRoot();

  // Three-step resolution. The SOURCE is carried alongside the path because an
  // unreachable socket is only diagnosable if the operator can tell which step
  // supplied the path they are looking at.
  let artifactMemorySocketPath: string;
  let artifactMemorySocketSource: ArtifactMemorySocketSource;
  if (process.env.ARTIFACT_MEMORY_SOCKET) {
    artifactMemorySocketPath = process.env.ARTIFACT_MEMORY_SOCKET;
    artifactMemorySocketSource = "ARTIFACT_MEMORY_SOCKET";
  } else {
    const runtimeConfig = resolve(artifactMemoryDerivedRoot, "artifact-memory-runtime.json");
    let fromRuntime: string | undefined;
    try {
      const parsed = JSON.parse(readFileSync(runtimeConfig, "utf-8")) as {
        service?: { socket_path?: unknown };
      };
      if (typeof parsed.service?.socket_path === "string" && parsed.service.socket_path) {
        fromRuntime = parsed.service.socket_path;
      }
    } catch {
      // Absent or unreadable is the normal case before provisioning; fall through.
    }
    if (fromRuntime) {
      artifactMemorySocketPath = fromRuntime;
      artifactMemorySocketSource = "artifact-memory-runtime.json";
    } else {
      // Same segments the provisioner names in SOCKET_RELATIVE_PARTS.
      artifactMemorySocketPath = resolve(
        artifactMemoryDerivedRoot,
        "services",
        "qdrant",
        "artifact-memory.sock",
      );
      artifactMemorySocketSource = "default";
    }
  }

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
    artifactMemoryDerivedRoot,
    artifactMemorySocketPath,
    artifactMemorySocketSource,
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

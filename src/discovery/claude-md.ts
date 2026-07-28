/**
 * CLAUDE.md file discovery — scans the platform directory tree for all
 * CLAUDE.md files across chart repos, source repos, infra repos, and the
 * workspace root.
 *
 * All paths are relative to the single active AWS account (000000000000).
 */

import fs from "node:fs/promises";
import path from "node:path";
import { glob } from "glob";
import type { ReferenceFile } from "../types.js";

/**
 * Discover all CLAUDE.md files across the platform monorepo and workspace.
 *
 * Scans these locations:
 *   - `{platformRoot}/charts/\*\/CLAUDE.md`          (Helm charts)
 *   - `{platformRoot}/source/\*\/CLAUDE.md`          (custom app source)
 *   - `{platformRoot}/infra/\*\/CLAUDE.md`           (IaC)
 *   - `{platformRoot}/ci-cd-templates/CLAUDE.md`     (CI templates)
 *   - `{platformRoot}/sandbox/CLAUDE.md`
 *   - `{workspaceRoot}/CLAUDE.md` (workspace root)
 *
 * Wildcard (chart/source/infra) entries are named by TIER + parent dir (e.g.
 * `charts/cert-manager/CLAUDE.md` -> "charts/cert-manager") so a chart and a
 * source app of the same basename stay distinct in the merge map. Standalone
 * files keep explicit names ("ci-cd-templates", "sandbox"); the workspace root
 * is "workspace".
 *
 * Returns files sorted alphabetically by name. Missing paths are silently
 * skipped.
 */
/**
 * Default discovery patterns, relative to the scan root.
 *
 * These replace four hardcoded employer-monorepo tiers (`charts/*`, `source/*`,
 * `infra/*`, `ci-cd-templates`) that matched nothing in an ordinary clone, so a
 * new adopter got silent zero-discovery and no way to say where their files are.
 *
 * MAX DEPTH IS 3 directories below the root, spelled out one level at a time
 * rather than as `**` on purpose: an unbounded walk of a monorepo (or of a home
 * directory someone points this at) is slow and surprising, and the bound is
 * then visible here instead of hidden in a glob option.
 */
export const DEFAULT_CLAUDE_MD_GLOBS = [
  "CLAUDE.md",
  "*/CLAUDE.md",
  "*/*/CLAUDE.md",
  "*/*/*/CLAUDE.md",
];

/** Never worth walking, and expensive when present. */
const IGNORED_DIRS = [
  "**/node_modules/**",
  "**/.git/**",
  "**/dist/**",
  "**/.venv/**",
  "**/venv/**",
  "**/__pycache__/**",
  "**/.next/**",
  "**/build/**",
];

/**
 * Name a discovered file by its directory path relative to the scan root, so
 * two files with the same basename stay distinct in the merge map.
 *
 * `charts/cert-manager/CLAUDE.md` -> "charts/cert-manager". A file at the root
 * itself has no directory part and becomes "workspace". Previously the
 * qualifier came from the first segment of the matching PATTERN, which only
 * worked because every pattern happened to start with a literal tier name; with
 * configurable globs that would produce names like "*" and re-introduce the
 * collision that let source/ silently overwrite charts/ in the search index.
 */
function nameForClaudeMd(root: string, filePath: string): string {
  const rel = path.relative(root, path.dirname(filePath));
  if (rel === "" || rel === ".") return "workspace";
  return rel.split(path.sep).join("/");
}

export async function discoverClaudeMdFiles(
  platformRoot: string,
  workspaceRoot?: string,
  globs: string[] = DEFAULT_CLAUDE_MD_GLOBS,
): Promise<ReferenceFile[]> {
  const results: ReferenceFile[] = [];
  // One entry per absolute path, so a file matched by several patterns — or
  // reachable from both roots — is read once and named once.
  const seen = new Set<string>();

  const roots = [platformRoot];
  if (workspaceRoot && path.resolve(workspaceRoot) !== path.resolve(platformRoot)) {
    roots.push(workspaceRoot);
  }

  for (const root of roots) {
    for (const pattern of globs) {
      let matches: string[];
      try {
        matches = await glob(pattern, {
          cwd: root,
          absolute: true,
          ignore: IGNORED_DIRS,
          follow: false,
        });
      } catch {
        // A missing or unreadable root is not an error: discovery is
        // best-effort and every source here is optional.
        continue;
      }

      for (const filePath of matches) {
        const resolved = path.resolve(filePath);
        if (seen.has(resolved)) continue;
        seen.add(resolved);
        const ref = await readClaudeMd(resolved, nameForClaudeMd(root, resolved));
        if (ref) results.push(ref);
      }
    }
  }

  results.sort((a, b) => a.name.localeCompare(b.name));
  return results;
}

/**
 * Scan all `.md` files directly inside `dir` and return them as ReferenceFile
 * objects. The display name is derived from the filename (strip `.md`,
 * replace hyphens with spaces). Missing or unreadable directories return [].
 */
export async function discoverReferencesInDir(
  dir: string,
): Promise<ReferenceFile[]> {
  let entries: string[];
  try {
    entries = await fs.readdir(dir);
  } catch {
    return [];
  }

  const results: ReferenceFile[] = [];
  for (const entry of entries) {
    if (!entry.endsWith(".md")) continue;
    const filePath = path.join(dir, entry);
    let stat;
    try {
      stat = await fs.stat(filePath);
    } catch {
      continue;
    }
    if (!stat.isFile()) continue;
    let content: string;
    try {
      content = await fs.readFile(filePath, "utf-8");
    } catch {
      continue;
    }
    const name = path.basename(entry, ".md").replaceAll("-", " ");
    results.push({ name, path: filePath, content: content.trim() });
  }
  return results;
}

/**
 * Merge CLAUDE.md files from the bundled `data/claude-md/` directory and the
 * live platform/workspace repos. Repo versions win when the same name appears
 * in both sources.
 *
 * @param platformRoot  Absolute path to the platform monorepo root.
 * @param workspaceRoot Optional absolute path to the workspace root.
 * @param dataClaudeMdDir Absolute path to the bundled `data/claude-md/` dir.
 */
export async function discoverClaudeMdFilesMerged(
  platformRoot: string,
  workspaceRoot: string | undefined,
  dataClaudeMdDir: string,
  globs: string[] = DEFAULT_CLAUDE_MD_GLOBS,
): Promise<ReferenceFile[]> {
  const [fromRepos, fromData] = await Promise.all([
    discoverClaudeMdFiles(platformRoot, workspaceRoot, globs),
    discoverReferencesInDir(dataClaudeMdDir),
  ]);
  const merged = new Map<string, ReferenceFile>();
  for (const f of fromData) merged.set(f.name, f);
  for (const f of fromRepos) merged.set(f.name, f); // repo versions win
  return [...merged.values()].sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Read a single CLAUDE.md file and return a ReferenceFile, or null if the
 * file doesn't exist or can't be read.
 *
 * @param filePath Absolute path to the CLAUDE.md file.
 * @param nameOverride If provided, use this as the name instead of deriving
 *   from the parent directory.
 */
async function readClaudeMd(
  filePath: string,
  nameOverride?: string,
): Promise<ReferenceFile | null> {
  let content: string;
  try {
    content = await fs.readFile(filePath, "utf-8");
  } catch {
    return null;
  }

  // Derive name from the parent directory name (e.g. "cert-manager" from
  // "charts/cert-manager/CLAUDE.md") unless an override is provided.
  const name = nameOverride ?? path.basename(path.dirname(filePath));

  return {
    name,
    path: filePath,
    content: content.trim(),
  };
}

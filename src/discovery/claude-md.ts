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
export async function discoverClaudeMdFiles(
  platformRoot: string,
  workspaceRoot?: string,
): Promise<ReferenceFile[]> {
  const results: ReferenceFile[] = [];

  // Wildcard glob patterns relative to platformRoot. If a directory does not
  // exist, glob returns [] (no throw), so missing optional directories are
  // handled gracefully.
  const patterns = [
    // Helm charts
    "charts/*/CLAUDE.md",
    // Custom app source
    "source/*/CLAUDE.md",
    // IaC
    "infra/*/CLAUDE.md",
  ];

  for (const pattern of patterns) {
    // "charts" | "source" | "infra" — used to qualify the display name so a
    // chart and a source app sharing a basename (6 such pairs today) don't
    // collide in the merge map. Pre-fix, the flat basename key let source/
    // silently overwrite charts/, dropping the chart CLAUDE.md from the search
    // index entirely (there is no name-addressed get_claude_md tool — the merged
    // list feeds only the search index).
    const tier = pattern.split("/")[0];
    let matches: string[];
    try {
      matches = await glob(pattern, {
        cwd: platformRoot,
        absolute: true,
      });
    } catch {
      continue;
    }

    for (const filePath of matches) {
      const base = path.basename(path.dirname(filePath));
      const ref = await readClaudeMd(filePath, `${tier}/${base}`);
      if (ref) results.push(ref);
    }
  }

  // Standalone (non-wildcard) CLAUDE.md files — single files at a known path.
  const standalones: Array<{ filePath: string; name: string }> = [
    {
      filePath: path.join(platformRoot, "ci-cd-templates", "CLAUDE.md"),
      name: "ci-cd-templates",
    },
    {
      filePath: path.join(platformRoot, "sandbox", "CLAUDE.md"),
      name: "sandbox",
    },
  ];

  for (const { filePath, name } of standalones) {
    const ref = await readClaudeMd(filePath, name);
    if (ref) results.push(ref);
  }

  // Workspace root CLAUDE.md.
  if (workspaceRoot) {
    const wsClaudeMd = path.join(workspaceRoot, "CLAUDE.md");
    const wsRef = await readClaudeMd(wsClaudeMd, "workspace");
    if (wsRef) results.push(wsRef);
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
): Promise<ReferenceFile[]> {
  const [fromRepos, fromData] = await Promise.all([
    discoverClaudeMdFiles(platformRoot, workspaceRoot),
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

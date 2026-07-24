/**
 * Context guide discovery - scans a context directory for topical .md files
 * and CLAUDE.md files in subdirectories.
 */

import fs from "node:fs/promises";
import path from "node:path";
import type { ContextGuide } from "../types.js";

/**
 * Discover all context guides in the given directory. Looks for:
 *   - Top-level *.md files (e.g. service-mesh.md, monitoring.md)
 *   - One-level-deep CLAUDE.md files in subdirectories
 *
 * The topic is derived from the filename (sans extension) for top-level
 * files, or from the subdirectory name for CLAUDE.md files.
 *
 * Returns guides sorted alphabetically by topic. Missing or unreadable
 * directories return an empty array (no throw).
 */
export async function discoverContextGuides(
  contextDir: string,
): Promise<ContextGuide[]> {
  let entries: string[];
  try {
    entries = await fs.readdir(contextDir);
  } catch {
    return [];
  }

  const guides: ContextGuide[] = [];

  for (const entry of entries) {
    const fullPath = path.join(contextDir, entry);

    let stat;
    try {
      stat = await fs.stat(fullPath);
    } catch {
      continue;
    }

    if (stat.isFile() && entry.endsWith(".md")) {
      // Top-level markdown file.
      let content: string;
      try {
        content = await fs.readFile(fullPath, "utf-8");
      } catch {
        continue;
      }

      const baseName = path.basename(entry, ".md");
      const topic = baseName.replace(/-/g, " ");

      guides.push({
        name: baseName,
        path: fullPath,
        content: content.trim(),
        topic,
      });
    } else if (stat.isDirectory()) {
      // Check for CLAUDE.md one level deep.
      const claudeMdPath = path.join(fullPath, "CLAUDE.md");
      let content: string;
      try {
        content = await fs.readFile(claudeMdPath, "utf-8");
      } catch {
        // No CLAUDE.md in this subdirectory — skip.
        continue;
      }

      const topic = entry.replace(/-/g, " ");

      guides.push({
        name: entry,
        path: claudeMdPath,
        content: content.trim(),
        topic,
      });
    }
  }

  guides.sort((a, b) => a.topic.localeCompare(b.topic));
  return guides;
}

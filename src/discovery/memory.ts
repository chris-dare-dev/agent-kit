/**
 * Personal-memory discovery — scans the per-engineer auto-memory tree
 * (`~/.claude/projects/<workspace-slug>/memory/*.md`) for `.md` files and
 * returns parsed MemoryFile objects.
 *
 * This tier is deliberately LOCAL and PRIVATE: the memory tree is gitignored
 * and machine-specific, so it is never bundled in `data/` and never shared.
 * Indexing it here only makes a single engineer's own lessons queryable on
 * their own machine — it closes the "I have a lesson and can't find it" gap
 * without promoting anything to the shared, MCP-served `data/references` tier.
 *
 * Like every other discoverer this is a FLAT `readdir()` (the memory tree is
 * flat); subdirectories are not recursed.
 */

import fs from "node:fs/promises";
import path from "node:path";
import type { MemoryFile } from "../types.js";

/**
 * Parse the leading YAML frontmatter fence of a memory file (if present) for
 * the `description:` line and the nested `metadata.type:` value. No YAML
 * dependency — a tolerant line scan over the first `---`…`---` block. Files
 * without frontmatter (e.g. MEMORY.md) yield empty description/category.
 */
function parseFrontmatter(content: string): {
  description: string;
  category: string;
} {
  let description = "";
  let category = "";
  const lines = content.split("\n");
  if (lines[0]?.trim() !== "---") return { description, category };

  let inMetadata = false;
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() === "---") break; // end of frontmatter

    const descMatch = /^description:\s*(.+)$/.exec(line);
    if (descMatch) {
      description = descMatch[1].trim();
      inMetadata = false;
      continue;
    }
    if (/^metadata:\s*$/.test(line)) {
      inMetadata = true;
      continue;
    }
    if (inMetadata) {
      const typeMatch = /^\s+type:\s*(.+)$/.exec(line);
      if (typeMatch) category = typeMatch[1].trim();
    }
  }
  return { description, category };
}

/**
 * Discover all memory files in the given directory. The display name is the
 * filename with the `.md` extension stripped (stable + unique on disk, e.g.
 * `reference_xappirsarole_rolename_xrd_pattern`).
 *
 * Returns memories sorted alphabetically by name. A missing or unreadable
 * directory returns an empty array (no throw) — graceful degradation when
 * MEMORY_ROOT is unset or the tree does not exist (sandbox / fresh checkout).
 */
export async function discoverMemory(memoryDir: string): Promise<MemoryFile[]> {
  let entries: string[];
  try {
    entries = await fs.readdir(memoryDir);
  } catch {
    return [];
  }

  const memories: MemoryFile[] = [];

  for (const entry of entries) {
    if (!entry.endsWith(".md")) continue;

    const filePath = path.join(memoryDir, entry);

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

    const name = path.basename(entry, ".md");
    const { description, category } = parseFrontmatter(content);

    memories.push({
      name,
      path: filePath,
      content: content.trim(),
      description,
      category,
    });
  }

  memories.sort((a, b) => a.name.localeCompare(b.name));
  return memories;
}

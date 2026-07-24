/**
 * Reference file discovery — scans a references directory for *.md files
 * and returns parsed ReferenceFile objects.
 */

import fs from "node:fs/promises";
import path from "node:path";
import type { ReferenceFile } from "../types.js";

/**
 * Discover all reference files in the given directory. Each reference is a
 * plain `.md` file. The display name is derived from the filename by
 * stripping the `.md` extension and replacing hyphens with spaces.
 *
 * Returns references sorted alphabetically by name. Missing or unreadable
 * directories return an empty array (no throw).
 */
export async function discoverReferences(
  referencesDir: string,
): Promise<ReferenceFile[]> {
  let entries: string[];
  try {
    entries = await fs.readdir(referencesDir);
  } catch {
    return [];
  }

  const references: ReferenceFile[] = [];

  for (const entry of entries) {
    if (!entry.endsWith(".md")) continue;

    const filePath = path.join(referencesDir, entry);

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

    const baseName = path.basename(entry, ".md");
    const name = baseName.replace(/-/g, " ");

    references.push({
      name,
      path: filePath,
      content: content.trim(),
    });
  }

  references.sort((a, b) => a.name.localeCompare(b.name));
  return references;
}

/**
 * Merge references from a primary directory and an overlay directory. Overlay
 * entries win when both directories define a reference with the same name.
 *
 * Missing or unreadable directories are silently treated as empty (delegated
 * to `discoverReferences`). Returns references sorted alphabetically by name.
 */
export async function discoverReferencesMerged(
  primaryDir: string,
  overlayDir: string,
): Promise<ReferenceFile[]> {
  const [primary, overlay] = await Promise.all([
    discoverReferences(primaryDir),
    discoverReferences(overlayDir),
  ]);
  const merged = new Map<string, ReferenceFile>();
  for (const r of primary) merged.set(r.name, r);
  for (const r of overlay) merged.set(r.name, r); // overlay wins
  return [...merged.values()].sort((a, b) => a.name.localeCompare(b.name));
}

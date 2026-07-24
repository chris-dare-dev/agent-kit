/**
 * Agent discovery — scans an agents directory for *.md agent definition
 * files with YAML frontmatter and returns parsed AgentDefinition objects.
 */

import fs from "node:fs/promises";
import path from "node:path";
import matter from "gray-matter";
import type { AgentDefinition } from "../types.js";

/**
 * Discover all agent definitions in the given directory. Each agent is a
 * single `.md` file with YAML frontmatter containing name, description,
 * tools, and (optionally) model.
 *
 * Returns agents sorted alphabetically by name. Missing or unreadable
 * directories return an empty array (no throw).
 */
export async function discoverAgents(
  agentsDir: string,
): Promise<AgentDefinition[]> {
  let entries: string[];
  try {
    entries = await fs.readdir(agentsDir);
  } catch {
    return [];
  }

  const agents: AgentDefinition[] = [];

  for (const entry of entries) {
    if (!entry.endsWith(".md")) continue;

    const filePath = path.join(agentsDir, entry);

    // Ensure it is a file, not a directory.
    let stat;
    try {
      stat = await fs.stat(filePath);
    } catch {
      continue;
    }
    if (!stat.isFile()) continue;

    let raw: string;
    try {
      raw = await fs.readFile(filePath, "utf-8");
    } catch {
      continue;
    }

    try {
      const parsed = matter(raw);
      const data = parsed.data as Record<string, unknown>;

      const baseName = path.basename(entry, ".md");
      const name = typeof data.name === "string" ? data.name : baseName;
      const description =
        typeof data.description === "string" ? data.description : "";
      const tools = parseStringOrList(data.tools);
      const model =
        typeof data.model === "string" ? data.model : undefined;

      agents.push({
        name,
        description,
        tools,
        model,
        content: parsed.content.trim(),
      });
    } catch {
      // Malformed frontmatter — skip.
      continue;
    }
  }

  agents.sort((a, b) => a.name.localeCompare(b.name));
  return agents;
}

/**
 * Merge agents from a primary directory and an overlay directory. Overlay
 * entries win when both directories define an agent with the same name.
 *
 * Missing or unreadable directories are silently treated as empty (delegated
 * to `discoverAgents`). Returns agents sorted alphabetically by name.
 */
export async function discoverAgentsMerged(
  primaryDir: string,
  overlayDir: string,
): Promise<AgentDefinition[]> {
  const [primary, overlay] = await Promise.all([
    discoverAgents(primaryDir),
    discoverAgents(overlayDir),
  ]);
  const merged = new Map<string, AgentDefinition>();
  for (const a of primary) merged.set(a.name, a);
  for (const a of overlay) merged.set(a.name, a); // overlay wins
  return [...merged.values()].sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Parse a frontmatter value that could be a YAML list, a comma-separated
 * string, or a single string into a string array.
 */
function parseStringOrList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.filter((v): v is string => typeof v === "string");
  }
  if (typeof value === "string") {
    return value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return [];
}

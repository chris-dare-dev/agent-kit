/**
 * Skill discovery — scans a skills directory for SKILL.md files with YAML
 * frontmatter and returns parsed SkillDefinition objects.
 */

import fs from "node:fs/promises";
import path from "node:path";
import matter from "gray-matter";
import type { SkillDefinition } from "../types.js";

/**
 * Discover all skills in the given directory. Each skill is a subdirectory
 * containing a `SKILL.md` file with YAML frontmatter (name, description,
 * allowed-tools). An optional `assets/` subdirectory may contain templates
 * and other supporting files.
 *
 * Returns skills sorted alphabetically by name. Missing or unreadable
 * directories return an empty array (no throw).
 */
export async function discoverSkills(
  skillsDir: string,
): Promise<SkillDefinition[]> {
  let entries: string[];
  try {
    entries = await fs.readdir(skillsDir);
  } catch {
    // Directory missing or unreadable — not an error, just no skills.
    return [];
  }

  const skills: SkillDefinition[] = [];

  for (const entry of entries) {
    const skillDir = path.join(skillsDir, entry);

    // Only look at directories.
    let stat;
    try {
      stat = await fs.stat(skillDir);
    } catch {
      continue;
    }
    if (!stat.isDirectory()) continue;

    const skillMdPath = path.join(skillDir, "SKILL.md");
    let raw: string;
    try {
      raw = await fs.readFile(skillMdPath, "utf-8");
    } catch {
      // No SKILL.md in this directory — skip.
      continue;
    }

    try {
      const parsed = matter(raw);
      const data = parsed.data as Record<string, unknown>;

      const name = typeof data.name === "string" ? data.name : entry;
      const description =
        typeof data.description === "string" ? data.description : "";

      // `allowed-tools` can be a YAML list or a comma-separated string.
      const allowedTools = parseStringOrList(data["allowed-tools"]);

      // Check for assets directory (specifically output-template.md).
      const assetsDir = path.join(skillDir, "assets");
      let assetsPath = skillDir; // default: the skill directory itself
      try {
        const assetsStat = await fs.stat(assetsDir);
        if (assetsStat.isDirectory()) {
          assetsPath = assetsDir;
        }
      } catch {
        // No assets directory — assetsPath stays as skillDir.
      }

      skills.push({
        name,
        description,
        allowedTools,
        content: parsed.content.trim(),
        assetsPath,
      });
    } catch {
      // Malformed frontmatter or other parse error — skip this skill.
      continue;
    }
  }

  skills.sort((a, b) => a.name.localeCompare(b.name));
  return skills;
}

/**
 * Merge skills from a primary directory and an overlay directory. Overlay
 * entries win when both directories define a skill with the same name.
 *
 * Missing or unreadable directories are silently treated as empty (delegated
 * to `discoverSkills`). Returns skills sorted alphabetically by name.
 */
export async function discoverSkillsMerged(
  primaryDir: string,
  overlayDir: string,
): Promise<SkillDefinition[]> {
  const [primary, overlay] = await Promise.all([
    discoverSkills(primaryDir),
    discoverSkills(overlayDir),
  ]);
  const merged = new Map<string, SkillDefinition>();
  for (const s of primary) merged.set(s.name, s);
  for (const s of overlay) merged.set(s.name, s); // overlay wins
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

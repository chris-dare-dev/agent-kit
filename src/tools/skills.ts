import fs from 'node:fs/promises';
import path from 'node:path';
import type { SkillDefinition } from '../types.js';
import { chunkByPolicy } from '../markdown/sections.js';
import { canonicalizeName } from '../discovery/canonicalize.js';

/**
 * Creates MCP tool handlers for listing and retrieving skill definitions.
 *
 * @param skills - The array of discovered SkillDefinitions, populated at server startup.
 */
export function createSkillTools(skills: SkillDefinition[]) {
  return {
    /**
     * list_skills — Return a JSON array of all discovered skills with name and description.
     */
    list_skills: async (): Promise<string> => {
      const entries = skills.map((s) => ({
        name: s.name,
        description: s.description,
      }));
      return JSON.stringify(entries, null, 2);
    },

    /**
     * get_skill — Return the full SKILL.md content for the named skill.
     * If the skill has an assets/output-template.md, appends it under an
     * "## Output Template" heading.
     *
     * Supports section chunking via the `section` arg — see chunkByPolicy:
     *   - omitted, small skill -> full content
     *   - omitted, large skill -> TOC + intro
     *   - "list"/"toc"         -> TOC only
     *   - "all"                -> full content
     *   - "<name>"             -> matching section, or TOC on miss
     */
    get_skill: async (args: { name: string; section?: string }): Promise<string> => {
      const target = canonicalizeName(args.name);
      const skill = skills.find((s) => canonicalizeName(s.name) === target);

      if (!skill) {
        const available = skills.map((s) => s.name).sort().join(', ');
        return `Skill "${args.name}" not found. Available skills: ${available}`;
      }

      let result = skill.content;

      const templatePath = path.join(skill.assetsPath, 'output-template.md');
      try {
        const template = await fs.readFile(templatePath, 'utf-8');
        result += `\n\n## Output Template\n\n${template}`;
      } catch {
        // No output-template.md — silently skip.
      }

      return chunkByPolicy(result, args.section);
    },
  };
}

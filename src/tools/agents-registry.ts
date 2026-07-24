import type { AgentDefinition } from "../types.js";
import { chunkByPolicy } from "../markdown/sections.js";
import { canonicalizeName } from "../discovery/canonicalize.js";

/**
 * Tool handlers for discovering and inspecting platform agents.
 *
 * Two tools are exposed:
 *
 *   - `list_agents`  — compact JSON inventory (name, description, model)
 *                      across all 17 agents.  ~1.7 KB.
 *   - `get_agent`    — full markdown definition for one named agent, with
 *                      optional section chunking (~1-3 KB full, less when
 *                      a section is requested).
 *
 * The previous `get_agents_registry` tool returned the full ~25 KB AGENTS.md
 * narrative on every call.  That content is still discoverable via
 * `search_platform_knowledge` (AGENTS.md remains in the search index), so
 * removing the dedicated tool reclaims ~6.3K tokens per former call without
 * losing access to the dispatch-pattern guidance.
 */
export function createAgentsRegistryTools(agents: AgentDefinition[]) {
  return {
    /**
     * Return a compact JSON inventory of all agents.  Use this first, then
     * call `get_agent` for any specific agent's full body.
     */
    list_agents: async (): Promise<string> => {
      const items = agents.map((a) => ({
        name: a.name,
        description: a.description,
        model: a.model || "default",
      }));
      return JSON.stringify(items, null, 2);
    },

    /**
     * Return the full definition for one named agent — frontmatter metadata
     * (description, model, tools) followed by the agent body.
     *
     * Supports section chunking — see chunkByPolicy:
     *   - omitted, small agent -> full content
     *   - omitted, large agent -> TOC + intro
     *   - "list"/"toc"         -> TOC only
     *   - "all"                -> full content
     *   - "<name>"             -> matching section, or TOC on miss
     */
    get_agent: async (args: { name: string; section?: string }): Promise<string> => {
      const target = canonicalizeName(args.name);
      const agent = agents.find((a) => canonicalizeName(a.name) === target);
      if (!agent) {
        const available = agents
          .map((a) => a.name)
          .sort()
          .join(", ");
        return `Agent "${args.name}" not found. Available agents: ${available}`;
      }

      const headerLines = [
        `# ${agent.name}`,
        "",
        `**Description**: ${agent.description}`,
        `**Model**: ${agent.model || "default"}`,
      ];
      if (agent.tools.length > 0) {
        headerLines.push(`**Tools**: ${agent.tools.join(", ")}`);
      }

      const fullText = `${headerLines.join("\n")}\n\n---\n\n${agent.content}`;
      return chunkByPolicy(fullText, args.section);
    },
  };
}

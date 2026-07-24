/**
 * MCP tool handlers for the per-engineer personal-memory tree.
 *
 * These tools make a single engineer's OWN auto-memory (`~/.claude/projects/
 * <slug>/memory/*.md`) queryable on their own machine. The tier stays LOCAL
 * and PRIVATE — nothing here is shared, pushed, or promoted to the MCP-served
 * `data/references` tier. The goal is narrow and concrete: end "I have a lesson
 * and can't find it" without forcing private notes into the shared knowledge base.
 *
 *   list_memory   — enumerate memory files (name + category + one-line summary)
 *   get_memory    — fetch one memory file by name
 *   search_memory — full-text search restricted to the memory tier
 */

import type { MemoryFile } from "../types.js";
import type { SearchResult } from "../search/index.js";

/**
 * Bind the discovered memory files (and a memory-scoped search function) at
 * startup and return the tool handler map.
 *
 * @param memories - All MemoryFile objects discovered from the memory tree.
 * @param searchMemoryFn - Function that runs the shared search index but
 *   returns only `type === "memory"` results (bound by the caller).
 */
export function createMemoryTools(
  memories: MemoryFile[],
  searchMemoryFn: (query: string, maxResults?: number) => SearchResult[],
) {
  return {
    list_memory: async () => {
      const items = memories.map((m) => ({
        name: m.name,
        category: m.category,
        description: m.description,
      }));
      return {
        content: [{ type: "text", text: JSON.stringify(items, null, 2) }],
      };
    },

    get_memory: async (args: { name: string }) => {
      const query = args.name.toLowerCase();
      // Exact name match first; fall back to a unique substring match so callers
      // can pass either the on-disk filename stem or a memorable fragment.
      let match = memories.find((m) => m.name.toLowerCase() === query);
      if (!match) {
        const partial = memories.filter((m) =>
          m.name.toLowerCase().includes(query),
        );
        if (partial.length === 1) match = partial[0];
      }

      if (!match) {
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                error: `Memory "${args.name}" not found (exact or unique-substring match).`,
                hint: "Use search_memory for fuzzy lookup, or list_memory to enumerate.",
              }),
            },
          ],
          isError: true,
        };
      }

      return { content: [{ type: "text", text: match.content }] };
    },

    search_memory: async (args: {
      query: string;
      max_results?: number;
    }): Promise<string> => {
      const results = searchMemoryFn(args.query, args.max_results ?? 10);
      if (results.length === 0) {
        return `No memory entries found for query: "${args.query}"`;
      }
      return results
        .map(
          (r) =>
            `[memory] ${r.title} (${r.path})\n  Score: ${r.score}\n  ${r.snippet}`,
        )
        .join("\n\n");
    },
  };
}

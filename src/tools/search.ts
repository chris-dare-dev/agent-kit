/**
 * MCP tool handler for searching platform knowledge.
 *
 * Wraps the search engine (src/search/index.ts) so that MCP clients can
 * query skills, agents, reference files, context guides, and CLAUDE.md
 * content through a single tool call.
 */

import type { SearchResult } from "../search/index.js";

/**
 * Factory that binds the search function at startup and returns the tool
 * handler map.  The search index is built once by the caller and passed in
 * here rather than re-built on every query.
 *
 * @param searchFn - Function that accepts a query string and optional
 *   max-results cap and returns an ordered array of SearchResult objects
 *   (highest score first).
 */
export function createSearchTools(
  searchFn: (query: string, maxResults?: number) => SearchResult[],
) {
  return {
    /**
     * Search the platform knowledge base.
     *
     * Searches across all indexed content — skills, agents, reference files,
     * context guides, and CLAUDE.md documentation — and returns the top
     * matching results formatted as readable text blocks.
     *
     * @param args.query      - Natural-language or keyword search query.
     * @param args.max_results - Maximum number of results to return (default 10).
     * @returns Formatted string with one block per result showing type, title,
     *   file path, relevance score, and a contextual snippet.
     */
    search_platform_knowledge: async (args: {
      query: string;
      max_results?: number;
    }): Promise<string> => {
      const results = searchFn(args.query, args.max_results ?? 10);

      if (results.length === 0) {
        return `No results found for query: "${args.query}"`;
      }

      return results
        .map(
          (r) =>
            `[${r.type}] ${r.title} (${r.path})\n  Score: ${r.score}\n  ${r.snippet}`,
        )
        .join("\n\n");
    },
  };
}

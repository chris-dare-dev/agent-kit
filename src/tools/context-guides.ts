import type { ContextGuide } from "../types.js";
import { chunkByPolicy } from "../markdown/sections.js";
import { canonicalizeName } from "../discovery/canonicalize.js";

/**
 * Returns true if the query matches the guide's topic.
 * Canonicalizes both sides (hyphen/space/case) so `get_context_guide("service-mesh")`
 * and `"service mesh"` both match the "service mesh" topic; partial matches
 * (e.g. "mesh" -> "service-mesh") are preserved.
 */
function matchesTopic(guide: ContextGuide, query: string): boolean {
  const q = canonicalizeName(query);
  const topic = canonicalizeName(guide.topic);
  return topic === q || topic.includes(q) || q.includes(topic);
}

export function createContextGuideTools(guides: ContextGuide[]) {
  return {
    list_context_guides: async () => {
      const items = guides.map((g) => ({
        topic: g.topic,
        path: g.path,
      }));
      return { content: [{ type: "text", text: JSON.stringify(items, null, 2) }] };
    },

    /**
     * Fetch a context guide.  Supports section chunking to avoid shipping
     * the full guide when the caller only needs one part.
     *
     * Section semantics (see chunkByPolicy):
     *   - omitted, small guide -> full content
     *   - omitted, large guide -> intro + TOC + how-to instruction
     *   - "list" / "toc"       -> TOC only
     *   - "all"                -> full content
     *   - "<name>"             -> matching section, or TOC on miss
     */
    get_context_guide: async (args: { topic: string; section?: string }) => {
      const matches = guides.filter((g) => matchesTopic(g, args.topic));

      if (matches.length === 0) {
        const available = guides.map((g) => g.topic);
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                error: `No context guide found for topic "${args.topic}".`,
                available,
              }),
            },
          ],
          isError: true,
        };
      }

      // If multiple guides match, return them all concatenated (chunking is
      // applied per-guide so each can be summarized independently).
      const text = matches
        .map((g) => {
          const chunked = chunkByPolicy(g.content, args.section);
          return matches.length > 1 ? `## ${g.topic}\n\n${chunked}` : chunked;
        })
        .join("\n\n---\n\n");

      return { content: [{ type: "text", text: text }] };
    },
  };
}

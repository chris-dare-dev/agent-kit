import type { ReferenceFile } from '../types.js';
import { canonicalizeName } from '../discovery/canonicalize.js';

/**
 * Extracts a one-line summary from a reference file's content.
 * Returns the first non-empty line that is not a markdown heading.
 */
function extractSummary(content: string): string {
  for (const line of content.split('\n')) {
    const trimmed = line.trim();
    if (trimmed && !trimmed.startsWith('#')) {
      return trimmed.length > 120 ? trimmed.slice(0, 117) + '...' : trimmed;
    }
  }
  return '';
}

export function createReferenceTools(references: ReferenceFile[]) {
  return {
    list_references: async () => {
      const items = references.map((ref) => ({
        name: ref.name,
        summary: extractSummary(ref.content),
      }));
      return { content: [{ type: 'text', text: JSON.stringify(items, null, 2) }] };
    },

    get_reference: async (args: { name: string }) => {
      // Canonicalize both sides so hyphen and space forms (and case) resolve
      // to the same reference — every CLAUDE.md tier cites the hyphenated form.
      const target = canonicalizeName(args.name);
      const match = references.find(
        (ref) => canonicalizeName(ref.name) === target,
      );

      if (!match) {
        const available = references.map((r) => r.name);
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify({
                error: `Reference "${args.name}" not found.`,
                available,
              }),
            },
          ],
          isError: true,
        };
      }

      return { content: [{ type: 'text', text: match.content }] };
    },
  };
}

/**
 * Markdown section utilities.
 *
 * Lets large markdown documents (context guides, SKILL.md, agent .md) be
 * served in chunks: a TOC of headings, or one named section, instead of the
 * whole document.  Reduces per-call token cost when the caller only needs
 * one part of a long doc.
 *
 * Headings inside fenced code blocks are intentionally ignored — they are
 * code, not document structure.
 */

export interface TOCEntry {
  /** Literal heading text (no leading hashes, no trailing whitespace). */
  title: string;
  /** Heading level: 2 for `##`, 3 for `###`, etc. */
  level: number;
  /** Lower-case, hyphenated slug derived from title. */
  slug: string;
  /** Zero-based line index of the heading in the original content. */
  lineIndex: number;
}

/**
 * Extract level-2-and-deeper headings from markdown content, skipping any
 * inside fenced code blocks.  Top-level `#` headings are skipped because
 * they almost always denote the document title rather than an addressable
 * section.
 */
export function extractTOC(content: string): TOCEntry[] {
  const lines = content.split("\n");
  const toc: TOCEntry[] = [];
  let inCodeFence = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Track fenced code blocks (``` or ~~~).
    if (/^(```|~~~)/.test(line)) {
      inCodeFence = !inCodeFence;
      continue;
    }
    if (inCodeFence) continue;

    // Match `##`-`######` headings only.
    const m = /^(#{2,6})\s+(.+?)\s*$/.exec(line);
    if (!m) continue;

    const level = m[1].length;
    const title = m[2].trim();
    const slug = slugify(title);

    toc.push({ title, level, slug, lineIndex: i });
  }

  return toc;
}

/**
 * Find a section by free-text query and return its full body
 * (heading line + content until the next heading of equal-or-shallower level).
 *
 * Matching priority:
 *   1. exact slug match
 *   2. exact heading text (case-insensitive)
 *   3. heading title contains query (case-insensitive)
 *   4. slug contains query slug
 *
 * Returns null when no heading matches.
 */
export function extractSection(
  content: string,
  query: string,
): string | null {
  const toc = extractTOC(content);
  if (toc.length === 0) return null;

  const qLower = query.trim().toLowerCase();
  const qSlug = slugify(query);

  let match: TOCEntry | undefined =
    toc.find((e) => e.slug === qSlug) ??
    toc.find((e) => e.title.toLowerCase() === qLower) ??
    toc.find((e) => e.title.toLowerCase().includes(qLower)) ??
    toc.find((e) => e.slug.includes(qSlug));

  if (!match) return null;

  const lines = content.split("\n");
  const start = match.lineIndex;
  let end = lines.length;

  // Walk forward through the TOC for the next equal-or-shallower heading.
  for (const next of toc) {
    if (next.lineIndex > start && next.level <= match.level) {
      end = next.lineIndex;
      break;
    }
  }

  return lines.slice(start, end).join("\n").trim();
}

/**
 * Render a TOC list as compact markdown — one bullet per heading, indented
 * by depth so nested sections are visually grouped under their parent.
 */
export function formatTOC(
  toc: TOCEntry[],
  opts: { instruction?: string } = {},
): string {
  const lines: string[] = [];
  if (opts.instruction) {
    lines.push(opts.instruction);
    lines.push("");
  }
  if (toc.length === 0) {
    lines.push("(no sections found)");
    return lines.join("\n");
  }
  for (const entry of toc) {
    const indent = "  ".repeat(Math.max(0, entry.level - 2));
    lines.push(`${indent}- ${entry.title}  \`section: "${entry.slug}"\``);
  }
  return lines.join("\n");
}

/**
 * Smart chunking helper used by tool handlers.
 *
 *   - section omitted, content small  -> full content (no token waste)
 *   - section omitted, content large  -> intro + TOC + how-to instruction
 *   - section "all"                   -> full content
 *   - section "list" or "toc"         -> TOC only
 *   - section "<name>"                -> matching section, or TOC if no match
 */
export function chunkByPolicy(
  content: string,
  sectionArg: string | undefined,
  /** Char threshold above which we default to TOC instead of full body. */
  largeThresholdChars: number = 12_000,
): string {
  const normalized = (sectionArg ?? "").trim().toLowerCase();

  // Explicit "all" — always return full body.
  if (normalized === "all") return content;

  // Explicit TOC request.
  if (normalized === "list" || normalized === "toc") {
    return formatTOC(extractTOC(content), {
      instruction: "Sections in this document:",
    });
  }

  // Specific section request.
  if (normalized) {
    const section = extractSection(content, sectionArg as string);
    if (section !== null) return section;
    // Miss — fall through with a helpful TOC.
    return [
      `Section "${sectionArg}" not found.`,
      "",
      formatTOC(extractTOC(content), { instruction: "Available sections:" }),
    ].join("\n");
  }

  // No section arg.  Decide based on size.
  if (content.length <= largeThresholdChars) return content;
  return summaryWithTOC(content);
}

/**
 * Render a "lead + TOC + instruction" summary of a large document.
 * The lead is whatever content precedes the first `##` heading, capped to
 * the first ~800 characters so it fits a small token budget.
 */
function summaryWithTOC(content: string): string {
  const firstH2 = content.search(/^##\s+/m);
  const leadRaw = firstH2 > 0 ? content.slice(0, firstH2).trim() : "";
  const lead =
    leadRaw.length > 800 ? leadRaw.slice(0, 800).trim() + "…" : leadRaw;

  const approxTokens = Math.ceil(content.length / 4);
  const toc = extractTOC(content);

  const lines: string[] = [];
  if (lead) {
    lines.push(lead);
    lines.push("");
  }
  lines.push(
    `**Chunked response.** Full document is ~${approxTokens.toLocaleString(
      "en-US",
    )} tokens. Available sections:`,
  );
  lines.push("");
  for (const entry of toc) {
    const indent = "  ".repeat(Math.max(0, entry.level - 2));
    lines.push(`${indent}- ${entry.title}  \`section: "${entry.slug}"\``);
  }
  lines.push("");
  lines.push(
    'Call again with `section: "<slug>"` for one section, or `section: "all"` for the full body.',
  );
  return lines.join("\n");
}

function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^\w\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

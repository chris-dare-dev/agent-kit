/**
 * Search index builder — constructs an inverted index from platform knowledge sources.
 *
 * Pure in-memory implementation with no external dependencies.
 * The corpus is ~800KB of markdown, so indexing is instant.
 */

import type { ContentType } from "../types.js";

// ── Types ─────────────────────────────────────────────────────────────────

/** A document to be indexed for full-text search. */
export interface SearchDocument {
  /** Absolute path to the source file. */
  path: string;
  /** Title extracted from the first H1 heading or the filename. */
  title: string;
  /** Full text content of the document. */
  content: string;
  /** Content category. */
  type: ContentType;
}

/** The search index: original documents plus an inverted term-to-document mapping. */
export interface SearchIndex {
  /** All indexed documents, ordered by insertion. */
  documents: SearchDocument[];
  /** Map from lowercase token to the set of document indices that contain it. */
  invertedIndex: Map<string, Set<number>>;
}

// ── Stop words ─────────────────────────────────────────────────────────────

/**
 * Common English stop words removed during tokenization.
 * These carry little semantic weight and would inflate the index
 * without improving search relevance.
 */
const STOP_WORDS: ReadonlySet<string> = new Set([
  // Articles & determiners
  'a', 'an', 'the', 'this', 'that', 'these', 'those',
  // Be-verbs
  'is', 'are', 'was', 'were', 'be', 'been', 'being',
  // Have-verbs
  'have', 'has', 'had', 'having',
  // Do-verbs
  'do', 'does', 'did', 'doing',
  // Modal verbs
  'will', 'would', 'could', 'should', 'shall', 'may', 'might', 'can', 'must',
  // Pronouns
  'i', 'me', 'my', 'myself', 'we', 'our', 'ours', 'ourselves',
  'you', 'your', 'yours', 'yourself', 'yourselves',
  'he', 'him', 'his', 'himself', 'she', 'her', 'hers', 'herself',
  'it', 'its', 'itself', 'they', 'them', 'their', 'theirs', 'themselves',
  // Prepositions & conjunctions
  'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from', 'as',
  'into', 'through', 'during', 'before', 'after', 'above', 'below',
  'between', 'under', 'again', 'further', 'then', 'once',
  'and', 'but', 'or', 'nor', 'not', 'so', 'if', 'than',
  // Common adverbs & misc
  'about', 'up', 'out', 'off', 'over', 'only', 'own', 'same',
  'too', 'very', 'just', 'also', 'now', 'here', 'there',
  'when', 'where', 'why', 'how', 'what', 'which', 'who', 'whom',
  'all', 'each', 'every', 'both', 'few', 'more', 'most', 'other',
  'some', 'such', 'any', 'while',
]);

// ── Tokenizer ──────────────────────────────────────────────────────────────

/**
 * Split text into lowercase tokens, stripping punctuation and stop words.
 *
 * Splitting strategy:
 *  1. Lowercase the input
 *  2. Replace underscores with hyphens so compound terms like AUTO_PASSTHROUGH
 *     become auto-passthrough (one token, not two)
 *  3. Replace non-alphanumeric characters (except hyphens inside words) with spaces
 *  4. Split on whitespace
 *  5. Discard single-character tokens and stop words
 */
export function tokenize(text: string): string[] {
  const normalized = text.toLowerCase().replaceAll('_', '-');
  return normalized
    .replace(/[^a-z0-9-]/g, ' ')
    .split(/\s+/)
    .filter((token) => token.length > 1 && !STOP_WORDS.has(token));
}

// ── Index builder ──────────────────────────────────────────────────────────

/**
 * Build an inverted search index from an array of documents.
 *
 * For each document, the full content AND title are tokenized. Title tokens
 * are included in the same inverted index (title boosting is handled at
 * query time by the search function).
 *
 * @param documents - Array of documents to index
 * @returns A SearchIndex with the original documents and the inverted index
 */
export function buildIndex(documents: SearchDocument[]): SearchIndex {
  const invertedIndex = new Map<string, Set<number>>();

  for (let docIdx = 0; docIdx < documents.length; docIdx++) {
    const doc = documents[docIdx];

    // Tokenize both title and content so title terms are discoverable too
    const titleTokens = tokenize(doc.title);
    const contentTokens = tokenize(doc.content);
    const allTokens = new Set([...titleTokens, ...contentTokens]);

    for (const token of allTokens) {
      let postings = invertedIndex.get(token);
      if (!postings) {
        postings = new Set<number>();
        invertedIndex.set(token, postings);
      }
      postings.add(docIdx);
    }
  }

  return { documents, invertedIndex };
}

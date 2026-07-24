/**
 * Full-text search engine over the platform knowledge index.
 *
 * Scoring model:
 *  - Each query term that appears in a document contributes to the score
 *  - Term frequency (count of occurrences in the document content) is included
 *  - Title matches receive a 3x multiplier
 *  - Multi-word queries use soft AND: documents matching ALL terms rank highest,
 *    but partial matches still appear with lower scores
 */

import type { SearchIndex } from './indexer.js';
import { tokenize } from './indexer.js';

// ── Types (self-contained) ─────────────────────────────────────────────────

/** A single search result with relevance metadata. */
export interface SearchResult {
  /** Absolute path to the matching file. */
  path: string;
  /** Title of the document. */
  title: string;
  /** ~200 character excerpt around the first match, with ellipsis. */
  snippet: string;
  /** Relevance score (higher = more relevant). */
  score: number;
  /** Content category. */
  type: string;
}

// ── Helpers ────────────────────────────────────────────────────────────────

/** Count occurrences of `term` in a pre-tokenized array. */
function countOccurrences(tokens: string[], term: string): number {
  let count = 0;
  for (const t of tokens) {
    if (t === term) count++;
  }
  return count;
}

/**
 * Extract a snippet of ~200 characters centered around the first occurrence
 * of any query term in the raw document content.
 *
 * The search is case-insensitive. If no term is found in the content (e.g.,
 * the match was title-only), the first ~200 characters of the content are used.
 */
function extractSnippet(content: string, queryTerms: string[]): string {
  const lowerContent = content.toLowerCase();
  let earliestPos = -1;

  // Find the first occurrence of any query term
  for (const term of queryTerms) {
    const pos = lowerContent.indexOf(term);
    if (pos !== -1 && (earliestPos === -1 || pos < earliestPos)) {
      earliestPos = pos;
    }
  }

  // If no term found in content, return the beginning of the document
  if (earliestPos === -1) {
    const end = Math.min(content.length, 400);
    const raw = content.slice(0, end).trim();
    return raw.length < content.length ? raw + '...' : raw;
  }

  // Grab ~200 chars before and ~200 chars after the match
  const contextBefore = 200;
  const contextAfter = 200;
  const start = Math.max(0, earliestPos - contextBefore);
  const end = Math.min(content.length, earliestPos + contextAfter);

  let snippet = content.slice(start, end).trim();

  // Add ellipsis for truncated boundaries
  if (start > 0) {
    snippet = '...' + snippet;
  }
  if (end < content.length) {
    snippet = snippet + '...';
  }

  return snippet;
}

// ── Candidate collection ───────────────────────────────────────────────────

/** Collect the union of document indices that contain at least one query term. */
function collectCandidates(
  invertedIndex: Map<string, Set<number>>,
  queryTerms: string[],
): Set<number> {
  const candidates = new Set<number>();
  for (const term of queryTerms) {
    const postings = invertedIndex.get(term);
    if (!postings) continue;
    for (const docIdx of postings) {
      candidates.add(docIdx);
    }
  }
  return candidates;
}

// ── Per-document scoring ───────────────────────────────────────────────────

/** Score breakdown for a single document against the query. */
interface DocumentScore {
  /** Raw relevance score (term frequency with title boost). */
  rawScore: number;
  /** Number of distinct query terms that matched. */
  termsMatched: number;
}

/**
 * Score a single document against the query terms.
 * Tokenizes content and title once, then tallies TF×IDF across all query
 * terms. Title occurrences receive a 3x multiplier.
 *
 * For the title boost, hyphenated title tokens are also expanded into their
 * component sub-tokens so that a query for "argocd" matches a title token
 * like "argocd-unstick".
 *
 * @param idfWeights - Pre-computed IDF weight per query term
 */
function scoreDocument(
  content: string,
  title: string,
  queryTerms: string[],
  idfWeights: Map<string, number>,
): DocumentScore {
  const contentTokens = tokenize(content);
  const titleTokens = tokenize(title);

  // Expand hyphenated title tokens into sub-tokens for the title boost check.
  // e.g. "argocd-unstick" → ["argocd-unstick", "argocd", "unstick"]
  const expandedTitleTokens = new Set<string>();
  for (const t of titleTokens) {
    expandedTitleTokens.add(t);
    if (t.includes('-')) {
      for (const sub of t.split('-')) {
        if (sub.length > 1) expandedTitleTokens.add(sub);
      }
    }
  }

  let rawScore = 0;
  let termsMatched = 0;

  for (const term of queryTerms) {
    const contentTF = countOccurrences(contentTokens, term);
    // Use expanded set for title boost: sub-tokens of hyphenated titles match too
    const titleBoost = expandedTitleTokens.has(term) ? 3 : 0;

    if (contentTF > 0 || titleBoost > 0) {
      const idf = idfWeights.get(term) ?? 1;
      rawScore += (contentTF + titleBoost) * idf;
      termsMatched++;
    }
  }

  return { rawScore, termsMatched };
}

// ── Search function ────────────────────────────────────────────────────────

/**
 * Search the index for documents matching the query string.
 *
 * @param index       - The pre-built search index
 * @param query       - Free-text query string
 * @param maxResults  - Maximum number of results to return (default 10)
 * @returns Scored search results sorted by relevance descending
 */
export function search(
  index: SearchIndex,
  query: string,
  maxResults: number = 10,
): SearchResult[] {
  // Edge cases: empty query or empty index
  if (!query || query.trim().length === 0) return [];
  if (index.documents.length === 0) return [];

  const queryTerms = tokenize(query);
  if (queryTerms.length === 0) return [];

  const candidateSet = collectCandidates(index.invertedIndex, queryTerms);
  if (candidateSet.size === 0) return [];

  // Compute IDF per query term: log(totalDocs / (docsContainingTerm + 1))
  // This penalises ubiquitous terms like "cluster", "dev", "helm" and
  // rewards specific terms like "pqc", "passthrough", "realm".
  const totalDocs = index.documents.length;
  const idfWeights = new Map<string, number>();
  for (const term of queryTerms) {
    const postings = index.invertedIndex.get(term);
    const docsWithTerm = postings ? postings.size : 0;
    idfWeights.set(term, Math.log(totalDocs / (docsWithTerm + 1)));
  }

  // Score each candidate and build results. Documents matching MORE query
  // terms rank higher via a multiplicative boost proportional to the
  // fraction of terms matched.
  const results: SearchResult[] = [];

  for (const docIdx of candidateSet) {
    const doc = index.documents[docIdx];
    const { rawScore, termsMatched } = scoreDocument(
      doc.content,
      doc.title,
      queryTerms,
      idfWeights,
    );
    if (rawScore === 0) continue;

    const matchRatio = termsMatched / queryTerms.length;
    const score = rawScore * (1 + matchRatio);
    const snippet = extractSnippet(doc.content, queryTerms);

    results.push({
      path: doc.path,
      title: doc.title,
      snippet,
      score,
      type: doc.type,
    });
  }

  // Sort by score descending, then by title ascending for stable ordering
  results.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    return a.title.localeCompare(b.title);
  });

  return results.slice(0, maxResults);
}

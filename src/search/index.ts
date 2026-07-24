/**
 * Search module barrel export.
 *
 * Re-exports the indexer and search engine so consumers can do:
 *   import { buildIndex, search, tokenize } from './search/index.js';
 */

export { buildIndex, tokenize } from './indexer.js';
export type { SearchDocument, SearchIndex } from './indexer.js';

export { search } from './search.js';
export type { SearchResult } from './search.js';

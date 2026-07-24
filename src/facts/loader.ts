/**
 * Structured-fact loader.
 *
 * Platform facts (environment map, architecture summary) live as JSON under
 * `data/facts/` — the single source of truth — rather than as TypeScript string
 * literals that silently lag `data/references/`. This loader reads them with fs
 * (not a JSON `import`) so the exact same code path works from source (tsx,
 * `src/facts/loader.ts`) and from the compiled tree (`dist/facts/loader.js`) —
 * `tsc` does not copy `.json` into `dist/`, but `data/` sits at the package root
 * in both cases, exactly like `config.ts` resolves the bundled `data/` dir.
 *
 * Loads are memoized (facts are static for the process lifetime). A missing or
 * malformed fact file throws a descriptive error; because every tool dispatch is
 * wrapped in the CallTool try/catch, that surfaces as a structured per-call error
 * and never takes the server down.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// data/facts/ relative to this module — ../../data/facts from both
// src/facts/loader.ts and dist/facts/loader.js (package root is two up).
const FACTS_DIR = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "data",
  "facts",
);

const cache = new Map<string, unknown>();

/**
 * Load and parse `data/facts/<basename>.json`, memoized by basename.
 *
 * @param basename Fact file name without the `.json` extension.
 * @throws if the file is missing or not valid JSON.
 */
export function loadFacts<T>(basename: string): T {
  const cached = cache.get(basename);
  if (cached !== undefined) return cached as T;

  const filePath = resolve(FACTS_DIR, `${basename}.json`);
  let raw: string;
  try {
    raw = readFileSync(filePath, "utf-8");
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    throw new Error(`Failed to read fact file ${filePath}: ${message}`);
  }

  let parsed: T;
  try {
    parsed = JSON.parse(raw) as T;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    throw new Error(`Fact file ${filePath} is not valid JSON: ${message}`);
  }

  cache.set(basename, parsed);
  return parsed;
}

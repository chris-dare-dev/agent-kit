/**
 * Every test file on disk must actually be run.
 *
 * `npm test` used to name three files explicitly, so five suites existed and
 * never executed — including both security suites. A test that is never run is
 * indistinguishable from a test that passes, which is the worse of the two
 * because it reads as coverage.
 *
 * The reason it was an explicit list is that `src/tools/artifact-memory.test.ts`
 * binds an AF_UNIX socket and errored on Windows; the fix was to omit the file
 * rather than to skip its socket-bound cases. Those cases now skip with an
 * explicit PLATFORM:win32 reason, so the whole tree can be globbed.
 *
 * Run by `npm test`, which globs src/**\/*.test.ts.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = resolve(dirname(fileURLToPath(import.meta.url)));
const REPO_ROOT = resolve(SRC, "..");

/** Every *.test.ts under src/, repo-relative and POSIX-separated. */
function discoverTestFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...discoverTestFiles(full));
    else if (entry.name.endsWith(".test.ts")) {
      found.push(relative(REPO_ROOT, full).split("\\").join("/"));
    }
  }
  return found.sort();
}

test("npm test globs the tree rather than listing files", () => {
  const pkg = JSON.parse(readFileSync(join(REPO_ROOT, "package.json"), "utf-8"));
  const script: string = pkg.scripts.test;

  assert.match(
    script,
    /src\/\*\*\/\*\.test\.ts/,
    "npm test must glob src/**/*.test.ts. An explicit file list silently drops " +
      "every suite nobody remembered to add — which is how five suites, both " +
      "security ones included, went unrun.",
  );

  // A list masquerading as a glob is the same defect with extra steps.
  const named = script.match(/src\/[^\s"']*\.test\.ts/g) ?? [];
  const literal = named.filter((entry) => !entry.includes("*"));
  assert.deepEqual(
    literal,
    [],
    `npm test names individual files: ${literal.join(", ")}. Add them to the ` +
      "glob's reach instead, or they will be the next suites to rot.",
  );
});

test("every discovered suite is reachable and non-trivial", () => {
  const files = discoverTestFiles(SRC);

  assert.ok(
    files.length >= 8,
    `expected at least 8 test files under src/, found ${files.length}: ${files.join(", ")}`,
  );

  // A file with no test() call is a file that cannot fail.
  for (const file of files) {
    const body = readFileSync(join(REPO_ROOT, file), "utf-8");
    assert.match(body, /\btest\(/, `${file} defines no test() and can never fail`);
  }
});

test("the golden updater still targets only the contract suite", () => {
  // Regenerating goldens from a glob would rewrite every fixture at once,
  // turning an intentional single-tool change into an unreviewable diff.
  const pkg = JSON.parse(readFileSync(join(REPO_ROOT, "package.json"), "utf-8"));
  const updater = readFileSync(join(REPO_ROOT, "scripts", "update-golden.mjs"), "utf-8");
  assert.match(pkg.scripts["test:update-golden"], /update-golden\.mjs/);
  assert.match(updater, /src\/mcp-contract\.test\.ts/);
  // Look for a GLOB, not for "**" — which also appears in a JSDoc opener.
  assert.doesNotMatch(
    updater,
    /["'`][^"'`]*\*\*\/[^"'`]*["'`]/,
    "the golden updater must target one file, not a glob",
  );
});

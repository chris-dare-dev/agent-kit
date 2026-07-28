/**
 * Personal-memory tier resolution.
 *
 * The tier used to be keyed on process.env.HOME. Under native PowerShell — how
 * Claude Code actually launches on Windows — HOME is undefined while USERPROFILE
 * is set, so the condition was false, memoryDir became "", and list/get/search
 * _memory returned empty results indistinguishable from "this user has no memory
 * files". These cases pin the three outcomes that must stay distinguishable:
 * explicit override, derived from the real home directory, and deliberately off.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadConfig } from "./config.js";

const FIXTURE_ROOT = resolve(process.cwd(), "tests", "fixtures", "mcp-contract");

/** Run loadConfig against a patched environment, always restoring it. */
function withEnv<T>(patch: Record<string, string | undefined>, fn: () => T): T {
  const saved = new Map<string, string | undefined>();
  for (const key of Object.keys(patch)) {
    saved.set(key, process.env[key]);
    if (patch[key] === undefined) delete process.env[key];
    else process.env[key] = patch[key];
  }
  try {
    return fn();
  } finally {
    for (const [key, value] of saved) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

const BASE = {
  PLATFORM_ROOT: FIXTURE_ROOT,
  WORKSPACE_ROOT: FIXTURE_ROOT,
  MEMORY_ROOT: undefined,
};

test("memory tier resolves without HOME, as it must on Windows", () => {
  const config = withEnv(
    { ...BASE, HOME: undefined, USERPROFILE: homedir() },
    () => loadConfig(),
  );
  assert.notEqual(
    config.memoryDir,
    "",
    "the tier must not silently disable itself when HOME is unset",
  );
  assert.ok(
    config.memoryDir.startsWith(homedir()),
    `memoryDir (${config.memoryDir}) must resolve under homedir() (${homedir()})`,
  );
});

test("MEMORY_ROOT wins over the derived path", () => {
  const explicit = resolve(FIXTURE_ROOT, "memory");
  const config = withEnv({ ...BASE, MEMORY_ROOT: explicit }, () => loadConfig());
  assert.equal(config.memoryDir, explicit);
});

test('MEMORY_ROOT="" is the deliberate disable path, not a bug', () => {
  const config = withEnv({ ...BASE, MEMORY_ROOT: "" }, () => loadConfig());
  assert.equal(
    config.memoryDir,
    "",
    'an empty MEMORY_ROOT must disable the tier — this is how the hermetic ' +
      "test fixtures and CI opt out",
  );
});

test("the tier stays disabled when no WORKSPACE_ROOT was provided", () => {
  // Without a workspace there is no slug to derive a per-project memory dir
  // from, so "" is correct here — but it must come from the missing workspace,
  // not from a missing HOME.
  const config = withEnv(
    { PLATFORM_ROOT: FIXTURE_ROOT, WORKSPACE_ROOT: undefined, MEMORY_ROOT: undefined },
    () => loadConfig(),
  );
  assert.equal(config.memoryDir, "");
});

// ---------------------------------------------------------------------------
// Root resolution and the discovery block.
//
// PLATFORM_ROOT used to be mandatory, with an error naming a "platform
// monorepo" unrelated to this repo, so the documented Quick Start died on its
// first command. It is now an optional override.
// ---------------------------------------------------------------------------

const PACKAGE_ROOT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
);

test("an empty environment yields a usable config rooted at the package", () => {
  const config = withEnv(
    {
      PLATFORM_ROOT: undefined,
      WORKSPACE_ROOT: undefined,
      MEMORY_ROOT: undefined,
      CONTEXT_GUIDES_DIR: undefined,
      CLAUDE_MD_GLOBS: undefined,
    },
    () => loadConfig(),
  );
  assert.equal(config.platformRoot, PACKAGE_ROOT);
  // The bundled content must be reachable, not merely addressed.
  assert.ok(
    existsSync(join(config.dataSkillsDir, "handoff", "SKILL.md")),
    `dataSkillsDir (${config.dataSkillsDir}) must contain the bundled handoff skill`,
  );
});

test("an explicitly bad PLATFORM_ROOT still fails loudly, naming the path", () => {
  const bogus = join(PACKAGE_ROOT, "no-such-directory-8f3a1c");
  assert.throws(
    () => withEnv({ PLATFORM_ROOT: bogus }, () => loadConfig()),
    (err: Error) => err.message.includes(bogus),
    "the message must name the supplied path so the typo is visible",
  );
});

test("CLAUDE.md globs default to a depth-bounded set and are overridable", () => {
  const base = { PLATFORM_ROOT: undefined, WORKSPACE_ROOT: undefined };
  const fallback = withEnv({ ...base, CLAUDE_MD_GLOBS: undefined }, () => loadConfig());
  assert.deepEqual(fallback.claudeMdGlobs, [
    "CLAUDE.md",
    "*/CLAUDE.md",
    "*/*/CLAUDE.md",
    "*/*/*/CLAUDE.md",
  ]);
  // None of the four employer-monorepo tiers may survive as a default.
  for (const dead of ["charts", "source", "infra", "ci-cd-templates"]) {
    assert.ok(
      !fallback.claudeMdGlobs.some((g) => g.includes(dead)),
      `"${dead}" must not be a built-in discovery path`,
    );
  }

  const overridden = withEnv(
    { ...base, CLAUDE_MD_GLOBS: "apps/*/CLAUDE.md , services/*/CLAUDE.md" },
    () => loadConfig(),
  );
  assert.deepEqual(overridden.claudeMdGlobs, [
    "apps/*/CLAUDE.md",
    "services/*/CLAUDE.md",
  ]);
});

test("CONTEXT_GUIDES_DIR overrides the discovered context directory", () => {
  const explicit = resolve(FIXTURE_ROOT, "sandbox", "context");
  const config = withEnv(
    { PLATFORM_ROOT: undefined, WORKSPACE_ROOT: undefined, CONTEXT_GUIDES_DIR: explicit },
    () => loadConfig(),
  );
  assert.equal(config.contextGuidesDir, explicit);
});

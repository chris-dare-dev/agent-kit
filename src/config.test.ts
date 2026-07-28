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
import { homedir } from "node:os";
import { resolve } from "node:path";
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

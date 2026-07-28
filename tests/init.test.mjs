/**
 * `agent-kit init` on a clone with no matching ancestors.
 *
 * setup-local.sh walked parent directories for one containing both `charts/`
 * and `tools/`, then for one containing `GitLab/`, and exited non-zero
 * otherwise — so on any ordinary clone it failed before doing anything, and
 * even `--build-only` was unreachable. These cases pin the replacement's two
 * promises: it works with no special ancestry, and it does not destroy content
 * it did not create.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** A throwaway clone containing only what init needs: data/ and scripts/. */
function bareClone(t) {
  const root = mkdtempSync(join(tmpdir(), "agent-kit-init-"));
  cpSync(join(REPO_ROOT, "data"), join(root, "data"), { recursive: true });
  mkdirSync(join(root, "scripts"), { recursive: true });
  cpSync(join(REPO_ROOT, "scripts", "init.mjs"), join(root, "scripts", "init.mjs"));
  t.after(() => rmSync(root, { recursive: true, force: true, maxRetries: 5 }));
  return root;
}

function init(root, args = []) {
  return spawnSync(process.execPath, [join(root, "scripts", "init.mjs"), ...args], {
    cwd: root,
    encoding: "utf-8",
    // No PLATFORM_ROOT / WORKSPACE_ROOT: the defaults are what is under test.
    env: { PATH: process.env.PATH ?? "", SystemRoot: process.env.SystemRoot ?? "" },
  });
}

const receiptOf = (root) =>
  JSON.parse(readFileSync(join(root, ".agent-kit", "install-receipt.json"), "utf-8"));

test("init succeeds in a clone with no matching ancestors", (t) => {
  const root = bareClone(t);
  const run = init(root);
  assert.equal(run.status, 0, `init failed:\n${run.stdout}\n${run.stderr}`);
  assert.ok(existsSync(join(root, ".claude", "skills")), "skills were not planted");
  assert.ok(existsSync(join(root, ".claude", "commands")), "commands were not planted");
});

test("the receipt records every path, its action and the resolved roots", (t) => {
  const root = bareClone(t);
  assert.equal(init(root).status, 0);
  const receipt = receiptOf(root);
  assert.equal(receipt.roots.workspaceRoot, root);
  assert.equal(receipt.roots.platformRoot, root);
  assert.ok(receipt.entries.length > 0, "receipt has no entries");
  for (const entry of receipt.entries) {
    assert.ok(entry.path, "entry without a path");
    assert.match(entry.action, /^(created|linked|copied|overwritten)$/);
    assert.ok(entry.at, "entry without a timestamp");
  }
});

test("init refuses to destroy content it did not create, and says which paths", (t) => {
  const root = bareClone(t);
  // Pre-existing user content, with no receipt claiming it.
  mkdirSync(join(root, ".claude", "commands"), { recursive: true });
  const mine = join(root, ".claude", "commands", "mine.md");
  writeFileSync(mine, "user content", "utf-8");

  const run = init(root);
  assert.notEqual(run.status, 0, "init must refuse rather than overwrite");
  assert.match(run.stderr, /refused/i);
  assert.ok(run.stderr.includes(join(root, ".claude", "commands")),
    "the refusal must name the exact path it would have removed");
  assert.equal(readFileSync(mine, "utf-8"), "user content", "user content was destroyed");
});

test("--force replaces, and records the prior content hash", (t) => {
  const root = bareClone(t);
  mkdirSync(join(root, ".claude", "commands"), { recursive: true });
  writeFileSync(join(root, ".claude", "commands", "mine.md"), "user content", "utf-8");

  assert.equal(init(root, ["--force"]).status, 0);
  const overwritten = receiptOf(root).entries.filter((e) => e.action === "overwritten");
  assert.ok(overwritten.length > 0, "nothing was recorded as overwritten");
  for (const entry of overwritten) {
    assert.match(entry.sha256_before ?? "", /^[0-9a-f]{64}$/,
      `${entry.path} was replaced without recording what was there`);
  }
});

test("a second init is idempotent and destroys nothing", (t) => {
  const root = bareClone(t);
  assert.equal(init(root).status, 0);
  const first = receiptOf(root).entries.length;
  const second = init(root);
  assert.equal(second.status, 0, `re-run failed:\n${second.stderr}`);
  assert.doesNotMatch(second.stderr, /refused/i);
  // The receipt is a statement of current state, so a re-run updates entries
  // rather than appending duplicates.
  assert.equal(receiptOf(root).entries.length, first);
});

test("writing outside the clone needs an explicit opt-in", (t) => {
  const root = bareClone(t);
  const outside = mkdtempSync(join(tmpdir(), "agent-kit-outside-"));
  t.after(() => rmSync(outside, { recursive: true, force: true, maxRetries: 5 }));

  const refused = init(root, ["--workspace-root", outside]);
  assert.notEqual(refused.status, 0, "must refuse to write outside the clone");
  assert.ok(refused.stderr.includes(outside), "the refusal must name the path");
  assert.ok(!existsSync(join(outside, ".claude")), "it wrote outside anyway");

  assert.equal(init(root, ["--workspace-root", outside, "--install-to"]).status, 0);
  assert.ok(existsSync(join(outside, ".claude", "skills")), "--install-to did not plant");
});

test("--build-only resolves no roots at all", (t) => {
  const root = bareClone(t);
  // No package.json in the bare clone, so the build cannot succeed — the point
  // is that it fails IN THE BUILD, never in root detection, which is where the
  // shell script it replaces gave up.
  const run = init(root, ["--build-only"]);
  assert.doesNotMatch(
    `${run.stdout}${run.stderr}`,
    /workspaceRoot|platformRoot|refused/,
    "--build-only must short-circuit before any root resolution",
  );
});

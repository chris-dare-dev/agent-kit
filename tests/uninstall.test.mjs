/**
 * `agent-kit uninstall` must undo exactly what init recorded, and nothing else.
 *
 * The round trip is the real assertion: a temp clone, init, uninstall --apply,
 * and the tree back to its pre-init state. If uninstall over-deletes, the
 * pre-existing fixture file disappears; if it under-deletes, a planted
 * directory survives.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync,
  rmSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function bareClone(t) {
  const root = mkdtempSync(join(tmpdir(), "agent-kit-uninstall-"));
  cpSync(join(REPO_ROOT, "data"), join(root, "data"), { recursive: true });
  mkdirSync(join(root, "scripts"), { recursive: true });
  for (const f of ["init.mjs", "uninstall.mjs"]) {
    cpSync(join(REPO_ROOT, "scripts", f), join(root, "scripts", f));
  }
  t.after(() => rmSync(root, { recursive: true, force: true, maxRetries: 5 }));
  return root;
}

const run = (root, script, args = []) =>
  spawnSync(process.execPath, [join(root, "scripts", script), ...args], {
    cwd: root, encoding: "utf-8",
    env: { PATH: process.env.PATH ?? "", SystemRoot: process.env.SystemRoot ?? "" },
  });

/** Sorted relative listing, so before/after can be compared exactly. */
function tree(root) {
  const out = [];
  const walk = (dir, prefix) => {
    for (const e of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const rel = prefix ? `${prefix}/${e.name}` : e.name;
      if (rel.startsWith("data") || rel.startsWith("scripts")) continue;
      out.push(rel);
      if (e.isDirectory()) walk(join(dir, e.name), rel);
    }
  };
  walk(root, "");
  return out;
}

test("init then uninstall --apply restores the pre-init tree", (t) => {
  const root = bareClone(t);
  const before = tree(root);

  assert.equal(run(root, "init.mjs").status, 0, "init failed");
  assert.ok(tree(root).length > before.length, "init planted nothing");

  const un = run(root, "uninstall.mjs", ["--apply"]);
  assert.equal(un.status, 0, `uninstall failed:\n${un.stdout}\n${un.stderr}`);

  // .agent-kit/ holds the receipt itself, which uninstall does not remove.
  const after = tree(root).filter((p) => !p.startsWith(".agent-kit"));
  assert.deepEqual(after, before, "the tree did not return to its pre-init state");
});

test("uninstall is a dry run by default and changes nothing", (t) => {
  const root = bareClone(t);
  assert.equal(run(root, "init.mjs").status, 0);
  const planted = tree(root);

  const dry = run(root, "uninstall.mjs");
  assert.equal(dry.status, 0);
  assert.match(dry.stdout, /DRY RUN/);
  assert.deepEqual(tree(root), planted, "a dry run modified the tree");
});

test("it never touches a path absent from the receipt", (t) => {
  const root = bareClone(t);
  assert.equal(run(root, "init.mjs").status, 0);
  // A file the user owns, inside a directory init DID plant.
  const mine = join(root, ".claude", "mine-keep.md");
  writeFileSync(mine, "user content", "utf-8");

  assert.equal(run(root, "uninstall.mjs", ["--apply"]).status, 0);
  assert.equal(
    readFileSync(mine, "utf-8"), "user content",
    "uninstall deleted a file that was not in the receipt",
  );
});

test("a path edited since init is skipped unless --force", (t) => {
  const root = bareClone(t);
  assert.equal(run(root, "init.mjs").status, 0);
  const agents = join(root, "AGENTS.md");
  assert.ok(existsSync(agents), "init did not plant AGENTS.md");
  writeFileSync(agents, "the user has since edited this", "utf-8");

  const skipped = run(root, "uninstall.mjs", ["--apply"]);
  assert.equal(skipped.status, 0);
  assert.match(skipped.stdout, /changed since init/);
  assert.ok(existsSync(agents), "an edited file was removed without --force");

  assert.equal(run(root, "uninstall.mjs", ["--apply", "--force"]).status, 0);
  assert.ok(!existsSync(agents), "--force did not remove it");
});

test("with no receipt it refuses rather than guessing", (t) => {
  const root = bareClone(t);
  const un = run(root, "uninstall.mjs", ["--apply"]);
  assert.notEqual(un.status, 0, "must refuse without a receipt");
  assert.match(un.stderr, /no receipt/i);
});

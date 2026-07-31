/**
 * `agent-kit init` on a clone with no matching ancestors.
 *
 * setup-local.sh walked parent directories for one containing both `charts/`
 * and `tools/`, then for a pre-fork VCS-root directory, and exited non-zero
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
  symlinkSync,
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
  // uninstall.mjs too: the backup contract is only meaningful as a round trip,
  // so the --force case has to be able to run the other half of it.
  for (const f of ["init.mjs", "uninstall.mjs"]) {
    cpSync(join(REPO_ROOT, "scripts", f), join(root, "scripts", f));
  }
  cpSync(join(REPO_ROOT, "scripts", "lib"), join(root, "scripts", "lib"), { recursive: true });
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

/**
 * Re-running init must not destroy edits inside a tree it planted.
 *
 * Receipt ownership used to be sufficient on its own: a path the receipt named
 * was replanted unconditionally, with no comparison against the content
 * recorded at install time. So an edit inside `.claude/` — or to a top-level
 * AGENTS.md — vanished on the next `agent-kit init`, silently and with no
 * backup. Ownership answers "did I create this?"; it cannot answer "is it still
 * what I created?", and only the second question licenses an overwrite.
 */
test("a second init preserves an edit made inside a planted directory", (t) => {
  const root = bareClone(t);
  assert.equal(init(root, ["--mode", "copy"]).status, 0);

  const edited = join(root, ".claude", "commands", "roadmap.md");
  assert.ok(existsSync(edited), "fixture missing: .claude/commands/roadmap.md");
  writeFileSync(edited, "THE USER HAS EDITED THIS\n", "utf-8");

  // Refuses, exactly as it already does for a path it never owned — and names
  // the tree, so the message is actionable rather than just a veto.
  const second = init(root, ["--mode", "copy"]);
  assert.notEqual(second.status, 0, "a second init overwrote an edited tree");
  assert.match(second.stderr, /edited since/i);
  assert.ok(
    second.stderr.includes(join(root, ".claude", "commands")),
    "the refusal must name the tree it would have replaced",
  );
  assert.equal(
    readFileSync(edited, "utf-8"), "THE USER HAS EDITED THIS\n",
    "a second init silently overwrote an edit inside a planted directory",
  );

  // --force still proceeds, but the edit is recoverable afterwards.
  assert.equal(init(root, ["--mode", "copy", "--force"]).status, 0);
  const entry = receiptOf(root).entries.find(
    (e) => e.path === join(root, ".claude", "commands"),
  );
  assert.ok(entry?.backup, "--force replaced an edited tree without backing it up");
  assert.equal(
    readFileSync(join(root, entry.backup, "roadmap.md"), "utf-8"),
    "THE USER HAS EDITED THIS\n",
    "the backup does not contain the edit it displaced",
  );
});

test("a second init preserves an edited top-level AGENTS.md", (t) => {
  const root = bareClone(t);
  assert.equal(init(root, ["--mode", "copy"]).status, 0);

  const agents = join(root, "AGENTS.md");
  assert.ok(existsSync(agents), "init did not plant AGENTS.md");
  writeFileSync(agents, "THE USER HAS EDITED THIS\n", "utf-8");

  assert.equal(init(root, ["--mode", "copy"]).status, 0);
  assert.equal(
    readFileSync(agents, "utf-8"), "THE USER HAS EDITED THIS\n",
    "a second init silently overwrote an edited AGENTS.md",
  );
});

/**
 * `--force` must displace content into a real backup, not merely hash it.
 *
 * uninstall.mjs already looks for `.agent-kit/backups/<sha>` and reports the
 * path as unrestorable when it is absent — which it always was, because init
 * never wrote one. A hash proves what was displaced and restores nothing.
 */
test("--force backs up displaced content, and uninstall restores it", (t) => {
  const root = bareClone(t);
  mkdirSync(join(root, ".claude", "commands"), { recursive: true });
  const mine = join(root, ".claude", "commands", "mine.md");
  writeFileSync(mine, "user content", "utf-8");

  assert.equal(init(root, ["--mode", "copy", "--force"]).status, 0);
  // --force replaces the whole directory, so the user's file is gone outright.
  assert.ok(!existsSync(mine), "fixture: nothing was displaced");

  const un = spawnSync(
    process.execPath,
    [join(root, "scripts", "uninstall.mjs"), "--apply"],
    { cwd: root, encoding: "utf-8",
      env: { PATH: process.env.PATH ?? "", SystemRoot: process.env.SystemRoot ?? "" } },
  );
  assert.equal(un.status, 0, `uninstall failed:\n${un.stdout}\n${un.stderr}`);
  assert.ok(
    existsSync(mine),
    "--force displaced a user directory and uninstall could not put it back",
  );
  assert.equal(readFileSync(mine, "utf-8"), "user content", "restored content differs");
});

/**
 * Ownership comes from the receipt, never from where a link happens to point.
 *
 * `isOurs` used to return true for ANY symlink resolving beneath data/, with no
 * receipt entry required. Planting then writes with `writeFileSync`, which
 * follows the link — so a link at a plant target sent init's write straight
 * into the bundled source it was supposed to be copying FROM.
 */
test("a symlink into data/ is not proof of ownership", (t) => {
  const root = bareClone(t);
  const source = join(root, "data", "claude-md", "AGENTS.md");
  const pristine = readFileSync(source, "utf-8");

  const target = join(root, "AGENTS.md");
  try {
    symlinkSync(source, target, "file");
  } catch {
    t.skip("this host will not create file symlinks");
    return;
  }

  const run = init(root, ["--mode", "copy"]);
  assert.equal(run.status, 0, `init failed:\n${run.stdout}\n${run.stderr}`);
  assert.match(run.stdout, /skipped .*AGENTS\.md/, "it did not skip the unowned link");
  assert.equal(
    readFileSync(source, "utf-8"), pristine,
    "init wrote through a symlink and corrupted its own bundled source",
  );
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

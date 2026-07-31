#!/usr/bin/env node
/**
 * `agent-kit uninstall` — undo what `agent-kit init` recorded, and nothing else.
 *
 * The receipt is the whole point. Before it existed, init planted symlinks and
 * copies into `.claude/`, wrote CLAUDE.md/AGENTS.md and `.mcp.json`, and kept no
 * inventory — so removal meant guessing, and guessing wrong meant deleting a
 * user's own files. This walks `.agent-kit/install-receipt.json` and refuses to
 * touch anything that is not in it.
 *
 * DRY RUN BY DEFAULT. Removal is the one operation where "I'll just try it" is
 * unacceptable, so nothing happens until --apply.
 *
 * RESTORING IS A COPY, NOT A HASH. `sha256_before` proves what was displaced;
 * it cannot reconstruct it. init now writes the displaced tree to
 * `.agent-kit/backups/<digest>` and records `backup` on the entry, so an
 * `overwritten` path is genuinely restorable — directories and symlinks
 * included. An entry whose backup is missing is still REPORTED rather than
 * quietly left replaced.
 *
 * WHAT LICENSES A DELETE. An entry is removed only when the tree still digests
 * to what init recorded. That comparison used to hash a directory's child
 * NAMES, so an edit inside a planted tree was invisible and the tree was
 * removed with the edit inside it. See scripts/lib/tree-digest.mjs.
 */
import {
  cpSync,
  existsSync,
  lstatSync,
  readFileSync,
  readdirSync,
  rmSync,
} from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { treeDigest } from "./lib/tree-digest.mjs";

const CLONE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const RECEIPT_PATH = join(CLONE_ROOT, ".agent-kit", "install-receipt.json");
const BACKUP_DIR = join(CLONE_ROOT, ".agent-kit", "backups");

const C = process.stdout.isTTY
  ? { g: "\x1b[32m", y: "\x1b[33m", r: "\x1b[31m", d: "\x1b[2m", x: "\x1b[0m" }
  : { g: "", y: "", r: "", d: "", x: "" };

const USAGE = `agent-kit uninstall — remove what init recorded

  (no flags)   dry run: print what would happen, change nothing
  --apply      actually remove/restore
  --force      proceed even when a path's content changed since init
  --receipt P  use an alternate receipt path
`;

function parseArgs(argv) {
  const args = { apply: false, force: false, help: false, receipt: RECEIPT_PATH };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--apply") args.apply = true;
    else if (a === "--force") args.force = true;
    else if (a === "-h" || a === "--help") args.help = true;
    else if (a === "--receipt") args.receipt = resolve(argv[++i]);
    else {
      console.error(`agent-kit uninstall: unknown argument "${a}" (try --help)`);
      process.exit(1);
    }
  }
  return args;
}

// The digest is imported rather than reimplemented. Both scripts used to carry
// their own copy, agreeing only by a comment — and both were wrong in the same
// way, hashing a directory's child NAMES. See scripts/lib/tree-digest.mjs.

const args = parseArgs(process.argv.slice(2));
if (args.help) {
  console.log(USAGE);
  process.exit(0);
}

if (!existsSync(args.receipt)) {
  console.error(
    `agent-kit uninstall: no receipt at ${args.receipt}.\n` +
      "Nothing is known to have been installed, so nothing will be removed. " +
      "(A receipt is written by `agent-kit init`.)",
  );
  process.exit(1);
}

let receipt;
try {
  receipt = JSON.parse(readFileSync(args.receipt, "utf-8"));
} catch (err) {
  console.error(`agent-kit uninstall: receipt is unreadable (${err.message})`);
  process.exit(1);
}

const entries = receipt.entries ?? [];
const plan = { remove: [], restore: [], changed: [], missing: [], unrestorable: [] };

for (const entry of entries) {
  const path = entry.path;
  if (!existsSync(path)) {
    plan.missing.push({ ...entry, why: "already gone" });
    continue;
  }
  // A path whose content moved on since init is the user's now, not ours.
  // A bare directory init merely CREATED (`.claude/` itself) carries no digest:
  // it is recorded before anything is planted into it, so any digest would be
  // stale by the end of the same run. It does not need one — the removal loop
  // below refuses to delete it unless it is empty, which is a stronger
  // guarantee than a content comparison.
  const isBareCreatedDir =
    entry.action === "created" && existsSync(path) && lstatSync(path).isDirectory();

  if (entry.action !== "overwritten" && !isBareCreatedDir) {
    const now = treeDigest(path);
    const then = entry.sha256_after ?? undefined;
    // No recorded digest means we cannot prove this is still what we planted,
    // and an unprovable claim must not authorise a recursive delete.
    if (!then && !args.force) {
      plan.changed.push({ ...entry, why: "no content digest recorded at install" });
      continue;
    }
    if (then && now !== then && !args.force) {
      plan.changed.push({ ...entry, why: "content changed since init" });
      continue;
    }
  }

  if (entry.action === "created" || entry.action === "linked" || entry.action === "copied") {
    plan.remove.push(entry);
  } else if (entry.action === "overwritten") {
    // `backup` is the receipt-relative copy init kept. Fall back to the old
    // hash-named layout so a receipt written before backups existed still
    // resolves if the directory happens to hold one.
    const backup = entry.backup
      ? resolve(CLONE_ROOT, entry.backup)
      : join(BACKUP_DIR, entry.sha256_before ?? "");
    if (existsSync(backup)) plan.restore.push({ ...entry, backup });
    else plan.unrestorable.push(entry);
  }
}

const rel = (p) => relative(CLONE_ROOT, p) || p;
const header = args.apply ? "uninstalling" : "DRY RUN — nothing will be changed";
console.log(`\nagent-kit uninstall  ${C.d}(${header})${C.x}\n`);
console.log(`  receipt: ${rel(args.receipt)}`);
if (receipt.roots) console.log(`  roots:   ${JSON.stringify(receipt.roots)}`);
console.log("");

const group = (title, items, colour, render) => {
  if (!items.length) return;
  console.log(`  ${colour}${title} (${items.length})${C.x}`);
  for (const item of items) console.log(`    ${render(item)}`);
  console.log("");
};

group("remove", plan.remove, C.g, (e) => `${e.action.padEnd(11)} ${rel(e.path)}`);
group("restore from backup", plan.restore, C.g, (e) => rel(e.path));
group(
  "SKIP — changed since init (use --force to remove anyway)",
  plan.changed, C.y, (e) => rel(e.path),
);
group(
  "SKIP — cannot restore: the receipt records a hash, not the prior content",
  plan.unrestorable, C.y, (e) => `${rel(e.path)}  (was ${e.sha256_before?.slice(0, 12)}…)`,
);
group("already gone", plan.missing, C.d, (e) => rel(e.path));

if (!args.apply) {
  console.log(`  Re-run with ${C.g}--apply${C.x} to perform the ${plan.remove.length + plan.restore.length} action(s) above.\n`);
  process.exit(0);
}

let removed = 0;
let restored = 0;
const failures = [];
const kept = [];
// Deepest path first, so a recorded parent directory is removed only after
// everything recorded inside it has gone.
// Split on BOTH separators. Matching only "/" makes every Windows path a single
// segment, which silently turns this sort into a no-op and lets a parent
// directory be removed before the children recorded inside it.
const depth = (p) => p.split(/[\\/]/).length;
plan.remove.sort((a, b) => depth(b.path) - depth(a.path));
for (const entry of plan.remove) {
  try {
    // A directory we merely CREATED (e.g. .claude/) may since have acquired the
    // user's own files. Remove it only if empty. A linked/copied entry is wholly
    // ours -- the whole planted tree -- so that goes recursively.
    const isBareDir =
      entry.action === "created" && lstatSync(entry.path).isDirectory();
    if (isBareDir && readdirSync(entry.path).length > 0) {
      kept.push(entry);
      continue;
    }
    rmSync(entry.path, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    removed += 1;
  } catch (err) {
    failures.push(`${rel(entry.path)}: ${err.message}`);
  }
}
for (const entry of plan.restore) {
  try {
    rmSync(entry.path, { recursive: true, force: true });
    // cpSync, not readFileSync/writeFileSync: what was displaced may be a whole
    // directory tree or a symlink, and reading "the content" of either loses it.
    // verbatimSymlinks keeps a link a link instead of inlining its target.
    cpSync(entry.backup, entry.path, { recursive: true, verbatimSymlinks: true });
    restored += 1;
  } catch (err) {
    failures.push(`${rel(entry.path)}: ${err.message}`);
  }
}

for (const entry of kept) {
  console.log(
    `  ${C.y}KEPT${C.x}   ${rel(entry.path)} — it now holds files this installer ` +
      "did not create",
  );
}
for (const failure of failures) console.error(`  ${C.r}FAILED${C.x} ${failure}`);
console.log(
  `\n  removed ${removed}, restored ${restored}` +
    (plan.unrestorable.length ? `, ${plan.unrestorable.length} left in place (unrestorable)` : "") +
    (failures.length ? `, ${failures.length} failed` : "") +
    ".\n",
);
if (plan.unrestorable.length) {
  console.log(
    `  ${C.y}Note${C.x}: the paths above were overwritten by init and cannot be\n` +
      "  restored — the receipt stores a content hash, not a copy. They are left\n" +
      "  exactly as init wrote them.\n",
  );
}
process.exit(failures.length ? 1 : 0);

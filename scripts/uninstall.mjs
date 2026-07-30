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
 * A LIMIT, STATED PLAINLY: the receipt records `sha256_before` for an
 * overwritten path, not its prior CONTENT. A hash proves what was displaced; it
 * cannot reconstruct it. So an `overwritten` entry is REPORTED, never restored,
 * unless a sibling `.agent-kit/backups/` copy exists (init does not write one
 * today). Silently leaving the user's file replaced while claiming to have
 * uninstalled would be worse than saying so.
 */
import { createHash } from "node:crypto";
import {
  existsSync,
  lstatSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

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

/** Same hashing rule init used, so the comparison is meaningful. */
function sha256(path) {
  try {
    if (lstatSync(path).isDirectory()) {
      return createHash("sha256").update(readdirSync(path).sort().join("\n")).digest("hex");
    }
    return createHash("sha256").update(readFileSync(path)).digest("hex");
  } catch {
    return undefined;
  }
}

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
  if (entry.action !== "overwritten") {
    const now = sha256(path);
    const then = entry.sha256_after ?? undefined;
    if (then && now && now !== then && !args.force) {
      plan.changed.push({ ...entry, why: "content changed since init" });
      continue;
    }
  }

  if (entry.action === "created" || entry.action === "linked" || entry.action === "copied") {
    plan.remove.push(entry);
  } else if (entry.action === "overwritten") {
    const backup = join(BACKUP_DIR, entry.sha256_before ?? "");
    if (entry.sha256_before && existsSync(backup)) plan.restore.push({ ...entry, backup });
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
    const content = readFileSync(entry.backup);
    rmSync(entry.path, { recursive: true, force: true });
    writeFileSync(entry.path, content);
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

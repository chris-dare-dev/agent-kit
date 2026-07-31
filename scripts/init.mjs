#!/usr/bin/env node
/**
 * `agent-kit init` — plant the bundled knowledge into a workspace's .claude/.
 *
 * Replaces setup-local.sh, which could not run here at all: it walked parent
 * directories for one containing both `charts/` and `tools/`, then for one
 * containing a pre-fork VCS-root directory, and called fatal otherwise. Neither exists in this
 * repository or in any ordinary clone, so even `--build-only` exited before
 * npm ci. Its `while [[ "$dir" != "/" ]]` walk also never terminates on a
 * Windows path root.
 *
 * Two behaviours are deliberately different from the script it replaces:
 *
 *   REFUSE-BY-DEFAULT. setup-local.sh `rm -rf`'d directories under .claude/ and
 *   overwrote CLAUDE.md at up to four locations with no prompt, no backup and
 *   no record. Here, anything this installer did not create is left alone
 *   unless --force is passed, and every path it would have removed is printed.
 *
 *   RECEIPTED. Every created or modified path is recorded in
 *   <clone>/.agent-kit/install-receipt.json, with the prior content hash when
 *   something was overwritten. Nothing recorded what it did before, which is
 *   also why no uninstall could exist.
 *
 * Plain Node, so one `node >= 20` prerequisite covers setup on all three OSes.
 */
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { treeDigest } from "./lib/tree-digest.mjs";

const CLONE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DATA_DIR = join(CLONE_ROOT, "data");
const RECEIPT_DIR = join(CLONE_ROOT, ".agent-kit");
const RECEIPT_PATH = join(RECEIPT_DIR, "install-receipt.json");
const BACKUP_DIR = join(RECEIPT_DIR, "backups");

/** Directories under data/ planted into <workspaceRoot>/.claude/. */
const PLANT_DIRS = ["skills", "agents", "references", "scripts", "hooks", "commands"];

const C = process.stdout.isTTY
  ? { g: "\x1b[32m", y: "\x1b[33m", r: "\x1b[31m", d: "\x1b[2m", x: "\x1b[0m" }
  : { g: "", y: "", r: "", d: "", x: "" };

const ok = (m) => console.log(`  ${C.g}OK${C.x}      ${m}`);
const warn = (m) => console.log(`  ${C.y}WARN${C.x}    ${m}`);
const info = (m) => console.log(`  ${C.d}${m}${C.x}`);

function die(message, paths = []) {
  console.error(`\n${C.r}agent-kit init: refused${C.x} — ${message}`);
  for (const p of paths) console.error(`    ${p}`);
  process.exit(1);
}

// ---- arguments -------------------------------------------------------------

function parseArgs(argv) {
  const args = {
    buildOnly: false, force: false, dryRun: false, help: false,
    mode: process.platform === "win32" ? "copy" : "symlink",
    platformRoot: undefined, workspaceRoot: undefined, installTo: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--build-only") args.buildOnly = true;
    else if (a === "--force") args.force = true;
    else if (a === "--dry-run") args.dryRun = true;
    else if (a === "--install-to") args.installTo = true;
    else if (a === "-h" || a === "--help") args.help = true;
    else if (a === "--mode") args.mode = argv[++i];
    else if (a === "--platform-root") args.platformRoot = argv[++i];
    else if (a === "--workspace-root") args.workspaceRoot = argv[++i];
    else die(`unknown argument "${a}" (try --help)`);
  }
  if (!["symlink", "copy"].includes(args.mode)) die(`--mode must be symlink or copy`);
  return args;
}

const USAGE = `agent-kit init — plant the bundled knowledge into a workspace

  --build-only            compile TypeScript and stop (no roots, no planting)
  --platform-root <dir>   extra content root      (default: the clone root)
  --workspace-root <dir>  root for .claude/       (default: the clone root)
  --mode symlink|copy     default: symlink on POSIX, copy on Windows
  --force                 replace paths this installer did not create
  --install-to            allow writing outside the clone
  --dry-run               print what would happen, change nothing
`;

// ---- receipt ---------------------------------------------------------------

function readReceipt() {
  try {
    return JSON.parse(readFileSync(RECEIPT_PATH, "utf-8"));
  } catch {
    return { version: 1, entries: [] };
  }
}

// ---- planting --------------------------------------------------------------

/**
 * Did THIS installer create `target`?
 *
 * The receipt is the only evidence. A symlink pointing into `data/` used to
 * count on its own, which meant anyone able to create a link could hand this
 * installer a path it believed it owned — and planting writes with
 * `writeFileSync`, which follows the link, so the write landed on the bundled
 * source. Ownership is a claim about history; only the receipt records history.
 */
function isOurs(target, receipt) {
  return receipt.entries.some((e) => resolve(e.path) === resolve(target));
}

/**
 * Has a path we DO own changed since we planted it?
 *
 * This is the second half of the ownership question, and the half that was
 * missing. `isOurs` answers "did I create this?"; a re-run then replanted on
 * that basis alone, destroying any edit made in between. Both answers are
 * required before an overwrite is licensed.
 *
 * A receipt entry with no recorded digest counts as drifted. We cannot prove
 * the content is still ours, and "cannot prove" must not resolve to "delete".
 */
function hasDrifted(target, receipt) {
  const entry = receipt.entries.find((e) => resolve(e.path) === resolve(target));
  if (!entry?.sha256_after) return true;
  return treeDigest(target) !== entry.sha256_after;
}

/** Is `target` inside `base`? `isAbsolute` matters: a cross-volume relative
 *  result on Windows (`D:\outside`) does not start with ".." and would pass. */
function contains(base, target) {
  const rel = relative(base, resolve(target));
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

const insideClone = (target) => contains(CLONE_ROOT, target);

/**
 * Copy whatever is at `path` into `.agent-kit/backups/<digest>` and return the
 * receipt-relative location, or undefined if there was nothing to displace.
 *
 * uninstall.mjs has always looked for a backup here and reported the path as
 * unrestorable when it found none — which was every time, because init recorded
 * a hash and called it a record. A hash proves what was displaced; it restores
 * nothing. `verbatimSymlinks` keeps a planted link a link instead of silently
 * inlining the tree it points at.
 */
function backup(path, digest) {
  if (!digest || !existsSync(path)) return undefined;
  const dest = join(BACKUP_DIR, digest);
  mkdirSync(BACKUP_DIR, { recursive: true });
  if (!existsSync(dest)) {
    cpSync(path, dest, { recursive: true, verbatimSymlinks: true });
  }
  return relative(CLONE_ROOT, dest).split("\\").join("/");
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) { console.log(USAGE); return 0; }

  console.log(`\nagent-kit init  ${C.d}(${CLONE_ROOT})${C.x}\n`);

  // --build-only short-circuits BEFORE any root resolution: building must not
  // depend on where the clone happens to sit.
  if (args.buildOnly) {
    // npm is a .cmd shim on Windows, which spawn cannot exec without a shell.
    const npm = (npmArgs) =>
      spawnSync("npm", npmArgs, {
        cwd: CLONE_ROOT,
        stdio: "inherit",
        shell: process.platform === "win32",
      });

    // A genuinely bare clone has no node_modules, so tsc is not there to run.
    // Install first — the script this replaces did `npm install && npm run
    // build`, and "build-only" that cannot build on a fresh clone is useless.
    if (!existsSync(join(CLONE_ROOT, "node_modules"))) {
      info("node_modules missing — installing dependencies first");
      const lockfile = existsSync(join(CLONE_ROOT, "package-lock.json"));
      const install = npm([lockfile ? "ci" : "install"]);
      if (install.status !== 0) {
        console.error(`dependency install failed (${install.error?.message ?? `exit ${install.status}`})`);
        return install.status ?? 1;
      }
    }

    const r = npm(["run", "build"]);
    if (r.status !== 0) {
      console.error(`build failed (${r.error?.message ?? `exit ${r.status}`})`);
      return r.status ?? 1;
    }
    ok("built dist/");
    return 0;
  }

  // Flags beat environment beats the clone root.
  const platformRoot = resolve(args.platformRoot ?? process.env.PLATFORM_ROOT ?? CLONE_ROOT);
  const workspaceRoot = resolve(args.workspaceRoot ?? process.env.WORKSPACE_ROOT ?? CLONE_ROOT);
  info(`platformRoot  = ${platformRoot}`);
  info(`workspaceRoot = ${workspaceRoot}`);
  info(`mode          = ${args.mode}${args.dryRun ? "  (dry run)" : ""}`);
  console.log("");

  if (!insideClone(workspaceRoot) && !args.installTo) {
    die(
      "planting would write outside the clone, which needs an explicit opt-in. " +
        "Re-run with --install-to if that is what you want.",
      [workspaceRoot],
    );
  }

  const receipt = readReceipt();
  const entries = [];
  const at = new Date().toISOString();
  const claudeDir = join(workspaceRoot, ".claude");

  // Pass 1 — find every collision BEFORE touching anything, so a refusal
  // reports the full list instead of stopping at the first one.
  // Two distinct refusals, both of which mean "this content is the user's":
  // never installed by us, or installed by us and edited since.
  const collisions = [];
  const drifted = [];
  for (const sub of PLANT_DIRS) {
    if (!existsSync(join(DATA_DIR, sub))) continue;
    const target = join(claudeDir, sub);
    if (!existsSync(target)) continue;
    if (!isOurs(target, receipt)) collisions.push(target);
    else if (hasDrifted(target, receipt)) drifted.push(target);
  }
  if (collisions.length > 0 && !args.force) {
    die(
      `${collisions.length} path(s) already exist and were not created by this ` +
        "installer. Nothing has been changed. Re-run with --force to replace " +
        "them (their prior content is backed up to .agent-kit/backups/), or " +
        "move them aside:",
      collisions,
    );
  }
  if (drifted.length > 0 && !args.force) {
    die(
      `${drifted.length} path(s) this installer planted have been edited since. ` +
        "Nothing has been changed. Re-run with --force to replace them (their " +
        "current content is backed up to .agent-kit/backups/ first), or move " +
        "them aside:",
      drifted,
    );
  }

  // Pass 2 — plant.
  // Record the .claude/ directory ONLY when we are the ones creating it, so
  // uninstall can remove it again and not leave an empty shell behind. If it
  // already existed it is the user's, and must survive an uninstall.
  const claudeDirExisted = existsSync(claudeDir);
  if (!args.dryRun) mkdirSync(claudeDir, { recursive: true });
  if (!args.dryRun && !claudeDirExisted) {
    entries.push({ path: claudeDir, action: "created", at });
  }
  for (const sub of PLANT_DIRS) {
    const source = join(DATA_DIR, sub);
    if (!existsSync(source)) { warn(`data/${sub} is missing — nothing to plant`); continue; }
    const target = join(claudeDir, sub);

    let action = args.mode === "symlink" ? "linked" : "copied";
    let shaBefore;
    let backupPath;
    const existed = existsSync(target);
    if (existed) {
      shaBefore = treeDigest(target);
      // Anything displaced is the user's content — whether we planted it and
      // they edited it, or it was never ours. Both reach here only under
      // --force, and both get a real copy kept before we remove anything.
      if (!isOurs(target, receipt) || hasDrifted(target, receipt)) {
        action = "overwritten";
        if (!args.dryRun) backupPath = backup(target, shaBefore);
      }
    }
    if (args.dryRun) { info(`would have ${action} .claude/${sub}`); continue; }

    if (existed) rmSync(target, { recursive: true, force: true });
    if (args.mode === "symlink") {
      try {
        symlinkSync(source, target, "junction");
      } catch (err) {
        // Unprivileged Windows accounts and some filesystems refuse symlinks.
        // Copying is the honest fallback; say so rather than failing setup.
        warn(`symlink failed (${err.code ?? err.message}); copied instead`);
        cpSync(source, target, { recursive: true });
        action = existed && action === "overwritten" ? "overwritten" : "copied";
      }
    } else {
      cpSync(source, target, { recursive: true });
    }
    // sha256_after is what makes `agent-kit uninstall` able to tell "this is
    // still what I planted" from "the user has edited it since".
    entries.push({
      path: target,
      action,
      ...(shaBefore ? { sha256_before: shaBefore } : {}),
      ...(backupPath ? { backup: backupPath } : {}),
      sha256_after: treeDigest(target),
      at,
    });
    ok(`${action.padEnd(11)} .claude/${sub}${backupPath ? `  ${C.d}(backed up)${C.x}` : ""}`);
  }

  // CLAUDE.md / AGENTS.md planting. The three targets setup-local.sh expected
  // (workspace-root.md, gitlab-domain.md, platform-monorepo.md) never existed
  // in data/claude-md/, and its plant helper `return 0`'d silently on a missing
  // source — so it reported success having planted nothing.
  const claudeMdPlants = [
    { source: join(DATA_DIR, "claude-md", "AGENTS.md"), target: join(workspaceRoot, "AGENTS.md") },
  ];
  for (const { source, target } of claudeMdPlants) {
    if (!existsSync(source)) { warn(`plant source missing, skipped: ${source}`); continue; }
    if (!insideClone(target) && !args.installTo) {
      warn(`skipped ${target} — outside the clone (pass --install-to to allow)`);
      continue;
    }
    const existed = existsSync(target);
    const shaBefore = existed ? treeDigest(target) : undefined;
    // Ownership alone licensed this overwrite before, so a user's edited
    // AGENTS.md was replaced on every re-run without warning or backup.
    const displacing = existed && (!isOurs(target, receipt) || hasDrifted(target, receipt));
    if (displacing && !args.force) {
      warn(
        `skipped ${target} — ${isOurs(target, receipt)
          ? "edited since this installer wrote it"
          : "exists and was not created by this installer"} (use --force)`,
      );
      continue;
    }
    if (args.dryRun) { info(`would plant ${target}`); continue; }
    const backupPath = displacing ? backup(target, shaBefore) : undefined;
    // Body only: the data/ masters may carry vault frontmatter, which the
    // active copies Claude Code auto-loads must not have.
    let text = readFileSync(source, "utf-8");
    if (text.startsWith("---\n") || text.startsWith("---\r\n")) {
      const end = text.indexOf("\n---\n", 4);
      if (end !== -1) text = text.slice(end + 5).replace(/^\n+/, "");
    }
    // writeFileSync FOLLOWS a symlink, so a link left at this path would send
    // the write to whatever it points at — including back into data/. Remove
    // the entry first and write a genuine file.
    if (existed) rmSync(target, { recursive: true, force: true });
    writeFileSync(target, text, "utf-8");
    entries.push({
      path: target,
      action: existed ? "overwritten" : "created",
      ...(shaBefore ? { sha256_before: shaBefore } : {}),
      ...(backupPath ? { backup: backupPath } : {}),
      sha256_after: treeDigest(target),
      at,
    });
    ok(`${(existed ? "overwritten" : "created").padEnd(11)} ${target}`);
  }

  // MCP registration. The shipped template carries ${AGENT_KIT_ROOT} rather than
  // a baked path — the old one hardcoded an employer monorepo path whose entry
  // point does not exist here, so the registration it produced could not launch.
  const mcpTemplate = join(DATA_DIR, "scripts", "template-mcp.json");
  const mcpTarget = join(workspaceRoot, ".mcp.json");
  if (!existsSync(mcpTemplate)) {
    warn(`plant source missing, skipped: ${mcpTemplate}`);
  } else if (
    existsSync(mcpTarget) &&
    (!isOurs(mcpTarget, receipt) || hasDrifted(mcpTarget, receipt)) &&
    !args.force
  ) {
    warn(
      `skipped ${mcpTarget} — ${isOurs(mcpTarget, receipt)
        ? "edited since this installer wrote it"
        : "exists and was not created by this installer"} (use --force)`,
    );
  } else if (args.dryRun) {
    info(`would have written ${mcpTarget}`);
  } else {
    const existed = existsSync(mcpTarget);
    const shaBefore = existed ? treeDigest(mcpTarget) : undefined;
    const backupPath =
      existed && (!isOurs(mcpTarget, receipt) || hasDrifted(mcpTarget, receipt))
        ? backup(mcpTarget, shaBefore)
        : undefined;
    const rendered = readFileSync(mcpTemplate, "utf-8")
      .split("${AGENT_KIT_ROOT}")
      .join(CLONE_ROOT.split("\\").join("/"));
    // Fail before writing rather than leaving unparseable JSON behind.
    try {
      JSON.parse(rendered);
    } catch (err) {
      die(`rendered .mcp.json is not valid JSON (${err.message}); ${mcpTemplate} is malformed`);
    }
    if (existed) rmSync(mcpTarget, { recursive: true, force: true });  // never write through a link
    writeFileSync(mcpTarget, rendered, "utf-8");
    entries.push({
      path: mcpTarget,
      action: existed ? "overwritten" : "created",
      ...(shaBefore ? { sha256_before: shaBefore } : {}),
      ...(backupPath ? { backup: backupPath } : {}),
      sha256_after: treeDigest(mcpTarget),
      at,
    });
    ok(`${(existed ? "overwritten" : "created").padEnd(11)} .mcp.json`);
  }

  if (args.dryRun) { console.log("\ndry run — nothing was changed.\n"); return 0; }

  // Merge by path so a re-run updates rather than appends: the receipt is a
  // statement of current state, not a log.
  const byPath = new Map(receipt.entries.map((e) => [resolve(e.path), e]));
  for (const e of entries) byPath.set(resolve(e.path), e);
  mkdirSync(RECEIPT_DIR, { recursive: true });
  writeFileSync(
    RECEIPT_PATH,
    JSON.stringify(
      { version: 1, updated_at: at, roots: { cloneRoot: CLONE_ROOT, platformRoot, workspaceRoot },
        mode: args.mode, entries: [...byPath.values()] },
      null, 2,
    ) + "\n",
    "utf-8",
  );
  ok(`receipt      ${relative(CLONE_ROOT, RECEIPT_PATH) || RECEIPT_PATH}`);
  console.log(`\nDone. Next: ${C.d}npm run build && npm run verify:quickstart${C.x}\n`);
  return 0;
}

process.exit(main());

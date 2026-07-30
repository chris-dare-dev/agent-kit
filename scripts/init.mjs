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
import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const CLONE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DATA_DIR = join(CLONE_ROOT, "data");
const RECEIPT_DIR = join(CLONE_ROOT, ".agent-kit");
const RECEIPT_PATH = join(RECEIPT_DIR, "install-receipt.json");

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

function sha256(path) {
  try {
    if (lstatSync(path).isDirectory()) {
      // Hash the sorted child names: enough to prove what was displaced
      // without reading a whole tree into memory.
      return createHash("sha256")
        .update(readdirSync(path).sort().join("\n"))
        .digest("hex");
    }
    return createHash("sha256").update(readFileSync(path)).digest("hex");
  } catch {
    return undefined;
  }
}

// ---- planting --------------------------------------------------------------

/** Did THIS installer create `target`? A symlink into our data/ counts. */
function isOurs(target, receipt) {
  if (receipt.entries.some((e) => resolve(e.path) === resolve(target))) return true;
  try {
    if (!lstatSync(target).isSymbolicLink()) return false;
    const real = realpathSync(target);
    return !relative(DATA_DIR, real).startsWith("..");
  } catch {
    return false;
  }
}

function insideClone(target) {
  const rel = relative(CLONE_ROOT, resolve(target));
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
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
  const collisions = [];
  for (const sub of PLANT_DIRS) {
    if (!existsSync(join(DATA_DIR, sub))) continue;
    const target = join(claudeDir, sub);
    if (existsSync(target) && !isOurs(target, receipt)) collisions.push(target);
  }
  if (collisions.length > 0 && !args.force) {
    die(
      `${collisions.length} path(s) already exist and were not created by this ` +
        "installer. Nothing has been changed. Re-run with --force to replace " +
        "them (their prior content hash is recorded in the receipt), or move " +
        "them aside:",
      collisions,
    );
  }

  // Pass 2 — plant.
  if (!args.dryRun) mkdirSync(claudeDir, { recursive: true });
  for (const sub of PLANT_DIRS) {
    const source = join(DATA_DIR, sub);
    if (!existsSync(source)) { warn(`data/${sub} is missing — nothing to plant`); continue; }
    const target = join(claudeDir, sub);

    let action = args.mode === "symlink" ? "linked" : "copied";
    let shaBefore;
    const existed = existsSync(target);
    if (existed) {
      shaBefore = sha256(target);
      if (!isOurs(target, receipt)) action = "overwritten";
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
    entries.push({ path: target, action, ...(shaBefore ? { sha256_before: shaBefore } : {}), at });
    ok(`${action.padEnd(11)} .claude/${sub}`);
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
    const shaBefore = existed ? sha256(target) : undefined;
    if (existed && !isOurs(target, receipt) && !args.force) {
      warn(`skipped ${target} — exists and was not created by this installer (use --force)`);
      continue;
    }
    if (args.dryRun) { info(`would plant ${target}`); continue; }
    // Body only: the data/ masters may carry vault frontmatter, which the
    // active copies Claude Code auto-loads must not have.
    let text = readFileSync(source, "utf-8");
    if (text.startsWith("---\n") || text.startsWith("---\r\n")) {
      const end = text.indexOf("\n---\n", 4);
      if (end !== -1) text = text.slice(end + 5).replace(/^\n+/, "");
    }
    writeFileSync(target, text, "utf-8");
    entries.push({
      path: target,
      action: existed ? "overwritten" : "created",
      ...(shaBefore ? { sha256_before: shaBefore } : {}),
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
  } else if (existsSync(mcpTarget) && !isOurs(mcpTarget, receipt) && !args.force) {
    warn(`skipped ${mcpTarget} — exists and was not created by this installer (use --force)`);
  } else if (args.dryRun) {
    info(`would have written ${mcpTarget}`);
  } else {
    const existed = existsSync(mcpTarget);
    const shaBefore = existed ? sha256(mcpTarget) : undefined;
    const rendered = readFileSync(mcpTemplate, "utf-8")
      .split("${AGENT_KIT_ROOT}")
      .join(CLONE_ROOT.split("\\").join("/"));
    // Fail before writing rather than leaving unparseable JSON behind.
    try {
      JSON.parse(rendered);
    } catch (err) {
      die(`rendered .mcp.json is not valid JSON (${err.message}); ${mcpTemplate} is malformed`);
    }
    writeFileSync(mcpTarget, rendered, "utf-8");
    entries.push({
      path: mcpTarget,
      action: existed ? "overwritten" : "created",
      ...(shaBefore ? { sha256_before: shaBefore } : {}),
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

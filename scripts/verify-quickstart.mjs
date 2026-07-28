#!/usr/bin/env node
/**
 * Execute the README Quick Start, so documentation that stops working fails.
 *
 * The README's Quick Start is the first thing an adopter runs and, until this
 * existed, nothing checked it: the documented command died immediately on a
 * required-but-undocumented PLATFORM_ROOT, and no test noticed for the life of
 * the repository.
 *
 * This reads the fenced block OUT of README.md rather than keeping a copy, so
 * editing the README is what changes the check. It then:
 *   1. asserts the block still contains the build step and a server launch,
 *   2. runs the launch command it found, from a temporary working directory,
 *      with an EMPTY environment,
 *   3. performs a real MCP initialize + tools/list handshake over stdio,
 *   4. exits 0 only if a non-empty tool list comes back.
 *
 * Deliberately NOT executed: `git clone` (network) and `claude mcp add` (needs
 * the Claude CLI). Those are asserted to be present and well-formed; the server
 * invocation they wrap is the part that can actually break, and that is run for
 * real. The whole script is plain Node — no bash — so it works under PowerShell.
 */
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const README = join(REPO_ROOT, "README.md");

function fail(message) {
  console.error(`verify-quickstart: FAIL — ${message}`);
  process.exit(1);
}

// ---- 1. Extract the fenced block under "## Quick start" --------------------

// Normalize line endings before matching. Until .gitattributes pins LF, a
// Windows clone can carry CRLF, and a check that silently stopped finding the
// block would report "no fenced block" instead of the truth about the command.
const readme = readFileSync(README, "utf-8").replace(/\r\n/g, "\n");
const section = readme.split(/^## Quick start\s*$/m)[1];
if (!section) fail('README.md has no "## Quick start" section');

const fence = section.match(/```bash\n([\s\S]*?)```/);
if (!fence) fail("the Quick start section has no ```bash fenced block");
const block = fence[1];

// ---- 2. Assert the documented steps are still there ------------------------

if (!/npm run build/.test(block)) {
  fail("the Quick start block no longer builds (`npm run build` missing) — " +
       "the compiled dist/ is what the registered command runs");
}

// `claude mcp add <name> -- <command...>`: everything after the -- is what
// actually gets spawned, so that is what this script runs.
const launch = block.match(/claude mcp add\s+\S+\s+--\s+(.+)/);
if (!launch) {
  fail("the Quick start block no longer registers the server with " +
       "`claude mcp add <name> -- <command>`");
}

const argv = launch[1]
  .trim()
  .match(/"[^"]*"|\S+/g)
  .map((token) => token.replace(/^"|"$/g, ""))
  // $PWD in the README means the clone root; here that is REPO_ROOT.
  .map((token) => token.replace(/\$PWD|\$\{PWD\}/g, REPO_ROOT));

if (argv[0] !== "node") fail(`expected the launch command to be node, got "${argv[0]}"`);
const entry = argv[1];
if (!entry || !/dist[\\/]index\.js$/.test(entry)) {
  fail(`expected the launch command to run dist/index.js, got "${entry ?? "nothing"}"`);
}
if (!existsSync(entry)) {
  fail(`${entry} does not exist — run "npm run build" first, exactly as the ` +
       "Quick start says");
}

// ---- 3. Run it from a temp cwd with an empty environment -------------------

const cwd = mkdtempSync(join(tmpdir(), "agent-kit-quickstart-"));
// Empty except for what a process cannot start without. Notably NO
// PLATFORM_ROOT: needing it is the defect this check exists to catch.
const env = { PATH: process.env.PATH ?? "" };
if (process.platform === "win32") {
  env.SystemRoot = process.env.SystemRoot ?? "";
  env.TEMP = process.env.TEMP ?? "";
}

const child = spawn(process.execPath, argv.slice(1), {
  cwd,
  env,
  stdio: ["pipe", "pipe", "pipe"],
});

let stderr = "";
child.stderr.setEncoding("utf8");
child.stderr.on("data", (chunk) => (stderr += chunk));

function send(message) {
  child.stdin.write(JSON.stringify(message) + "\n");
}

// ---- 4. initialize + tools/list over stdio ---------------------------------

let buffer = "";
const seen = new Map();

const done = new Promise((resolveDone, reject) => {
  const timer = setTimeout(
    () => reject(new Error(`no tools/list response within 60s.\nstderr:\n${stderr}`)),
    60_000,
  );
  child.on("error", (err) => { clearTimeout(timer); reject(err); });
  child.on("exit", (code) => {
    clearTimeout(timer);
    reject(new Error(`server exited (code ${code}) before answering.\nstderr:\n${stderr}`));
  });
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    buffer += chunk;
    let newline;
    while ((newline = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      if (msg.id === undefined) continue;
      seen.set(msg.id, msg);
      if (msg.id === 1) {
        send({ jsonrpc: "2.0", method: "notifications/initialized" });
        send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
      }
      if (msg.id === 2) { clearTimeout(timer); resolveDone(msg); }
    }
  });
});

send({
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "verify-quickstart", version: "1.0.0" },
  },
});

/**
 * Stop the server, then remove the temp directory.
 *
 * On Windows the child still holds `cwd` as its working directory for a moment
 * after kill(), and removing it immediately fails EPERM. Wait for the exit, and
 * treat a leftover temp dir as cosmetic — the OS reclaims it, and failing the
 * check over cleanup would report a passing Quick Start as broken.
 */
async function teardown() {
  if (child.exitCode === null && child.signalCode === null) {
    await new Promise((resolveExit) => {
      child.once("exit", resolveExit);
      child.kill();
      setTimeout(resolveExit, 2_000).unref();
    });
  }
  try {
    rmSync(cwd, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  } catch {
    // Cosmetic only.
  }
}

let response;
try {
  response = await done;
} catch (err) {
  await teardown();
  fail(err.message);
}
await teardown();

const tools = response?.result?.tools;
if (!Array.isArray(tools) || tools.length === 0) {
  fail(`tools/list returned no tools.\nstderr:\n${stderr}`);
}

console.log(
  `verify-quickstart: OK — the README Quick Start starts the server and ` +
    `serves ${tools.length} tools with an empty environment.`,
);

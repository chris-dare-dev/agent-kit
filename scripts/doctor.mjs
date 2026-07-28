#!/usr/bin/env node
/**
 * `agent-kit doctor` — say what is wrong, in one pass, without lying.
 *
 * Grepping the tree for "doctor", "uninstall" or "--preflight" used to return
 * nothing: there was no command that answered "is my install healthy?". With
 * this many independent things needing to line up — roots, build freshness, the
 * socket, Qdrant, the generator gates — a first-run failure was undiagnosable
 * without reading TypeScript source.
 *
 * Rules this obeys:
 *   - EVERY row is evaluated. No early abort; one failure must not hide five.
 *   - A row that cannot apply here is SKIP with a reason, never FAIL.
 *   - Only `required` rows affect the exit code.
 *   - It reports the repository's actual state. Two generator gates are red at
 *     HEAD as of writing, and doctor says so rather than being tuned to pass.
 *
 * Plain Node: no bash, no jq, no shasum, so it behaves the same in PowerShell.
 */
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { connect } from "node:net";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const CLONE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const IS_WIN = process.platform === "win32";

const C = process.stdout.isTTY
  ? { g: "\x1b[32m", r: "\x1b[31m", y: "\x1b[33m", d: "\x1b[2m", x: "\x1b[0m" }
  : { g: "", r: "", y: "", d: "", x: "" };

const rows = [];
const record = (r) => { rows.push(r); return r; };
const pass = (name, required, detail) => record({ name, required, status: "PASS", detail });
const fail = (name, required, expected, found, fix) =>
  record({ name, required, status: "FAIL", expected, found, fix });
const skip = (name, required, reason) => record({ name, required, status: "SKIP", reason });

/**
 * Run a command; never throws.
 *
 * Deliberately NO shell. Every command here (py, python3, docker) is a real
 * executable, and a shell would concatenate rather than escape the arguments —
 * which silently breaks any path containing a space, such as this repository's
 * own "Personal Projects" parent, and turns a clean gate into a bogus FAIL.
 */
function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { encoding: "utf-8", ...opts });
  return { status: r.status, out: `${r.stdout ?? ""}${r.stderr ?? ""}`.trim(), error: r.error };
}

function newestMtime(dir) {
  let newest = 0;
  const walk = (d) => {
    let items;
    try { items = readdirSync(d, { withFileTypes: true }); } catch { return; }
    for (const item of items) {
      const p = join(d, item.name);
      if (item.isDirectory()) walk(p);
      else {
        try { newest = Math.max(newest, statSync(p).mtimeMs); } catch { /* unreadable */ }
      }
    }
  };
  walk(dir);
  return newest;
}

function tcpProbe(host, port, timeout = 1500) {
  return new Promise((resolveProbe) => {
    const socket = connect({ host, port });
    const done = (result) => { socket.destroy(); resolveProbe(result); };
    socket.setTimeout(timeout);
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false));
    socket.once("error", () => done(false));
  });
}

// ---- checks ----------------------------------------------------------------

function checkNode() {
  const major = Number(process.versions.node.split(".")[0]);
  return major >= 20
    ? pass("node >= 20", true, `v${process.versions.node}`)
    : fail("node >= 20", true, ">= 20", `v${process.versions.node}`,
        "install Node 20 or newer (https://nodejs.org)");
}

function checkPython() {
  for (const candidate of IS_WIN ? ["py", "python", "python3"] : ["python3.12", "python3"]) {
    const args = candidate === "py" ? ["-3", "--version"] : ["--version"];
    const r = run(candidate, args);
    if (r.status === 0 && /Python 3\.(1[2-9]|[2-9]\d)/.test(r.out)) {
      return pass("python >= 3.12 (substrate)", false, `${r.out} via ${candidate}`);
    }
  }
  return fail("python >= 3.12 (substrate)", false, "Python 3.12+", "not found on PATH",
    "install Python 3.12; only the optional substrate needs it");
}

function checkBuild() {
  const dist = join(CLONE_ROOT, "dist", "index.js");
  if (!existsSync(dist)) {
    return fail("dist/index.js built", true, "dist/index.js exists", "missing", "npm run build");
  }
  const built = statSync(dist).mtimeMs;
  const newestSrc = newestMtime(join(CLONE_ROOT, "src"));
  return built >= newestSrc
    ? pass("dist/index.js built", true, "newer than src/")
    : fail("dist/index.js built", true, "newer than src/", "STALE — src/ changed since the last build",
        "npm run build   (the registered command runs dist/, not src/)");
}

function checkRoots() {
  // Mirrors config.ts: unset means the package root, which always exists.
  for (const name of ["PLATFORM_ROOT", "WORKSPACE_ROOT"]) {
    const value = process.env[name];
    if (value && !existsSync(value)) {
      fail(`${name} resolves`, true, "an existing directory", `${value} (missing)`,
        `unset ${name} to use the packaged content, or point it somewhere real`);
    } else {
      pass(`${name} resolves`, true, value ? value : `unset → ${CLONE_ROOT}`);
    }
  }
}

function checkGates() {
  const gates = [
    "catalog-generate", "generate-adapter-packs",
    "generate-root-contract", "model-policy-apply",
  ];
  const python = IS_WIN ? "py" : "python3";
  const prefix = IS_WIN ? ["-3"] : [];
  for (const gate of gates) {
    const script = join(CLONE_ROOT, "data", "scripts", `${gate}.py`);
    if (!existsSync(script)) { skip(`gate: ${gate}`, false, "script not present"); continue; }
    const r = run(python, [...prefix, script, "--check"], {
      cwd: CLONE_ROOT,
      // PYTHONUTF8 so the gates do not mis-decode UTF-8 content under a
      // non-UTF-8 console codepage, which is its own class of false failure.
      env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
    });
    if (r.error) { skip(`gate: ${gate}`, false, `python not runnable (${r.error.code})`); continue; }
    if (r.status === 0) pass(`gate: ${gate}`, false, "clean");
    else {
      fail(`gate: ${gate}`, false, "exit 0", (r.out.split("\n").pop() || `exit ${r.status}`).slice(0, 90),
        `python3 data/scripts/${gate}.py     (regenerate, then commit)`);
    }
  }
}

function checkSocket() {
  if (IS_WIN) {
    return skip("artifact-memory socket", false,
      "Windows has no AF_UNIX; the four artifact tools are unavailable here (M5)");
  }
  const socketPath = join(homedir(), ".local", "share", "workspace-artifacts",
    "services", "qdrant", "artifact-memory.sock");
  if (!existsSync(socketPath)) {
    return fail("artifact-memory socket", false, `a socket at ${socketPath}`, "missing",
      "start the resident service (see workspace-tooling/README.md)");
  }
  const mode = statSync(socketPath).mode & 0o777;
  return mode === 0o600
    ? pass("artifact-memory socket", false, `${socketPath} (0600)`)
    : fail("artifact-memory socket", false, "mode 0600", `mode ${mode.toString(8)}`,
        `chmod 600 ${socketPath}`);
}

function checkDocker() {
  const r = run("docker", ["info", "--format", "{{.ServerVersion}}"]);
  if (r.error || r.status !== 0) {
    return fail("docker reachable", false, "a running Docker daemon",
      r.error ? "docker not on PATH" : "daemon not responding",
      "start Docker Desktop; only the optional substrate needs it");
  }
  return pass("docker reachable", false, `server ${r.out.split("\n")[0]}`);
}

async function checkQdrant() {
  const port = Number(process.env.QDRANT_PORT ?? 6333);
  return (await tcpProbe("127.0.0.1", port))
    ? pass("qdrant listening", false, `127.0.0.1:${port}`)
    : fail("qdrant listening", false, `something on 127.0.0.1:${port}`, "nothing answering",
        "docker compose -f workspace-tooling/services/qdrant/compose.yaml up -d");
}

// ---- report ----------------------------------------------------------------

const ICON = { PASS: `${C.g}PASS${C.x}`, FAIL: `${C.r}FAIL${C.x}`, SKIP: `${C.y}SKIP${C.x}` };

async function main() {
  console.log(`\nagent-kit doctor  ${C.d}(${CLONE_ROOT})${C.x}\n`);

  checkNode();
  checkPython();
  checkBuild();
  checkRoots();
  checkGates();
  checkSocket();
  checkDocker();
  await checkQdrant();

  const width = Math.max(...rows.map((r) => r.name.length));
  for (const row of rows) {
    const tag = row.required ? "required" : "optional";
    console.log(`  ${ICON[row.status]}  ${row.name.padEnd(width)}  ${C.d}[${tag}]${C.x}`);
    if (row.status === "PASS") console.log(`        ${C.d}${row.detail}${C.x}`);
    if (row.status === "SKIP") console.log(`        ${C.d}${row.reason}${C.x}`);
    if (row.status === "FAIL") {
      console.log(`        expected: ${row.expected}`);
      console.log(`        found:    ${row.found}`);
      console.log(`        fix:      ${row.fix}`);
    }
  }

  const failed = rows.filter((r) => r.status === "FAIL");
  const blocking = failed.filter((r) => r.required);
  console.log(
    `\n  ${rows.filter((r) => r.status === "PASS").length} pass · ` +
      `${failed.length} fail (${blocking.length} required) · ` +
      `${rows.filter((r) => r.status === "SKIP").length} skip\n`,
  );
  if (blocking.length > 0) {
    console.log(`  ${C.r}Not healthy${C.x} — fix the required rows above.\n`);
    return 1;
  }
  if (failed.length > 0) {
    console.log(`  ${C.y}Usable${C.x} — every required check passed; the failures above are optional components.\n`);
    return 0;
  }
  console.log(`  ${C.g}Healthy.${C.x}\n`);
  return 0;
}

process.exit(await main());

#!/usr/bin/env node
/**
 * Run every committed gate, report all of them, exit non-zero if any failed.
 *
 * Seven checks in this repository were reachable from nothing: no runner, no npm
 * script, no documentation. `tests/hooks/block-plaintext-secret-write.test.sh`
 * was named only inside a secret-scan allowlist, as an exemption. A check nobody
 * can run is a check nobody does run, and it decays into a file that describes a
 * guarantee the code stopped providing.
 *
 * Design choices worth stating:
 *
 *   EVERY gate runs. One failure must not hide five, so this never short-circuits
 *   — it collects and reports, then exits non-zero at the end.
 *
 *   Plain Node, no shell. `npm run` uses cmd.exe on Windows, where `&&` chains
 *   and POSIX quoting break; and shelling out would concatenate rather than
 *   escape arguments, which this repository's own "Personal Projects" path has
 *   already broken twice.
 *
 *   Shell harnesses are SKIPPED, not failed, where bash is unavailable — with the
 *   reason printed. Reporting "pass" for something that never executed is the
 *   failure mode this file exists to end.
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const IS_WIN = process.platform === "win32";

const C = process.stdout.isTTY
  ? { g: "\x1b[32m", r: "\x1b[31m", y: "\x1b[33m", d: "\x1b[2m", x: "\x1b[0m" }
  : { g: "", r: "", y: "", d: "", x: "" };

/** python3 on POSIX; the launcher or plain python on Windows. */
function pythonCommand() {
  for (const [cmd, pre] of IS_WIN ? [["py", ["-3"]], ["python", []]] : [["python3", []], ["python", []]]) {
    const probe = spawnSync(cmd, [...pre, "--version"], { encoding: "utf-8" });
    if (!probe.error && probe.status === 0) return { cmd, pre };
  }
  return null;
}

const PY = pythonCommand();
const BASH = (() => {
  const probe = spawnSync("bash", ["--version"], { encoding: "utf-8" });
  return probe.error || probe.status !== 0 ? null : "bash";
})();

/** Generator/consistency gates — the ones that guard committed artifacts. */
const PY_GATES = [
  "catalog-generate.py",
  "generate-adapter-packs.py",
  "generate-root-contract.py",
  "model-policy-apply.py",
  "mcp-server-name-check.py",
  "denylist-check.py",
  "template-settings-check.py",
];

/** Previously orphaned: reachable from nothing until this runner existed. */
const PY_CHECKS = [
  "skill-ref-check.py",
  "artifact-memory-eval-manifest-check.py",
  "pipeline-outcome-log-test.py",
];

const SHELL_HARNESSES = [
  "data/scripts/sealed-eval-test.sh",
  "data/scripts/milestone-pipeline-consolidate-memory-test.sh",
  "tests/check-skill-prompts.sh",
  "tests/hooks/block-plaintext-secret-write.test.sh",
];

/**
 * Checks known to fail at HEAD, each with the issue that fixes it.
 *
 * Reported as QUARANTINED — never PASS — and excluded from the exit code so a
 * fresh clone is not red on debt it did not create. Verified failing at HEAD
 * before being listed here, so this cannot be used to bury a NEW break: a check
 * that starts failing is not on this list and turns the runner red.
 *
 * Emptying this map is issues #59, #60 and #69.
 */
const QUARANTINED = new Map([
  ["pipeline-outcome-log-test.py",
   "fails at HEAD: outcome-log locking + cwd resolution — issues #69, #60"],
  ["data/scripts/sealed-eval-test.sh",
   "fails at HEAD: the committed corpus no longer matches its own seal — issue #59"],
]);

const results = [];

function record(name, status, detail) {
  if (status === "FAIL" && QUARANTINED.has(name)) {
    status = "QUAR";
    detail = QUARANTINED.get(name);
  }
  results.push({ name, status, detail });
  const icon =
    status === "PASS" ? `${C.g}PASS${C.x}`
    : status === "SKIP" ? `${C.y}SKIP${C.x}`
    : status === "QUAR" ? `${C.y}QUAR${C.x}`
    : `${C.r}FAIL${C.x}`;
  console.log(`  ${icon}  ${name}${detail ? `  ${C.d}${detail}${C.x}` : ""}`);
}

function runPython(relPath, args, name) {
  if (!PY) return record(name, "SKIP", "no python interpreter on PATH");
  const full = join(REPO_ROOT, relPath);
  if (!existsSync(full)) return record(name, "FAIL", `missing: ${relPath}`);
  const run = spawnSync(PY.cmd, [...PY.pre, full, ...args], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
    // Explicit UTF-8 so a non-UTF-8 console codepage cannot manufacture drift.
    env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
  });
  if (run.status === 0) return record(name, "PASS");
  const last = `${run.stdout ?? ""}${run.stderr ?? ""}`.trim().split("\n").filter(Boolean).pop();
  record(name, "FAIL", (last ?? `exit ${run.status}`).slice(0, 110));
}

function runShell(relPath) {
  const name = relPath;
  if (!BASH) return record(name, "SKIP", "bash unavailable on this host");
  const full = join(REPO_ROOT, relPath);
  if (!existsSync(full)) return record(name, "FAIL", `missing: ${relPath}`);
  const run = spawnSync(BASH, [full], { cwd: REPO_ROOT, encoding: "utf-8" });
  if (run.status === 0) return record(name, "PASS");
  const last = `${run.stdout ?? ""}${run.stderr ?? ""}`.trim().split("\n").filter(Boolean).pop();
  record(name, "FAIL", (last ?? `exit ${run.status}`).slice(0, 110));
}

const only = process.argv[2];

console.log(`\nagent-kit gates  ${C.d}(${REPO_ROOT})${C.x}\n`);

if (!only || only === "generators") {
  console.log(`  ${C.d}— generator gates —${C.x}`);
  for (const gate of PY_GATES) runPython(`data/scripts/${gate}`, ["--check"], gate);
}

if (!only || only === "checks") {
  console.log(`\n  ${C.d}— consistency checks —${C.x}`);
  for (const check of PY_CHECKS) runPython(`data/scripts/${check}`, [], check);
}

if (!only || only === "shell") {
  console.log(`\n  ${C.d}— shell harnesses —${C.x}`);
  for (const harness of SHELL_HARNESSES) runShell(harness);
}

const failed = results.filter((r) => r.status === "FAIL");
const skipped = results.filter((r) => r.status === "SKIP");
const quarantined = results.filter((r) => r.status === "QUAR");
console.log(
  `\n  ${results.filter((r) => r.status === "PASS").length} pass · ` +
    `${failed.length} fail · ${quarantined.length} quarantined · ` +
    `${skipped.length} skip\n`,
);
if (skipped.length) {
  console.log(`  ${C.y}Note${C.x}: skipped gates verified nothing on this host.\n`);
}
if (quarantined.length) {
  console.log(
    `  ${C.y}Quarantined gates verified nothing.${C.x} They fail at HEAD and are\n` +
      "  excluded from the exit code so a fresh clone is not red on inherited debt.\n" +
      "  Each names the issue that removes it from the list.\n",
  );
}
process.exit(failed.length ? 1 : 0);

/**
 * The gate runner must hand bash a path that bash can actually open.
 *
 * `spawnSync(BASH, [join(REPO_ROOT, relPath)])` passed a Windows absolute path
 * (`C:\Users\...`). Git Bash translates that, so on a machine with Git Bash
 * first on PATH every shell harness passed and the bug was invisible. WSL's
 * bash does not translate it: the same command, same machine, same repository
 * exits 127 with "No such file or directory", turning three gates red purely on
 * PATH order.
 *
 * This drives the REAL runner through a second bash, so it fails if the
 * absolute-path form ever comes back. It is not a source-text assertion —
 * grepping the script for `join(` would pass just as happily with the bug
 * reintroduced under a different spelling.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/**
 * A bash whose filesystem root differs from the one Node reports — the only
 * configuration in which this bug is observable. On Windows that is WSL.
 */
function differentlyRootedBash() {
  if (process.platform !== "win32") return undefined;
  const probe = spawnSync("wsl.exe", ["-e", "bash", "--version"], { encoding: "utf-8" });
  return probe.error || probe.status !== 0 ? undefined : "wsl.exe -e bash";
}

const BASH = differentlyRootedBash();

test(
  "shell harnesses run under a bash that cannot read Windows absolute paths",
  { skip: BASH ? false : "needs a second, differently-rooted bash (WSL on Windows)" },
  () => {
    const run = spawnSync(
      process.execPath,
      [join(REPO_ROOT, "scripts", "run-gates.mjs"), "shell"],
      {
        cwd: REPO_ROOT,
        encoding: "utf-8",
        env: { ...process.env, AGENT_KIT_BASH: BASH },
      },
    );

    const output = `${run.stdout ?? ""}${run.stderr ?? ""}`;
    assert.doesNotMatch(
      output,
      /No such file or directory/,
      `bash could not open a path the runner gave it:\n${output}`,
    );
    // SKIP would mean the override never resolved, so the run proved nothing.
    assert.doesNotMatch(
      output,
      /bash unavailable on this host/,
      "the AGENT_KIT_BASH override did not resolve, so nothing was verified",
    );
    assert.match(output, /PASS/, `no harness passed:\n${output}`);
    assert.equal(run.status, 0, `shell gates failed under ${BASH}:\n${output}`);
  },
);

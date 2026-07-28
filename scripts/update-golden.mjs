#!/usr/bin/env node
/**
 * Regenerate every tools/list golden fixture, on any OS.
 *
 * `UPDATE_GOLDEN=1 node ...` in an npm script only works on POSIX: npm runs
 * scripts through cmd.exe on Windows, where the VAR=value prefix is a syntax
 * error rather than an assignment. Setting the variable here keeps the command
 * `npm run test:update-golden` on all three platforms with no extra dependency.
 *
 * Both goldens are written in one pass: the full 17-tool list (only on a host
 * that can actually serve the artifact-memory group) and the 13-tool degraded
 * list (always, because the contract test stubs the platform).
 */
import { spawnSync } from "node:child_process";

const result = spawnSync(
  process.execPath,
  ["--import", "tsx", "--test", "src/mcp-contract.test.ts"],
  { stdio: "inherit", env: { ...process.env, UPDATE_GOLDEN: "1" } },
);

if (result.error) {
  console.error(`failed to launch the test runner: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);

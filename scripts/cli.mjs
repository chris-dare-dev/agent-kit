#!/usr/bin/env node
/**
 * `agent-kit` — the one entry point.
 *
 * Subcommands live in their own files and are spawned rather than imported, so
 * each stays independently runnable (`node scripts/init.mjs`) and an exit code
 * from a subcommand is this process's exit code, unchanged.
 */
import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPTS = resolve(dirname(fileURLToPath(import.meta.url)));

const COMMANDS = {
  init: { file: "init.mjs", blurb: "plant the bundled knowledge into a workspace's .claude/" },
  doctor: { file: "doctor.mjs", blurb: "check every prerequisite and report what is wrong" },
  "verify-quickstart": {
    file: "verify-quickstart.mjs",
    blurb: "execute the README Quick Start and prove it still works",
  },
};

const [name, ...rest] = process.argv.slice(2);

if (!name || name === "-h" || name === "--help" || name === "help") {
  const width = Math.max(...Object.keys(COMMANDS).map((c) => c.length));
  console.log("\nagent-kit <command> [options]\n");
  for (const [command, { blurb }] of Object.entries(COMMANDS)) {
    console.log(`  ${command.padEnd(width)}  ${blurb}`);
  }
  console.log("\nRun `agent-kit <command> --help` for a command's own options.\n");
  process.exit(name ? 0 : 1);
}

const command = COMMANDS[name];
if (!command) {
  console.error(
    `agent-kit: unknown command "${name}". Known: ${Object.keys(COMMANDS).join(", ")}`,
  );
  process.exit(1);
}

const result = spawnSync(process.execPath, [join(SCRIPTS, command.file), ...rest], {
  stdio: "inherit",
});
process.exit(result.status ?? 1);

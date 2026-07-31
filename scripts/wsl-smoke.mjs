#!/usr/bin/env node
/**
 * Windows-host driver for the WSL2 smoke test.
 *
 * Checks that WSL2 is actually there, then runs `scripts/wsl/smoke.sh` inside
 * the guest. The check comes first and is bounded, because the failure this
 * exists to prevent is a Windows user watching a command hang or produce a
 * cascade of unrelated errors when the real answer is "WSL2 is not installed"
 * (M2, gates-green-t-wsl-path).
 *
 *   node scripts/wsl-smoke.mjs [--guest-path <path-inside-wsl>] [--distro <name>]
 *
 * The guest path defaults to $AGENT_KIT_WSL_PATH, then ~/agent-kit. It is NOT
 * derived from this clone's location: the repo is supposed to live on the Linux
 * filesystem, not under /mnt/c — see docs/platforms/windows-wsl.md.
 *
 * Exit: 0 the query round-tripped · 1 the smoke test failed · 2 WSL2 is
 * unavailable · 3 the substrate is not provisioned in the guest.
 */
import { spawnSync } from "node:child_process";

/** Bounded so "is WSL2 here" can never itself be the thing that hangs. */
const PROBE_TIMEOUT_MS = 2000;

function parseArgs(argv) {
  const out = { guestPath: process.env.AGENT_KIT_WSL_PATH || "~/agent-kit", distro: null };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--guest-path") out.guestPath = argv[++i];
    else if (argv[i] === "--distro") out.distro = argv[++i];
    else if (argv[i] === "-h" || argv[i] === "--help") out.help = true;
    else {
      console.error(`wsl-smoke: unknown argument: ${argv[i]}`);
      process.exit(2);
    }
  }
  return out;
}

function refuseNoWsl(detail) {
  console.error(
    [
      "wsl-smoke: WSL2 is not available on this host.",
      `  ${detail}`,
      "",
      "  The resident artifact-memory service listens on a Unix-domain socket,",
      "  which Windows does not provide. WSL2 is the supported Windows path;",
      "  a native transport is milestone M5 (native-everywhere).",
      "",
      "  Install it (elevated PowerShell, then reboot):",
      "      wsl --install",
      "",
      "  Then follow docs/platforms/windows-wsl.md.",
    ].join("\n"),
  );
  process.exit(2);
}

const args = parseArgs(process.argv.slice(2));
if (args.help) {
  console.log("node scripts/wsl-smoke.mjs [--guest-path <path>] [--distro <name>]");
  process.exit(0);
}

if (process.platform !== "win32") {
  console.error(
    "wsl-smoke: this driver is for a Windows host. On Linux or macOS run the " +
      "guest script directly:\n    scripts/wsl/smoke.sh",
  );
  process.exit(2);
}

// `wsl.exe --status` is the cheapest question that distinguishes "not
// installed" (ENOENT) from "installed but no distro" (non-zero / empty).
const probe = spawnSync("wsl.exe", ["--status"], {
  timeout: PROBE_TIMEOUT_MS,
  encoding: "utf-8",
  windowsHide: true,
});

if (probe.error) {
  refuseNoWsl(
    probe.error.code === "ENOENT"
      ? "wsl.exe is not on PATH."
      : `wsl.exe --status failed: ${probe.error.message}`,
  );
}
if (probe.status !== 0) {
  refuseNoWsl(`wsl.exe --status exited ${probe.status}.`);
}

// A distro must exist, not just the WSL feature. `-q` lists only names.
const distros = spawnSync("wsl.exe", ["-l", "-q"], {
  timeout: PROBE_TIMEOUT_MS,
  encoding: "utf-8",
  windowsHide: true,
});
// wsl.exe emits UTF-16LE; Node decoded it as utf-8, so strip the interleaved NULs.
const names = (distros.stdout || "")
  .replace(/\0/g, "")
  .split(/\r?\n/)
  .map((s) => s.trim())
  .filter(Boolean);
if (names.length === 0) {
  refuseNoWsl("WSL is present but no distribution is installed (`wsl --install -d Ubuntu`).");
}

const distro = args.distro || names[0];
console.log(`wsl-smoke: distro     ${distro}`);
console.log(`wsl-smoke: guest path ${args.guestPath}`);

const wslArgs = ["-d", distro, "-e", "bash", "-lc", `cd ${args.guestPath} && scripts/wsl/smoke.sh`];
const run = spawnSync("wsl.exe", wslArgs, { stdio: "inherit", windowsHide: true });

if (run.error) {
  console.error(`wsl-smoke: could not run the guest script: ${run.error.message}`);
  process.exit(1);
}
if (run.status === 3) {
  console.error(
    "wsl-smoke: the substrate is not provisioned in the guest. Inside WSL run:\n" +
      `    cd ${args.guestPath} && scripts/wsl/setup.sh --apply`,
  );
  process.exit(3);
}
process.exit(run.status ?? 1);

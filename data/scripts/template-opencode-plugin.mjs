/**
 * template-opencode-plugin.mjs — OpenCode adapter for the plaintext-secret
 * write guard (S1.2, provider-neutral-agent-kit-remediation-m1).
 *
 * CANONICAL SOURCE (git-tracked). Planted copy lives at
 * `platform/.opencode/plugin/secret-guard.js` and is registered via the
 * `plugin` key in `platform/opencode.json`. The `plugin/` subdir is
 * generator-exempt: generate-adapter-packs.py `apply()` writes only
 * `.opencode/{agents,skills,commands}` and prunes only `.codex/agents/*.toml`,
 * so a future `--apply` cannot overwrite or remove this file.
 *
 * DESIGN: thin adapter, NOT a reimplementation — maps OpenCode's native
 * payload shapes onto the provider-agnostic CORE
 * (data/hooks/block-plaintext-secret-write.sh) via BunShell, so the detection
 * regexes live in exactly one place.
 *   edit-type ask:  metadata.{filepath,diff}  -> {tool_input:{file_path,content}}
 *   bash-type ask:  metadata.command          -> {tool_input:{command}}   (INFERRED — see NOTES)
 *
 * ============================= NOTES — PENDING LIVE-VERIFICATION =============================
 * Status as of 2026-07-12 (opencode 1.17.17): coverage is UNVERIFIED. Do not
 * count this adapter as closing the OpenCode leg of E1/KR1 until a live
 * OpenCode session confirms:
 *  1. FIRING CONDITION: whether `permission.ask` fires at all for edit/bash
 *     tool calls under platform/opencode.json's current permission block (only
 *     `external_directory` is configured; edit/bash run on OpenCode DEFAULTS,
 *     whose routing through permission.ask is unmeasured — SP1 recovered the
 *     payload SHAPE via strings(1) on the binary, not the firing condition).
 *  2. BASH PAYLOAD SHAPE: `metadata.command` for bash-type asks is INFERRED by
 *     symmetry with the measured edit shape (`metadata.{filepath,diff}`), not
 *     measured. If the real field differs the bash mapping silently no-ops
 *     (fails OPEN, consistent with the CORE's fail-open posture).
 *  3. FALLBACK ARGS SHAPE: `tool.execute.before` fires unconditionally per the
 *     Hooks type contract (@opencode-ai/plugin@1.17.17 dist/index.d.ts), and is
 *     registered below as the documented fallback — but `output.args`'s
 *     per-tool field names (command? filePath? content?) are unmeasured. It
 *     probes the plausible field names defensively and denies by THROW (the
 *     only abort mechanism that hook exposes).
 * Verification recipe: run `opencode` in the platform dir with this plugin
 * registered plus a temporary `console.error` in each hook; attempt (a) a
 * Write of a fake glpat- token to .mcp.json, (b) the same via `echo ... >
 * .mcp.json` in the bash tool; record which hook fired and the exact payloads.
 * ==============================================================================================
 *
 * Fails OPEN everywhere (guard unresolvable, shell error, parse error) — a
 * broken plugin must never wedge the session; parity with the bash CORE.
 */
import { existsSync } from "node:fs";
import { join } from "node:path";

/** Clone-root-relative path to the guard. */
const GUARD_REL = "data/hooks/block-plaintext-secret-write.sh";

/**
 * Locate the guard: AGENT_KIT_ROOT if set, else relative to the project.
 *
 * This used to try three candidates, one of which was
 * a hardcoded pre-fork employer monorepo path — so every plugin this
 * kit emitted carried a former employer's internal group name into a stranger's
 * repository, and all three candidates missed on any machine but one. Combined
 * with the deliberate fail-open below, that meant every non-author install ran
 * completely unguarded and said nothing about it.
 */
function resolveGuard(directory) {
  const roots = [process.env.AGENT_KIT_ROOT, directory].filter(Boolean);
  const attempted = roots.map((root) => join(root, GUARD_REL));
  return {
    guard: attempted.find((path) => existsSync(path)) || null,
    attempted,
  };
}

export const SecretGuardPlugin = async ({ $, directory }) => {
  const { guard, attempted } = resolveGuard(directory);

  // Still fails open — a broken plugin must never wedge a session, which is the
  // documented parity with the bash CORE. What changes is the SILENCE: an
  // operator who thinks they are guarded and is not now finds out at load time.
  // Warn here, once at plugin load, and never inside denyReason — that runs per
  // intercepted tool call and would spam until people silence it.
  if (!guard) {
    process.stderr.write(
      "[agent-kit] secret-guard INACTIVE: block-plaintext-secret-write.sh not " +
        `found. Looked in: ${attempted.join(", ") || "(no root available)"}. ` +
        "Set AGENT_KIT_ROOT to your agent-kit clone to enable it.\n",
    );
  }

  const denyReason = async (toolInput) => {
    if (!guard) return null; // fail open: CORE not found on this machine
    try {
      const payload = JSON.stringify({ tool_input: toolInput });
      const out = await $`printf '%s' ${payload} | bash ${guard}`.quiet().text();
      if (!out.trim()) return null;
      const decision = JSON.parse(out)?.hookSpecificOutput;
      return decision?.permissionDecision === "deny"
        ? decision.permissionDecisionReason || "plaintext credential write blocked"
        : null;
    } catch {
      return null; // fail open on any shell/parse error
    }
  };

  const mapPermission = (input) => {
    const md = input?.metadata || {};
    if (input?.type === "edit" && md.filepath) {
      return { file_path: String(md.filepath), content: String(md.diff ?? "") };
    }
    if (md.command) {
      // Bash-type shape INFERRED, not measured — see NOTES item 2.
      return { command: String(md.command) };
    }
    return null;
  };

  return {
    // PRIMARY interception point (SP1-measured edit payload shape).
    "permission.ask": async (input, output) => {
      const ti = mapPermission(input);
      if (!ti) return;
      const reason = await denyReason(ti);
      if (reason) output.status = "deny";
    },

    // DOCUMENTED FALLBACK (fires unconditionally per the Hooks contract) in
    // case permission.ask never fires for edit/bash under default permission
    // config. args field names are probed defensively — see NOTES item 3.
    "tool.execute.before": async (input, output) => {
      const args = output?.args;
      if (!args || typeof args !== "object") return;
      let ti = null;
      const fp = args.filePath || args.file_path || args.filepath;
      if (fp) {
        ti = { file_path: String(fp), content: String(args.content ?? args.newString ?? args.new_string ?? "") };
      } else if (args.command) {
        ti = { command: String(args.command) };
      }
      if (!ti) return;
      const reason = await denyReason(ti);
      if (reason) {
        // tool.execute.before exposes no status field; throwing is the only
        // way to abort the tool call from this hook.
        throw new Error(reason);
      }
    },
  };
};

/**
 * guard.ts — thin, pure tool-input guard over the M1 AIDefence scanner.
 *
 * This is the E2 wiring helper for `ruflo-security-salvage-m2`. It does NOT
 * change scanner logic — it only `import`s `scan()` from aidefence.ts and maps a
 * scan result onto a mode-aware {block | log-only} decision. Keeping it in a
 * SEPARATE module (not inside aidefence.ts) preserves the milestone lock "do not
 * modify the scanner" with a crisp boundary, and makes the guard unit-testable
 * without booting the MCP server.
 *
 * Zero runtime dependencies. Node stdlib + the scanner ONLY.
 *
 * Enforcement modes (L-6 ramp — see research brief §5):
 *   - "log"     (DEFAULT): a SAFETY match is recorded (`matched:true`) but the
 *     call is NOT blocked (`blocked:false`). The caller emits one structured
 *     stderr line and falls through to normal dispatch. Used to soak the FP rate
 *     on real multi-engineer traffic before any input is ever blocked.
 *   - "enforce": a SAFETY match blocks the call (`blocked:true`); the caller
 *     returns an isError with the matched rule id + class + severity.
 *
 * The guard keys ONLY on the three SAFETY classes (prompt_injection,
 * path_traversal, command_injection). PII is handled separately by the always-on
 * pre-external-write gate in wiki.ts — it never blocks a tool call here.
 */
import { scan } from "./aidefence.js";
import type { ThreatClass } from "./aidefence-rules.js";
import type { ServerProfile } from "../types.js";

export type GuardMode = "enforce" | "log";

/**
 * Resolve the guard mode from the trust profile and the AIDEFENCE_GUARD_MODE env.
 *
 * A `shared` deployment HARD-FORCES `enforce` (fail closed) — it is NOT
 * overridable by AIDEFENCE_GUARD_MODE, so a stray `=log` in a shared chart's
 * values can never silently downgrade the trust boundary to soak mode. The
 * `personal` profile keeps the deliberate log-mode default (see the CAVEAT in
 * index.ts) and still honors an explicit `=enforce` override. Pure + unit-testable.
 */
export function resolveGuardMode(
  serverProfile: ServerProfile,
  envValue: string | undefined,
): GuardMode {
  if (serverProfile === "shared") return "enforce";
  return envValue === "enforce" ? "enforce" : "log";
}

/**
 * Decision produced by the guard for one serialized tool-input string.
 *  - `matched`  — a SAFETY rule fired (mode-independent fact about the input).
 *  - `blocked`  — true ONLY in enforce mode when a SAFETY rule fired.
 *  - `ruleId` / `cls` / `severity` — the FIRST matched safety rule (the one the
 *    block message / log line surfaces). Present iff `matched` is true.
 */
export interface GuardDecision {
  readonly blocked: boolean;
  readonly matched: boolean;
  readonly ruleId?: string;
  readonly cls?: ThreatClass;
  readonly severity?: string;
}

/**
 * guardToolInput — scan one serialized tool-input string and return a mode-aware
 * decision. Pure: same input + mode → same output (the scanner is pure).
 *
 * The caller serializes the tool arguments (`JSON.stringify(argObj)`) and hands
 * the whole string in — matching the corpus shape the M1 FP rate (0% on 984 real
 * tool-call entries) was measured against. Per-field scanning is intentionally
 * avoided (see brief §1.2).
 *
 * @param serializedArgs the tool input as one string (typically JSON.stringify(argObj))
 * @param mode           "enforce" blocks on a SAFETY match; "log" never blocks
 */
export function guardToolInput(
  serializedArgs: string,
  mode: GuardMode,
): GuardDecision {
  const result = scan(serializedArgs);
  // scan().safe === (matches.length === 0); a match means a SAFETY rule fired.
  if (result.safe) {
    return { blocked: false, matched: false };
  }
  // First matched safety rule drives the surfaced message (scan preserves order:
  // injection → traversal → command, first-match-per-rule).
  const first = result.matches[0];
  return {
    blocked: mode === "enforce",
    matched: true,
    ruleId: first.ruleId,
    cls: first.cls,
    severity: first.severity,
  };
}

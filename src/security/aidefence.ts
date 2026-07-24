/**
 * aidefence.ts — stdlib-only AIDefence scanner (is_safe / has_pii / scan).
 *
 * Reimplemented (NOT forked) from ruflo (MIT): github.com/ruvnet/ruflo
 *   v3/@claude-flow/aidefence/src/domain/services/threat-detection-service.ts
 * License: MIT (ruflo). The detection patterns live as DATA in aidefence-rules.ts;
 * this module is the pure-function scanning logic over those patterns.
 *
 * Zero runtime dependencies. Node stdlib ONLY — no external calls, no native
 * modules, no new deps. The scanner is pure regex over strings; node `crypto` is
 * permitted by the lock but is NOT needed here (no hashing/randomness required),
 * so it is intentionally not imported. Pure functions: same input → same output.
 *
 * Behavioral parity with the spike POC (poc/scanner.py) is load-bearing — the
 * measured FP/FN (inj FN 1.11%, PII FN 0%, FP 0% on 984 real entries) depend on it.
 * Do NOT change normalization or matching semantics without re-measuring.
 */
import {
  SAFETY_RULES,
  PII_RULES,
  RULESET_VERSION,
  type Severity,
  type ThreatClass,
} from "./aidefence-rules.js";

/** One matched safety rule with its span (indices into the NORMALIZED string). */
export interface ScanMatch {
  readonly ruleId: string;
  readonly cls: ThreatClass;
  readonly sub: string;
  readonly severity: Severity;
  readonly start: number; // index into the NORMALIZED string (POC reports normalized spans)
  readonly end: number;
}

/** has_pii result — found flag + spans (indices into RAW text; matched bounded ≤60 chars). */
export interface PiiResult {
  readonly found: boolean;
  readonly spans: ReadonlyArray<{
    readonly ruleId: string;
    readonly sub: string;
    readonly start: number; // index into the RAW (un-normalized) text — POC's has_pii uses raw
    readonly end: number;
    readonly matched: string; // m.group()[:60]
  }>;
}

/** Full scan: safety matches (injection/traversal/command) + the pii block. */
export interface ScanResult {
  readonly safe: boolean; // === matches.length === 0
  readonly matches: readonly ScanMatch[];
  readonly pii: PiiResult;
  readonly rulesetVersion: string; // RULESET_VERSION — for version assertions
}

/** Zero-width characters stripped during normalization (ZWSP U+200B … BOM U+FEFF). */
const ZERO_WIDTH = /[​-‍﻿]/g;
const WHITESPACE = /\s+/g;

/**
 * _normalize — mirror the POC's _normalize EXACTLY (used for the 3 SAFETY classes
 * only; has_pii deliberately matches RAW text so credential spans/lengths are exact).
 * NFKC normalize, strip zero-width chars, collapse all whitespace runs to a single
 * space, then trim.
 */
function normalize(text: string): string {
  return text
    .normalize("NFKC")
    .replace(ZERO_WIDTH, "")
    .replace(WHITESPACE, " ")
    .trim();
}

/** True iff the input is empty or only whitespace (POC: `not text or not text.strip()`). */
function isBlank(input: string): boolean {
  return input.length === 0 || input.trim().length === 0;
}

/**
 * isSafe — true iff NO injection/traversal/command-injection rule matches the
 * normalized input. Empty/blank input → true (safe). Pure; does not mutate the
 * shared rule regexes (SAFETY_RULES carry no `g` flag, so `.test` is stateless).
 */
export function isSafe(input: string): boolean {
  if (isBlank(input)) return true;
  const norm = normalize(input);
  for (const rule of SAFETY_RULES) {
    if (rule.pattern.test(norm)) return false;
  }
  return true;
}

/**
 * hasPii — scan the RAW input for PII/credential leaks. Collects ALL spans across
 * all PII rules (POC uses finditer). Empty/blank input → { found:false, spans:[] }.
 *
 * The PII rules carry the `g` flag (to iterate all matches), which makes them
 * stateful via lastIndex. To stay pure and re-entrant we clone a fresh RegExp per
 * call rather than relying on resetting lastIndex on the shared instance — this
 * guards the stateful-lastIndex footgun under concurrent/repeated calls.
 */
export function hasPii(input: string): PiiResult {
  if (isBlank(input)) return { found: false, spans: [] };
  const spans: Array<{
    ruleId: string;
    sub: string;
    start: number;
    end: number;
    matched: string;
  }> = [];
  for (const rule of PII_RULES) {
    // Fresh regex per call — never mutate the shared rule.pattern lastIndex.
    const re = new RegExp(rule.pattern.source, rule.pattern.flags);
    let m: RegExpExecArray | null;
    while ((m = re.exec(input)) !== null) {
      spans.push({
        ruleId: rule.id,
        sub: rule.sub,
        start: m.index,
        end: m.index + m[0].length,
        matched: m[0].slice(0, 60),
      });
      // Guard against zero-width matches creating an infinite loop.
      if (m[0].length === 0) re.lastIndex += 1;
    }
  }
  return { found: spans.length > 0, spans };
}

/**
 * scan — full scan combining the 3 safety classes (over the normalized string, with
 * first-match-per-rule de-dup, mirroring the POC's `seen` set) and the PII block
 * (over raw text). `safe` is true iff there are zero safety matches.
 */
export function scan(input: string): ScanResult {
  if (isBlank(input)) {
    return {
      safe: true,
      matches: [],
      pii: { found: false, spans: [] },
      rulesetVersion: RULESET_VERSION,
    };
  }
  const norm = normalize(input);
  const matches: ScanMatch[] = [];
  const seen = new Set<string>();
  for (const rule of SAFETY_RULES) {
    if (seen.has(rule.id)) continue;
    // SAFETY_RULES carry no `g` flag → exec returns the first match, stateless.
    const m = rule.pattern.exec(norm);
    if (m) {
      seen.add(rule.id);
      matches.push({
        ruleId: rule.id,
        cls: rule.cls,
        sub: rule.sub,
        severity: rule.severity,
        start: m.index,
        end: m.index + m[0].length,
      });
    }
  }
  return {
    safe: matches.length === 0,
    matches,
    pii: hasPii(input),
    rulesetVersion: RULESET_VERSION,
  };
}

/**
 * snake_case aliases — roadmap S1.2 AC names the surface `is_safe` / `has_pii` /
 * `scan`. JS convention is camelCase; we export both so the AC's literal names
 * resolve and E2 (the MCP wiring milestone) can call either form.
 */
export const is_safe = isSafe;
export const has_pii = hasPii;

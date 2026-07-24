/**
 * aidefence-rules.ts — ruflo AIDefence detection patterns transcribed as DATA.
 *
 * Reimplemented (NOT forked) from ruflo (MIT): github.com/ruvnet/ruflo
 *   v3/@claude-flow/aidefence/src/domain/services/threat-detection-service.ts
 * License: MIT (ruflo). Patterns-as-data ONLY; no ruflo executable logic copied.
 *
 * This is the TIGHTENED variant — the one validated by spike
 * `ruflo-aidefence-guard-spike-1` (verdict GO). Six DC-3 scope adjustments are
 * baked in (see RULE_ADJUSTMENTS_NOTE below). Measured on that spike:
 *   injection FN 1.11% (1/90 well-formed), PII FN 0% (0/30), FP 0% on 984 real
 *   captured tool-call entries.
 * Preserving these patterns character-for-character is what preserves those FP/FN
 * numbers — do NOT "improve" the regexes.
 *
 * ONE DELIBERATE DEVIATION (2026-07-23, genericization): PII-01's internal-domain
 * allowlist originally carried the originating organisation's mail domain. It was
 * dropped rather than renamed, since a placeholder domain would allowlist nothing
 * real. The measured FP figure above was taken WITH that domain present, so it no
 * longer transfers exactly: deployments should re-add their own internal domains
 * here (and to the regex below) and re-measure before trusting the number.
 *
 * The baseline (faithful, un-tightened) variant is intentionally NOT shipped here.
 * m1 ships exactly one ruleset = the tightened set below.
 */

export const RULESET_VERSION = "1.0.0-tightened" as const; // S1.1 AC: ruleset reports its version

export type ThreatClass =
  | "prompt_injection"
  | "path_traversal"
  | "command_injection"
  | "pii";

export type Severity = "low" | "medium" | "high" | "critical";

export interface Rule {
  readonly id: string; // e.g. "PI-01", "PT-01T", "PII-01"
  readonly cls: ThreatClass;
  readonly sub: string; // sub-category ("email", "jailbreak", …); === cls when n/a
  readonly severity: Severity;
  readonly pattern: RegExp; // compiled regex (data; the only "logic" is .test/.exec in the scanner)
}

/**
 * Gate-worthiness scope — committed design parameter from the spike (DC-2).
 * These values are NOT gate-worthy PII: they pervade legitimate platform traffic
 * and MUST NOT trigger has_pii. This block is the REASON PII-01 is narrowed and
 * PII-09 was dropped (it is documentation + an assertion target for the benign
 * fixtures, NOT a runtime allow-list — the rules themselves already encode it).
 *   - Platform AWS account IDs: 783534138444, 000000000000
 *   - Internal email domains:   @example.com, @company.io, @cluster.local
 *   - RFC1918 private IPs:       10.x, 192.168.x, 172.16–31.x (encoded by the
 *                                ABSENCE of a private-IP rule — PII-09 dropped)
 * This scope is NOT revisable without re-measuring against the real corpus.
 */
export const NON_GATEWORTHY = {
  accountIds: ["783534138444", "000000000000"] as const,
  emailDomains: ["example.com", "company.io", "cluster.local"] as const,
  // RFC1918 ranges are encoded by the ABSENCE of a private-IP rule (PII-09 dropped).
} as const;

/**
 * RULE_ADJUSTMENTS_NOTE — the six pre-registered DC-3 tightenings baked into this
 * ruleset (vs the faithful ruflo baseline). Each closed a real-traffic false-positive:
 *   adj 1 (PII-01): exclude @example.com / @company.io / @cluster.local
 *                   internal-domain service emails — firing on ci-bot@example.com is an FP.
 *   adj 2 (PII-09): DROPPED entirely — RFC1918 private IPs are not gate-worthy and
 *                   pervade legitimate kubectl/mesh/config traffic.
 *   adj 3a (PI-13): DROPPED bare `system:` (fires on k8s RBAC system:masters/system:node)
 *                   and REPLACED by PI-13T which requires injection-shaped context.
 *   adj 3b (PI-18): DROPPED entirely — bare `base64`/`rot13`/`hex encode`/`url encode`
 *                   keyword fires on legitimate tooling docs and helm values.
 *   adj 4a (PT-01): DROPPED bare `../` (fires on Helm/kustomize ../base, ../../overlays/dev).
 *   adj 4b (PT-06): DROPPED bare multi-hop `../../` (same Helm/kustomize FP class), and
 *                   REPLACED by PT-01T which requires a sensitive-target component.
 *
 * UNIMPLEMENTED_CATEGORIES (48/51 ruflo categories reimplemented; these 3 are not
 * expressible as a static regex table and are carried as design notes only — NOT code):
 *   1. behavioral_analysis / lyapunov_attractor — Lyapunov-exponent + attractor-type
 *      classification over action sequences; not a static regex. Fallback: a dedicated
 *      BehavioralAnalysisService (time-series anomaly scoring) if M1 graduates.
 *   2. hnsw_vector_similarity_search — needs an embedding model + vector store; not a
 *      pattern table. Out of scope for a rules-as-data gate.
 *   3. policy_verification / formal_proof — temporal-logic prover (proofStatus
 *      valid/invalid/timeout); cannot be a regex. Out of scope for the SP1 gate.
 */

/* ------------------------------------------------------------------------- *
 * 1a. PROMPT_INJECTION — tightened = baseline minus PI-13/PI-18, plus PI-13T  *
 * ------------------------------------------------------------------------- */
export const INJECTION_RULES: readonly Rule[] = [
  { id: "PI-01", cls: "prompt_injection", sub: "instruction_override", severity: "critical",
    pattern: /ignore\s+(all\s+)?(previous\s+)?instructions/i },
  { id: "PI-02", cls: "prompt_injection", sub: "instruction_override", severity: "critical",
    pattern: /forget\s+(everything|all|previous)/i },
  { id: "PI-03", cls: "prompt_injection", sub: "instruction_override", severity: "critical",
    pattern: /disregard\s+(all\s+)?(previous|prior|above)/i },
  { id: "PI-04", cls: "prompt_injection", sub: "instruction_override", severity: "high",
    pattern: /do\s+not\s+follow\s+(the\s+)?(previous|above|prior)/i },
  { id: "PI-05", cls: "prompt_injection", sub: "role_switching", severity: "high",
    pattern: /you\s+are\s+now\s+(?!going|about|ready)/i },
  { id: "PI-06", cls: "prompt_injection", sub: "role_switching", severity: "high",
    pattern: /act\s+as\s+(if\s+you\s+are\s+)?a?\s*(different|new|another)/i },
  { id: "PI-07", cls: "prompt_injection", sub: "role_switching", severity: "medium",
    pattern: /pretend\s+(to\s+be|you\s+are)/i },
  { id: "PI-08", cls: "prompt_injection", sub: "jailbreak", severity: "critical",
    pattern: /\bDAN\b.*\bmode\b|\bmode\b.*\bDAN\b/i },
  { id: "PI-09", cls: "prompt_injection", sub: "jailbreak", severity: "critical",
    pattern: /jailbreak/i },
  { id: "PI-10", cls: "prompt_injection", sub: "jailbreak", severity: "critical",
    pattern: /bypass\s+(your\s+)?(restrictions|limitations|rules|filters)/i },
  { id: "PI-11", cls: "prompt_injection", sub: "jailbreak", severity: "high",
    pattern: /without\s+(any\s+)?(restrictions|limitations|rules)/i },
  { id: "PI-12", cls: "prompt_injection", sub: "jailbreak", severity: "critical",
    pattern: /disable\s+(your\s+)?(safety\s+|content\s+)?filter/i },
  // PI-13 (bare `system:`) DROPPED — DC-3 adj 3a — replaced by PI-13T below.
  { id: "PI-14", cls: "prompt_injection", sub: "context_manipulation", severity: "high",
    pattern: /\[system\]|\{system\}|system\s+prompt/i },
  { id: "PI-15", cls: "prompt_injection", sub: "context_manipulation", severity: "high",
    pattern: /reveal\s+(your\s+)?(system\s+)?prompt/i },
  { id: "PI-16", cls: "prompt_injection", sub: "context_manipulation", severity: "medium",
    pattern: /what\s+(is|are)\s+your\s+(initial\s+)?instructions/i },
  // PI-17 has NO `i` flag in the POC — kept verbatim.
  { id: "PI-17", cls: "prompt_injection", sub: "context_manipulation", severity: "medium",
    pattern: /\[\[.*?\]\]|<<.*?>>|\{\{.*?\}\}/ },
  // PI-18 (bare `base64`/`rot13`/`hex encode`/`url encode`) DROPPED — DC-3 adj 3b.
  { id: "PI-19", cls: "prompt_injection", sub: "encoding_attack", severity: "medium",
    pattern: /decode\s+this|encrypted\s+message/i },
  { id: "PI-20", cls: "prompt_injection", sub: "hypothetical", severity: "low",
    pattern: /hypothetically|in\s+theory|theoretically|if\s+you\s+could/i },
  { id: "PI-21", cls: "prompt_injection", sub: "hypothetical", severity: "low",
    pattern: /for\s+(educational|research|academic)\s+purposes/i },
  { id: "PI-22", cls: "prompt_injection", sub: "jailbreak", severity: "high",
    pattern: /developer\s+mode|dev\s+mode|debug\s+mode/i },
  { id: "PI-23", cls: "prompt_injection", sub: "jailbreak", severity: "high",
    pattern: /enable\s+(hidden|secret|special)\s+(features|mode|commands)/i },
  { id: "PI-24", cls: "prompt_injection", sub: "context_manipulation", severity: "high",
    pattern: /```system|```instruction|```prompt/i },
  { id: "PI-25", cls: "prompt_injection", sub: "context_manipulation", severity: "medium",
    pattern: /---\s*(system|instruction|prompt)/i },
  // PI-13T (ADDED, DC-3 adj 3a) — replaces PI-13; requires injection-shaped context.
  // The scanner normalizes (collapses whitespace to single spaces) before matching,
  // so the `(?:^|\n)\s*system\s*:` alternative effectively matches `^\s*system:` or a
  // ` system:` boundary on the collapsed single-line string. Alternation kept intact.
  { id: "PI-13T", cls: "prompt_injection", sub: "context_manipulation", severity: "critical",
    pattern: /<\|system\|>|<system>|(?:^|\n)\s*system\s*:\s*(?:you\s+are|ignore|forget|disregard)/i },
];

/* ------------------------------------------------------------------------- *
 * 1b. PATH_TRAVERSAL — tightened = baseline minus PT-01/PT-06, plus PT-01T    *
 * ------------------------------------------------------------------------- */
export const PATH_TRAVERSAL_RULES: readonly Rule[] = [
  // PT-01 (bare `../`) DROPPED — DC-3 adj 4a — replaced by PT-01T below.
  { id: "PT-02", cls: "path_traversal", sub: "path_traversal", severity: "critical",
    pattern: /\.\.%2f|\.\.%5c/i },
  { id: "PT-03", cls: "path_traversal", sub: "path_traversal", severity: "high",
    pattern: /\/etc\/(passwd|shadow|hosts|sudoers)/i },
  // PT-04 has NO `i` flag in the POC — char-class casing is explicit; kept verbatim.
  { id: "PT-04", cls: "path_traversal", sub: "path_traversal", severity: "high",
    pattern: /[Cc]:\\[Ww]indows\\[Ss]ystem32/ },
  { id: "PT-05", cls: "path_traversal", sub: "path_traversal", severity: "medium",
    pattern: /\/proc\/self\/environ|\/proc\/self\/cmdline/i },
  // PT-06 (bare multi-hop `../../`) DROPPED — DC-3 adj 4b — replaced by PT-01T.
  // PT-01T (ADDED, DC-3 adj 4) — must be byte-faithful: its two alternatives are the
  // load-bearing FP/FN tradeoff. Authored via String.raw so backslashes survive copy.
  //   alt 1: 2+ `../` hops reaching a sensitive target (etc|proc|windows|shadow|passwd|sudoers|system32)
  //   alt 2: a single `../` directly into etc|proc|windows
  // KNOWN-FN (closable, M1-P2): misses JSON-escaped double-backslash Windows paths like
  //   {"path":"..\\..\\windows\\system32\\..."} — the `\\` does not match [/\\]. Close via
  //   JSON-unescape preprocessing before scan (deferred). See brief §5.
  { id: "PT-01T", cls: "path_traversal", sub: "path_traversal", severity: "critical",
    pattern: new RegExp(
      String.raw`(?:\.\.[/\\]){2,}(?:[^/\\]{0,40}[/\\])*(?:etc|proc|windows|shadow|passwd|sudoers|system32)` +
        String.raw`|(?:\.\.[/\\])(?:etc|proc|windows)[/\\]`,
      "i",
    ) },
];

/* ------------------------------------------------------------------------- *
 * 1c. COMMAND_INJECTION — UNCHANGED (baseline == tightened); CI-01..CI-08     *
 * ------------------------------------------------------------------------- */
export const COMMAND_INJECTION_RULES: readonly Rule[] = [
  // CI-01 is the source of the 3 known STRUCTURAL FPs — legit `$(git rev-parse …)`,
  // `$(kubectl …)`, `$(cat VERSION)` in Helm values fire here. Do NOT tighten CI-01:
  // a regex cannot distinguish a legit `$(cmd)` value from an attack `$(cmd)` without
  // shell-AST context, and tightening would create FNs on legit embedded attack
  // substitutions. Real-traffic FP was 0% (these only appear in synthetic stress).
  // The {1,200} bound is intentional (ReDoS-bounded) — keep it. See brief §5.
  { id: "CI-01", cls: "command_injection", sub: "command_injection", severity: "critical",
    pattern: /\$\([^)]{1,200}\)/ },
  { id: "CI-02", cls: "command_injection", sub: "command_injection", severity: "critical",
    pattern: /`[^`]{1,200}`/ },
  { id: "CI-03", cls: "command_injection", sub: "command_injection", severity: "critical",
    pattern: /;\s*(rm|curl|wget|chmod|chown|bash|sh|python[23]?|perl)\b/i },
  { id: "CI-04", cls: "command_injection", sub: "command_injection", severity: "high",
    pattern: /\|\s*(curl|wget|sh|bash|nc|ncat|python)\b/i },
  { id: "CI-05", cls: "command_injection", sub: "command_injection", severity: "high",
    pattern: /&&\s*(curl|wget|sh|bash|rm|cat|echo|python)\b/i },
  { id: "CI-06", cls: "command_injection", sub: "command_injection", severity: "critical",
    pattern: /;\s*rm\s+-[rf]/i },
  { id: "CI-07", cls: "command_injection", sub: "command_injection", severity: "medium",
    pattern: /\|\s*sh\b|\|\s*bash\b/i },
  { id: "CI-08", cls: "command_injection", sub: "command_injection", severity: "medium",
    pattern: /\$\{IFS\}|\$\(printf/i },
];

/* ------------------------------------------------------------------------- *
 * 1d. PII — tightened: PII-01 internal-domain-narrowed, PII-09 dropped        *
 *                                                                             *
 * These patterns carry the `g` flag so the scanner's has_pii can collect ALL  *
 * spans (POC uses finditer). The scanner MUST reset lastIndex=0 before each    *
 * input (or clone per-call) — guard the stateful-lastIndex footgun.           *
 * ------------------------------------------------------------------------- */
export const PII_RULES: readonly Rule[] = [
  // PII-01 (narrowed, DC-3 adj 1) — internal-domain negative lookahead. NO `i` flag
  // in the POC. Excludes @example.com / @company.io / @cluster.local.
  { id: "PII-01", cls: "pii", sub: "email", severity: "medium",
    pattern: /\b[A-Za-z0-9._%+\-]+@(?![A-Za-z0-9.\-]*example\.com\b|company\.io\b|cluster\.local\b)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b/g },
  { id: "PII-02", cls: "pii", sub: "ssn", severity: "high",
    pattern: /\b\d{3}-\d{2}-\d{4}\b/g },
  { id: "PII-03", cls: "pii", sub: "credit_card", severity: "high",
    pattern: /\b\d{4}[\-\s]?\d{4}[\-\s]?\d{4}[\-\s]?\d{4}\b/g },
  { id: "PII-04", cls: "pii", sub: "api_key", severity: "critical",
    pattern: /\b(sk-[a-zA-Z0-9]{48}|sk-ant-[a-zA-Z0-9\-]{20,})\b/g },
  { id: "PII-05", cls: "pii", sub: "github_token", severity: "critical",
    pattern: /\b(ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82})\b/g },
  { id: "PII-06", cls: "pii", sub: "password", severity: "high",
    pattern: /password\s*[:=]\s*["']?[^"'\s]{4,}["']?/gi },
  // PII-07 has NO `i` flag in the POC — uppercase AKIA is intentional.
  { id: "PII-07", cls: "pii", sub: "aws_access_key", severity: "critical",
    pattern: /\bAKIA[0-9A-Z]{16}\b/g },
  { id: "PII-08", cls: "pii", sub: "bearer_token", severity: "high",
    pattern: /\bBearer\s+[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\b/gi },
  // PII-09 (RFC1918 private IPs) DROPPED — DC-3 adj 2 — not gate-worthy per DC-2.
];

/** The three "is_safe" classes, in scan order. PII is queried separately by has_pii. */
export const SAFETY_RULES: readonly Rule[] = [
  ...INJECTION_RULES,
  ...PATH_TRAVERSAL_RULES,
  ...COMMAND_INJECTION_RULES,
];

export const ALL_RULES: readonly Rule[] = [...SAFETY_RULES, ...PII_RULES];

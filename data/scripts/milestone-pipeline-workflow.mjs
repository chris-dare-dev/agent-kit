// milestone-pipeline-workflow.mjs — Workflow-tool port of Step 3's review wave (2026-07-16)
// ---------------------------------------------------------------------------
// Workflow-tool port of /milestone-pipeline Step 3 (blind adversarial assessment)
// ONLY. It replaces the markdown orchestrator's hand-driven reviewer fan-out —
// the main session composing N dispatches, then hand-writing N receipts — with a
// deterministic JS control flow fed by a script that already persisted the bytes.
//
// WHY THIS EXISTS: a real run (dispatcher-receipt-authz-m3a, 2026-07-16) reached
// `rectify-running` and could NOT reach `code-complete`, because ad-hoc inline
// Workflow dispatch produced the WORK but none of the ASSESSMENT ARTIFACTS the
// state machine demands — no agent-body snapshots, no persisted prompts, no task
// ids, no review-manifest.json. This port makes those artifacts a precondition of
// dispatch rather than an afterthought.
//
// Token-economics mapping vs the markdown command:
//   Step 3 fan-out   2-4 blind reviewers -> inherit frontmatter. No override.
//                    (milestone-adversary + milestone-delivery-integrity-adversary
//                     are deep-reasoning-max; the conditional critics carry their
//                     own stamped class.) The wave is the expensive part and stays
//                     exactly as expensive — this port buys PROVENANCE, not tokens.
//   control flow / wave sequencing (was the Opus main session) -> plain JS = 0 tokens.
//   prompt construction (was main-session string assembly, the X-H4 hazard)
//                    -> milestone-pipeline-review-prepare.py = 0 tokens AND one source.
// The saving is real but secondary; the POINT is that the prompt is no longer
// AUTHORED by a model that could paraphrase it.
//
// WHAT IS AND IS NOT CLOSED (read this before trusting the provenance)
// -------------------------------------------------------------------
// Closed by construction: prepare.py's `write_bytes(prompt)` and the `prompt` it
// emits on stdout are the SAME Python `str`. The persisted file and the emitted
// value cannot disagree — there is no second composition step between them.
//
// NOT closed by construction: the emitted JSON reaches `Workflow({ args })` only
// because the orchestrating MODEL re-emits a 20-43KB blob inside a tool call.
// There is no file-based args mechanism; args are model-composed. A compaction
// truncation of the body tail would slip past the envelope tripwires below (they
// only inspect the head), and the manifest hashes the pristine FILE, so nothing
// downstream can see what was actually dispatched.
//
// That hop is covered — NOT by the harness, but by the length+checksum tripwire
// in `_verifyDispatchBytes` below, which recomputes over the args value we are
// about to send and THROWS on divergence. It is a truncation/mangling detector.
// It is NOT a defence against a harness that mutates the string between the
// tripwire and the model call, and it is NOT proof the reviewer received those
// bytes. Say exactly that; do not restore "cannot diverge" phrasing anywhere.
//
// Integration — the main session runs THREE calls around this one (Step 0 stays
// inline because a workflow has no filesystem and cannot prompt the user):
//   1. python3 data/scripts/milestone-pipeline-review-prepare.py --id ID --repo-root R --stage assessment
//   2. Workflow({ scriptPath: "data/scripts/milestone-pipeline-workflow.mjs",
//                 args: { ...<that JSON>, id, repoRoot } })
//   3. python3 data/scripts/milestone-pipeline-review-manifest.py --id ID --repo-root R \
//        --prepare-result <prepare.json> --workflow-result <workflow.json>
//   4. checkpoint --set critics_run/critique_files/... then `critique-complete`
// Rectify (Step 4) and the gated push (Step 5) STAY in the main session.
//
// HARD CONSTRAINT (Workflow tool contract): no filesystem, no Node API, and
// Date.now() / new Date() / Math.random() THROW. So this file cannot snapshot a
// body, hash anything, stamp a time, or write the manifest. Every one of those is
// a deterministic claim and lives in the two Python scripts — per the platform's
// own rule: "Agents may author artifact contents, but only
// milestone-pipeline-artifacts.py can validate a delivery claim."
// ---------------------------------------------------------------------------

export const meta = {
  name: 'milestone-pipeline-review',
  description: "Blind adversarial assessment wave for /milestone-pipeline Step 3: dispatches every deterministically selected reviewer in parallel, blind to siblings, with prompt bytes re-measured against milestone-pipeline-review-prepare.py's length+checksum immediately before dispatch (a mangled args blob throws rather than dispatching void provenance). Produces the per-reviewer metadata that milestone-pipeline-review-manifest.py turns into review-manifest.json. Does not rectify, checkpoint, or push.",
  phases: [
    { title: 'Assessment', detail: 'parallel blind reviewers dispatched with their persisted prompt bytes' },
  ],
}

// ---- inputs ----------------------------------------------------------------
// Be robust to how the Workflow runtime delivers `args`: object, JSON string, or absent.
const _args = (typeof args === 'string') ? (args.trim() ? JSON.parse(args) : {}) : (args || {})
const { id, repoRoot, reviewers, required_reviewers: requiredReviewers } = _args

if (!id) throw new Error('milestone-pipeline-workflow requires args.id (the milestone id)')
if (!repoRoot) throw new Error('milestone-pipeline-workflow requires args.repoRoot (absolute target repo)')
if (!Array.isArray(reviewers) || reviewers.length === 0) {
  throw new Error(
    'milestone-pipeline-workflow requires args.reviewers[] from milestone-pipeline-review-prepare.py — ' +
    'do NOT hand-author this array; its prompts must be the persisted bytes'
  )
}

// Fail closed on a wave that cannot produce a valid manifest anyway. Dispatching
// an incomplete wave burns max-effort reviewer budget for a manifest the
// validator will refuse (`reviews: assessment roles must exactly equal
// required_reviewers`), so refuse BEFORE spending it.
//
// An ABSENT or malformed `required_reviewers` is a THROW, never a skip. This
// check used to run only `if (Array.isArray(requiredReviewers))`, so dropping the
// key — exactly what a truncated or hand-rebuilt args blob does — silently
// disabled the completeness gate on a mandatory-reviewer wave. A completeness
// check that quietly skips is this pipeline's own recurring defect; the whole
// point of the key is that it is not optional.
if (!Array.isArray(requiredReviewers) || requiredReviewers.length === 0) {
  throw new Error(
    'milestone-pipeline-workflow requires a non-empty args.required_reviewers[] — ' +
    `got ${JSON.stringify(requiredReviewers)}. Pass milestone-pipeline-review-prepare.py output ` +
    'through UNMODIFIED; the key is never optional, and without it the wave-completeness gate ' +
    'would silently pass a wave that omits a mandatory blind critic.'
  )
}
for (const role of requiredReviewers) {
  if (typeof role !== 'string' || role.length === 0) {
    throw new Error(
      `milestone-pipeline-workflow: args.required_reviewers[] holds a non-string entry ${JSON.stringify(role)} — ` +
      'the selection cannot be compared; refusing the wave.'
    )
  }
}
{
  const dispatched = new Set(reviewers.map(r => r.role))
  const missing = requiredReviewers.filter(r => !dispatched.has(r))
  if (missing.length > 0) {
    throw new Error(
      `milestone-pipeline-workflow: wave omits deterministically required reviewer(s) ${JSON.stringify(missing)}. ` +
      'The validator recomputes this selection from the live diff; a manifest cannot omit a selected reviewer. ' +
      'Re-run prepare without --roles, or run the remaining wave and merge both prepare results.'
    )
  }
}

// --- BEGIN PORTABLE CHECKSUM (mirrored by milestone-pipeline-review-prepare.py) ---
// The agreement with Python is load-bearing, so this block is deliberately dull:
//
// * It hashes UTF-16 CODE UNITS (`charCodeAt`), which is what JS's `.length` and
//   `charCodeAt` natively expose. Python mirrors it by encoding `utf-16-le` and
//   pairing the bytes — NOT by `ord()` over codepoints, which diverges from JS on
//   every astral char (an emoji is 1 ord but 2 code units).
// * It uses ONLY `+`, `*`, `&`, `^`, `>>>` and `Number.prototype.toString(16)`.
//   No TextEncoder, no Buffer, no crypto, no Node API — none of which can be
//   assumed present in the Workflow sandbox. It also avoids `Math.imul` (ES2015)
//   and `String.prototype.padStart` (ES2017): both are almost certainly present,
//   but "almost certainly" is the kind of assumption this file exists to kill.
//
// * MEASURED IN THE REAL SANDBOX 2026-07-17 (this block executed verbatim inside a
//   live Workflow run, not node, not a dialect reproduction):
//       charCodeAt ✓   (255).toString(16)==='ff' ✓   bitwise/>>>0 ✓
//       Math.imul  ✓   padStart ✓        <- the avoidance was NOT needed
//       TextEncoder ✗  Buffer  ✗         <- the avoidance WAS needed
//   9/9 agreement with prepare.py's mirror over the ASCII/CRLF/BMP/astral/ZWJ/
//   U+10000/U+10FFFF corpus, and tail-truncation past the body delimiter detected.
//   So: the two ES6+ helpers turn out to be available (the caution cost nothing and
//   is kept — it widens the floor for free), but TextEncoder/Buffer are genuinely
//   ABSENT. The obvious UTF-8 implementation would have thrown on EVERY healthy
//   wave and pressured the next engineer to delete the tripwire as broken. That
//   half of the caution was load-bearing, and only running it here could tell the
//   two halves apart. Do NOT "simplify" this to TextEncoder.
// * `_mul32` does the 32-bit multiply by splitting the accumulator into 16-bit
//   halves so every intermediate product stays well under 2^53 and is therefore
//   exact in float64; `>>> 0` then reduces mod 2^32.
//
// prepare.py's --self-test EXTRACTS THIS EXACT TEXT from this file and runs it in
// node against the Python implementation over an ASCII/CRLF/BMP/astral corpus, so
// this block and its Python mirror cannot drift apart unnoticed.
function _mul32(a, b) {
  const aLo = a & 0xffff
  const aHi = a >>> 16
  // (aHi*2^16 + aLo) * b  mod 2^32  ==  ((aHi*b mod 2^16) * 2^16 + aLo*b) mod 2^32
  const hiPart = (((aHi * b) & 0xffff) * 65536) >>> 0
  const loPart = (aLo * b) >>> 0
  return (hiPart + loPart) >>> 0
}

function _promptChecksum(s) {
  // FNV-1a, 32-bit, over UTF-16 code units.
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h = (h ^ s.charCodeAt(i)) >>> 0
    h = _mul32(h, 16777619)
  }
  let hex = h.toString(16)
  while (hex.length < 8) hex = '0' + hex
  return hex
}
// --- END PORTABLE CHECKSUM ---

// THE MODEL-COMPOSED ARGS HOP — the one leg that is not closed by construction.
//
// `reviewers[].prompt` did not arrive here from prepare.py's stdout directly; the
// orchestrating model re-emitted it inside a tool call. Verify that what we hold
// is what prepare.py measured, and refuse loudly if it is not.
//
// Placed in the PRE-FLIGHT loop rather than inside the `parallel` thunk on
// purpose: a throw here propagates out of the workflow uncaught and costs zero
// reviewer budget, whereas a throw inside a thunk could be coerced to a `null`
// result by `parallel` and resurface as the far less specific dead-agent error.
// Nothing mutates `r.prompt` between this check and the `agent(r.prompt, ...)`
// call, so this IS the byte-state that gets dispatched.
function _verifyDispatchBytes(r) {
  if (typeof r.prompt_len_utf16 !== 'number' || typeof r.prompt_checksum !== 'string'
      || r.prompt_checksum.length === 0) {
    throw new Error(
      `milestone-pipeline-workflow: reviewers[${r.role}] carries no prompt_len_utf16/prompt_checksum ` +
      'tripwire values. They are emitted by milestone-pipeline-review-prepare.py and are NOT optional: ' +
      'without them the model-composed args hop is unverified and a truncated prompt would dispatch silently.'
    )
  }
  const actualLen = r.prompt.length
  const actualChecksum = _promptChecksum(r.prompt)
  if (actualLen !== r.prompt_len_utf16 || actualChecksum !== r.prompt_checksum) {
    throw new Error(
      `milestone-pipeline-workflow: reviewers[${r.role}].prompt DOES NOT MATCH what ` +
      'milestone-pipeline-review-prepare.py persisted and measured.\n' +
      `  expected: length=${r.prompt_len_utf16} checksum=${r.prompt_checksum}\n` +
      `  actual:   length=${actualLen} checksum=${actualChecksum}\n` +
      `  delta:    ${actualLen - r.prompt_len_utf16} UTF-16 code unit(s)\n` +
      'The args blob was truncated, re-wrapped, or otherwise mangled on the model-composed hop ' +
      `between prepare.py's stdout and this call. The persisted prompt file (${r.prompt_path}) is ` +
      'the authority and is unaffected; the manifest would hash THAT file and pass, so dispatching ' +
      'now would produce void provenance that no downstream check can see and that ' +
      '`critique-complete` would make unrepairable. Refusing the wave.\n' +
      'Fix: re-run prepare and pass its stdout through byte-for-byte — do not rebuild reviewers[].'
    )
  }
}

for (const r of reviewers) {
  for (const field of ['role', 'task_id', 'agent_type', 'prompt', 'critique_abs']) {
    if (!r || typeof r[field] !== 'string' || r[field].length === 0) {
      throw new Error(`milestone-pipeline-workflow: reviewers[] entry is missing ${field}`)
    }
  }
  // Cheap structural proof that we were handed a real dispatch prompt and not a
  // summary/reference note — the exact dna-rem-m6 failure. The Python side owns
  // the authoritative envelope contract; this is a tripwire, not a validator.
  if (!r.prompt.startsWith('MILESTONE_REVIEW_DISPATCH_V2\n')) {
    throw new Error(
      `milestone-pipeline-workflow: reviewers[${r.role}].prompt is not a MILESTONE_REVIEW_DISPATCH_V2 envelope. ` +
      'Pass milestone-pipeline-review-prepare.py output through UNMODIFIED.'
    )
  }
  if (!r.prompt.includes('\n--- CANONICAL AGENT BODY ---\n')) {
    throw new Error(
      `milestone-pipeline-workflow: reviewers[${r.role}].prompt carries no canonical body delimiter.`
    )
  }
  // Both envelope tripwires above only inspect the HEAD of the prompt, so neither
  // sees a truncated body tail — which is the likeliest mangling. This does.
  _verifyDispatchBytes(r)
}

// The reviewer reports its OWN times: this file cannot call Date.now() (it
// throws), and the reviewers all carry Bash (`date -u +%Y-%m-%dT%H:%M:%SZ`).
// The schema is the ONLY channel for that ask — the dispatch envelope forbids
// trailing instructions after the agent body, so nothing may be appended to the
// prompt to request them.
const REVIEW_RETURN = {
  type: 'object',
  additionalProperties: false,
  required: ['task_id', 'role', 'verdict', 'critique_path', 'started_at', 'completed_at'],
  properties: {
    task_id:       { type: 'string', description: 'the task leaf embedded in your CRITIQUE_PATH filename' },
    role:          { type: 'string' },
    verdict:       { type: 'string', enum: ['SHIP', 'SHIP-WITH-FIXES', 'DO-NOT-SHIP'] },
    critique_path: { type: 'string', description: 'absolute path of the critique you wrote (your CRITIQUE_PATH)' },
    started_at:    { type: 'string', description: 'RFC3339 UTC, from `date -u +%Y-%m-%dT%H:%M:%SZ` when you began' },
    completed_at:  { type: 'string', description: 'RFC3339 UTC, from `date -u +%Y-%m-%dT%H:%M:%SZ` when you finished' },
  },
}

// ---- Phase 1: ASSESSMENT (parallel + blind) --------------------------------
phase('Assessment')
log(`ASSESSMENT: dispatching ${reviewers.length} blind reviewer(s) for "${id}": ${reviewers.map(r => r.role).join(', ')}`)

const results = await parallel(reviewers.map(r => () =>
  agent(
    // ┌──────────────────────────────────────────────────────────────────────┐
    // │ X-H4 — DO NOT TOUCH THIS ARGUMENT.                                   │
    // │                                                                      │
    // │ `r.prompt` is intended to be the byte-for-byte content of the        │
    // │ persisted file `artifacts/reviews/<role>-<task>-prompt.md`, which    │
    // │ prepare.py wrote and emitted from ONE in-memory string. The manifest │
    // │ binds sha256(that FILE). `_verifyDispatchBytes` has just re-measured │
    // │ this exact value against prepare.py's length+checksum, so a mangled  │
    // │ args blob has already thrown — but that tripwire is the ONLY thing   │
    // │ standing here. It is not a harness guarantee.                        │
    // │                                                                      │
    // │ The validator hashes the FILE and CANNOT see what was actually sent. │
    // │ So if you concatenate, template, trim, truncate, prepend context, or │
    // │ append even one instruction line here — AFTER the tripwire ran — the │
    // │ validation still PASSES while the whole assessment lane's provenance │
    // │ becomes VOID, and that void is UNREPAIRABLE: `critique-complete`     │
    // │ pins immutable_root_hash, PHASE_EDGES is forward-only, and           │
    // │ review-append refuses out-of-band findings. The only recovery is     │
    // │ re-initializing the milestone.                                       │
    // │                                                                      │
    // │ This exact mistake voided dna-rem-m6 on 2026-07-15: prompts were     │
    // │ built correctly and dispatched as reference notes. The validator     │
    // │ passed them. The lane was unrecoverable.                             │
    // │                                                                      │
    // │ Anything a reviewer must be TOLD belongs in the envelope headers or  │
    // │ the canonical agent body — both owned by prepare.py. Anything the    │
    // │ orchestrator must ASK belongs in `schema`, which is not prompt bytes.│
    // │                                                                      │
    // │ Send the verified string. Nothing else.                              │
    // └──────────────────────────────────────────────────────────────────────┘
    r.prompt,
    {
      agentType: r.agent_type,
      label: r.task_id,
      phase: 'Assessment',
      schema: REVIEW_RETURN,
      model: undefined,   // no model overrides — inherit stamped frontmatter
      // No worktree: reviewers are READ-ONLY and each writes exactly one
      // disjoint critique path under artifacts/reviews/. A worktree would also
      // strand the untracked .claude/notes/ artifacts (pipeline-pattern-v2 §4).
    }
  )
))

// FAIL LOUD on a dead agent. This pipeline's own lesson: a dead critic once
// returned `falsified_count: 0`, indistinguishable from a clean pass. And
// `.filter(Boolean)` — the idiom the read-only scout workflows use — would
// SILENTLY DROP a mandatory blind reviewer here, which is strictly worse: the
// wave would look complete, the manifest would be built from a short list, and
// `required_reviewers` would then refuse it at `critique-complete` with a
// confusing message... or, if the dropped critic was the only one that would
// have found the bug, it would not refuse at all. Never coerce null to clean.
const returned = []
for (let i = 0; i < results.length; i++) {
  const r = results[i]
  const spec = reviewers[i]
  if (!r) {
    throw new Error(
      `ASSESSMENT: reviewer ${spec.role} (task ${spec.task_id}) returned null — the agent died or was never dispatched. ` +
      'A null reviewer is NOT a clean pass. The wave is invalid; do not build a manifest from it. ' +
      'Re-run prepare (fresh task ids) and re-dispatch the whole wave.'
    )
  }
  if (r.role !== spec.role) {
    throw new Error(
      `ASSESSMENT: reviewer ${spec.role} returned role ${JSON.stringify(r.role)} — role/receipt mismatch; refusing the wave.`
    )
  }
  // Identity binding. The receipt must bind to the persisted prompt. PRIMARY
  // signal: the reviewer's self-reported task_id equals the dispatched leaf.
  // FALLBACK: some reviewer bodies whose own "Return value" section predates the
  // port's StructuredOutput schema never mention task_id, and reliably return
  // their exact CRITIQUE_PATH while filling task_id with a salient prompt value
  // (e.g. the milestone ID) instead of the leaf — observed repeatably on
  // milestone-adversary, 2026-07-20. The dispatched critique path is an
  // EQUALLY-STRONG identity and in fact STRONGER: prepare.py bound
  // spec.critique_abs, the agent wrote there, and the manifest builder
  // independently re-hashes the file at that path (the authority named on the
  // critique_path/critique_expected fields below). So accept a task_id-echo
  // mismatch IFF the returned critique_path is EXACTLY the dispatched one; a
  // mismatch on BOTH is genuinely unbindable and still throws.
  const taskIdOk = r.task_id === spec.task_id
  const critiquePathOk =
    typeof r.critique_path === 'string' &&
    r.critique_path.trim() === spec.critique_abs
  if (!taskIdOk && !critiquePathOk) {
    throw new Error(
      `ASSESSMENT: reviewer ${spec.role} returned task_id ${JSON.stringify(r.task_id)} ` +
      `(dispatched ${JSON.stringify(spec.task_id)}) AND critique_path ${JSON.stringify(r.critique_path)} ` +
      `(dispatched ${JSON.stringify(spec.critique_abs)}) — neither binds the receipt to its ` +
      `persisted prompt; refusing the wave.`
    )
  }
  if (!taskIdOk) {
    log(
      `ASSESSMENT: ${spec.role} task_id echo mismatch ` +
      `(${JSON.stringify(r.task_id)} != ${JSON.stringify(spec.task_id)}); ` +
      `bound via exact critique_path match (manifest re-hashes the file on disk).`
    )
  }
  returned.push({
    role: spec.role,
    task_id: spec.task_id,
    agent_type: spec.agent_type,
    verdict: r.verdict,
    // Both are carried: `critique_path` is what the reviewer claims it wrote and
    // `critique_expected` is what prepare.py bound. The manifest builder is the
    // authority that reconciles them against the file on disk.
    critique_path: r.critique_path,
    critique_expected: spec.critique_abs,
    prompt_path: spec.prompt_path,
    prompt_sha256: spec.prompt_sha256,
    agent_body_path: spec.agent_body_path,
    agent_body_snapshot_path: spec.agent_body_snapshot_path,
    agent_body_sha256: spec.agent_body_sha256,
    started_at: r.started_at,
    completed_at: r.completed_at,
  })
  log(`ASSESSMENT: ${spec.role} -> ${r.verdict} (task ${spec.task_id})`)
}

// ---- Return to the main session (which runs the manifest builder + checkpoint)
return {
  id,
  repo_root: repoRoot,
  stage: 'assessment',
  dispatched: reviewers.length,
  returned: returned.length,
  reviews: returned,
}

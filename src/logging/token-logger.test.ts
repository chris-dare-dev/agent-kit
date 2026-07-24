/**
 * S2.2 regression — token-log args are HASHED under SERVER_PROFILE=shared and
 * keep their compact preview under the default (personal) profile.
 *
 * Constructs two TokenLoggers against temp JSONL files, logs an identical
 * sensitive-looking args payload through each, and asserts the persisted `args`
 * field. The load-bearing property is the NEGATIVE one: under shared, no
 * substring of the raw args survives into the log.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ServerProfile } from "../types.js";
import { TokenLogger } from "./token-logger.js";

const SECRET = "abc123def456";
const INPUT = JSON.stringify({ query: `confluence-api-token-${SECRET}-value` });

/** Log one call through a fresh logger of the given profile; return the parsed entry. */
async function logOnce(profile?: ServerProfile): Promise<Record<string, unknown>> {
  const dir = await mkdtemp(join(tmpdir(), "toklog-"));
  const logPath = join(dir, "log.jsonl");
  const logger = new TokenLogger(logPath, profile);
  logger.logCall("search_platform_knowledge", INPUT, "output text", 5, {});
  // The JSONL append is fire-and-forget — poll for the line to land.
  let content = "";
  for (let i = 0; i < 100; i++) {
    try {
      content = await readFile(logPath, "utf-8");
    } catch {
      /* not written yet */
    }
    if (content.trim().length > 0) break;
    await new Promise((r) => setTimeout(r, 20));
  }
  await rm(dir, { recursive: true, force: true });
  assert.ok(content.trim().length > 0, "log line should have been written");
  return JSON.parse(content.trim().split("\n").pop() as string);
}

test("shared profile hashes token-log args — no raw content persisted", async () => {
  const entry = await logOnce("shared");
  assert.match(String(entry.args), /^[0-9a-f]{8}$/, "shared args must be an 8-hex sha256 prefix");
  assert.ok(!String(entry.args).includes(SECRET), "shared args must NOT contain the raw payload");
});

test("personal profile keeps the compact args preview — no regression", async () => {
  const entry = await logOnce(); // default => personal
  assert.ok(String(entry.args).includes(SECRET), "personal args keep the raw preview (today's behavior)");
  assert.ok(String(entry.args).length <= 120, "personal args are the ≤120-char truncation");
});

// --- rectification: salted hash (L2), section omission (L1), read redaction (M1) ---

test("shared: identical args hash DIFFERENTLY across loggers (salted, L2)", async () => {
  const a = await logOnce("shared");
  const b = await logOnce("shared");
  assert.match(String(a.args), /^[0-9a-f]{8}$/);
  assert.match(String(b.args), /^[0-9a-f]{8}$/);
  assert.notEqual(a.args, b.args, "salted hash must differ across sessions (no cross-session correlation)");
});

test("shared: the section arg is omitted, not persisted raw (L1)", async () => {
  const dir = await mkdtemp(join(tmpdir(), "toklog-"));
  const logPath = join(dir, "log.jsonl");
  const logger = new TokenLogger(logPath, "shared");
  logger.logCall(
    "get_skill",
    JSON.stringify({ name: "x", section: "SECRET-SECTION" }),
    "out",
    5,
    { args: { section: "SECRET-SECTION" } },
  );
  let content = "";
  for (let i = 0; i < 100; i++) {
    try {
      content = await readFile(logPath, "utf-8");
    } catch {
      /* not written yet */
    }
    if (content.trim().length > 0) break;
    await new Promise((r) => setTimeout(r, 20));
  }
  await rm(dir, { recursive: true, force: true });
  const entry = JSON.parse(content.trim());
  assert.equal(entry.section, undefined, "section must be omitted under shared");
  assert.ok(!JSON.stringify(entry).includes("SECRET-SECTION"), "no raw section fragment may leak");
});

test("shared: get_token_stats read-side redacts pre-existing raw args (M1)", async () => {
  const dir = await mkdtemp(join(tmpdir(), "toklog-"));
  const logPath = join(dir, "log.jsonl");
  // A personal-format entry with raw args, as if the log was filled before a flip.
  const seeded = {
    ts: "2026-07-11T00:00:00.000Z",
    sessionId: "seed",
    tool: "search_platform_knowledge",
    inputEst: 10,
    outputEst: 99999,
    args: '{"query":"raw-secret-xyz"}',
  };
  await writeFile(logPath, JSON.stringify(seeded) + "\n");
  const logger = new TokenLogger(logPath, "shared");
  const stats = await logger.getFullStats();
  await rm(dir, { recursive: true, force: true });
  const top = stats.topLargestCalls[0];
  assert.ok(top, "the seeded entry should surface as a top call");
  assert.ok(
    !String(top.args ?? "").includes("raw-secret-xyz"),
    "raw args from a pre-existing entry must be redacted on read under shared",
  );
});

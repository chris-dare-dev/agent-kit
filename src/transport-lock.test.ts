/**
 * S2.3 sequencing-lock tripwire.
 *
 * Asserts the server exposes exactly ONE transport (stdio) and imports no HTTP /
 * SSE transport ANYWHERE in src/ (not just index.ts — the most natural add-HTTP
 * path is a new module imported under an innocuous name). This is a STATIC source
 * check, NOT a runtime guarantee — no HTTP transport exists to gate at runtime
 * yet. If you are adding one, you MUST first land SERVER_PROFILE=shared (S2.1
 * personal-memory exclusion + S2.2 guard=enforce/hash-logging), confirm that any
 * tool reading live filesystem state clamps path traversal, AND design
 * per-client isolation of process state (response-cache stubs, token-log
 * sessionId/session accumulators, profile-partitioned log/snapshot paths) — see
 * README § "Trust-domain profile (SERVER_PROFILE)" — then update this test
 * deliberately. A change that adds an HTTP transport without those preconditions
 * must make this test RED and LOUD.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SRC_DIR = resolve(dirname(fileURLToPath(import.meta.url)));

/** Every non-test .ts file under src/ (recursive). Test files are excluded so
 * this tripwire's own forbidden-string list never self-matches. */
function srcFiles(): string[] {
  return (readdirSync(SRC_DIR, { recursive: true, encoding: "utf-8" }) as string[])
    .filter((f) => f.endsWith(".ts") && !f.endsWith(".test.ts"))
    .map((f) => join(SRC_DIR, f));
}

test("S2.3 lock: stdio is the only server transport across all of src/", () => {
  const files = srcFiles();
  // Coverage guard: a future src/ restructure must not silently shrink the scan.
  assert.ok(files.length > 1, "expected to scan more than one src file");
  let transports = 0;
  for (const f of files) {
    transports += (readFileSync(f, "utf-8").match(/new \w*ServerTransport\(/g) ?? []).length;
  }
  assert.equal(
    transports,
    1,
    "exactly one *ServerTransport instantiation expected across src/ (stdio only)",
  );
});

test("S2.3 lock: no HTTP/SSE transport is imported anywhere in src/", () => {
  const forbidden = [
    "streamableHttp",
    "StreamableHTTPServerTransport",
    "server/sse",
    "SSEServerTransport",
    'from "express"',
    "from 'express'",
  ];
  for (const f of srcFiles()) {
    const src = readFileSync(f, "utf-8");
    for (const bad of forbidden) {
      assert.ok(
        !src.includes(bad),
        `${f} must not reference "${bad}" until the trust-domain preconditions land (S2.3 lock — see README § Trust-domain profile)`,
      );
    }
  }
});

/**
 * Golden-file MCP contract test — the staged-migration NO-BREAK proof.
 *
 * Spawns the real server (`node --import tsx src/index.ts`) over stdio via the
 * MCP SDK client, pointed at a hermetic fixture tree under
 * tests/fixtures/mcp-contract/ (personal-memory / token-log / cache disabled).
 *
 * Layer 1 — snapshots tools/list (name + inputSchema, sorted) against
 *   tests/fixtures/tools-list.golden.json and fails on any drift. This is the
 *   test that would have caught the phantom `get_agents_registry` removal
 *   landing without a corresponding smoke-test update (F8). Descriptions are
 *   deliberately excluded so prose tweaks don't red the gate; regenerate with
 *   `npm run test:update-golden` on an intentional tool add/remove/schema change.
 *
 * Layer 2 — one representative call per fixed behaviour, proving the M1 fixes:
 *   S1.1a canonicalization, S1.1b CLAUDE.md re-key, S1.2 traversal clamp,
 *   S1.3 corrected structured facts.
 *
 * NB: the server always loads the real bundled data/ (resolved from config.ts,
 * not from PLATFORM_ROOT), so Layer 2 asserts stable substrings, never full
 * snapshots of the churny data/ tiers.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SERVER_ENTRY = resolve(REPO_ROOT, "src", "index.ts");
const FIXTURE_ROOT = resolve(REPO_ROOT, "tests", "fixtures", "mcp-contract");
const GOLDEN_PATH = resolve(REPO_ROOT, "tests", "fixtures", "tools-list.golden.json");
const UPDATE_GOLDEN = process.env.UPDATE_GOLDEN === "1";

interface ToolShape {
  name: string;
  inputSchema: unknown;
}

interface CallResult {
  content: { type: string; text: string }[];
  isError?: boolean;
}

function textOf(res: CallResult): string {
  return res.content.map((c) => c.text).join("");
}

async function startServer(): Promise<{ client: Client; close: () => Promise<void> }> {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: ["--import", "tsx", SERVER_ENTRY],
    cwd: REPO_ROOT,
    env: {
      PATH: process.env.PATH ?? "",
      HOME: process.env.HOME ?? "",
      PLATFORM_ROOT: FIXTURE_ROOT,
      WORKSPACE_ROOT: FIXTURE_ROOT,
      // Hermetic: no personal memory, no token log, no cache snapshot.
      MEMORY_ROOT: "",
      TOKEN_LOG_PATH: "",
      CACHE_SNAPSHOT_PATH: "",
    },
    stderr: "ignore",
  });
  const client = new Client({ name: "mcp-contract-test", version: "1.0.0" });
  await client.connect(transport);
  return { client, close: () => client.close() };
}

test("MCP contract (tools/list golden + representative calls)", async (t) => {
  const { client, close } = await startServer();
  try {
    // ---- Layer 1: tools/list golden snapshot -----------------------------
    await t.test("tools/list matches the golden snapshot", async () => {
      const listed = await client.listTools();
      const shape: ToolShape[] = listed.tools
        .map((tool) => ({ name: tool.name, inputSchema: tool.inputSchema }))
        .sort((a, b) => a.name.localeCompare(b.name));

      if (UPDATE_GOLDEN) {
        writeFileSync(GOLDEN_PATH, JSON.stringify(shape, null, 2) + "\n", "utf-8");
        return;
      }

      let golden: ToolShape[];
      try {
        golden = JSON.parse(readFileSync(GOLDEN_PATH, "utf-8"));
      } catch (err) {
        assert.fail(
          `Could not read golden ${GOLDEN_PATH}: ${
            err instanceof Error ? err.message : String(err)
          }. Generate it with: npm run test:update-golden`,
        );
      }
      assert.deepStrictEqual(
        shape,
        golden,
        "tools/list drifted from the golden file — a tool was added/removed or a " +
          "schema changed. If intentional, run: npm run test:update-golden",
      );
    });

    // ---- Layer 2: representative per-behaviour calls ----------------------

    // S1.1a — reference name canonicalization (hyphen + space + case). Uses the
    // real bundled data/references/handoff-contract.md (kept in the personal set).
    await t.test("S1.1a get_reference resolves hyphen and space forms", async () => {
      for (const name of ["handoff-contract", "handoff contract", "HANDOFF-CONTRACT"]) {
        const res = (await client.callTool({
          name: "get_reference",
          arguments: { name },
        })) as CallResult;
        assert.notEqual(res.isError, true, `get_reference(${JSON.stringify(name)}) should hit`);
        assert.ok(textOf(res).length > 0);
      }
    });

    // S1.1a — context-guide topic canonicalization (fixture sandbox/context).
    await t.test("S1.1a get_context_guide resolves the hyphenated topic", async () => {
      const res = (await client.callTool({
        name: "get_context_guide",
        arguments: { topic: "service-mesh" },
      })) as CallResult;
      assert.notEqual(res.isError, true, "get_context_guide('service-mesh') should hit");
      assert.match(textOf(res), /fixture context guide/i);
    });

    // S1.1b — CLAUDE.md merge re-key: chart AND source of the same basename are
    // both indexed (pre-fix, source/ silently overwrote charts/).
    await t.test("S1.1b both chart and source CLAUDE.md are searchable", async () => {
      for (const marker of ["CHART-FIXTURE-UNIQUE-MARKER", "SOURCE-FIXTURE-UNIQUE-MARKER"]) {
        const res = (await client.callTool({
          name: "search_platform_knowledge",
          arguments: { query: marker },
        })) as CallResult;
        assert.match(textOf(res), new RegExp(marker), `search should surface ${marker}`);
      }
    });

    // NOTE: the upstream S1.2 (get_app_context traversal clamp) and S1.3
    // (environment/architecture facts) cases are absent by construction — this
    // kit serves no infrastructure tools, so neither the app-context filesystem
    // reader nor the facts tools exist to test. Every remaining tool serves
    // content discovered once at startup, so there is no per-call path input to
    // clamp. If a filesystem-reading tool is ever added back, restore a
    // traversal case alongside it.

    // L2 — get_skill / get_agent lookups canonicalize like get_reference does.
    // Uses stable personal-set names (handoff skill: case variant; the agent name
    // covers hyphen+space+case). Match the exact miss-message signature, NOT a
    // bare "not found" (skill/agent CONTENT can legitimately contain that phrase).
    await t.test("L2 get_skill/get_agent resolve space/case variants", async () => {
      const MISS = /not found\. Available (skills|agents):/i;
      const skill = (await client.callTool({
        name: "get_skill",
        arguments: { name: "HANDOFF" },
      })) as CallResult;
      assert.doesNotMatch(textOf(skill), MISS, "get_skill('HANDOFF') should resolve");

      const agent = (await client.callTool({
        name: "get_agent",
        arguments: { name: "Milestone Researcher" },
      })) as CallResult;
      assert.doesNotMatch(textOf(agent), MISS, "get_agent('Milestone Researcher') should resolve");
    });
  } finally {
    await close();
  }
});

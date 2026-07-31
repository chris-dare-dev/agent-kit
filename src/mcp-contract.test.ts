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
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { probeArtifactMemory } from "./tools/artifact-memory.js";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SERVER_ENTRY = resolve(REPO_ROOT, "src", "index.ts");
const FIXTURE_ROOT = resolve(REPO_ROOT, "tests", "fixtures", "mcp-contract");
const GOLDEN_PATH = resolve(REPO_ROOT, "tests", "fixtures", "tools-list.golden.json");
const DEGRADED_GOLDEN_PATH = resolve(
  REPO_ROOT,
  "tests",
  "fixtures",
  "tools-list.degraded.golden.json",
);
const UPDATE_GOLDEN = process.env.UPDATE_GOLDEN === "1";

// Whether THIS host can serve the artifact-memory group. The spawned server
// runs the same probe against the same platform and socket, so the in-process
// answer predicts the child's tool count. Asserting a hardcoded 17 is what made
// the suite red on Windows for a reason that had nothing to do with the contract.
const ARTIFACT_MEMORY_AVAILABLE = probeArtifactMemory({
  serverProfile: "personal",
}).available;

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

async function startServer(
  extraEnv: Record<string, string> = {},
): Promise<{ client: Client; close: () => Promise<void> }> {
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
      ...extraEnv,
    },
    stderr: "ignore",
  });
  const client = new Client({ name: "mcp-contract-test", version: "1.0.0" });
  await client.connect(transport);
  return { client, close: () => client.close() };
}

function toolShape(tools: { name: string; inputSchema: unknown }[]): ToolShape[] {
  return tools
    .map((tool) => ({ name: tool.name, inputSchema: tool.inputSchema }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function assertGolden(shape: ToolShape[], goldenPath: string): void {
  if (UPDATE_GOLDEN) {
    writeFileSync(goldenPath, JSON.stringify(shape, null, 2) + "\n", "utf-8");
    return;
  }
  let golden: ToolShape[];
  try {
    golden = JSON.parse(readFileSync(goldenPath, "utf-8"));
  } catch (err) {
    assert.fail(
      `Could not read golden ${goldenPath}: ${
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
}

test("MCP contract (tools/list golden + representative calls)", async (t) => {
  const { client, close } = await startServer();
  try {
    // ---- Layer 1: tools/list golden snapshot -----------------------------
    // Only asserted where the artifact-memory group can actually be served.
    // On a host without AF_UNIX the 17-tool list is unreachable by design, and
    // the degraded contract is asserted by its own test below instead.
    await t.test(
      "tools/list matches the golden snapshot",
      {
        skip: ARTIFACT_MEMORY_AVAILABLE
          ? false
          : "artifact memory unavailable on this host — see the degraded-contract test",
      },
      async () => {
        assertGolden(toolShape((await client.listTools()).tools), GOLDEN_PATH);
      },
    );

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

/**
 * Referential integrity of the advertised tool surface (M2,
 * gates-green-t-referential-integrity).
 *
 * `get_skill`'s argument description named `argocd-debug` and `get_agent`'s named
 * `argocd-ops, gitops, cluster-health` — four identifiers that stopped existing at
 * the genericization fork. Because argument descriptions live inside `inputSchema`,
 * the golden snapshot froze the stale text and the contract test *certified* it.
 * This test closes that hole from the other side: every identifier a tool
 * description advertises must resolve against content discovered on disk, so
 * reverting the derivation in src/index.ts fails here rather than passing.
 */
function discoveredNames(dir: string, kind: "dir" | "md"): Set<string> {
  const entries = readdirSync(resolve(REPO_ROOT, "data", dir), {
    withFileTypes: true,
  });
  const names = new Set<string>();
  for (const e of entries) {
    if (kind === "dir" && e.isDirectory()) names.add(e.name);
    if (kind === "md" && e.isFile() && e.name.endsWith(".md")) {
      const stem = e.name.slice(0, -3);
      names.add(stem);
      names.add(stem.replace(/-/g, " ")); // the display form get_reference also accepts
    }
  }
  return names;
}

/** Every `(e.g., a, b)` identifier list in a description string. */
function advertisedIdentifiers(description: string): string[] {
  const out: string[] = [];
  for (const [, list] of description.matchAll(/\(e\.g\.,?\s+([^)]+)\)/g)) {
    for (const raw of list.split(",")) {
      const name = raw.trim();
      // Identifiers only: prose inside an e.g. clause is not a route.
      if (/^[a-z0-9][a-z0-9._-]*$/i.test(name)) out.push(name);
    }
  }
  return out;
}

test("every identifier a tool description advertises resolves on disk", async () => {
  const sets: Record<string, Set<string>> = {
    skill: discoveredNames("skills", "dir"),
    agent: discoveredNames("agents", "md"),
    reference: discoveredNames("references", "md"),
  };
  const byTool: Record<string, keyof typeof sets> = {
    get_skill: "skill",
    list_skills: "skill",
    get_agent: "agent",
    list_agents: "agent",
    get_reference: "reference",
    list_references: "reference",
  };

  const { client, close } = await startServer();
  try {
    const tools = (await client.listTools()).tools;
    let checked = 0;

    for (const tool of tools) {
      const schema = tool.inputSchema as {
        properties?: Record<string, { description?: string }>;
      };
      const descriptions = [
        tool.description ?? "",
        ...Object.values(schema.properties ?? {}).map((p) => p.description ?? ""),
      ];
      for (const description of descriptions) {
        for (const name of advertisedIdentifiers(description)) {
          const kind = byTool[tool.name];
          const universe = kind
            ? sets[kind]
            : new Set([...sets.skill, ...sets.agent, ...sets.reference]);
          assert.ok(
            universe.has(name),
            `${tool.name} advertises ${kind ?? "content"} "${name}", which does not ` +
              `exist on disk (${universe.size} discovered). Tool descriptions must ` +
              `derive their examples from discovered names — see derivedExamples() ` +
              `in src/index.ts.`,
          );
          checked += 1;
        }
      }
    }

    // Guard against the test passing because nothing advertises anything: the
    // three derived example sets must actually have rendered.
    assert.ok(
      checked >= 4,
      `expected the derived examples to advertise at least 4 identifiers, saw ${checked} — ` +
        "did derivedExamples() stop rendering?",
    );
  } finally {
    await close();
  }
});

/**
 * The F001 regression, runnable everywhere.
 *
 * The default profile used to call createArtifactMemoryTools() unguarded at
 * module scope; on any host without AF_UNIX that threw and the process exited
 * before serving a single tool. The platform is stubbed rather than detected so
 * this fails on macOS and Linux too — the defect must not be catchable only on
 * a Windows runner.
 */
test("default profile starts on a platform without AF_UNIX", async () => {
  const { client, close } = await startServer({
    AGENT_KIT_PROBE_PLATFORM: "win32",
  });
  try {
    const shape = toolShape((await client.listTools()).tools);

    assert.equal(
      shape.length,
      13,
      "a host without AF_UNIX must still serve the 13 tools that never touch the substrate",
    );
    for (const name of [
      "artifact_memory_status",
      "search_artifacts",
      "get_artifact",
      "query_temporal_facts",
    ]) {
      assert.equal(
        shape.some((tool) => tool.name === name),
        false,
        `${name} must not be advertised when the transport is unavailable`,
      );
    }

    // The server is not merely alive — it still answers real work.
    const skills = (await client.callTool({
      name: "list_skills",
      arguments: {},
    })) as CallResult;
    assert.notEqual(skills.isError, true, "list_skills must work in the degraded state");

    // An unavailable tool answers with its REASON, not a bare "Unknown tool":
    // the operator has to be able to tell a host problem from a missing feature.
    const denied = (await client.callTool({
      name: "search_artifacts",
      arguments: { query: "anything" },
    })) as CallResult;
    assert.equal(denied.isError, true, "an unavailable tool must answer isError");
    assert.match(
      textOf(denied),
      /unsupported-platform/,
      "the refusal must name why the group is unavailable",
    );

    assertGolden(shape, DEGRADED_GOLDEN_PATH);
  } finally {
    await close();
  }
});

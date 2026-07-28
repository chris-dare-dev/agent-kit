/**
 * S2.1 negative regression — SERVER_PROFILE=shared excludes the personal-memory
 * tier from EVERY tool, while the default (personal) profile is unchanged.
 *
 * Spawns the real server twice over stdio (shared / default) against the same
 * memory fixture (MEMORY_ROOT points at tests/fixtures/mcp-contract/memory, so
 * the memory IS present on disk — the gate must hide it by PROFILE, not by
 * absence). The epic's non-negotiable core: no personal memory leaks under shared.
 */

import { test } from "node:test";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import assert from "node:assert/strict";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { probeArtifactMemory } from "./tools/artifact-memory.js";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SERVER_ENTRY = resolve(REPO_ROOT, "src", "index.ts");
const FIXTURE_ROOT = resolve(REPO_ROOT, "tests", "fixtures", "mcp-contract");
const MEMORY_DIR = resolve(FIXTURE_ROOT, "memory");
const MARKER = "WORKSPACE-SHARED-PROFILE-NEGATIVE-MARKER-7f3a1c9e";
const MEMORY_NAME = "fixture_shared_profile";
const ARTIFACT_MEMORY_TOOLS = [
  "artifact_memory_status",
  "search_artifacts",
  "get_artifact",
  "query_temporal_facts",
] as const;
const SHARED_ARTIFACT_CALLS: Record<
  (typeof ARTIFACT_MEMORY_TOOLS)[number],
  Record<string, unknown>
> = {
  artifact_memory_status: {},
  search_artifacts: { query: "SENSITIVE-ARTIFACT-QUERY" },
  get_artifact: { relative_path: ".claude/agent-memory/private.md" },
  query_temporal_facts: {
    query: "SENSITIVE-PILOT-QUERY",
    group_id: "private_pilot",
  },
};

interface CallResult {
  content: { type: string; text: string }[];
  isError?: boolean;
}
function textOf(r: CallResult): string {
  return r.content.map((c) => c.text).join("");
}

async function startServer(
  profile: string | undefined,
  extraEnv: Record<string, string> = {},
) {
  const env: Record<string, string> = {
    PATH: process.env.PATH ?? "",
    HOME: process.env.HOME ?? "",
    PLATFORM_ROOT: FIXTURE_ROOT,
    WORKSPACE_ROOT: FIXTURE_ROOT,
    MEMORY_ROOT: MEMORY_DIR, // memory IS configured — the gate must hide by profile
    TOKEN_LOG_PATH: "",
    CACHE_SNAPSHOT_PATH: "",
  };
  Object.assign(env, extraEnv);
  if (profile) env.SERVER_PROFILE = profile;
  if (profile === "shared") {
    // Shared must not even initialize the excluded local backend.
    env.PERSONAL_ARTIFACT_PYTHON = "relative-path-that-personal-would-reject";
  }
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: ["--import", "tsx", SERVER_ENTRY],
    cwd: REPO_ROOT,
    env,
    stderr: "ignore",
  });
  const client = new Client({ name: "server-profile-test", version: "1.0.0" });
  await client.connect(transport);
  return { client, close: () => client.close() };
}

const marker = new RegExp(MARKER);
const memName = new RegExp(MEMORY_NAME);
// The `[memory]` result-type tag is emitted ONLY on an actual memory search hit —
// never in a "No results found for query: <marker>" echo. It is the correct
// discriminator for the search tools (asserting on the marker itself would false-
// match the echoed query).
const memTag = /\[memory\]/;

test("SERVER_PROFILE=shared hides personal memory from every tool", async () => {
  const { client, close } = await startServer("shared");
  try {
    const search = (await client.callTool({
      name: "search_platform_knowledge",
      arguments: { query: MARKER },
    })) as CallResult;
    assert.doesNotMatch(textOf(search), memTag, "shared: search_platform_knowledge returned a memory result");

    const smem = (await client.callTool({
      name: "search_memory",
      arguments: { query: MARKER },
    })) as CallResult;
    assert.doesNotMatch(textOf(smem), memTag, "shared: search_memory returned a memory result");

    const list = (await client.callTool({ name: "list_memory", arguments: {} })) as CallResult;
    assert.doesNotMatch(textOf(list), memName, "shared: list_memory leaked the entry");

    const get = (await client.callTool({
      name: "get_memory",
      arguments: { name: MEMORY_NAME },
    })) as CallResult;
    // The not-found message echoes the NAME, not the body — assert the body marker is absent.
    assert.doesNotMatch(textOf(get), marker, "shared: get_memory leaked memory content");
  } finally {
    await close();
  }
});

test("SERVER_PROFILE=shared neither advertises nor dispatches artifact memory", async () => {
  const { client, close } = await startServer("shared");
  try {
    const listed = new Set((await client.listTools()).tools.map((tool) => tool.name));
    for (const name of ARTIFACT_MEMORY_TOOLS) {
      assert.equal(listed.has(name), false, `shared advertised ${name}`);
      const direct = (await client.callTool({
        name,
        arguments: SHARED_ARTIFACT_CALLS[name],
      })) as CallResult;
      assert.equal(direct.isError, true, `shared dispatched ${name}`);
      assert.match(textOf(direct), new RegExp(`Unknown tool: ${name}`));
      assert.doesNotMatch(
        textOf(direct),
        /SENSITIVE|private/,
        `shared echoed arguments for ${name}`,
      );
    }
  } finally {
    await close();
  }
});

test("default (personal) profile still serves personal memory — no regression", async () => {
  const { client, close } = await startServer(undefined);
  try {
    const search = (await client.callTool({
      name: "search_platform_knowledge",
      arguments: { query: MARKER },
    })) as CallResult;
    assert.match(textOf(search), memTag, "personal: search_platform_knowledge should surface the memory result");
    assert.match(textOf(search), marker, "personal: the memory content should be present");

    const smem = (await client.callTool({
      name: "search_memory",
      arguments: { query: MARKER },
    })) as CallResult;
    assert.match(textOf(smem), memTag, "personal: search_memory should surface the memory result");

    const list = (await client.callTool({ name: "list_memory", arguments: {} })) as CallResult;
    assert.match(textOf(list), memName, "personal: list_memory should include the fixture");

    const get = (await client.callTool({
      name: "get_memory",
      arguments: { name: MEMORY_NAME },
    })) as CallResult;
    assert.match(textOf(get), marker, "personal: get_memory should return content");
  } finally {
    await close();
  }
});

// The contract is "advertise if and only if the group can be served", not
// "always advertise". Asserting the latter made this red on Windows for a
// capability reason rather than a profile reason — the thing this file tests.
test("personal profile advertises artifact memory exactly when it is available", async () => {
  const available = probeArtifactMemory({ serverProfile: "personal" }).available;
  const { client, close } = await startServer(undefined);
  try {
    const listed = new Set((await client.listTools()).tools.map((tool) => tool.name));
    for (const name of ARTIFACT_MEMORY_TOOLS) {
      assert.equal(
        listed.has(name),
        available,
        available
          ? `personal omitted ${name} despite the transport being available`
          : `personal advertised ${name} despite the transport being unavailable`,
      );
    }
  } finally {
    await close();
  }
});

// A personal host whose transport is unusable must degrade, never die — and it
// must still refuse the excluded tools with a reason rather than serving them.
test("personal profile survives an unavailable artifact-memory transport", async () => {
  const { client, close } = await startServer(undefined, {
    AGENT_KIT_PROBE_PLATFORM: "win32",
  });
  try {
    const listed = new Set((await client.listTools()).tools.map((tool) => tool.name));
    for (const name of ARTIFACT_MEMORY_TOOLS) {
      assert.equal(listed.has(name), false, `unavailable transport still advertised ${name}`);
    }
    // Personal memory is a separate tier and must be untouched by the failure.
    const list = (await client.callTool({
      name: "list_memory",
      arguments: {},
    })) as CallResult;
    assert.match(textOf(list), memName, "the memory tier must survive an artifact-memory failure");

    const direct = (await client.callTool({
      name: "search_artifacts",
      arguments: SHARED_ARTIFACT_CALLS.search_artifacts,
    })) as CallResult;
    assert.equal(direct.isError, true, "unavailable artifact tool must answer isError");
    assert.match(textOf(direct), /unsupported-platform/, "the refusal must name the reason");
  } finally {
    await close();
  }
});

// ---------------------------------------------------------------------------
// Startup degradation lines.
//
// "Disabled because nothing resolved", "enabled but empty", "excluded by
// profile" and "the transport failed" all used to look the same from outside:
// a silent `0`, or a dead process. Each is now one greppable line, and this is
// what stops a future change from quietly removing them again.
// ---------------------------------------------------------------------------

/** Start the server, collect stderr until it is listening, then stop it. */
async function startupLog(env: Record<string, string>): Promise<string> {
  const child = spawn(process.execPath, ["--import", "tsx", SERVER_ENTRY], {
    cwd: REPO_ROOT,
    env: { PATH: process.env.PATH ?? "", PLATFORM_ROOT: FIXTURE_ROOT, ...env },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  try {
    await new Promise<void>((resolveDone, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`server never started; stderr was:\n${stderr}`)),
        30_000,
      );
      child.stderr.on("data", (chunk: string) => {
        stderr += chunk;
        if (stderr.includes("Server started")) {
          clearTimeout(timer);
          resolveDone();
        }
      });
      child.on("error", (err) => {
        clearTimeout(timer);
        reject(err);
      });
      child.on("exit", (code) => {
        clearTimeout(timer);
        reject(new Error(`server exited early (code ${code}); stderr:\n${stderr}`));
      });
    });
  } finally {
    child.kill();
  }
  return stderr;
}

test("startup states each emit their own distinguishable line", async (t) => {
  await t.test("memory tier disabled by an empty MEMORY_ROOT", async () => {
    const log = await startupLog({ WORKSPACE_ROOT: FIXTURE_ROOT, MEMORY_ROOT: "" });
    assert.match(log, /memory tier disabled: MEMORY_ROOT is set to the empty string/);
    assert.doesNotMatch(log, /memory tier enabled/);
  });

  await t.test("memory tier enabled but empty still names its path", async () => {
    const empty = await mkdtemp(join(tmpdir(), "agent-kit-empty-memory-"));
    t.after(() => rm(empty, { recursive: true, force: true }));
    const log = await startupLog({ WORKSPACE_ROOT: FIXTURE_ROOT, MEMORY_ROOT: empty });
    // The path is the discriminator between "enabled, nothing in it" and "off".
    assert.match(log, /memory tier enabled, 0 entries at /);
    assert.ok(log.includes(empty), "the enabled line must carry the resolved path");
    assert.doesNotMatch(log, /memory tier disabled/);
  });

  await t.test("an unavailable transport names its reason exactly once", async () => {
    const log = await startupLog({
      WORKSPACE_ROOT: FIXTURE_ROOT,
      MEMORY_ROOT: "",
      AGENT_KIT_PROBE_PLATFORM: "win32",
    });
    const lines = log
      .split("\n")
      .filter((line) => line.includes("artifact-memory tools unavailable"));
    assert.equal(lines.length, 1, "exactly one unavailability line");
    assert.match(lines[0], /^\[agent-kit-mcp\] artifact-memory tools unavailable: .+$/);
    assert.match(lines[0], /unsupported-platform/);
  });

  await t.test("the shared profile is excluded, never reported as failed", async () => {
    const log = await startupLog({
      WORKSPACE_ROOT: FIXTURE_ROOT,
      MEMORY_ROOT: MEMORY_DIR,
      SERVER_PROFILE: "shared",
    });
    assert.match(log, /Artifact-memory tools EXCLUDED \(shared profile\)/);
    assert.doesNotMatch(
      log,
      /artifact-memory tools unavailable/,
      "policy exclusion must never be reported as a transport failure",
    );
    assert.doesNotMatch(log, /memory tier (enabled|disabled)/, "that line is personal-only");
  });
});

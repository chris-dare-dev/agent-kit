import assert from "node:assert/strict";
import { chmod, mkdtemp, realpath, rm, symlink, writeFile } from "node:fs/promises";
import {
  createServer,
  type IncomingHttpHeaders,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import { homedir } from "node:os";
import { join } from "node:path";
import { test, type TestContext } from "node:test";
import {
  createArtifactMemoryTools,
  type ArtifactMemoryRequest,
  type ArtifactMemoryRequester,
} from "./artifact-memory.js";

const SOCKET_ERROR_FRAGMENT = "socket is not configured securely";

interface CapturedRequest {
  request: ArtifactMemoryRequest;
}

type SocketHandler = (
  request: IncomingMessage,
  response: ServerResponse,
) => void;

function jsonResponse(
  payload: unknown,
  init: ResponseInit = {},
): Response {
  const headers = new Headers(init.headers);
  if (!headers.has("content-type")) {
    headers.set("content-type", "application/json; charset=utf-8");
  }
  return new Response(JSON.stringify(payload), { ...init, headers });
}

async function listen(server: Server, socketPath: string): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const onError = (error: Error) => {
      server.off("listening", onListening);
      reject(error);
    };
    const onListening = () => {
      server.off("error", onError);
      resolve();
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(socketPath);
  });
}

async function close(server: Server): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function socketFixture(
  t: TestContext,
  handler: SocketHandler = () => {
    assert.fail("mocked requester should not reach the real Unix socket");
  },
): Promise<{ root: string; socketPath: string }> {
  const tempRoot = await mkdtemp(join(homedir(), "am-"));
  const root = await realpath(tempRoot);
  await chmod(root, 0o700);
  const socketPath = join(root, "artifact-memory.sock");
  const server = createServer(handler);
  await listen(server, socketPath);
  await chmod(socketPath, 0o600);
  t.after(async () => {
    await close(server);
    await rm(root, { recursive: true, force: true });
  });
  return { root, socketPath };
}

async function harness(
  t: TestContext,
  payload: unknown = { ok: true },
) {
  const { socketPath } = await socketFixture(t);
  const calls: CapturedRequest[] = [];
  const requester: ArtifactMemoryRequester = async (request) => {
    calls.push({ request });
    return jsonResponse(payload);
  };
  return {
    calls,
    socketPath,
    tools: createArtifactMemoryTools({ socketPath }, requester),
  };
}

test("status posts bounded JSON over the owner-private Unix socket without authorization", async (t) => {
  let resolveRequest: ((value: {
    body: string;
    headers: IncomingHttpHeaders;
    method: string | undefined;
    path: string | undefined;
  }) => void) | undefined;
  const received = new Promise<{
    body: string;
    headers: IncomingHttpHeaders;
    method: string | undefined;
    path: string | undefined;
  }>((resolve) => {
    resolveRequest = resolve;
  });
  const { socketPath } = await socketFixture(t, (request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      resolveRequest?.({
        body: Buffer.concat(chunks).toString("utf8"),
        headers: request.headers,
        method: request.method,
        path: request.url,
      });
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ mode: "resident-service-status" }));
    });
  });

  const tools = createArtifactMemoryTools({ socketPath });
  const result = JSON.parse(await tools.artifact_memory_status());
  const request = await received;
  assert.equal(result.mode, "resident-service-status");
  assert.equal(request.method, "POST");
  assert.equal(request.path, "/v1/status");
  assert.deepEqual(JSON.parse(request.body), {});
  assert.equal(request.headers.accept, "application/json");
  assert.equal(request.headers["content-type"], "application/json");
  assert.equal(request.headers.connection, "close");
  assert.equal(request.headers["x-artifact-memory-protocol"], "1");
  assert.equal(request.headers.authorization, undefined);
  assert.equal(request.headers["x-api-key"], undefined);
});

test("every route announces the adapter's wire protocol to the service", async (t) => {
  const { calls, tools } = await harness(t);
  await tools.artifact_memory_status();
  await tools.search_artifacts({ query: "ownership" });
  await tools.get_artifact({ relative_path: "plans/roadmap.md" });
  await tools.query_temporal_facts({ query: "ownership" });
  assert.equal(calls.length, 4);
  for (const { request } of calls) {
    assert.equal(
      request.headers["X-Artifact-Memory-Protocol"],
      "1",
      `${request.path} must declare its protocol`,
    );
  }
});

test("stale adapter against an upgraded service fails with a restart instruction", async (t) => {
  const { socketPath } = await socketFixture(t);
  const invokeWith = (response: Response) =>
    createArtifactMemoryTools(
      { socketPath },
      async () => response,
    ).artifact_memory_status();

  // The service refused this adapter's protocol outright.
  await assert.rejects(
    invokeWith(
      jsonResponse(
        {
          error: "client protocol 1 is not served by this build",
          service_protocol_version: 2,
          action: "restart-session",
        },
        {
          status: 426,
          headers: { "X-Artifact-Memory-Protocol": "2" },
        },
      ),
    ),
    /artifact memory service was upgraded to an incompatible protocol; restart this session/,
  );

  // A 426 from a service too old to announce its protocol is still fenced.
  await assert.rejects(
    invokeWith(jsonResponse({ error: "upgrade required" }, { status: 426 })),
    /restart this session/,
  );

  // Served 200, but the announced contract is not the one this build parses —
  // the silent-success case F-09 exists to kill.
  for (const announced of ["2", "0", "", "1.0", "v1", "not-a-number", "  "]) {
    await assert.rejects(
      invokeWith(
        jsonResponse(
          { mode: "resident-service-status", results: [] },
          { headers: { "X-Artifact-Memory-Protocol": announced } },
        ),
      ),
      /restart this session/,
      `announced protocol ${JSON.stringify(announced)} must not be parsed`,
    );
  }

  // Matching protocol, and the version-less pre-fencing service, both stay
  // readable so either side can ship first.
  const matched = JSON.parse(
    await invokeWith(
      jsonResponse(
        { mode: "resident-service-status" },
        { headers: { "X-Artifact-Memory-Protocol": "1" } },
      ),
    ),
  );
  assert.equal(matched.mode, "resident-service-status");
  const versionless = JSON.parse(
    await invokeWith(jsonResponse({ mode: "resident-service-status" })),
  );
  assert.equal(versionless.mode, "resident-service-status");
});

test("version fencing outranks status decoding and leaks no service detail", async (t) => {
  const { socketPath } = await socketFixture(t);
  const upgradeSecret = "qdrant-admin-key-in-upgrade-body";
  const tools = createArtifactMemoryTools(
    { socketPath },
    async () =>
      jsonResponse(
        { error: `leaked ${upgradeSecret}`, request_id: "secret-id" },
        {
          status: 426,
          headers: { "X-Artifact-Memory-Protocol": "7" },
        },
      ),
  );
  await assert.rejects(tools.artifact_memory_status(), (error: Error) => {
    assert.match(error.message, /restart this session/);
    assert.doesNotMatch(error.message, /HTTP 426/);
    assert.doesNotMatch(error.message, new RegExp(upgradeSecret));
    assert.doesNotMatch(error.message, /secret-id/);
    return true;
  });
});

test("search validates bounds and maps only current-revision filters", async (t) => {
  const { calls, tools } = await harness(t);
  await tools.search_artifacts({
    query: "milestone ownership",
    max_results: 5,
    project: "platform",
    artifact_type: "handoff",
  });
  assert.deepEqual(JSON.parse(calls[0].request.body), {
    query: "milestone ownership",
    limit: 5,
    include_history: false,
    project: "platform",
    artifact_type: "handoff",
  });
  await assert.rejects(
    tools.search_artifacts({
      query: "old milestone ownership",
      include_history: true,
    }),
    /historical artifact snippets are disabled/,
  );
  await assert.rejects(
    tools.search_artifacts({
      query: "old milestone ownership",
      include_history: "false",
    }),
    /include_history must be a boolean/,
  );
  assert.equal(calls.length, 1, "history denial must not invoke the service");
  await assert.rejects(
    tools.search_artifacts({ query: "x", max_results: 26 }),
    /between 1 and 25/,
  );
  await assert.rejects(
    tools.search_artifacts({ query: "   " }),
    /query must contain/,
  );
});

test("get and facts map JSON bodies and retain pilot denial", async (t) => {
  const { calls, tools } = await harness(t);
  await tools.get_artifact({
    relative_path: "plans/roadmap.md",
    max_chars: 50_000,
  });
  assert.deepEqual(JSON.parse(calls[0].request.body), {
    relative_path: "plans/roadmap.md",
    max_chars: 50_000,
  });
  await tools.query_temporal_facts({
    query: "who owns ingestion",
    group_id: "approved_v3",
  });
  assert.deepEqual(JSON.parse(calls[1].request.body), {
    query: "who owns ingestion",
    limit: 8,
    include_pilot: false,
    group_id: "approved_v3",
  });
  await assert.rejects(
    tools.query_temporal_facts({
      query: "pilot ownership",
      include_pilot: true,
    }),
    /unapproved Graphiti pilot access is disabled/,
  );
  await assert.rejects(
    tools.query_temporal_facts({
      query: "pilot ownership",
      include_pilot: 0,
    }),
    /include_pilot must be a boolean/,
  );
  assert.equal(calls.length, 2, "pilot denial must not invoke the service");
  await assert.rejects(
    tools.get_artifact({ relative_path: "plans/a.md", max_chars: 100 }),
    /between 256 and 50000/,
  );
});

test("configuration accepts only normalized portable Unix socket paths", () => {
  for (const invalid of [
    "artifact-memory.sock",
    "/tmp/../tmp/artifact-memory.sock",
    "/tmp/artifact-memory.sock/",
    "/tmp/socket\0suffix",
    `/${"x".repeat(103)}`,
  ]) {
    assert.throws(
      () => createArtifactMemoryTools({ socketPath: invalid }),
      /socket path/,
      invalid,
    );
  }
  assert.doesNotThrow(() =>
    createArtifactMemoryTools({
      socketPath: join(homedir(), "artifact-memory.sock"),
    }),
  );
});

test("socket symlink, endpoint type, mode, and parent permissions fail before a request", async (t) => {
  let handlerCalls = 0;
  const { root, socketPath } = await socketFixture(t, () => {
    handlerCalls += 1;
  });
  let requesterCalls = 0;
  const requester: ArtifactMemoryRequester = async () => {
    requesterCalls += 1;
    return jsonResponse({ ok: true });
  };
  const linkedSocket = join(root, "linked.sock");
  await symlink(socketPath, linkedSocket);
  const regularFile = join(root, "regular.sock");
  await writeFile(regularFile, "not a socket", { mode: 0o600 });

  for (const invalidSocketPath of [linkedSocket, regularFile]) {
    const tools = createArtifactMemoryTools(
      { socketPath: invalidSocketPath },
      requester,
    );
    await assert.rejects(
      tools.artifact_memory_status(),
      new RegExp(SOCKET_ERROR_FRAGMENT),
    );
  }
  await chmod(socketPath, 0o666);
  await assert.rejects(
    createArtifactMemoryTools({ socketPath }, requester).artifact_memory_status(),
    new RegExp(SOCKET_ERROR_FRAGMENT),
  );
  await chmod(socketPath, 0o600);
  await chmod(root, 0o755);
  await assert.rejects(
    createArtifactMemoryTools({ socketPath }, requester).artifact_memory_status(),
    new RegExp(SOCKET_ERROR_FRAGMENT),
  );
  assert.equal(requesterCalls, 0, "unsafe endpoints must fail before a request");
  assert.equal(handlerCalls, 0, "unsafe endpoints must never reach the service");
});

test("socket security is revalidated for every request", async (t) => {
  const { calls, socketPath, tools } = await harness(t);
  await tools.artifact_memory_status();
  await chmod(socketPath, 0o666);
  await assert.rejects(
    tools.artifact_memory_status(),
    new RegExp(SOCKET_ERROR_FRAGMENT),
  );
  assert.equal(calls.length, 1);
});

test("socket and backend diagnostics are never reflected in errors", async (t) => {
  const { socketPath } = await socketFixture(t);
  const networkSecret = "qdrant-admin-key-super-secret";
  const unavailable = createArtifactMemoryTools(
    { socketPath },
    async () => {
      throw new Error(networkSecret);
    },
  );
  await assert.rejects(unavailable.artifact_memory_status(), (error: Error) => {
    assert.equal(error.message, "artifact memory service is unavailable");
    assert.doesNotMatch(error.message, new RegExp(networkSecret));
    return true;
  });

  const rejected = createArtifactMemoryTools(
    { socketPath },
    async () =>
      jsonResponse(
        { error: `backend leaked ${networkSecret}`, request_id: "secret-id" },
        { status: 400 },
      ),
  );
  await assert.rejects(rejected.artifact_memory_status(), (error: Error) => {
    assert.equal(
      error.message,
      "artifact memory service rejected request (HTTP 400)",
    );
    assert.doesNotMatch(error.message, new RegExp(networkSecret));
    assert.doesNotMatch(error.message, /secret-id/);
    return true;
  });
});

test("redirects, content type, JSON shape, and response size fail closed", async (t) => {
  const invokeWith = async (response: Response) => {
    const { socketPath } = await socketFixture(t);
    const tools = createArtifactMemoryTools({ socketPath }, async () => response);
    return tools.artifact_memory_status();
  };

  await assert.rejects(
    invokeWith(
      jsonResponse(
        { redirected: true },
        { status: 302, headers: { Location: "http://127.0.0.1:9999/" } },
      ),
    ),
    /redirect rejected/,
  );
  await assert.rejects(
    invokeWith(
      new Response("not JSON", {
        status: 200,
        headers: { "Content-Type": "text/plain" },
      }),
    ),
    /non-JSON response/,
  );
  await assert.rejects(
    invokeWith(
      new Response("{broken", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
    /returned invalid JSON/,
  );
  await assert.rejects(invokeWith(jsonResponse(["not", "an", "object"])), /JSON object/);
  await assert.rejects(
    invokeWith(
      new Response("{}", {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": String(2 * 1024 * 1024 + 1),
        },
      }),
    ),
    /response exceeds 2 MiB/,
  );
  await assert.rejects(
    invokeWith(
      new Response(`{"value":"${"x".repeat(2 * 1024 * 1024)}"}`, {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
    /response exceeds 2 MiB/,
  );
  const streamSecret = "stream-internal-secret";
  await assert.rejects(
    invokeWith(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('{"partial":'));
            controller.error(new Error(streamSecret));
          },
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    ),
    (error: Error) => {
      assert.equal(
        error.message,
        "artifact memory service returned an unreadable response",
      );
      assert.doesNotMatch(error.message, new RegExp(streamSecret));
      return true;
    },
  );

  const prettyPrintAmplifier = {
    values: Array.from({ length: 300_000 }, () => []),
  };
  const compact = await invokeWith(jsonResponse(prettyPrintAmplifier));
  assert.ok(
    Buffer.byteLength(compact, "utf8") <= 2 * 1024 * 1024,
    "adapter output must retain the 2 MiB response bound",
  );
});

test("adapter forwards no database, graph, model, or bearer credentials", async (t) => {
  const { calls, tools } = await harness(t);
  const previous = {
    qdrant: process.env.QDRANT_API_KEY,
    falkor: process.env.FALKORDB_PASSWORD,
    model: process.env.HUGGING_FACE_HUB_TOKEN,
  };
  process.env.QDRANT_API_KEY = "qdrant-secret";
  process.env.FALKORDB_PASSWORD = "falkor-secret";
  process.env.HUGGING_FACE_HUB_TOKEN = "model-secret";
  try {
    await tools.artifact_memory_status();
  } finally {
    if (previous.qdrant === undefined) delete process.env.QDRANT_API_KEY;
    else process.env.QDRANT_API_KEY = previous.qdrant;
    if (previous.falkor === undefined) delete process.env.FALKORDB_PASSWORD;
    else process.env.FALKORDB_PASSWORD = previous.falkor;
    if (previous.model === undefined) delete process.env.HUGGING_FACE_HUB_TOKEN;
    else process.env.HUGGING_FACE_HUB_TOKEN = previous.model;
  }
  const serialized = JSON.stringify(calls[0]);
  assert.doesNotMatch(serialized, /qdrant-secret/);
  assert.doesNotMatch(serialized, /falkor-secret/);
  assert.doesNotMatch(serialized, /model-secret/);
  assert.equal("Authorization" in calls[0].request.headers, false);
  assert.equal("authorization" in calls[0].request.headers, false);
});

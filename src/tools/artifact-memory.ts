/**
 * Read-only adapter for the resident Artifact Memory Service.
 *
 * The service owns Qdrant, FalkorDB, model, catalog, and canonical-file
 * verification. This adapter reaches it only through an owner-private Unix
 * socket, so it never reads or forwards a service bearer token, database, or
 * model credential.
 */

import { existsSync, lstatSync, type Stats } from "node:fs";
import { lstat } from "node:fs/promises";
import {
  request as httpRequest,
  type IncomingHttpHeaders,
  type IncomingMessage,
} from "node:http";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, normalize, sep } from "node:path";
import { Readable } from "node:stream";

export interface ArtifactMemoryConfig {
  /**
   * Absolute path to the resident service's Unix socket.
   *
   * NOT a test-only knob any more. loadConfig() resolves this in three steps —
   * ARTIFACT_MEMORY_SOCKET, then service.socket_path in
   * <derived-root>/artifact-memory-runtime.json, then the default derived from
   * artifactMemoryDerivedRoot — and passes the result in. When it was
   * documented as test-only there was no environment variable that reached it,
   * so a wrong path could not be corrected without editing source.
   */
  socketPath?: string;
  /** Which resolution step supplied socketPath; used in error text. */
  socketPathSource?: string;
}

export interface ArtifactMemoryRequest {
  socketPath: string;
  path: string;
  method: "POST";
  body: string;
  headers: Readonly<Record<string, string>>;
  signal: AbortSignal;
}

export interface ArtifactMemoryResponse {
  status: number;
  headers: Headers;
  body: ReadableStream<Uint8Array> | null;
}

export type ArtifactMemoryRequester = (
  request: ArtifactMemoryRequest,
) => Promise<ArtifactMemoryResponse>;

/**
 * Last-resort socket path, used only when loadConfig() did not supply one.
 *
 * Mirrors workspace-tooling/artifact_memory_provision.SOCKET_RELATIVE_PARTS
 * under the same per-OS derived root as artifact_runtime.derived_root(). This
 * constant previously said "workspace-artifacts" while the provisioner wrote
 * "personal-artifacts", so the adapter dialled a socket nothing ever bound.
 */
function defaultDerivedRoot(): string {
  const override = process.env.AGENT_KIT_DERIVED_ROOT;
  if (override) return override;
  const xdg = process.env.XDG_DATA_HOME;
  if (xdg) return join(xdg, "agent-kit");
  if (process.platform === "win32") {
    return join(process.env.LOCALAPPDATA || join(homedir(), "AppData", "Local"), "agent-kit");
  }
  if (process.platform === "darwin") {
    return join(homedir(), "Library", "Application Support", "agent-kit");
  }
  return join(homedir(), ".local", "share", "agent-kit");
}

const DEFAULT_SOCKET_PATH = join(
  defaultDerivedRoot(),
  "services",
  "qdrant",
  "artifact-memory.sock",
);
const MAX_JSON_BYTES = 2 * 1024 * 1024;
const MAX_HEADER_BYTES = 16 * 1024;
const MAX_SOCKET_PATH_BYTES = 103;
const TIMEOUT_MS = 30_000;
/**
 * Wire-protocol major version, sent on every request and validated on every
 * response. This adapter is loaded once per provider session and keeps running
 * its build until that session restarts, so a service upgrade would otherwise
 * be invisible here — the version fence is what turns a silent mixed-version
 * conversation into an explicit restart instruction. Must track
 * PROTOCOL_VERSION in scripts/artifact_memory_service.py.
 */
const PROTOCOL_VERSION = 1;
const PROTOCOL_HEADER = "x-artifact-memory-protocol";
const UPGRADE_REQUIRED_STATUS = 426;
const SOCKET_ERROR = "artifact memory service socket is not configured securely";
const UNAVAILABLE_ERROR = "artifact memory service is unavailable";
const UPGRADE_ERROR =
  "artifact memory service was upgraded to an incompatible protocol; restart this session to load the current adapter";
const ALLOWED_ROUTES = new Set([
  "/v1/status",
  "/v1/search",
  "/v1/get",
  "/v1/facts",
]);

class ArtifactMemoryServiceError extends Error {}

function responseHeaders(headers: IncomingHttpHeaders): Headers {
  const result = new Headers();
  for (const [name, value] of Object.entries(headers)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      for (const entry of value) result.append(name, entry);
    } else {
      result.set(name, value);
    }
  }
  return result;
}

function asArtifactMemoryResponse(
  response: IncomingMessage,
): ArtifactMemoryResponse {
  return {
    status: response.statusCode ?? 0,
    headers: responseHeaders(response.headers),
    body: Readable.toWeb(response) as ReadableStream<Uint8Array>,
  };
}

const defaultRequester: ArtifactMemoryRequester = (request) =>
  new Promise((resolve, reject) => {
    const clientRequest = httpRequest(
      {
        socketPath: request.socketPath,
        path: request.path,
        method: request.method,
        headers: request.headers,
        agent: false,
        signal: request.signal,
        maxHeaderSize: MAX_HEADER_BYTES,
      },
      (response) => resolve(asArtifactMemoryResponse(response)),
    );
    clientRequest.once("error", reject);
    clientRequest.once("upgrade", (_response, socket) => {
      socket.destroy();
      reject(new Error("unexpected protocol upgrade"));
    });
    clientRequest.end(request.body);
  });

function requireString(
  value: unknown,
  name: string,
  maximum = 1_000,
): string {
  if (typeof value !== "string") {
    throw new Error(`${name} must be a string`);
  }
  const result = value.trim();
  if (!result || result.length > maximum || result.includes("\0")) {
    throw new Error(`${name} must contain 1-${maximum} safe characters`);
  }
  return result;
}

function optionalString(value: unknown, name: string): string | undefined {
  if (value === undefined) return undefined;
  return requireString(value, name, 500);
}

function boundedInteger(
  value: unknown,
  name: string,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const result = value === undefined ? fallback : value;
  if (
    typeof result !== "number" ||
    !Number.isInteger(result) ||
    result < minimum ||
    result > maximum
  ) {
    throw new Error(`${name} must be an integer between ${minimum} and ${maximum}`);
  }
  return result;
}

/**
 * Why a socket path is unacceptable, or undefined when it is fine.
 *
 * Split out of absoluteSocketPath so the startup probe can report WHICH
 * property failed instead of restating the whole rule. Deliberately says
 * nothing about platform support or about the socket existing — those are
 * separate questions with separate answers (see probeArtifactMemory).
 */
function insecureSocketPathReason(value: string): string | undefined {
  const home = normalize(homedir());
  const homePrefix = `${home}${sep}`;
  if (!isAbsolute(value)) return "path is not absolute";
  if (value.includes("\0")) return "path contains a NUL byte";
  if (value.endsWith(sep)) return "path ends with a separator";
  if (normalize(value) !== value) return "path is not normalized";
  if (!value.startsWith(homePrefix)) {
    return `path is not under the current user's home directory (${home})`;
  }
  if (Buffer.byteLength(value, "utf8") > MAX_SOCKET_PATH_BYTES) {
    return `path exceeds the ${MAX_SOCKET_PATH_BYTES}-byte sun_path limit`;
  }
  return undefined;
}

function absoluteSocketPath(
  value: string,
  platform: NodeJS.Platform = process.platform,
): string {
  if (platform === "win32" || insecureSocketPathReason(value) !== undefined) {
    throw new Error(
      `artifact memory service socket path must be an absolute, normalized Unix path under the current user's home directory no longer than ${MAX_SOCKET_PATH_BYTES} bytes`,
    );
  }
  return value;
}

export type ArtifactMemoryUnavailableReason =
  | "unsupported-platform"
  | "socket-missing"
  | "socket-insecure"
  | "disabled-by-profile";

export type ArtifactMemoryProbeResult =
  | { available: true; socketPath: string }
  | {
      available: false;
      reason: ArtifactMemoryUnavailableReason;
      detail: string;
    };

export interface ArtifactMemoryProbeConfig {
  /** Trust profile; "shared" excludes this tool group by policy, not by capability. */
  serverProfile?: "personal" | "shared";
  /** Test-only override for the fixed local service socket. */
  socketPath?: string;
  /**
   * Test-only override for the host platform, so the win32 branch is
   * exercisable from a POSIX developer machine and from a Linux CI runner.
   * Mirrors the existing socketPath override on ArtifactMemoryConfig.
   */
  platform?: NodeJS.Platform;
}

/**
 * Decide whether the artifact-memory tool group can be served, WITHOUT throwing.
 *
 * Registration used to be `config.serverProfile === "personal"` with no check
 * that the transport was usable, so an unusable transport surfaced as an
 * uncaught exception at module scope and took the whole server — including the
 * 13 tools that never touch the substrate — down with it. Four genuinely
 * different situations are now four distinguishable answers:
 *
 *   disabled-by-profile  a shared deployment excludes the group on purpose
 *   unsupported-platform this host has no AF_UNIX (Windows)
 *   socket-insecure      the path exists but another account could hijack it
 *   socket-missing       nothing is listening — the service is probably stopped
 *
 * Synchronous on purpose: this runs once at startup and its one filesystem
 * touch is a single existsSync, so it stays callable from module scope.
 */
export function probeArtifactMemory(
  config: ArtifactMemoryProbeConfig = {},
): ArtifactMemoryProbeResult {
  if (config.serverProfile === "shared") {
    return {
      available: false,
      reason: "disabled-by-profile",
      detail: "SERVER_PROFILE=shared excludes the artifact-memory tool group",
    };
  }

  const platform = config.platform ?? process.platform;
  const socketPath = config.socketPath ?? DEFAULT_SOCKET_PATH;

  if (platform === "win32") {
    return {
      available: false,
      reason: "unsupported-platform",
      detail:
        `${platform} has no AF_UNIX support, and the resident service is ` +
        `reachable only over a Unix domain socket (attempted ${socketPath})`,
    };
  }

  const insecure = insecureSocketPathReason(socketPath);
  if (insecure !== undefined) {
    return {
      available: false,
      reason: "socket-insecure",
      detail: `${insecure} (attempted ${socketPath})`,
    };
  }

  if (!existsSync(socketPath)) {
    return {
      available: false,
      reason: "socket-missing",
      detail:
        `no socket at ${socketPath} — the artifact-memory service is ` +
        "probably not running",
    };
  }

  // Existence is not availability. `existsSync` alone reported an ordinary
  // file as a working service, so four tools were advertised and then failed
  // on first use. This applies the SAME rules the per-request validator does —
  // socket-ness, ownership and mode, up the whole chain — so a tool is only
  // offered when it can actually be called.
  const uid = typeof process.getuid === "function" ? process.getuid() : undefined;
  if (uid === undefined) {
    return {
      available: false,
      reason: "unsupported-platform",
      detail: `this host exposes no process.getuid(), so ${socketPath} ownership cannot be verified`,
    };
  }
  for (const current of socketChainPaths(socketPath)) {
    let stat: Stats;
    try {
      stat = lstatSync(current);
    } catch {
      return {
        available: false,
        reason: "socket-missing",
        detail: `${current} is unreadable while checking ${socketPath}`,
      };
    }
    const reason = socketLinkReason(current, socketPath, stat, uid);
    if (reason !== undefined) {
      return {
        available: false,
        reason: "socket-insecure",
        detail: `${reason} (attempted ${socketPath})`,
      };
    }
  }

  return { available: true, socketPath };
}

function currentUid(): number {
  if (typeof process.getuid !== "function") {
    throw new ArtifactMemoryServiceError(SOCKET_ERROR);
  }
  return process.getuid();
}

function isMissingSocket(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    (error as { code?: unknown }).code === "ENOENT"
  );
}

/**
 * Reject a socket if another account could replace its endpoint or any
 * caller-controlled part of its path. Processes with the same Unix UID are
 * one trust principal; that is the strongest portable boundary available to a
 * local stdio adapter.
 */
/**
 * Every path from the home directory down to the socket, in order.
 *
 * Shared so the startup probe and the per-request validator walk the SAME
 * chain. They used to walk different ones: the probe checked path syntax and
 * `existsSync`, which an ordinary file satisfies, so four tools were advertised
 * as available and then failed on first use against a path that was never a
 * socket.
 */
function socketChainPaths(socketPath: string): string[] {
  const home = normalize(homedir());
  const components = socketPath
    .slice(home.length + 1)
    .split(sep)
    .filter(Boolean);
  const paths: string[] = [];
  let current = home;
  for (const component of ["", ...components]) {
    if (component) current = join(current, component);
    paths.push(current);
  }
  return paths;
}

/**
 * The security rule for ONE link in that chain, as a reason or undefined.
 *
 * The single definition of "secure socket" for this adapter. Ownership and mode
 * are the whole point — existence proves nothing, since any regular file exists
 * just as convincingly as a live socket.
 */
function socketLinkReason(
  current: string,
  socketPath: string,
  stat: Stats,
  uid: number,
): string | undefined {
  if (stat.isSymbolicLink()) return `${current} is a symlink`;
  if (current !== socketPath) {
    if (!stat.isDirectory()) return `${current} is not a directory`;
    if (stat.uid !== uid) return `${current} is owned by uid ${stat.uid}, not ${uid}`;
    if ((stat.mode & 0o022) !== 0) return `${current} is group- or world-writable`;
  }
  if (current === dirname(socketPath)) {
    if ((stat.mode & 0o077) !== 0) return `${current} is accessible to other accounts`;
    if ((stat.mode & 0o700) !== 0o700) return `${current} is not fully owner-accessible`;
  }
  if (current === socketPath) {
    if (!stat.isSocket()) return `${current} exists but is not a socket`;
    if (stat.uid !== uid) return `${current} is owned by uid ${stat.uid}, not ${uid}`;
    if ((stat.mode & 0o077) !== 0) return `${current} is accessible to other accounts`;
    if ((stat.mode & 0o600) !== 0o600) return `${current} is not owner read/write`;
  }
  return undefined;
}

async function validateSecureSocket(socketPath: string): Promise<void> {
  const uid = currentUid();
  try {
    for (const current of socketChainPaths(socketPath)) {
      const stat = await lstat(current);
      if (socketLinkReason(current, socketPath, stat, uid) !== undefined) {
        throw new ArtifactMemoryServiceError(SOCKET_ERROR);
      }
    }
  } catch (error) {
    if (error instanceof ArtifactMemoryServiceError) throw error;
    if (isMissingSocket(error)) {
      throw new ArtifactMemoryServiceError(UNAVAILABLE_ERROR);
    }
    throw new ArtifactMemoryServiceError(SOCKET_ERROR);
  }
}

/**
 * Reject a response this adapter build cannot safely parse.
 *
 * Two upgrade shapes are fenced here: an explicit 426 (the service refused our
 * protocol) and a served response announcing a protocol other than ours (an
 * older service that answered us without checking). A header-less response is
 * a pre-fencing service and stays acceptable until both sides have shipped.
 */
function assertServedProtocol(response: ArtifactMemoryResponse): void {
  const served = response.headers.get(PROTOCOL_HEADER);
  if (served === null) {
    if (response.status === UPGRADE_REQUIRED_STATUS) {
      throw new ArtifactMemoryServiceError(UPGRADE_ERROR);
    }
    return;
  }
  const trimmed = served.trim();
  if (
    response.status === UPGRADE_REQUIRED_STATUS ||
    !/^[0-9]{1,9}$/.test(trimmed) ||
    Number(trimmed) !== PROTOCOL_VERSION
  ) {
    throw new ArtifactMemoryServiceError(UPGRADE_ERROR);
  }
}

async function readBoundedJson(
  response: ArtifactMemoryResponse,
): Promise<Record<string, unknown>> {
  if (response.status >= 300 && response.status < 400) {
    await response.body?.cancel().catch(() => undefined);
    throw new ArtifactMemoryServiceError(
      "artifact memory service redirect rejected",
    );
  }
  // Version-fence before the generic status and shape checks so an upgraded
  // service yields "restart this session", not an opaque HTTP-code error.
  try {
    assertServedProtocol(response);
  } catch (error) {
    await response.body?.cancel().catch(() => undefined);
    throw error;
  }
  if (response.status !== 200) {
    await response.body?.cancel().catch(() => undefined);
    throw new ArtifactMemoryServiceError(
      `artifact memory service rejected request (HTTP ${response.status})`,
    );
  }

  const contentType = response.headers.get("content-type");
  if (contentType?.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
    await response.body?.cancel().catch(() => undefined);
    throw new ArtifactMemoryServiceError(
      "artifact memory service returned a non-JSON response",
    );
  }

  const contentLength = response.headers.get("content-length");
  if (contentLength !== null) {
    if (!/^[0-9]+$/.test(contentLength) || Number(contentLength) > MAX_JSON_BYTES) {
      await response.body?.cancel().catch(() => undefined);
      throw new ArtifactMemoryServiceError(
        "artifact memory service response exceeds 2 MiB",
      );
    }
  }
  if (response.body === null) {
    throw new ArtifactMemoryServiceError(
      "artifact memory service returned invalid JSON",
    );
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_JSON_BYTES) {
        await reader.cancel().catch(() => undefined);
        throw new ArtifactMemoryServiceError(
          "artifact memory service response exceeds 2 MiB",
        );
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  let payload: unknown;
  try {
    const raw = Buffer.concat(chunks, total);
    payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
  } catch {
    throw new ArtifactMemoryServiceError(
      "artifact memory service returned invalid JSON",
    );
  }
  if (
    payload === null ||
    typeof payload !== "object" ||
    Array.isArray(payload)
  ) {
    throw new ArtifactMemoryServiceError(
      "artifact memory service response must be a JSON object",
    );
  }
  return payload as Record<string, unknown>;
}

export function createArtifactMemoryTools(
  config: ArtifactMemoryConfig = {},
  requester: ArtifactMemoryRequester = defaultRequester,
) {
  const socketPath = absoluteSocketPath(
    config.socketPath ?? DEFAULT_SOCKET_PATH,
  );

  async function invoke(
    route: string,
    payload: Record<string, unknown>,
  ): Promise<string> {
    if (!ALLOWED_ROUTES.has(route)) {
      throw new Error("artifact memory service route is not allowed");
    }
    const encoded = JSON.stringify(payload);
    if (Buffer.byteLength(encoded, "utf8") > MAX_JSON_BYTES) {
      throw new Error("artifact memory service request exceeds 2 MiB");
    }
    await validateSecureSocket(socketPath);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    timer.unref();
    try {
      let response: ArtifactMemoryResponse;
      try {
        response = await requester({
          socketPath,
          path: route,
          method: "POST",
          body: encoded,
          headers: {
            Accept: "application/json",
            Connection: "close",
            "Content-Length": String(Buffer.byteLength(encoded, "utf8")),
            "Content-Type": "application/json",
            "X-Artifact-Memory-Protocol": String(PROTOCOL_VERSION),
          },
          signal: controller.signal,
        });
      } catch {
        if (controller.signal.aborted) {
          throw new ArtifactMemoryServiceError(
            "artifact memory service request timed out after 30000ms",
          );
        }
        throw new ArtifactMemoryServiceError(UNAVAILABLE_ERROR);
      }
      let result: Record<string, unknown>;
      try {
        result = await readBoundedJson(response);
      } catch (error) {
        if (controller.signal.aborted) {
          throw new ArtifactMemoryServiceError(
            "artifact memory service request timed out after 30000ms",
          );
        }
        if (error instanceof ArtifactMemoryServiceError) throw error;
        throw new ArtifactMemoryServiceError(
          "artifact memory service returned an unreadable response",
        );
      }
      const serialized = JSON.stringify(result);
      if (Buffer.byteLength(serialized, "utf8") > MAX_JSON_BYTES) {
        throw new ArtifactMemoryServiceError(
          "artifact memory service response exceeds 2 MiB",
        );
      }
      return serialized;
    } finally {
      clearTimeout(timer);
    }
  }

  return {
    async artifact_memory_status(): Promise<string> {
      return invoke("/v1/status", {});
    },

    async search_artifacts(args: {
      query?: unknown;
      max_results?: unknown;
      include_history?: unknown;
      project?: unknown;
      artifact_type?: unknown;
      authority_class?: unknown;
      repository?: unknown;
      lifecycle_hint?: unknown;
    }): Promise<string> {
      if (
        args.include_history !== undefined &&
        typeof args.include_history !== "boolean"
      ) {
        throw new Error("include_history must be a boolean");
      }
      if (args.include_history === true) {
        throw new Error(
          "historical artifact snippets are disabled until immutable CAS-backed revision retrieval exists",
        );
      }
      const payload: Record<string, unknown> = {
        query: requireString(args.query, "query"),
        limit: boundedInteger(args.max_results, "max_results", 8, 1, 25),
        include_history: false,
      };
      for (const [field, value, name] of [
        ["project", args.project, "project"],
        ["artifact_type", args.artifact_type, "artifact_type"],
        ["authority_class", args.authority_class, "authority_class"],
        ["repository", args.repository, "repository"],
        ["lifecycle_hint", args.lifecycle_hint, "lifecycle_hint"],
      ] as const) {
        const parsed = optionalString(value, name);
        if (parsed !== undefined) payload[field] = parsed;
      }
      return invoke("/v1/search", payload);
    },

    async get_artifact(args: {
      relative_path?: unknown;
      max_chars?: unknown;
    }): Promise<string> {
      return invoke("/v1/get", {
        relative_path: requireString(
          args.relative_path,
          "relative_path",
          2_000,
        ),
        max_chars: boundedInteger(
          args.max_chars,
          "max_chars",
          12_000,
          256,
          50_000,
        ),
      });
    },

    async query_temporal_facts(args: {
      query?: unknown;
      max_results?: unknown;
      group_id?: unknown;
      include_pilot?: unknown;
    }): Promise<string> {
      if (
        args.include_pilot !== undefined &&
        typeof args.include_pilot !== "boolean"
      ) {
        throw new Error("include_pilot must be a boolean");
      }
      if (args.include_pilot === true) {
        throw new Error(
          "unapproved Graphiti pilot access is disabled until publication and approval controls exist",
        );
      }
      const payload: Record<string, unknown> = {
        query: requireString(args.query, "query"),
        limit: boundedInteger(args.max_results, "max_results", 8, 1, 25),
        include_pilot: false,
      };
      const groupId = optionalString(args.group_id, "group_id");
      if (groupId !== undefined) payload.group_id = groupId;
      return invoke("/v1/facts", payload);
    },
  };
}

import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const UV_BIN = "/home/adam/.local/bin/uv";
const SHARED_STORE = "/home/adam/.omp/ontologies/shared";
const SPAWN_TIMEOUT_MS = 20_000;
const MAX_TEXT_CHARS = 8_000;
const MAX_RESULT_CHARS = 8_000;
const MAX_ARG_STRING_CHARS = 2_000;
const MAX_ARG_ITEMS = 100;
const MAX_ARG_DEPTH = 10;
const MAX_ARG_CHARS = 20_000;
const MAX_CALLS = 200;
const MAX_PACKET_CHARS = 2_000_000;
const MAX_SEEN_KEYS = 5_000;

type CaptureStatus = "completed" | "failed" | "interrupted";

interface CaptureCall {
  tool: string;
  arguments: Record<string, unknown>;
  result: unknown;
  is_error: boolean;
}

interface CapturePacket {
  project_root: string;
  session_id: string;
  turn_id: string;
  question: string;
  final_answer: string;
  status: CaptureStatus;
  calls: CaptureCall[];
}

interface ExtractedTurn {
  turnId: string;
  question: string;
  finalAnswer: string;
  status: CaptureStatus;
  calls: CaptureCall[];
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function truncateText(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  return `${text.slice(0, maxChars)}\n…[truncated ${text.length - maxChars} chars]`;
}

function safeString(value: unknown): string {
  try {
    if (typeof value === "string") return value;
    const json = JSON.stringify(value);
    if (typeof json === "string") return json;
  } catch {
    // fall through to String()
  }
  try {
    return String(value);
  } catch {
    return "[unserializable]";
  }
}

function boundValue(value: unknown, depth: number, seen: WeakSet<object>): unknown {
  if (typeof value === "string") return truncateText(value, MAX_ARG_STRING_CHARS);
  if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
  if (typeof value === "boolean" || value === null) return value;
  if (value === undefined) return "undefined";
  if (typeof value === "bigint") return String(value);
  if (typeof value === "function" || typeof value === "symbol") {
    try {
      return truncateText(String(value), MAX_ARG_STRING_CHARS);
    } catch {
      return "[unserializable]";
    }
  }
  if (typeof value === "object" && value !== null) {
    if (seen.has(value)) return "[circular]";
    if (value instanceof Date) {
      try {
        return value.toISOString();
      } catch {
        return "[invalid date]";
      }
    }
    if (depth >= MAX_ARG_DEPTH) {
      return truncateText(safeString(value), MAX_ARG_STRING_CHARS);
    }
    seen.add(value);
    try {
      if (Array.isArray(value)) {
        const items = value.slice(0, MAX_ARG_ITEMS).map((item) => boundValue(item, depth + 1, seen));
        if (value.length > MAX_ARG_ITEMS) {
          items.push(`…[truncated ${value.length - MAX_ARG_ITEMS} items]`);
        }
        return items;
      }
      if (isRecord(value)) {
        const entries = Object.entries(value).slice(0, MAX_ARG_ITEMS);
        const out: Record<string, unknown> = {};
        for (const [key, item] of entries) {
          out[key] = boundValue(item, depth + 1, seen);
        }
        if (Object.keys(value).length > MAX_ARG_ITEMS) {
          out._truncated = `dropped ${Object.keys(value).length - MAX_ARG_ITEMS} keys`;
        }
        return out;
      }
      return truncateText(safeString(value), MAX_ARG_STRING_CHARS);
    } finally {
      seen.delete(value);
    }
  }
  return truncateText(safeString(value), MAX_ARG_STRING_CHARS);
}

export function sanitizeArguments(args: unknown): Record<string, unknown> {
  if (!isRecord(args)) return {};
  const bounded = boundValue(args, 0, new WeakSet());
  if (!isRecord(bounded)) return {};
  let serialized: string;
  try {
    serialized = JSON.stringify(bounded) ?? "";
  } catch {
    return { _truncated: true, preview: "[unserializable]", summary: "arguments not JSON-serializable" };
  }
  if (serialized.length <= MAX_ARG_CHARS) return bounded;
  return {
    _truncated: true,
    preview: serialized.slice(0, MAX_ARG_CHARS),
    summary: `${serialized.length} chars; preview shows first ${MAX_ARG_CHARS} chars.`,
  };
}

function textBlocksOnly(content: unknown): string[] {
  if (typeof content === "string") return [content];
  if (!Array.isArray(content)) return [];
  const out: string[] = [];
  for (const block of content) {
    if (isRecord(block) && block.type === "text" && typeof block.text === "string") {
      out.push(block.text);
    }
  }
  return out;
}

interface ToolCallSeen {
  id: string;
  name: string;
  args: unknown;
}

export function extractTurn(branch: readonly unknown[]): ExtractedTurn | null {
  if (!Array.isArray(branch) || branch.length === 0) return null;

  let lastUserIndex = -1;
  let turnId = "";
  for (let i = branch.length - 1; i >= 0; i--) {
    const entry = branch[i];
    if (!isRecord(entry) || entry.type !== "message" || !isRecord(entry.message)) continue;
    const message = entry.message;
    if (message.role !== "user") continue;
    if (message.synthetic === true) continue;
    if (message.attribution === "agent") continue;
    if (typeof entry.id !== "string" || entry.id.length === 0) continue;
    lastUserIndex = i;
    turnId = entry.id;
    break;
  }
  if (lastUserIndex < 0 || !turnId) return null;

  const userEntry = branch[lastUserIndex];
  const userMessage = isRecord(userEntry) && isRecord(userEntry.message) ? userEntry.message : null;
  const questionRaw = userMessage ? textBlocksOnly(userMessage.content).join("") : "";
  if (!questionRaw.trim()) return null;
  const question = truncateText(questionRaw.trim(), MAX_TEXT_CHARS);

  let finalAnswer = "";
  let status: CaptureStatus = "completed";
  for (let i = branch.length - 1; i >= lastUserIndex; i--) {
    const entry = branch[i];
    if (!isRecord(entry) || entry.type !== "message" || !isRecord(entry.message)) continue;
    const message = entry.message;
    if (message.role !== "assistant") continue;
    finalAnswer = truncateText(textBlocksOnly(message.content).join(""), MAX_TEXT_CHARS);
    const stopReason = message.stopReason;
    if (stopReason === "error") status = "failed";
    else if (stopReason === "aborted") status = "interrupted";
    else status = "completed";
    break;
  }

  const toolCalls: ToolCallSeen[] = [];
  const resultsById = new Map<string, Record<string, unknown>>();
  for (let i = lastUserIndex; i < branch.length; i++) {
    const entry = branch[i];
    if (!isRecord(entry) || entry.type !== "message" || !isRecord(entry.message)) continue;
    const message = entry.message;
    if (message.role === "assistant" && Array.isArray(message.content)) {
      for (const block of message.content) {
        if (!isRecord(block) || block.type !== "toolCall") continue;
        if (typeof block.id !== "string" || block.id.length === 0) continue;
        if (typeof block.name !== "string" || !block.name.trim()) continue;
        toolCalls.push({ id: block.id, name: block.name.trim().slice(0, 256), args: block.arguments });
      }
    } else if (message.role === "toolResult" && typeof message.toolCallId === "string" && message.toolCallId) {
      resultsById.set(message.toolCallId, message);
    }
  }

  const calls: CaptureCall[] = [];
  for (const call of toolCalls) {
    const resultEntry = resultsById.get(call.id);
    if (!resultEntry) continue;
    const text = textBlocksOnly(resultEntry.content).join("");
    let result: unknown;
    if (text.trim()) {
      result = truncateText(text, MAX_RESULT_CHARS);
    } else if (resultEntry.details !== undefined) {
      result = boundValue(resultEntry.details, 0, new WeakSet());
      if (result === undefined) result = "";
    } else {
      result = "";
    }
    calls.push({
      tool: call.name,
      arguments: sanitizeArguments(call.args),
      result,
      is_error: resultEntry.isError === true,
    });
    if (calls.length >= MAX_CALLS) break;
  }

  return { turnId, question, finalAnswer, status, calls };
}

function getCloneRoot(): string | null {
  try {
    const rawUrl = import.meta.url.split("?")[0];
    const here = realpathSync(fileURLToPath(new URL(rawUrl)));
    const root = path.resolve(path.dirname(here), "..", "..");
    if (!existsSync(path.join(root, "pyproject.toml"))) return null;
    if (!existsSync(path.join(root, "evoontology", "trajectory", "omp_capture.py"))) return null;
    return root;
  } catch {
    return null;
  }
}

interface SpawnOutcome {
  code: number | null;
  signal: string | null;
  stdout: string;
  stderr: string;
}

export function resolveStore(): string {
  const override = process.env.EVO_ONTOLOGY_STORE?.trim();
  if (override && path.isAbsolute(override)) return override;
  return SHARED_STORE;
}

export function storeOverrideInvalid(): boolean {
  const override = process.env.EVO_ONTOLOGY_STORE?.trim();
  return !!override && !path.isAbsolute(override);
}

const ON_DEMAND_CUSTOM_TYPE = "evo-ontology-on-demand";

/**
 * Short conditional advisory injected via `before_agent_start` on every
 * genuine user prompt. The if-and-only-if condition lets the model judge
 * intent — no lexical gate in the hook, so no false-negative classifier
 * can suppress a valid data task, and generic turns never build. It
 * writes no ontology state by itself.
 */
export function buildOnDemandGuidance(projectRoot: string, workspace: string = SHARED_STORE): string {
  const root = projectRoot && projectRoot.trim() ? projectRoot.trim() : "<unknown project root>";
  const store = workspace && workspace.trim() ? workspace.trim() : SHARED_STORE;
  return [
    "[EvoOntology] If and only if this request identifies a data source AND an analytical goal:",
    `check the project lane via evo-semantic \`list_versions\` (workspace \`${store}\`, project_root \`${root}\`);`,
    "if the lane has no active version, autonomously follow the build-ontology skill — ground every claim in observed evidence, run `validate_semantics` before publish, never fabricate; if an active version exists, use it.",
    "Generic coding turns: do not build. This hook writes no ontology state.",
  ].join("\n");
}

function runCapture(cloneRoot: string, store: string, packetJson: string): Promise<SpawnOutcome> {
  const { promise, resolve } = Promise.withResolvers<SpawnOutcome>();
  let settled = false;
  const done = (outcome: SpawnOutcome) => {
    if (settled) return;
    settled = true;
    resolve(outcome);
  };
  let child: ChildProcess;
  const pythonPath = process.env.PYTHONPATH?.trim()
    ? `${cloneRoot}:${process.env.PYTHONPATH.trim()}`
    : cloneRoot;
  try {
    child = spawn(
      UV_BIN,
      [
        "run",
        "--offline",
        "--no-sync",
        "--project",
        cloneRoot,
        "python",
        "-m",
        "evoontology.trajectory.omp_capture",
        "--store",
        store,
      ],
      {
        stdio: ["pipe", "pipe", "pipe"],
        timeout: SPAWN_TIMEOUT_MS,
        cwd: cloneRoot,
        env: { ...process.env, PYTHONPATH: pythonPath },
      },
    );
  } catch (error) {
    done({ code: null, signal: null, stdout: "", stderr: safeString(error) });
    return promise;
  }
  let stdout = "";
  let stderr = "";
  const cap = 65_536;
  child.stdout?.on("data", (chunk) => {
    try {
      stdout += String(chunk);
      if (stdout.length > cap) stdout = stdout.slice(-cap);
    } catch {
      // ignore collector faults; close handler still settles
    }
  });
  child.stderr?.on("data", (chunk) => {
    try {
      stderr += String(chunk);
      if (stderr.length > cap) stderr = stderr.slice(-cap);
    } catch {
      // ignore collector faults; close handler still settles
    }
  });
  child.on("error", (error: unknown) => {
    done({ code: null, signal: null, stdout, stderr: `${stderr}${safeString(error)}`.slice(-cap) });
  });
  child.on("close", (code: number | null, signal: string | null) => {
    done({ code, signal: signal ?? null, stdout, stderr });
  });
  try {
    child.stdin?.on("error", () => {});
    child.stdin?.write(packetJson);
    child.stdin?.end();
  } catch (error) {
    try {
      child.kill();
    } catch {
      // kill is best-effort; close handler still settles
    }
    done({ code: null, signal: null, stdout, stderr: `${stderr}${safeString(error)}`.slice(-cap) });
  }
  return promise;
}

export default function evoCaptureExtension(pi: ExtensionAPI): void {
  pi.setLabel("evo-capture");
  const seen = new Set<string>();

  // Short conditional advisory on every genuine user prompt, evaluated per
  // turn (projects/tasks can change mid-session, so there is intentionally
  // no once-per-session latch and no lexical intent gate — the model judges
  // the if-and-only-if condition, so no false-negative classifier can
  // suppress a valid data task. Generic turns receive the advisory but
  // never build.
  // Returns `{ message }` only: extension messages compose additively across
  // handlers, while `systemPrompt` is a last-writer-wins full replacement —
  // this hook must never override other extensions' system prompts. It never
  // calls pi.sendMessage (no recursive turns) and never writes ontology
  // state.
  pi.on("before_agent_start", (event, ctx) => {
    try {
      const prompt = event?.prompt;
      if (typeof prompt !== "string" || !prompt.trim()) return;
      const cwd = ctx?.cwd;
      if (typeof cwd !== "string" || !cwd) return;
      return {
        message: {
          customType: ON_DEMAND_CUSTOM_TYPE,
          content: buildOnDemandGuidance(cwd, resolveStore()),
          display: true,
          attribution: "agent" as const,
        },
      };
    } catch (error) {
      try {
        pi.logger.debug("[evo-capture] on-demand nudge skipped", {
          error: safeString(error).slice(0, 500),
        });
      } catch {
        // logging must never break the turn
      }
      return;
    }
  });

  pi.on("agent_end", async (event, ctx) => {
    try {
      let addedKey: string | null = null;
      if (event?.willContinue === true) return;
      const sessionManager = ctx?.sessionManager;
      const branch = sessionManager?.getBranch?.();
      if (!Array.isArray(branch) || branch.length === 0) return;
      const cwd = ctx?.cwd;
      if (typeof cwd !== "string" || !cwd) return;
      let sessionId = "";
      try {
        sessionId = sessionManager.getSessionId();
      } catch {
        return;
      }
      if (typeof sessionId !== "string" || !sessionId) return;

      const turn = extractTurn(branch);
      if (!turn) return;

      const dedupKey = `${sessionId}:${turn.turnId}`;
      if (seen.has(dedupKey)) return;
      if (seen.size >= MAX_SEEN_KEYS) seen.clear();
      // Optimistic add guards concurrent duplicate spawns; every failure
      // path below deletes the key so a retry can re-record (the recorder
      // CLI is idempotent by project + session + turn).
      seen.add(dedupKey);
      addedKey = dedupKey;

      const cloneRoot = getCloneRoot();
      if (!cloneRoot) {
        try {
          pi.logger.error("[evo-capture] clone root not found from extension path; skipping turn", {
            sessionId,
            turnId: turn.turnId,
          });
        } catch {
          // logging must never break the turn
        }
        seen.delete(dedupKey);
        return;
      }

      const packet: CapturePacket = {
        project_root: cwd,
        session_id: sessionId,
        turn_id: turn.turnId,
        question: turn.question,
        final_answer: turn.finalAnswer,
        status: turn.status,
        calls: turn.calls,
      };
      let packetJson: string;
      try {
        packetJson = JSON.stringify(packet);
      } catch (error) {
        try {
          pi.logger.error("[evo-capture] packet not JSON-serializable; skipping turn", {
            sessionId,
            turnId: turn.turnId,
            error: safeString(error).slice(0, 500),
          });
        } catch {
          // logging must never break the turn
        }
        seen.delete(dedupKey);
        return;
      }
      if (packetJson.length > MAX_PACKET_CHARS) {
        const trimmed: CapturePacket = { ...packet, calls: packet.calls.slice(0, 50) };
        try {
          packetJson = JSON.stringify(trimmed);
        } catch {
          seen.delete(dedupKey);
          return;
        }
        if (packetJson.length > MAX_PACKET_CHARS) {
          try {
            pi.logger.warn("[evo-capture] packet exceeds size cap; skipping turn", {
              sessionId,
              turnId: turn.turnId,
              chars: packetJson.length,
            });
          } catch {
            // logging must never break the turn
          }
          seen.delete(dedupKey);
          return;
        }
      }

      const store = resolveStore();
      if (storeOverrideInvalid()) {
        try {
          pi.logger.warn("[evo-capture] ignoring non-absolute EVO_ONTOLOGY_STORE override", {
            sessionId,
            turnId: turn.turnId,
          });
        } catch {
          // logging must never break the turn
        }
      }
      const outcome = await runCapture(cloneRoot, store, packetJson);
      if (outcome.code !== 0) {
        try {
          pi.logger.error("[evo-capture] capture helper failed", {
            sessionId,
            turnId: turn.turnId,
            code: outcome.code,
            signal: outcome.signal,
            stderr: outcome.stderr.slice(-2000),
          });
        } catch {
          // logging must never break the turn
        }
        seen.delete(dedupKey);
        return;
      }
      try {
        pi.logger.debug("[evo-capture] turn recorded", {
          sessionId,
          turnId: turn.turnId,
          stdout: outcome.stdout.slice(0, 500),
        });
      } catch {
        // logging must never break the turn
      }
    } catch (error) {
      if (addedKey !== null) seen.delete(addedKey);
      try {
        pi.logger.error("[evo-capture] unexpected capture failure", {
          error: safeString(error).slice(0, 1000),
        });
      } catch {
        // logging must never break the turn
      }
    }
  });
}

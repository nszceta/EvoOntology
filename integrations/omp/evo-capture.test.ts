import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import evoCaptureExtension, {
  automationEnabled,
  canDispatchMaintenance,
  extractTurn,
  hasGenuineUserSince,
  hasMaintenanceSince,
  isMaintenanceEntry,
  isPlanModeActive,
  isRecord,
  MAINTENANCE_CUSTOM_TYPE,
  parseAutomationPayload,
  parseClassifierVerdict,
  resolveStore,
  resolveUv,
  sanitizeArguments,
  toAutomationClaim,
} from "./evo-capture";

function userEntry(id: string, text: string, extra?: Record<string, unknown>) {
  return {
    id,
    type: "message",
    message: { role: "user", content: [{ type: "text", text }], ...extra },
  };
}

function assistantEntry(id: string, text: string, extra?: Record<string, unknown>) {
  return {
    id,
    type: "message",
    message: { role: "assistant", content: [{ type: "text", text }], ...extra },
  };
}

describe("extractTurn", () => {
  test("extracts the last genuine user turn with final answer", () => {
    const turn = extractTurn([
      userEntry("t1", "First question"),
      assistantEntry("a1", "First answer"),
      userEntry("t2", "Analyze sales.csv trends"),
      assistantEntry("a2", "Done"),
    ]);
    expect(turn?.turnId).toBe("t2");
    expect(turn?.question).toBe("Analyze sales.csv trends");
    expect(turn?.finalAnswer).toBe("Done");
    expect(turn?.status).toBe("completed");
    expect(turn?.calls).toEqual([]);
  });

  test("skips synthetic and agent-attributed user messages", () => {
    const turn = extractTurn([
      userEntry("t1", "Genuine question"),
      assistantEntry("a1", "Answer"),
      userEntry("t2", "Synthetic follow-up", { synthetic: true }),
      userEntry("t3", "Agent echo", { attribution: "agent" }),
      assistantEntry("a2", "Later answer"),
    ]);
    expect(turn?.turnId).toBe("t1");
    expect(turn?.question).toBe("Genuine question");
    expect(turn?.finalAnswer).toBe("Later answer");
  });

  test("returns null without a genuine non-empty user turn", () => {
    expect(extractTurn([])).toBeNull();
    expect(extractTurn([assistantEntry("a1", "Orphan answer")])).toBeNull();
    expect(extractTurn([userEntry("t1", "   ")])).toBeNull();
    expect(extractTurn([userEntry("t1", "Synthetic only", { synthetic: true })])).toBeNull();
  });

  test("maps stop reasons to failed and interrupted", () => {
    const failed = extractTurn([
      userEntry("t1", "Question"),
      assistantEntry("a1", "Boom", { stopReason: "error" }),
    ]);
    expect(failed?.status).toBe("failed");
    const interrupted = extractTurn([
      userEntry("t1", "Question"),
      assistantEntry("a1", "Partial", { stopReason: "aborted" }),
    ]);
    expect(interrupted?.status).toBe("interrupted");
  });

  test("pairs tool calls with results and drops unpaired calls", () => {
    const turn = extractTurn([
      userEntry("t1", "Question"),
      {
        id: "a1",
        type: "message",
        message: {
          role: "assistant",
          content: [
            {
              type: "toolCall",
              id: "c1",
              name: "read",
              arguments: { path: "sales.csv" },
            },
            { type: "toolCall", id: "c2", name: "bash", arguments: { command: "ls" } },
          ],
        },
      },
      {
        id: "r1",
        type: "message",
        message: {
          role: "toolResult",
          toolCallId: "c1",
          content: [{ type: "text", text: "a,b\n1,2" }],
          isError: false,
        },
      },
      {
        id: "r2",
        type: "message",
        message: {
          role: "toolResult",
          toolCallId: "missing",
          content: [{ type: "text", text: "orphan" }],
          isError: true,
        },
      },
      assistantEntry("a2", "Done"),
    ]);
    expect(turn?.calls).toHaveLength(1);
    expect(turn?.calls[0]?.tool).toBe("read");
    expect(turn?.calls[0]?.arguments).toEqual({ path: "sales.csv" });
    expect(turn?.calls[0]?.result).toBe("a,b\n1,2");
    expect(turn?.calls[0]?.is_error).toBe(false);
  });

  test("truncates overlong question and answer with a marker", () => {
    const long = "x".repeat(9000);
    const turn = extractTurn([userEntry("t1", long), assistantEntry("a1", long)]);
    expect(turn?.question).toContain("[truncated");
    expect(turn?.finalAnswer).toContain("[truncated");
    expect((turn?.question ?? "").length).toBeLessThan(9000);
  });
});

describe("sanitizeArguments", () => {
  test("bounds circular input without throwing", () => {
    const args: Record<string, unknown> = { path: "sales.csv" };
    args["self"] = args;
    expect(sanitizeArguments(args)).toEqual({ path: "sales.csv", self: "[circular]" });
  });

  test("returns empty record for non-record input", () => {
    expect(sanitizeArguments("nope")).toEqual({});
    expect(sanitizeArguments(null)).toEqual({});
  });
});

interface HookMessage {
  customType: string;
  content: string;
  display: boolean;
  attribution: string;
}

interface HookResult {
  message?: HookMessage;
}

describe("parseClassifierVerdict", () => {
  test("accepts bare YES/NO case-insensitively with optional period", () => {
    expect(parseClassifierVerdict("YES")).toBe("yes");
    expect(parseClassifierVerdict("  yes. ")).toBe("yes");
    expect(parseClassifierVerdict("Yes")).toBe("yes");
    expect(parseClassifierVerdict("NO")).toBe("no");
    expect(parseClassifierVerdict("  no. ")).toBe("no");
    expect(parseClassifierVerdict("nO")).toBe("no");
  });

  test("rejects prose, hedged, and non-string replies as unknown", () => {
    expect(parseClassifierVerdict("YES, it has both")).toBe("unknown");
    expect(parseClassifierVerdict("NO because no data")).toBe("unknown");
    expect(parseClassifierVerdict("YES\nNO")).toBe("unknown");
    expect(parseClassifierVerdict("")).toBe("unknown");
    expect(parseClassifierVerdict("   ")).toBe("unknown");
    expect(parseClassifierVerdict("maybe")).toBe("unknown");
    expect(parseClassifierVerdict(undefined)).toBe("unknown");
    expect(parseClassifierVerdict(null)).toBe("unknown");
    expect(parseClassifierVerdict(42)).toBe("unknown");
    expect(parseClassifierVerdict({ verdict: "yes" })).toBe("unknown");
  });
});

describe("before_agent_start tiny gate", () => {
  interface Mount {
    fire: (prompt: string, cwd?: string) => Promise<HookResult | undefined>;
    creates: () => number;
    debugLogs: () => unknown[][];
  }

  function mount(replyText: string, hooks?: {
    model?: unknown;
    failCreate?: boolean;
    failTurn?: boolean;
    stopReason?: string;
  }): Mount {
    const model = hooks && "model" in hooks ? hooks.model : { id: "tiny" };
    const handlers: Record<string, (event: unknown, ctx: unknown) => Promise<unknown>> = {};
    const debugLogs: unknown[][] = [];
    let creates = 0;
    const sdk = {
      createAgentSession: async () => {
        creates += 1;
        if (hooks?.failCreate) throw new Error("sdk unavailable");
        return {
          session: {
            runEphemeralTurn: async () => {
              if (hooks?.failTurn) throw new Error("inference failed");
              return { replyText, assistantMessage: { stopReason: hooks?.stopReason ?? "stop" } };
            },
            dispose: async () => {},
          },
        };
      },
      SessionManager: { inMemory: () => ({}) },
    };
    const stubPi = {
      setLabel: () => {},
      on: (event: string, handler: (event: unknown, ctx: unknown) => Promise<unknown>) => {
        handlers[event] = handler;
      },
      logger: {
        debug: (...args: unknown[]) => {
          debugLogs.push(args);
        },
        error: () => {},
        warn: () => {},
        info: () => {},
      },
      pi: sdk,
    };
    evoCaptureExtension(stubPi as unknown as ExtensionAPI);
    const fire = async (prompt: string, cwd = "/repo") => {
      const ctx = {
        cwd,
        models: {
          resolve: () => model,
        },
        modelRegistry: { id: "registry" },
      } as unknown as ExtensionContext;
      const handler = handlers["before_agent_start"];
      if (!handler) throw new Error("before_agent_start handler not registered");
      const result = await handler({ type: "before_agent_start", prompt, systemPrompt: [] }, ctx);
      return result as HookResult | undefined;
    };
    return {
      fire,
      creates: () => creates,
      debugLogs: () => debugLogs,
    };
  }

  test("generic turn returns no message", async () => {
    const gate = mount("NO");
    const result = await gate.fire("fix the typo in README");
    expect(result).toBeUndefined();
    expect(gate.creates()).toBe(1);
  });

  test("retry replays reuse one inference instead of rerunning the model", async () => {
    const gate = mount("NO");
    const prompt = "fix the typo in README";
    await gate.fire(prompt);
    await gate.fire(prompt);
    await gate.fire(prompt);
    expect(gate.creates()).toBe(1);
    const other = mount("NO");
    await other.fire("first prompt");
    await other.fire("second prompt");
    expect(other.creates()).toBe(2);
  });

  test("verdicts do not leak across projects", async () => {
    const gate = mount("NO");
    await gate.fire("same prompt", "/repo-a");
    await gate.fire("same prompt", "/repo-b");
    expect(gate.creates()).toBe(2);
    await gate.fire("same prompt", "/repo-a");
    expect(gate.creates()).toBe(2);
  });

  test("empty prompt returns nothing without running inference", async () => {
    const gate = mount("YES");
    expect(await gate.fire("   ")).toBeUndefined();
    expect(gate.creates()).toBe(0);
  });

});

describe("maintenance boundary", () => {
  function maintenanceEntry(id: string) {
    return { id, type: "custom", customType: MAINTENANCE_CUSTOM_TYPE, content: "job job-1" };
  }

  test("post-maintenance branch with no fresh user captures nothing", () => {
    const turn = extractTurn([
      userEntry("t1", "Analyze sales.csv trends"),
      assistantEntry("a1", "Done"),
      maintenanceEntry("m1"),
      assistantEntry("a2", "Maintenance outcome summary"),
    ]);
    expect(turn).toBeNull();
  });

  test("fresh user after the boundary is captured normally", () => {
    const turn = extractTurn([
      userEntry("t1", "Analyze sales.csv trends"),
      assistantEntry("a1", "Done"),
      maintenanceEntry("m1"),
      assistantEntry("a2", "Maintenance outcome summary"),
      userEntry("t2", "Now fix the typo in README"),
      assistantEntry("a3", "Fixed"),
    ]);
    expect(turn?.turnId).toBe("t2");
    expect(turn?.question).toBe("Now fix the typo in README");
  });

  test("on-demand advisory entries are not boundaries", () => {
    const turn = extractTurn([
      userEntry("t1", "Analyze sales.csv trends"),
      { id: "h1", type: "custom", customType: "evo-ontology-on-demand", content: "advisory" },
      assistantEntry("a1", "Done"),
    ]);
    expect(turn?.turnId).toBe("t1");
  });

  test("boundary helpers recognize maintenance entries only", () => {
    const maint = maintenanceEntry("m1");
    expect(isMaintenanceEntry(maint)).toBe(true);
    expect(
      isMaintenanceEntry({ id: "m2", type: "message", message: { customType: MAINTENANCE_CUSTOM_TYPE } }),
    ).toBe(true);
    expect(isMaintenanceEntry(userEntry("t1", "hi"))).toBe(false);
    expect(isMaintenanceEntry(null)).toBe(false);
    const branch = [userEntry("t1", "hi"), maint];
    expect(hasMaintenanceSince(branch, 0)).toBe(true);
    expect(hasMaintenanceSince(branch, 2)).toBe(false);
    expect(hasGenuineUserSince(branch, 0)).toBe(true);
    expect(hasGenuineUserSince(branch, 1)).toBe(false);
    expect(
      hasGenuineUserSince([userEntry("t9", "echo", { attribution: "agent" })], 0),
    ).toBe(false);
  });
});

describe("canDispatchMaintenance", () => {
  function guards(overrides?: Record<string, unknown>) {
    return {
      automationEnabled: true,
      isIdle: true,
      hasPendingMessages: false,
      userTurnActive: false,
      planModeActive: false,
      hasActiveJob: false,
      cooldownUntil: 0,
      uncertainLeaseUntil: 0,
      now: 1_000_000,
      ...overrides,
    };
  }

  test("dispatches when every guard agrees", () => {
    expect(canDispatchMaintenance(guards()).ok).toBe(true);
  });

  test("each guard blocks independently", () => {
    expect(canDispatchMaintenance(guards({ automationEnabled: false })).ok).toBe(false);
    expect(canDispatchMaintenance(guards({ hasActiveJob: true })).ok).toBe(false);
    expect(canDispatchMaintenance(guards({ isIdle: false })).ok).toBe(false);
    expect(canDispatchMaintenance(guards({ hasPendingMessages: true })).ok).toBe(false);
    expect(canDispatchMaintenance(guards({ userTurnActive: true })).ok).toBe(false);
    expect(canDispatchMaintenance(guards({ planModeActive: true })).ok).toBe(false);
    expect(canDispatchMaintenance(guards({ cooldownUntil: 2_000_000 })).ok).toBe(false);
    expect(canDispatchMaintenance(guards({ cooldownUntil: 500_000 })).ok).toBe(true);
    expect(canDispatchMaintenance(guards({ uncertainLeaseUntil: 2_000_000 })).ok).toBe(false);
  });
});

describe("isPlanModeActive", () => {
  function planCtx(branch: unknown[]): ExtensionContext {
    return { sessionManager: { getBranch: () => branch } } as unknown as ExtensionContext;
  }

  function modeEntry(id: string, mode: string) {
    return { id, type: "mode_change", mode, data: { planFilePath: "local://PLAN.md" } };
  }

  test("follows the newest persisted mode change", () => {
    expect(isPlanModeActive(undefined)).toBe(false);
    expect(isPlanModeActive(planCtx([]))).toBe(false);
    expect(isPlanModeActive(planCtx([userEntry("t1", "hi")]))).toBe(false);
    expect(isPlanModeActive(planCtx([modeEntry("e1", "plan")]))).toBe(true);
    expect(isPlanModeActive(planCtx([modeEntry("e1", "plan_paused")]))).toBe(true);
    expect(isPlanModeActive(planCtx([modeEntry("e1", "plan"), modeEntry("e2", "none")]))).toBe(false);
    expect(isPlanModeActive(planCtx([modeEntry("e2", "none"), modeEntry("e1", "plan")]))).toBe(true);
    expect(isPlanModeActive(planCtx([modeEntry("e1", "vibe")]))).toBe(false);
  });
});

describe("automation protocol parsing", () => {
  function fullJob(overrides?: Record<string, unknown>): Record<string, unknown> {
    return {
      id: "job-1",
      kind: "build",
      owner: "sess-1",
      lease_token: "tok-1",
      expires_at: "2030-01-01T00:00:00Z",
      max_rounds: 2,
      timeout_seconds: 600,
      seed: { question: "Cluster failures", source_ref: "sess-1/t0" },
      ...overrides,
    };
  }

  test("accepts a full claimed job with actual budgets", () => {
    const claim = toAutomationClaim({ status: "claimed", job: fullJob() });
    expect(claim?.status).toBe("claimed");
    if (claim?.status !== "claimed") throw new Error("expected claimed");
    expect(claim.job.seed?.question).toBe("Cluster failures");
    expect(claim.job.max_rounds).toBe(2);
    expect(claim.job.timeout_seconds).toBe(600);
  });

  test("rejects claims without lease identity", () => {
    expect(toAutomationClaim({ status: "claimed", job: { id: "job-1", kind: "build", owner: "sess-1" } })).toBeNull();
    expect(
      toAutomationClaim({ status: "claimed", job: { id: "job-1", kind: "chart", owner: "s", lease_token: "t" } }),
    ).toBeNull();
    expect(toAutomationClaim({ status: "migrating" })).toBeNull();
    expect(toAutomationClaim(null)).toBeNull();
  });

  test("rejects claims missing coordinator budgets instead of defaulting", () => {
    const noRounds = fullJob();
    delete noRounds.max_rounds;
    expect(toAutomationClaim({ status: "claimed", job: noRounds })).toBeNull();
    const noTimeout = fullJob();
    delete noTimeout.timeout_seconds;
    expect(toAutomationClaim({ status: "claimed", job: noTimeout })).toBeNull();
    expect(toAutomationClaim({ status: "claimed", job: fullJob({ max_rounds: "two" }) })).toBeNull();
  });

  test("resume requires the frozen start cutoff, null preserved", () => {
    const base = fullJob({ kind: "resume", run_id: "run-1" });
    delete base.seed;
    expect(toAutomationClaim({ status: "claimed", job: base })).toBeNull();
    const emptyBatch = toAutomationClaim({ status: "claimed", job: { ...base, trajectory_checkpoint: null } });
    expect(emptyBatch?.status).toBe("claimed");
    if (emptyBatch?.status !== "claimed") throw new Error("expected claimed");
    expect(emptyBatch.job.trajectory_checkpoint).toBeNull();
    const cut = toAutomationClaim({ status: "claimed", job: { ...base, trajectory_checkpoint: "traj-9" } });
    if (cut?.status !== "claimed") throw new Error("expected claimed");
    expect(cut.job.trajectory_checkpoint).toBe("traj-9");
  });

  test("passes non-claimed statuses through with string fields only", () => {
    const busy = toAutomationClaim({ status: "busy", reason: "owned", next_check_at: "2030-01-01T00:00:00Z" });
    expect(busy).toEqual({ status: "busy", reason: "owned", next_check_at: "2030-01-01T00:00:00Z" });
  });

  test("parses exact stdout JSON and rejects the rest", () => {
    expect(parseAutomationPayload('{"status":"idle"}')).toEqual({ status: "idle" });
    expect(parseAutomationPayload("   ")).toBeNull();
    expect(parseAutomationPayload("not json")).toBeNull();
    expect(parseAutomationPayload("[1,2]")).toBeNull();
    expect(parseAutomationPayload('{"status":"idle"}\n{"status":"busy"}')).toBeNull();
  });
});

describe("portable resolution", () => {
  const ENV_KEYS = ["EVO_ONTOLOGY_UV", "EVO_ONTOLOGY_STORE", "EVO_ONTOLOGY_AUTOMATION"];
  let saved: Record<string, string | undefined> = {};

  beforeEach(() => {
    saved = {};
    for (const key of ENV_KEYS) saved[key] = process.env[key];
    for (const key of ENV_KEYS) delete process.env[key];
  });

  afterEach(() => {
    for (const key of ENV_KEYS) {
      const value = saved[key];
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  test("store defaults to the runtime home directory", () => {
    expect(resolveStore()).toBe(join(homedir(), ".omp", "ontologies", "shared"));
  });

  test("absolute env store wins, relative is ignored, scoped default fills the gap", () => {
    process.env.EVO_ONTOLOGY_STORE = "/data/shared";
    expect(resolveStore("/scoped/store")).toBe("/data/shared");
    process.env.EVO_ONTOLOGY_STORE = "relative/path";
    expect(resolveStore()).toBe(join(homedir(), ".omp", "ontologies", "shared"));
    delete process.env.EVO_ONTOLOGY_STORE;
    expect(resolveStore("/scoped/store")).toBe("/scoped/store");
    expect(resolveStore("relative/scoped")).toBe(join(homedir(), ".omp", "ontologies", "shared"));
  });

  test("uv resolves via env, then scoped default, then PATH", () => {
    expect(resolveUv()).toBe("uv");
    expect(resolveUv("/opt/uv")).toBe("/opt/uv");
    process.env.EVO_ONTOLOGY_UV = "/custom/uv";
    expect(resolveUv("/opt/uv")).toBe("/custom/uv");
  });

  test("automationEnabled honors the opt-out", () => {
    expect(automationEnabled()).toBe(true);
    process.env.EVO_ONTOLOGY_AUTOMATION = "0";
    expect(automationEnabled()).toBe(false);
    process.env.EVO_ONTOLOGY_AUTOMATION = "1";
    expect(automationEnabled()).toBe(true);
  });
});

describe("dispatcher integration", () => {
  const EXTRA_ENV = [
    "EVO_TEST_LOG",
    "EVO_TEST_CLAIM",
    "EVO_TEST_SEED",
    "EVO_TEST_HEARTBEAT",
    "EVO_TEST_FINISH",
    "EVO_TEST_RELEASE",
    "EVO_TEST_FINISH_DELAY",
  ];
  const BASE_ENV = ["EVO_ONTOLOGY_UV", "EVO_ONTOLOGY_STORE", "EVO_ONTOLOGY_AUTOMATION", ...EXTRA_ENV];
  let saved: Record<string, string | undefined> = {};

  beforeEach(() => {
    saved = {};
    for (const key of BASE_ENV) saved[key] = process.env[key];
  });

  afterEach(() => {
    for (const key of BASE_ENV) {
      const value = saved[key];
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  interface StubTimer {
    cb: (...args: unknown[]) => void;
    ms: number;
  }

  interface Harness {
    handlers: Record<string, (event: unknown, ctx: unknown) => Promise<unknown>>;
    sent: { message: Record<string, unknown>; options: unknown }[];
    ctx: ExtensionContext;
    timers: StubTimer[];
    creates: () => number;
    aborts: () => number;
    setIdle: (value: boolean) => void;
    setPending: (value: boolean) => void;
    setBranch: (branch: unknown[]) => void;
  }

  function writeFakeUv(): { script: string; log: string; dir: string } {
    const dir = mkdtempSync(join(tmpdir(), "evo-fake-uv-"));
    const log = join(dir, "stdin.log");
    writeFileSync(log, "");
    const responses: Record<string, unknown> = {
      claim: {
        status: "claimed",
        job: {
          id: "job-1",
          kind: "build",
          owner: "sess-1",
          lease_token: "tok-1",
          expires_at: "2030-01-01T00:00:00Z",
          max_rounds: 2,
          timeout_seconds: 600,
          seed: { question: "Cluster nightly failures from warehouse logs", source_ref: "sess-1/t0" },
        },
      },
      seed: { status: "seeded", seed_key: "k1", duplicate: false, cleared_cooldown: false },
      heartbeat: { status: "heartbeat", job_id: "job-1", expires_at: "2030-01-01T00:00:00Z" },
      finish: { status: "finished", outcome: "completed", reason: "ok", next_check_at: "2030-01-01T00:00:00Z" },
      release: { status: "released", job_id: "job-1" },
    };
    for (const [name, payload] of Object.entries(responses)) {
      writeFileSync(join(dir, `${name}.json`), JSON.stringify(payload));
    }
    const script = join(dir, "fake-uv");
    writeFileSync(
      script,
      [
        "#!/bin/sh",
        'body="$(cat)"',
        `printf '%s\\n' "$body" >> "${log}"`,
        'case "$body" in',
        `*'"op":"seed"'*) cat "${dir}/seed.json";;`,
        `*'"op":"heartbeat"'*) cat "${dir}/heartbeat.json";;`,
        `*'"op":"finish"'*) sleep "\${EVO_TEST_FINISH_DELAY:-0}"; cat "${dir}/finish.json";;`,
        `*'"op":"release"'*) cat "${dir}/release.json";;`,
        `*) cat "${dir}/claim.json";;`,
        "esac",
        "",
      ].join("\n"),
    );
    chmodSync(script, 0o755);
    return { script, log, dir };
  }

  function readPackets(log: string): Record<string, unknown>[] {
    return readFileSync(log, "utf8")
      .split("\n")
      .filter((line) => line.trim())
      .map((line) => JSON.parse(line) as Record<string, unknown>);
  }

  function mountDispatcher(options?: {
    idle?: boolean;
    pending?: boolean;
    branch?: unknown[];
    sessionId?: string;
    cwd?: string;
    tinyReply?: string;
  }): Harness {
    const handlers: Record<string, (event: unknown, ctx: unknown) => Promise<unknown>> = {};
    const sent: Harness["sent"] = [];
    const timers: StubTimer[] = [];
    let idle = options?.idle ?? true;
    let pending = options?.pending ?? false;
    let branch: unknown[] = options?.branch ?? [];
    const sessionId = options?.sessionId ?? "sess-1";
    const tinyReply = options?.tinyReply ?? "NO";
    let creates = 0;
    let aborts = 0;
    const sdk = {
      createAgentSession: async () => {
        creates += 1;
        return {
          session: {
            runEphemeralTurn: async () => ({ replyText: tinyReply, assistantMessage: { stopReason: "stop" } }),
            dispose: async () => {},
          },
        };
      },
      SessionManager: { inMemory: () => ({}) },
    };
    const stubPi = {
      setLabel: () => {},
      on: (event: string, handler: (event: unknown, ctx: unknown) => Promise<unknown>) => {
        handlers[event] = handler;
      },
      logger: { debug: () => {}, error: () => {}, warn: () => {}, info: () => {} },
      pi: sdk,
      sendMessage: (message: unknown, sendOptions: unknown) => {
        if (isRecord(message)) {
          sent.push({ message, options: sendOptions });
        }
      },
    };
    evoCaptureExtension(stubPi as unknown as ExtensionAPI);
    const ctx = {
      cwd: options?.cwd ?? "/repo",
      isIdle: () => idle,
      hasPendingMessages: () => pending,
      abort: () => {
        aborts += 1;
      },
      shutdown: () => {},
      sessionManager: { getBranch: () => branch, getSessionId: () => sessionId },
      setInterval: (cb: (...args: unknown[]) => void, ms?: number) => {
        const timer: StubTimer = { cb, ms: ms ?? 0 };
        timers.push(timer);
        return timer;
      },
      setTimeout: (cb: (...args: unknown[]) => void, ms?: number) => {
        const timer: StubTimer = { cb, ms: ms ?? 0 };
        timers.push(timer);
        return timer;
      },
      clearTimer: (timer: unknown) => {
        const index = timers.findIndex((candidate) => candidate === timer);
        if (index >= 0) timers.splice(index, 1);
      },
      models: { resolve: () => ({ id: "tiny" }) },
      modelRegistry: { id: "registry" },
    };
    return {
      handlers,
      sent,
      ctx: ctx as unknown as ExtensionContext,
      timers,
      creates: () => creates,
      aborts: () => aborts,
      setIdle: (value: boolean) => {
        idle = value;
      },
      setPending: (value: boolean) => {
        pending = value;
      },
      setBranch: (next: unknown[]) => {
        branch = next;
      },
    };
  }

  function waitForLog(log: string, predicate: (packets: Record<string, unknown>[]) => boolean): Promise<void> {
    const deadline = Date.now() + 10_000;
    return new Promise<void>((resolve, reject) => {
      const poll = () => {
        let packets: Record<string, unknown>[] = [];
        try {
          packets = readPackets(log);
        } catch {
          packets = [];
        }
        if (predicate(packets)) {
          resolve();
          return;
        }
        if (Date.now() >= deadline) {
          reject(new Error("timed out waiting for coordinator traffic"));
          return;
        }
        setImmediate(poll);
      };
      poll();
    });
  }

  function userBranch(): unknown[] {
    return [userEntry("t1", "Analyze sales.csv trends"), assistantEntry("a1", "Done")];
  }

  function maintenanceBranch(): unknown[] {
    return [
      ...userBranch(),
      { id: "m1", type: "custom", customType: MAINTENANCE_CUSTOM_TYPE, content: "job job-1" },
      assistantEntry("a2", "Maintenance outcome summary"),
    ];
  }

  test("starts maintenance over the native follow-up turn when idle", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    process.env.EVO_ONTOLOGY_STORE = join(tmpdir(), "evo-test-store");
    const harness = mountDispatcher({ branch: userBranch() });
    const handler = harness.handlers["agent_end"];
    if (!handler) throw new Error("agent_end handler not registered");
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    const first = harness.sent[0];
    if (!first) throw new Error("expected a maintenance trigger");
    expect(first.message["customType"]).toBe(MAINTENANCE_CUSTOM_TYPE);
    expect(first.message["display"]).toBe(false);
    expect(first.options).toEqual({ deliverAs: "followUp", triggerTurn: true });
    expect(typeof first.message["content"]).toBe("string");
    expect(first.message["content"] as string).toContain("job-1");
    const details: unknown = first.message["details"];
    expect(isRecord(details) ? details["jobId"] : undefined).toBe("job-1");
    expect(isRecord(details) && "leaseToken" in details).toBe(false);
    const packets = readPackets(fake.log);
    const claims = packets.filter((packet) => packet["op"] === "claim");
    expect(claims).toHaveLength(1);
    expect(claims[0]?.["owner"]).toBe("sess-1");
  });

  test("stays put while busy or when messages are pending", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const busy = mountDispatcher({ idle: false, branch: userBranch() });
    const busyHandler = busy.handlers["agent_end"];
    if (!busyHandler) throw new Error("agent_end handler not registered");
    await busyHandler({ type: "agent_end", willContinue: false }, busy.ctx);
    expect(busy.sent).toHaveLength(0);
    expect(readPackets(fake.log).filter((packet) => packet["op"] === "claim")).toHaveLength(0);
    const queued = mountDispatcher({ pending: true, branch: userBranch() });
    const queuedHandler = queued.handlers["agent_end"];
    if (!queuedHandler) throw new Error("agent_end handler not registered");
    await queuedHandler({ type: "agent_end", willContinue: false }, queued.ctx);
    expect(queued.sent).toHaveLength(0);
  });

  test("session start arms a managed idle timer that honors user activity", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const harness = mountDispatcher({ idle: false, branch: userBranch() });
    const start = harness.handlers["session_start"];
    if (!start) throw new Error("session_start handler not registered");
    await start({ type: "session_start" }, harness.ctx);
    expect(harness.timers.map((timer) => timer.ms)).toContain(60_000);
    expect(harness.sent).toHaveLength(0);
    const timer = harness.timers.find((candidate) => candidate.ms === 60_000);
    if (!timer) throw new Error("idle timer not armed");
    const input = harness.handlers["input"];
    if (!input) throw new Error("input handler not registered");
    await input({ type: "input" }, harness.ctx);
    harness.setIdle(true);
    timer.cb();
    expect(harness.sent).toHaveLength(0);
    expect(readPackets(fake.log).filter((packet) => packet["op"] === "claim")).toHaveLength(0);
  });

  test("plan mode on the branch blocks dispatch", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const harness = mountDispatcher({
      branch: [...userBranch(), { id: "e1", type: "mode_change", mode: "plan" }],
    });
    const handler = harness.handlers["agent_end"];
    if (!handler) throw new Error("agent_end handler not registered");
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(0);
    expect(readPackets(fake.log).filter((packet) => packet["op"] === "claim")).toHaveLength(0);
  });

  test("maintenance terminal settles the lease without capture or redispatch", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const harness = mountDispatcher({ branch: userBranch() });
    const handler = harness.handlers["agent_end"];
    if (!handler) throw new Error("agent_end handler not registered");
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    harness.setBranch(maintenanceBranch());
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    const packets = readPackets(fake.log);
    const finishes = packets.filter((packet) => packet["op"] === "finish");
    expect(finishes).toHaveLength(1);
    expect(finishes[0]?.["job_id"]).toBe("job-1");
    expect(finishes[0]?.["owner"]).toBe("sess-1");
    expect(finishes[0]?.["token"]).toBe("tok-1");
    expect("outcome" in (finishes[0] ?? {})).toBe(false);
  });

  test("fresh user after maintenance is captured and the lease still settles", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const harness = mountDispatcher({ branch: userBranch() });
    const handler = harness.handlers["agent_end"];
    if (!handler) throw new Error("agent_end handler not registered");
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    harness.setBranch([
      ...maintenanceBranch(),
      userEntry("t2", "Now fix the typo in README"),
      assistantEntry("a3", "Fixed"),
    ]);
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    const packets = readPackets(fake.log);
    expect(packets.filter((packet) => packet["op"] === "finish")).toHaveLength(1);
    const captures = packets.filter((packet) => packet["turn_id"] === "t2");
    expect(captures).toHaveLength(1);
  });

  test("shutdown releases the lease without starting new turns", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const harness = mountDispatcher({ branch: userBranch() });
    const handler = harness.handlers["agent_end"];
    if (!handler) throw new Error("agent_end handler not registered");
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    const shutdown = harness.handlers["session_shutdown"];
    if (!shutdown) throw new Error("session_shutdown handler not registered");
    await shutdown({ type: "session_shutdown" }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    const packets = readPackets(fake.log);
    const releases = packets.filter((packet) => packet["op"] === "release");
    expect(releases).toHaveLength(1);
    expect(releases[0]?.["job_id"]).toBe("job-1");
    expect(releases[0]?.["token"]).toBe("tok-1");
  });

  test("folded dispatch settles via finish with zero aborts", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const harness = mountDispatcher({ branch: userBranch() });
    const handler = harness.handlers["agent_end"];
    if (!handler) throw new Error("agent_end handler not registered");
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    // The maintenance turn never starts: the branch keeps no marker.
    const timeout = harness.timers.find((candidate) => candidate.ms === 600_000);
    if (!timeout) throw new Error("execution timeout not armed");
    const beats = harness.timers.filter((candidate) => candidate.ms === 180_000);
    expect(beats).toHaveLength(1);
    timeout.cb();
    await waitForLog(fake.log, (packets) => packets.some((packet) => packet["op"] === "finish"));
    const packets = readPackets(fake.log);
    expect(packets.filter((packet) => packet["op"] === "finish")).toHaveLength(1);
    expect(packets.filter((packet) => packet["op"] === "release")).toHaveLength(0);
    expect(harness.timers.filter((candidate) => candidate.ms === 180_000)).toHaveLength(0);
    expect(harness.aborts()).toBe(0);
  });

  test("pending input at timeout settles via finish with zero aborts", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const harness = mountDispatcher({ branch: userBranch() });
    const handler = harness.handlers["agent_end"];
    if (!handler) throw new Error("agent_end handler not registered");
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    const input = harness.handlers["input"];
    if (!input) throw new Error("input handler not registered");
    await input({ type: "input" }, harness.ctx);
    const timeout = harness.timers.find((candidate) => candidate.ms === 600_000);
    if (!timeout) throw new Error("execution timeout not armed");
    timeout.cb();
    await waitForLog(fake.log, (packets) => packets.some((packet) => packet["op"] === "finish"));
    expect(readPackets(fake.log).filter((packet) => packet["op"] === "finish")).toHaveLength(1);
    expect(harness.timers.filter((candidate) => candidate.ms === 180_000)).toHaveLength(0);
    expect(harness.aborts()).toBe(0);
  });

  test("running own turn at timeout aborts once and still settles", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const harness = mountDispatcher({ branch: userBranch() });
    const handler = harness.handlers["agent_end"];
    if (!handler) throw new Error("agent_end handler not registered");
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    harness.setBranch([
      ...userBranch(),
      { id: "m1", type: "custom", customType: MAINTENANCE_CUSTOM_TYPE, content: "job job-1" },
    ]);
    harness.setIdle(false);
    const timeout = harness.timers.find((candidate) => candidate.ms === 600_000);
    if (!timeout) throw new Error("execution timeout not armed");
    timeout.cb();
    await waitForLog(fake.log, (packets) => packets.some((packet) => packet["op"] === "finish"));
    expect(harness.aborts()).toBe(1);
    expect(readPackets(fake.log).filter((packet) => packet["op"] === "finish")).toHaveLength(1);
  });

  test("concurrent terminals share one finish without duplicate dispatch", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    process.env.EVO_TEST_FINISH_DELAY = "2";
    const harness = mountDispatcher({ branch: userBranch() });
    const handler = harness.handlers["agent_end"];
    if (!handler) throw new Error("agent_end handler not registered");
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    harness.setBranch([
      ...userBranch(),
      { id: "m1", type: "custom", customType: MAINTENANCE_CUSTOM_TYPE, content: "job job-1" },
      assistantEntry("a2", "Maintenance outcome summary"),
    ]);
    // A second terminal lands while the first finish is still resolving.
    const first = handler({ type: "agent_end", willContinue: false }, harness.ctx);
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    await first;
    const packets = readPackets(fake.log);
    expect(packets.filter((packet) => packet["op"] === "finish")).toHaveLength(1);
    expect(packets.filter((packet) => packet["op"] === "claim")).toHaveLength(1);
    expect(harness.sent).toHaveLength(1);
  });

  test("failed finish suppresses redispatch across capture and seed wakes", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    writeFileSync(join(fake.dir, "finish.json"), "not json");
    const harness = mountDispatcher({ branch: userBranch(), tinyReply: "YES" });
    const handler = harness.handlers["agent_end"];
    if (!handler) throw new Error("agent_end handler not registered");
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    harness.setBranch([
      ...userBranch(),
      { id: "m1", type: "custom", customType: MAINTENANCE_CUSTOM_TYPE, content: "job job-1" },
      assistantEntry("a2", "Maintenance outcome summary"),
    ]);
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    await waitForLog(fake.log, (packets) => packets.some((packet) => packet["op"] === "finish"));
    // Fresh evidence wakes fire, but the uncertain-lease fence survives them.
    harness.setBranch([
      ...userBranch(),
      { id: "m1", type: "custom", customType: MAINTENANCE_CUSTOM_TYPE, content: "job job-1" },
      assistantEntry("a2", "Maintenance outcome summary"),
      userEntry("t2", "Now fix the typo in README"),
      assistantEntry("a3", "Fixed"),
    ]);
    await handler({ type: "agent_end", willContinue: false }, harness.ctx);
    const gate = harness.handlers["before_agent_start"];
    if (!gate) throw new Error("before_agent_start handler not registered");
    await gate(
      { type: "before_agent_start", prompt: "Cluster nightly failures from warehouse logs", systemPrompt: [] },
      harness.ctx,
    );
    await waitForLog(fake.log, (packets) => packets.some((packet) => packet["op"] === "seed"));
    const packets = readPackets(fake.log);
    expect(packets.filter((packet) => packet["op"] === "finish")).toHaveLength(1);
    expect(packets.filter((packet) => packet["op"] === "claim")).toHaveLength(1);
    expect(harness.sent).toHaveLength(1);
  });

  test("YES verdicts persist a seed while other verdicts seed nothing", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const yes = mountDispatcher({ tinyReply: "YES" });
    const yesGate = yes.handlers["before_agent_start"];
    if (!yesGate) throw new Error("before_agent_start handler not registered");
    const prompt = "Cluster nightly failures from warehouse logs by service";
    await yesGate({ type: "before_agent_start", prompt, systemPrompt: [] }, yes.ctx);
    await waitForLog(fake.log, (packets) => packets.some((packet) => packet["op"] === "seed"));
    const seeds = readPackets(fake.log).filter((packet) => packet["op"] === "seed");
    expect(seeds).toHaveLength(1);
    expect(seeds[0]?.["question"]).toBe(prompt);
    const odd = mountDispatcher({ tinyReply: "maybe" });
    const oddGate = odd.handlers["before_agent_start"];
    if (!oddGate) throw new Error("before_agent_start handler not registered");
    await oddGate({ type: "before_agent_start", prompt, systemPrompt: [] }, odd.ctx);
    // The unknown verdict resolves before the gate returns, so the no-seed
    // decision already happened: this absence check is deterministic.
    expect(readPackets(fake.log).filter((packet) => packet["op"] === "seed")).toHaveLength(1);
  });

  test("maintenance turns are never reclassified and never seed", async () => {
    const fake = writeFakeUv();
    process.env.EVO_ONTOLOGY_UV = fake.script;
    const harness = mountDispatcher({ branch: userBranch(), tinyReply: "YES" });
    const end = harness.handlers["agent_end"];
    if (!end) throw new Error("agent_end handler not registered");
    await end({ type: "agent_end", willContinue: false }, harness.ctx);
    expect(harness.sent).toHaveLength(1);
    const seedCount = () => readPackets(fake.log).filter((packet) => packet["op"] === "seed").length;
    const before = seedCount();
    harness.setBranch(maintenanceBranch());
    const gate = harness.handlers["before_agent_start"];
    if (!gate) throw new Error("before_agent_start handler not registered");
    const result = await gate(
      { type: "before_agent_start", prompt: "Cluster nightly failures from warehouse logs", systemPrompt: [] },
      harness.ctx,
    );
    expect(result).toBeUndefined();
    expect(harness.creates()).toBe(0);
    expect(seedCount()).toBe(before);
  });
});

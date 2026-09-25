import { describe, expect, test } from "bun:test";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import evoCaptureExtension, { extractTurn, parseClassifierVerdict, sanitizeArguments } from "./evo-capture";

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

  test("data task returns a hidden task instruction without user text", async () => {
    const gate = mount("YES");
    const result = await gate.fire("Analyze quux-plugh-9z.csv for churn trends");
    expect(result?.message?.display).toBe(false);
    expect(result?.message?.content).toContain("agent: 'task'");
    expect(result?.message?.content).not.toContain("quux-plugh-9z");
    for (const entry of gate.debugLogs()) {
      expect(JSON.stringify(entry)).not.toContain("quux-plugh-9z");
    }
  });

  test("ambiguous reply fails open with hidden conditional guidance", async () => {
    const gate = mount("YES, probably");
    const result = await gate.fire("Analyze sales.csv");
    expect(result?.message?.display).toBe(false);
    expect(result?.message?.content).toContain("If and only if");
    expect(result?.message?.content).not.toContain("agent: 'task'");
  });

  test("missing @tiny fails open without running inference", async () => {
    const gate = mount("YES", { model: undefined });
    const result = await gate.fire("Analyze sales.csv");
    expect(result?.message?.display).toBe(false);
    expect(result?.message?.content).toContain("If and only if");
    expect(result?.message?.content).not.toContain("agent: 'task'");
    expect(gate.creates()).toBe(0);
  });

  test("classifier failure fails open with hidden conditional guidance", async () => {
    const failedCreate = mount("YES", { failCreate: true });
    const viaCreate = await failedCreate.fire("Analyze sales.csv");
    expect(viaCreate?.message?.display).toBe(false);
    expect(viaCreate?.message?.content).toContain("If and only if");
    expect(viaCreate?.message?.content).not.toContain("agent: 'task'");
    const failedTurn = mount("YES", { failTurn: true });
    const viaTurn = await failedTurn.fire("Analyze sales.csv");
    expect(viaTurn?.message?.display).toBe(false);
    expect(viaTurn?.message?.content).toContain("If and only if");
    expect(viaTurn?.message?.content).not.toContain("agent: 'task'");
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

  test("NO on an overlong prompt fails open instead of staying silent", async () => {
    const gate = mount("NO");
    const prompt = `${"padding ".repeat(400)}quux-plugh-9z tail`;
    expect(prompt.length).toBeGreaterThan(2000);
    const result = await gate.fire(prompt);
    expect(result?.message?.display).toBe(false);
    expect(result?.message?.content).toContain("If and only if");
    expect(result?.message?.content).not.toContain("agent: 'task'");
  });

  test("budget-truncated verdict fails open instead of trusting a cut reply", async () => {
    const gate = mount("NO", { stopReason: "length" });
    const result = await gate.fire("fix the typo in README");
    expect(result?.message?.display).toBe(false);
    expect(result?.message?.content).toContain("If and only if");
    expect(result?.message?.content).not.toContain("agent: 'task'");
  });
});

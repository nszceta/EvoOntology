import { describe, expect, test } from "bun:test";
import { extractTurn, sanitizeArguments } from "./evo-capture";

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

/**
 * Unit tests for the RBAC extension.
 *
 * Simulates the pi.on("tool_call", handler) pattern by capturing handlers
 * registered by the extension factory, then invoking them with synthetic
 * ToolCallEvent objects.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";

// ---------------------------------------------------------------------------
// Mock pi-coding-agent types (only type imports are needed, but the mock
// prevents module resolution errors)
// ---------------------------------------------------------------------------

vi.mock("@mariozechner/pi-coding-agent", () => ({}));

// ---------------------------------------------------------------------------
// Imports
// ---------------------------------------------------------------------------

import { createRBACExtension } from "../../../src/extensions/rbac.js";
import type { RBACConfig } from "../../../src/extensions/rbac.js";

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function createMockPi() {
  const handlers = new Map<string, Function[]>();
  return {
    on: (event: string, handler: Function) => {
      if (!handlers.has(event)) handlers.set(event, []);
      handlers.get(event)!.push(handler);
    },
    trigger: (event: string, data: any) => {
      const fns = handlers.get(event);
      if (!fns || fns.length === 0) return undefined;
      return fns[0](data);
    },
    handlers,
  };
}

/** Create a synthetic tool_call event. */
function toolCall(toolName: string, input: Record<string, unknown> = {}) {
  return { toolName, input };
}

/** Create a bash tool_call event. */
function bashCall(command: string) {
  return toolCall("bash", { command });
}

/** Create a write tool_call event. */
function writeCall(filePath: string) {
  return toolCall("write", { file_path: filePath });
}

/** Create an edit tool_call event. */
function editCall(filePath: string) {
  return toolCall("edit", { file_path: filePath });
}

/** Create a read tool_call event. */
function readCall(filePath?: string) {
  return toolCall("read", filePath ? { file_path: filePath } : {});
}

/** Create a grep tool_call event. */
function grepCall(pattern?: string) {
  return toolCall("grep", pattern ? { pattern } : {});
}

// =========================================================================
// Tests
// =========================================================================

describe("RBAC Extension", () => {
  let pi: ReturnType<typeof createMockPi>;

  beforeEach(() => {
    pi = createMockPi();
  });

  // -----------------------------------------------------------------------
  // Investigator role
  // -----------------------------------------------------------------------

  describe("investigator role", () => {
    const config: RBACConfig = { role: "investigator" };

    it("blocks write tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", writeCall("/tmp/file.ts"));
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("investigator");
      expect(result.reason).toContain("write");
    });

    it("blocks edit tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", editCall("/tmp/file.ts"));
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("investigator");
    });

    it("allows read tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", readCall());
      expect(result).toBeUndefined();
    });

    it("allows grep tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", grepCall("error"));
      expect(result).toBeUndefined();
    });

    it("allows find tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", toolCall("find"));
      expect(result).toBeUndefined();
    });

    it("allows ls tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", toolCall("ls"));
      expect(result).toBeUndefined();
    });

    it("allows safe bash command (git status)", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", bashCall("git status"));
      expect(result).toBeUndefined();
    });

    it("allows safe bash command (cat file)", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", bashCall("cat /tmp/foo.log"));
      expect(result).toBeUndefined();
    });

    it("allows safe bash command (git log)", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", bashCall("git log --oneline -10"));
      expect(result).toBeUndefined();
    });

    it("blocks unsafe bash command (npm install)", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", bashCall("npm install express"));
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("investigator");
    });

    it("blocks unsafe bash command (rm -rf)", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", bashCall("rm -rf /tmp"));
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
    });

    it("blocks unknown tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", toolCall("custom_tool"));
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("unknown tool");
    });
  });

  // -----------------------------------------------------------------------
  // Developer role
  // -----------------------------------------------------------------------

  describe("developer role", () => {
    const sandbox = "/home/agent/Projects/my-repo";
    const config: RBACConfig = {
      role: "developer",
      sandboxPaths: [sandbox],
    };

    it("allows all read tools", () => {
      createRBACExtension(config)(pi as any);
      expect(pi.trigger("tool_call", readCall())).toBeUndefined();
      expect(pi.trigger("tool_call", grepCall())).toBeUndefined();
      expect(pi.trigger("tool_call", toolCall("find"))).toBeUndefined();
      expect(pi.trigger("tool_call", toolCall("ls"))).toBeUndefined();
    });

    it("allows write inside sandbox", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger(
        "tool_call",
        writeCall("/home/agent/Projects/my-repo/src/index.ts"),
      );
      expect(result).toBeUndefined();
    });

    it("allows edit inside sandbox", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger(
        "tool_call",
        editCall("/home/agent/Projects/my-repo/src/index.ts"),
      );
      expect(result).toBeUndefined();
    });

    it("blocks write outside sandbox", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger(
        "tool_call",
        writeCall("/outside/sandbox/file.ts"),
      );
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("developer");
      expect(result.reason).toContain("/outside/sandbox/file.ts");
    });

    it("blocks edit outside sandbox", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger(
        "tool_call",
        editCall("/etc/passwd"),
      );
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
    });

    it("allows bash commands", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", bashCall("npm test"));
      expect(result).toBeUndefined();
    });

    it("blocks bash with redirect outside sandbox", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger(
        "tool_call",
        bashCall("echo 'hack' > /etc/crontab"),
      );
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("outside sandbox");
    });

    it("allows bash with redirect inside sandbox", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger(
        "tool_call",
        bashCall("echo 'ok' > /home/agent/Projects/my-repo/output.txt"),
      );
      expect(result).toBeUndefined();
    });
  });

  // -----------------------------------------------------------------------
  // Reviewer role
  // -----------------------------------------------------------------------

  describe("reviewer role", () => {
    const config: RBACConfig = { role: "reviewer" };

    it("allows read tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", readCall());
      expect(result).toBeUndefined();
    });

    it("allows grep tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", grepCall("pattern"));
      expect(result).toBeUndefined();
    });

    it("blocks bash tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", bashCall("git status"));
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("reviewer");
    });

    it("blocks write tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", writeCall("/tmp/file.ts"));
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
    });

    it("blocks edit tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", editCall("/tmp/file.ts"));
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
    });

    it("blocks unknown tool", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", toolCall("deploy"));
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("reviewer");
    });
  });

  // -----------------------------------------------------------------------
  // Full role
  // -----------------------------------------------------------------------

  describe("full role", () => {
    const config: RBACConfig = { role: "full" };

    it("allows everything -- no handler registered", () => {
      createRBACExtension(config)(pi as any);
      // Full role returns early, so no tool_call handler is registered
      expect(pi.handlers.has("tool_call")).toBe(false);
    });

    it("does not block write", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", writeCall("/etc/passwd"));
      // No handler, so result is undefined
      expect(result).toBeUndefined();
    });

    it("does not block bash", () => {
      createRBACExtension(config)(pi as any);
      const result = pi.trigger("tool_call", bashCall("rm -rf /"));
      expect(result).toBeUndefined();
    });
  });

  // -----------------------------------------------------------------------
  // Sandbox path enforcement
  // -----------------------------------------------------------------------

  describe("sandbox path enforcement", () => {
    it("write to /outside/sandbox is blocked for developer", () => {
      const config: RBACConfig = {
        role: "developer",
        sandboxPaths: ["/home/agent/Projects/allowed"],
      };
      createRBACExtension(config)(pi as any);

      const result = pi.trigger(
        "tool_call",
        writeCall("/outside/sandbox/evil.ts"),
      );
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
    });

    it("allows write to exact sandbox path", () => {
      const config: RBACConfig = {
        role: "developer",
        sandboxPaths: ["/home/agent/Projects/allowed"],
      };
      createRBACExtension(config)(pi as any);

      // Exact path match should be allowed
      const result = pi.trigger(
        "tool_call",
        writeCall("/home/agent/Projects/allowed"),
      );
      expect(result).toBeUndefined();
    });

    it("developer with no sandbox paths allows all writes", () => {
      const config: RBACConfig = {
        role: "developer",
        sandboxPaths: [],
      };
      createRBACExtension(config)(pi as any);

      const result = pi.trigger(
        "tool_call",
        writeCall("/anywhere/at/all/file.ts"),
      );
      expect(result).toBeUndefined();
    });

    it("developer with no sandboxPaths config allows all writes", () => {
      const config: RBACConfig = { role: "developer" };
      createRBACExtension(config)(pi as any);

      const result = pi.trigger(
        "tool_call",
        writeCall("/anywhere/file.ts"),
      );
      expect(result).toBeUndefined();
    });

    it("investigator with sandbox still blocks writes regardless", () => {
      const config: RBACConfig = {
        role: "investigator",
        sandboxPaths: ["/home/agent/Projects/allowed"],
      };
      createRBACExtension(config)(pi as any);

      // Even inside sandbox, investigator cannot write
      const result = pi.trigger(
        "tool_call",
        writeCall("/home/agent/Projects/allowed/file.ts"),
      );
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
    });
  });
});

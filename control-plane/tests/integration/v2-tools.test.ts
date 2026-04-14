/**
 * Integration tests for SWE-Manager V2 tool wiring.
 *
 * Verifies that all 11 tools:
 * - Can be created from context
 * - Return structured AgentToolResult
 * - Handle missing providers gracefully (return error text, don't throw)
 * - Delegation tools resolve engines from config
 */

import { describe, it, expect, vi } from "vitest";
import { loadConfig } from "../../src/config/loader.js";
import { createLogger } from "../../src/utils/logger.js";
import { createAllTools, EXPECTED_TOOL_COUNT } from "../../src/tools/index.js";
import type { SWEContext } from "../../src/shared/context.js";

// ---------------------------------------------------------------------------
// Shared test context (no Supabase, no engine, no notifier)
// ---------------------------------------------------------------------------

function createTestContext(overrides?: Partial<SWEContext>): SWEContext {
  return {
    config: loadConfig(),
    logger: createLogger({ level: "error" }),
    cwd: process.cwd(),
    ...overrides,
  };
}

/** Execute a tool by name with given params. */
async function callTool(
  tools: ReturnType<typeof createAllTools>,
  name: string,
  params: Record<string, unknown>,
) {
  const tool = tools.find((t) => t.name === name);
  if (!tool) throw new Error(`Tool not found: ${name}`);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (tool as any).execute("test-call-id", params, undefined, undefined, {});
}

// =========================================================================
// Tool creation
// =========================================================================

describe("V2 Tool Creation", () => {
  it("creates all 11 tools from context", () => {
    const ctx = createTestContext();
    const tools = createAllTools(ctx);
    expect(tools).toHaveLength(EXPECTED_TOOL_COUNT);
  });

  it("all tools have name, label, and description", () => {
    const ctx = createTestContext();
    const tools = createAllTools(ctx);
    for (const tool of tools) {
      expect(tool.name).toBeTruthy();
      expect(tool.label).toBeTruthy();
      expect(tool.description).toBeTruthy();
    }
  });

  it("all tool names are unique", () => {
    const ctx = createTestContext();
    const tools = createAllTools(ctx);
    const names = tools.map((t) => t.name);
    expect(new Set(names).size).toBe(names.length);
  });

  it("tool names match expected set", () => {
    const ctx = createTestContext();
    const tools = createAllTools(ctx);
    const names = new Set(tools.map((t) => t.name));
    const expected = [
      "ticket_list",
      "ticket_update",
      "ticket_create",
      "github_issues",
      "github_import",
      "delegate_investigation",
      "delegate_development",
      "check_stability",
      "check_health",
      "send_notification",
      "manage_workspace",
    ];
    for (const name of expected) {
      expect(names.has(name)).toBe(true);
    }
  });
});

// =========================================================================
// Graceful degradation without providers
// =========================================================================

describe("V2 Tools: Graceful degradation", () => {
  const ctx = createTestContext(); // No providers configured
  const tools = createAllTools(ctx);

  it("ticket_list returns error when no store configured", async () => {
    const result = await callTool(tools, "ticket_list", {});
    expect(result.content[0].text).toContain("not configured");
  });

  it("ticket_update returns error when no store configured", async () => {
    const result = await callTool(tools, "ticket_update", {
      ticketId: "T-001",
      status: "resolved",
    });
    expect(result.content[0].text).toContain("not configured");
  });

  it("ticket_create returns error when no store configured", async () => {
    const result = await callTool(tools, "ticket_create", {
      title: "Test",
      description: "Test ticket",
      severity: "medium",
    });
    expect(result.content[0].text).toContain("not configured");
  });

  it("github_import returns error when no store configured", async () => {
    const result = await callTool(tools, "github_import", {
      repo: "test/repo",
    });
    expect(result.content[0].text).toContain("not configured");
  });

  it("delegate_investigation returns error when no store configured", async () => {
    const result = await callTool(tools, "delegate_investigation", {
      ticketId: "T-001",
    });
    expect(result.content[0].text).toContain("not configured");
  });

  it("delegate_development returns error when no store configured", async () => {
    const result = await callTool(tools, "delegate_development", {
      ticketId: "T-001",
    });
    expect(result.content[0].text).toContain("not configured");
  });

  it("check_stability returns WARN when no store configured", async () => {
    const result = await callTool(tools, "check_stability", {});
    expect(result.content[0].text).toContain("WARN");
  });

  it("check_health returns health snapshot even without providers", async () => {
    const result = await callTool(tools, "check_health", {});
    const text = result.content[0].text;
    expect(text).toContain("teamId");
    expect(text).toContain("circuitBreaker");
    expect(text).toContain("supabase");
    expect(text).toContain("engines");
  }, 15000); // SSH engine health checks need extra time

  it("send_notification returns error when no notifier configured", async () => {
    const result = await callTool(tools, "send_notification", {
      message: "test alert",
    });
    // Notification provider defaults to "telegram" but is not wired
    expect(result.content[0].text).toContain("not configured");
  });
});

// =========================================================================
// Tool result structure
// =========================================================================

describe("V2 Tools: Result structure", () => {
  const ctx = createTestContext();
  const tools = createAllTools(ctx);

  it("all tools return AgentToolResult with content array", async () => {
    // Test each tool returns valid structure (even if it returns an error)
    const testCases = [
      { name: "ticket_list", params: {} },
      { name: "ticket_update", params: { ticketId: "T-001" } },
      { name: "ticket_create", params: { title: "t", description: "d", severity: "low" } },
      { name: "github_import", params: { repo: "test/repo" } },
      { name: "delegate_investigation", params: { ticketId: "T-001" } },
      { name: "delegate_development", params: { ticketId: "T-001" } },
      { name: "check_stability", params: {} },
      { name: "check_health", params: {} },
      { name: "send_notification", params: { message: "test" } },
    ];

    for (const { name, params } of testCases) {
      const result = await callTool(tools, name, params);
      expect(result).toHaveProperty("content");
      expect(Array.isArray(result.content)).toBe(true);
      expect(result.content.length).toBeGreaterThan(0);
      expect(result.content[0]).toHaveProperty("type", "text");
      expect(result.content[0]).toHaveProperty("text");
      expect(typeof result.content[0].text).toBe("string");
      expect(result).toHaveProperty("details");
    }
  }, 15000); // SSH engine health checks in check_health need extra time
});

// =========================================================================
// Engine resolver integration
// =========================================================================

describe("V2 Engine Resolution", () => {
  it("resolveEngineForRole falls back to claude-cli when no delegation config", async () => {
    const { resolveEngineForRole } = await import(
      "../../src/shared/engine-resolver.js"
    );
    const config = loadConfig();
    // No delegation config = falls back to claude-cli
    const engine = resolveEngineForRole("investigator", config);
    expect(engine.name).toBe("claude-cli");
  });

  it("getDelegationConfig returns defaults for unconfigured role", async () => {
    const { getDelegationConfig } = await import(
      "../../src/shared/engine-resolver.js"
    );
    const config = loadConfig();
    const delegation = getDelegationConfig("investigator", config);
    expect(delegation.engine).toBe("claude-cli");
    expect(delegation.readOnly).toBe(true); // investigators are read-only
    expect(delegation.timeout).toBe(1800);
  });
});

// =========================================================================
// Prompt builder
// =========================================================================

describe("V2 Prompt Builder", () => {
  it("buildInvestigationPrompt includes ticket data", async () => {
    const { buildInvestigationPrompt } = await import(
      "../../src/shared/prompt-builder.js"
    );
    const { createTicket } = await import("../../src/models/ticket.js");

    const ticket = createTicket("Login fails on mobile", "Users report 500 errors", {
      severity: "high",
      metadata: { repo: "org/webapp", fingerprint: "test-fp" },
    });

    const prompt = buildInvestigationPrompt(ticket);
    expect(prompt).toContain("Login fails on mobile");
    expect(prompt).toContain("high");
    expect(prompt).toContain("org/webapp");
    expect(prompt).toContain("Root cause");
    expect(prompt).toContain("read-only");
  });

  it("buildDevelopmentPrompt includes investigation report", async () => {
    const { buildDevelopmentPrompt } = await import(
      "../../src/shared/prompt-builder.js"
    );
    const { createTicket } = await import("../../src/models/ticket.js");

    const ticket = createTicket("Login fails", "500 errors", {
      investigationReport: "Root cause: missing null check in auth.ts:42",
    });

    const prompt = buildDevelopmentPrompt(ticket, "/tmp/workspace");
    expect(prompt).toContain("missing null check");
    expect(prompt).toContain("/tmp/workspace");
    expect(prompt).toContain("Investigation Report");
  });
});

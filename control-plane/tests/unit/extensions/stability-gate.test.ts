/**
 * Unit tests for the stability gate extension.
 *
 * Simulates the pi.on("tool_call", handler) pattern by capturing handlers
 * registered by the extension factory, then invoking them with synthetic
 * ToolCallEvent objects.
 *
 * The stability gate enforces ticket-count checks before allowing
 * delegate_investigation and delegate_development tool calls.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";

// ---------------------------------------------------------------------------
// Mock pi-coding-agent types
// ---------------------------------------------------------------------------

vi.mock("@mariozechner/pi-coding-agent", () => ({}));

// ---------------------------------------------------------------------------
// Imports
// ---------------------------------------------------------------------------

import {
  createStabilityGateExtension,
  createStabilityGateWithQuery,
} from "../../../src/extensions/stability-gate.js";
import type {
  StabilityGateConfig,
  TicketStoreProvider,
  GuardrailsProvider,
} from "../../../src/extensions/stability-gate.js";

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

/** Create a synthetic tool_call event for a delegation tool. */
function delegateCall(toolName: string, input: Record<string, unknown> = {}) {
  return { toolName, input };
}

/** Create a mock ticket store that returns the given tickets. */
function mockTicketStore(
  tickets: Array<{ severity: string }>,
): TicketStoreProvider {
  return {
    listOpen: vi.fn().mockResolvedValue(tickets),
  };
}

/** Create a mock ticket store that rejects with an error. */
function failingTicketStore(error: Error): TicketStoreProvider {
  return {
    listOpen: vi.fn().mockRejectedValue(error),
  };
}

/**
 * Helper: prime the cache by waiting for the initial eager evaluation.
 *
 * The extension eagerly calls getStabilityResult() on creation. We need
 * to wait for that promise to settle before triggering tool calls so
 * the cache is populated.
 */
async function primeCache(): Promise<void> {
  // Two ticks: one for the listOpen promise, one for the cache write.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
}

// =========================================================================
// Tests
// =========================================================================

describe("Stability Gate Extension", () => {
  let pi: ReturnType<typeof createMockPi>;

  beforeEach(() => {
    pi = createMockPi();
  });

  // -----------------------------------------------------------------------
  // 1. PASS allows delegation
  // -----------------------------------------------------------------------

  describe("PASS verdict", () => {
    it("allows delegate_investigation when ticket counts are within thresholds", async () => {
      const store = mockTicketStore([
        { severity: "medium" },
        { severity: "low" },
      ]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        maxOpenHigh: 3,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_investigation", { ticketId: "t-001" }),
      );
      expect(result).toBeUndefined(); // allowed
    });

    it("allows delegate_development when ticket counts are within thresholds", async () => {
      const store = mockTicketStore([
        { severity: "high" },
        { severity: "high" },
        { severity: "medium" },
      ]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        maxOpenHigh: 3,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_development", { ticketId: "t-002" }),
      );
      expect(result).toBeUndefined(); // allowed
    });
  });

  // -----------------------------------------------------------------------
  // 2. BLOCK prevents delegation
  // -----------------------------------------------------------------------

  describe("BLOCK verdict", () => {
    it("blocks delegate_investigation when critical tickets exceed threshold", async () => {
      const store = mockTicketStore([
        { severity: "critical" },
        { severity: "high" },
        { severity: "medium" },
      ]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0, // 1 critical > 0 max
        maxOpenHigh: 5,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_investigation", { ticketId: "t-003" }),
      );
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("StabilityGate");
      expect(result.reason).toContain("BLOCKED");
      expect(result.reason).toContain("critical");
    });

    it("blocks delegate_development when critical tickets exceed threshold", async () => {
      const store = mockTicketStore([
        { severity: "critical" },
        { severity: "critical" },
      ]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 1, // 2 critical > 1 max
        maxOpenHigh: 5,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_development", { ticketId: "t-004" }),
      );
      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("BLOCKED");
      expect(result.reason).toContain("2 critical");
    });

    it("calls onBlocked callback when blocking", async () => {
      const onBlocked = vi.fn();
      const store = mockTicketStore([{ severity: "critical" }]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        cacheSeconds: 60,
        onBlocked,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      pi.trigger(
        "tool_call",
        delegateCall("delegate_investigation", { ticketId: "t-005" }),
      );

      expect(onBlocked).toHaveBeenCalledTimes(1);
      expect(onBlocked).toHaveBeenCalledWith(
        "delegate_investigation",
        expect.objectContaining({
          verdict: "BLOCK",
          criticalCount: 1,
        }),
      );
    });
  });

  // -----------------------------------------------------------------------
  // 3. WARN allows with warning
  // -----------------------------------------------------------------------

  describe("WARN verdict", () => {
    it("allows delegate_investigation with WARN when high tickets exceed threshold", async () => {
      const onWarn = vi.fn();
      const store = mockTicketStore([
        { severity: "high" },
        { severity: "high" },
        { severity: "high" },
        { severity: "high" }, // 4 high > 3 max
      ]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        maxOpenHigh: 3,
        cacheSeconds: 60,
        onWarn,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_investigation", { ticketId: "t-006" }),
      );

      // WARN allows the tool call (result is undefined)
      expect(result).toBeUndefined();
      // But the onWarn callback fires
      expect(onWarn).toHaveBeenCalledTimes(1);
      expect(onWarn).toHaveBeenCalledWith(
        "delegate_investigation",
        expect.objectContaining({
          verdict: "WARN",
          highCount: 4,
        }),
      );
    });

    it("allows delegate_development with WARN when high tickets exceed threshold", async () => {
      const onWarn = vi.fn();
      const store = mockTicketStore([
        { severity: "high" },
        { severity: "high" },
        { severity: "high" },
        { severity: "high" },
        { severity: "high" }, // 5 high > 3 max
        { severity: "medium" },
      ]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        maxOpenHigh: 3,
        cacheSeconds: 60,
        onWarn,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_development", { ticketId: "t-007" }),
      );

      expect(result).toBeUndefined(); // allowed
      expect(onWarn).toHaveBeenCalledTimes(1);
      expect(onWarn).toHaveBeenCalledWith(
        "delegate_development",
        expect.objectContaining({ verdict: "WARN", highCount: 5 }),
      );
    });
  });

  // -----------------------------------------------------------------------
  // 4. Cache works
  // -----------------------------------------------------------------------

  describe("cache behavior", () => {
    it("uses cached result within cacheSeconds window", async () => {
      const listOpen = vi.fn().mockResolvedValue([]);
      const store: TicketStoreProvider = { listOpen };
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        maxOpenHigh: 3,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      // listOpen was called once (eager prime)
      expect(listOpen).toHaveBeenCalledTimes(1);

      // Trigger multiple tool calls -- cache should be used
      pi.trigger("tool_call", delegateCall("delegate_investigation"));
      pi.trigger("tool_call", delegateCall("delegate_development"));
      pi.trigger("tool_call", delegateCall("delegate_investigation"));

      // listOpen should NOT have been called again
      expect(listOpen).toHaveBeenCalledTimes(1);
    });

    it("refreshes cache after cacheSeconds elapse", async () => {
      const listOpen = vi.fn().mockResolvedValue([]);
      const store: TicketStoreProvider = { listOpen };
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        maxOpenHigh: 3,
        cacheSeconds: 1, // 1 second cache
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      expect(listOpen).toHaveBeenCalledTimes(1);

      // Wait for cache to expire (1.1 seconds)
      await new Promise((r) => setTimeout(r, 1100));

      // Trigger a tool call -- should fire async refresh
      pi.trigger("tool_call", delegateCall("delegate_investigation"));

      // Wait for the async refresh
      await primeCache();

      // listOpen should have been called again
      expect(listOpen).toHaveBeenCalledTimes(2);
    });
  });

  // -----------------------------------------------------------------------
  // 5. Missing provider fails open (graceful pass)
  // -----------------------------------------------------------------------

  describe("missing provider", () => {
    it("allows delegation when no ticket store is configured", async () => {
      // No ticketStoreProvider or guardrailsProvider
      const config: StabilityGateConfig = {
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_investigation", { ticketId: "t-010" }),
      );

      // Should fail open
      expect(result).toBeUndefined();
    });

    it("allows delegation when ticket store throws an error", async () => {
      const store = failingTicketStore(new Error("Connection refused"));
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        cacheSeconds: 60,
      };

      // Suppress expected console.warn
      const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_development", { ticketId: "t-011" }),
      );

      // Should fail open
      expect(result).toBeUndefined();

      warnSpy.mockRestore();
    });
  });

  // -----------------------------------------------------------------------
  // Non-gated tools pass through
  // -----------------------------------------------------------------------

  describe("non-gated tools", () => {
    it("does not intercept bash tool", async () => {
      const store = mockTicketStore([{ severity: "critical" }]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      // bash should pass through even when stability is BLOCK
      const result = pi.trigger("tool_call", {
        toolName: "bash",
        input: { command: "npm test" },
      });
      expect(result).toBeUndefined();
    });

    it("does not intercept read tool", async () => {
      const store = mockTicketStore([{ severity: "critical" }]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger("tool_call", {
        toolName: "read",
        input: { file_path: "/tmp/file.ts" },
      });
      expect(result).toBeUndefined();
    });

    it("does not intercept write tool", async () => {
      const store = mockTicketStore([{ severity: "critical" }]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger("tool_call", {
        toolName: "write",
        input: { file_path: "/tmp/file.ts" },
      });
      expect(result).toBeUndefined();
    });

    it("does not intercept check_stability tool", async () => {
      const store = mockTicketStore([{ severity: "critical" }]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        maxOpenCritical: 0,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger("tool_call", {
        toolName: "check_stability",
        input: {},
      });
      expect(result).toBeUndefined();
    });
  });

  // -----------------------------------------------------------------------
  // GuardrailsProvider integration
  // -----------------------------------------------------------------------

  describe("guardrails provider", () => {
    it("uses guardrails provider when configured", async () => {
      const guardrails: GuardrailsProvider = {
        evaluate: vi.fn().mockReturnValue({
          allowed: false,
          blocked: true,
          reason: "Circuit breaker tripped",
          gate: "circuit_breaker",
          details: { open_critical: 2, open_high: 5 },
          evaluatedGates: ["circuit_breaker"],
        }),
      };

      const config: StabilityGateConfig = {
        guardrailsProvider: guardrails,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_investigation"),
      );

      expect(result).toBeDefined();
      expect(result.block).toBe(true);
      expect(result.reason).toContain("Circuit breaker tripped");
    });

    it("allows when guardrails provider says allowed", async () => {
      const guardrails: GuardrailsProvider = {
        evaluate: vi.fn().mockReturnValue({
          allowed: true,
          blocked: false,
          reason: "All gates passed",
          gate: "all_clear",
          details: { open_critical: 0, open_high: 1 },
          evaluatedGates: ["circuit_breaker", "stability_gate", "all_clear"],
        }),
      };

      const config: StabilityGateConfig = {
        guardrailsProvider: guardrails,
        cacheSeconds: 60,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_development"),
      );

      expect(result).toBeUndefined(); // allowed
    });
  });

  // -----------------------------------------------------------------------
  // Default configuration
  // -----------------------------------------------------------------------

  describe("default configuration", () => {
    it("works with no config at all (all defaults)", async () => {
      createStabilityGateExtension()(pi as any);
      await primeCache();

      // No provider -> fail open
      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_investigation"),
      );
      expect(result).toBeUndefined();
    });

    it("uses default maxOpenCritical=0 when not specified", async () => {
      const store = mockTicketStore([{ severity: "critical" }]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        // maxOpenCritical not set -- defaults to 0
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_investigation"),
      );

      expect(result).toBeDefined();
      expect(result.block).toBe(true);
    });

    it("uses default maxOpenHigh=3 when not specified", async () => {
      const onWarn = vi.fn();
      const store = mockTicketStore([
        { severity: "high" },
        { severity: "high" },
        { severity: "high" },
        { severity: "high" }, // 4 high > default 3
      ]);
      const config: StabilityGateConfig = {
        ticketStoreProvider: store,
        // maxOpenHigh not set -- defaults to 3
        onWarn,
      };

      createStabilityGateExtension(config)(pi as any);
      await primeCache();

      const result = pi.trigger(
        "tool_call",
        delegateCall("delegate_development"),
      );

      // WARN allows but fires callback
      expect(result).toBeUndefined();
      expect(onWarn).toHaveBeenCalledTimes(1);
    });
  });

  // -----------------------------------------------------------------------
  // createStabilityGateWithQuery
  // -----------------------------------------------------------------------

  describe("createStabilityGateWithQuery", () => {
    it("provides queryable last result after BLOCK", async () => {
      const store = mockTicketStore([{ severity: "critical" }]);
      const { extension, getLastResult } = createStabilityGateWithQuery({
        ticketStoreProvider: store,
        maxOpenCritical: 0,
      });

      extension(pi as any);
      await primeCache();

      // No result tracked until a tool call fires onBlocked
      expect(getLastResult()).toBeNull();

      // Trigger a blocked call
      pi.trigger(
        "tool_call",
        delegateCall("delegate_investigation"),
      );

      const last = getLastResult();
      expect(last).not.toBeNull();
      expect(last!.verdict).toBe("BLOCK");
      expect(last!.criticalCount).toBe(1);
    });
  });
});

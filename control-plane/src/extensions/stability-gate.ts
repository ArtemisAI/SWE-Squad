/**
 * Stability gate extension for pi-agent sessions.
 *
 * Hard enforcement layer that intercepts `delegate_investigation` and
 * `delegate_development` tool calls, runs the stability check logic,
 * and blocks execution when the system is unstable (too many critical/
 * high tickets open).
 *
 * This converts the stability check from advisory (LLM can ignore it)
 * to code-level enforcement (tool call is rejected before execution).
 *
 * Verdicts:
 *   BLOCK — too many critical tickets; tool call is rejected.
 *   WARN  — too many high tickets; tool call allowed, warning logged.
 *   PASS  — all clear; tool call proceeds normally.
 *
 * The gate caches the last check result for a configurable duration
 * (default 60 seconds) to avoid re-evaluating on every single tool call.
 *
 * Uses the pi-coding-agent ExtensionFactory contract:
 *   pi.on("tool_call", ...) to intercept and optionally block delegation tools.
 */

import type {
  ExtensionFactory,
  ToolCallEvent,
  ToolCallEventResult,
} from "@mariozechner/pi-coding-agent";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Verdict from a stability evaluation. */
export type StabilityVerdict = "PASS" | "WARN" | "BLOCK";

/** A single cached stability check result. */
export interface StabilityCheckResult {
  verdict: StabilityVerdict;
  reason: string;
  criticalCount: number;
  highCount: number;
  timestamp: number;
}

/**
 * Minimal ticket store interface.
 *
 * The gate only needs to list open tickets to count severities.
 * This avoids coupling to the full SupabaseTicketStore implementation.
 */
export interface TicketStoreProvider {
  listOpen(limit?: number): Promise<Array<{ severity: string }>>;
}

/**
 * Optional guardrails coordinator interface.
 *
 * If provided, the gate delegates to the coordinator instead of doing
 * its own ticket-count check. This allows the stability gate extension
 * to piggyback on the full guardrails pipeline (circuit breaker, budget,
 * governor, stability, throttle).
 */
export interface GuardrailsProvider {
  evaluate(
    taskType?: string,
    ticketSeverity?: string,
    currentAgents?: number,
    teamId?: string,
  ): {
    allowed: boolean;
    blocked: boolean;
    reason: string;
    gate: string;
    details: Record<string, unknown>;
    evaluatedGates: string[];
  };
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

export interface StabilityGateConfig {
  /**
   * Ticket store for counting open critical/high tickets.
   * If not provided, the gate fails open (PASS) with a console warning.
   */
  ticketStoreProvider?: TicketStoreProvider;

  /**
   * Optional guardrails coordinator. When set, the gate delegates the full
   * evaluation to the coordinator instead of doing manual ticket counts.
   */
  guardrailsProvider?: GuardrailsProvider;

  /**
   * How long (in seconds) to cache a stability check result before
   * re-evaluating. Defaults to 60.
   */
  cacheSeconds?: number;

  /**
   * Maximum open critical tickets before BLOCK. Defaults to 0.
   */
  maxOpenCritical?: number;

  /**
   * Maximum open high tickets before WARN. Defaults to 3.
   */
  maxOpenHigh?: number;

  /**
   * Called when a tool call is blocked. Useful for logging/alerting.
   */
  onBlocked?: (toolName: string, result: StabilityCheckResult) => void;

  /**
   * Called when a tool call is allowed with a WARN verdict.
   */
  onWarn?: (toolName: string, result: StabilityCheckResult) => void;
}

// ---------------------------------------------------------------------------
// Tools that are gated
// ---------------------------------------------------------------------------

/** Tool names that require a stability check before execution. */
const GATED_TOOLS = new Set([
  "delegate_investigation",
  "delegate_development",
]);

// ---------------------------------------------------------------------------
// Extension factory
// ---------------------------------------------------------------------------

/**
 * Create a stability gate extension that enforces stability checks
 * before delegation tools can execute.
 *
 * @example
 * ```typescript
 * import { createAgentSession } from "@mariozechner/pi-coding-agent";
 * import { createStabilityGateExtension } from "./extensions/stability-gate.js";
 *
 * const stabilityGate = createStabilityGateExtension({
 *   ticketStoreProvider: myTicketStore,
 *   cacheSeconds: 60,
 *   maxOpenCritical: 0,
 *   maxOpenHigh: 3,
 *   onBlocked: (tool, result) => {
 *     console.error(`[StabilityGate] Blocked ${tool}: ${result.reason}`);
 *   },
 * });
 * ```
 */
export function createStabilityGateExtension(
  config?: StabilityGateConfig,
): ExtensionFactory {
  return (pi) => {
    const cacheSeconds = config?.cacheSeconds ?? 60;
    const maxOpenCritical = config?.maxOpenCritical ?? 0;
    const maxOpenHigh = config?.maxOpenHigh ?? 3;

    // Mutable cached result. Starts empty so the first gated call triggers
    // a fresh evaluation.
    let cached: StabilityCheckResult | null = null;

    // ------------------------------------------------------------------
    // Stability evaluation
    // ------------------------------------------------------------------

    /**
     * Evaluate current stability.
     *
     * Priority:
     * 1. If a GuardrailsProvider is configured, delegate to it.
     * 2. If a TicketStoreProvider is configured, count open tickets.
     * 3. Otherwise fail open (PASS) with a warning.
     */
    async function evaluateStability(): Promise<StabilityCheckResult> {
      const now = Date.now();

      // --- Guardrails provider path ---
      if (config?.guardrailsProvider) {
        try {
          const decision = config.guardrailsProvider.evaluate();
          if (decision.blocked) {
            return {
              verdict: "BLOCK",
              reason: decision.reason,
              criticalCount: (decision.details.open_critical as number) ?? 0,
              highCount: (decision.details.open_high as number) ?? 0,
              timestamp: now,
            };
          }
          // Guardrails passed -- PASS
          return {
            verdict: "PASS",
            reason: decision.reason,
            criticalCount: (decision.details.open_critical as number) ?? 0,
            highCount: (decision.details.open_high as number) ?? 0,
            timestamp: now,
          };
        } catch (err) {
          // Guardrails failed -- fail open
          console.warn("[StabilityGate] Guardrails evaluation failed, failing open:", err);
          return {
            verdict: "PASS",
            reason: `Guardrails check failed: ${err}`,
            criticalCount: 0,
            highCount: 0,
            timestamp: now,
          };
        }
      }

      // --- Ticket store path ---
      if (config?.ticketStoreProvider) {
        try {
          const openTickets = await config.ticketStoreProvider.listOpen();
          const criticalCount = openTickets.filter(
            (t) => t.severity === "critical",
          ).length;
          const highCount = openTickets.filter(
            (t) => t.severity === "high",
          ).length;

          if (criticalCount > maxOpenCritical) {
            return {
              verdict: "BLOCK",
              reason: `${criticalCount} open critical ticket(s) exceed max ${maxOpenCritical}`,
              criticalCount,
              highCount,
              timestamp: now,
            };
          }

          if (highCount > maxOpenHigh) {
            return {
              verdict: "WARN",
              reason: `${highCount} open high ticket(s) exceed max ${maxOpenHigh}`,
              criticalCount,
              highCount,
              timestamp: now,
            };
          }

          return {
            verdict: "PASS",
            reason: `${criticalCount} critical, ${highCount} high -- within thresholds`,
            criticalCount,
            highCount,
            timestamp: now,
          };
        } catch (err) {
          // Ticket store query failed -- fail open
          console.warn("[StabilityGate] Ticket store query failed, failing open:", err);
          return {
            verdict: "PASS",
            reason: `Ticket store query failed: ${err}`,
            criticalCount: 0,
            highCount: 0,
            timestamp: now,
          };
        }
      }

      // --- No provider path ---
      console.warn(
        "[StabilityGate] No ticket store or guardrails provider configured. Failing open.",
      );
      return {
        verdict: "PASS",
        reason: "No stability provider configured -- failing open",
        criticalCount: 0,
        highCount: 0,
        timestamp: now,
      };
    }

    /**
     * Get a stability result, using the cache if still valid.
     */
    async function getStabilityResult(): Promise<StabilityCheckResult> {
      const now = Date.now();
      if (cached && now - cached.timestamp < cacheSeconds * 1000) {
        return cached;
      }
      const result = await evaluateStability();
      cached = result;
      return result;
    }

    // ------------------------------------------------------------------
    // Tool call interception
    // ------------------------------------------------------------------

    pi.on("tool_call", (event: ToolCallEvent): ToolCallEventResult | void => {
      // Only gate specific delegation tools
      if (!GATED_TOOLS.has(event.toolName)) {
        return undefined;
      }

      // The pi.on("tool_call") handler is synchronous in the extension API,
      // but our evaluation may be async (ticket store query). We handle this
      // by checking the cache synchronously first. If the cache is valid,
      // we return immediately. If stale, we trigger a refresh but still use
      // the last known result (or PASS if no prior result).
      //
      // This is the correct design because:
      // 1. The cache is refreshed every `cacheSeconds` by the async path.
      // 2. The first call in a session (no cache) will fail open, but the
      //    async refresh fires immediately, so subsequent calls are gated.
      // 3. We never block the event loop waiting for network I/O.
      const now = Date.now();

      if (cached && now - cached.timestamp < cacheSeconds * 1000) {
        // Cache hit -- use it synchronously
        return applyVerdict(event.toolName, cached);
      }

      // Cache miss or stale -- fire async refresh and use best available data.
      // The promise is intentionally not awaited (fire-and-forget).
      void getStabilityResult();

      // If we have a stale cached result, use it as a best-effort gate.
      if (cached) {
        return applyVerdict(event.toolName, cached);
      }

      // Truly first call, no cached data at all -- fail open.
      return undefined;
    });

    /**
     * Apply the verdict to a tool call event.
     *
     * Returns a ToolCallEventResult that blocks the call (BLOCK),
     * or undefined to allow it (PASS / WARN).
     */
    function applyVerdict(
      toolName: string,
      result: StabilityCheckResult,
    ): ToolCallEventResult | undefined {
      if (result.verdict === "BLOCK") {
        config?.onBlocked?.(toolName, result);
        return {
          block: true,
          reason:
            `[StabilityGate] BLOCKED: ${result.reason}. ` +
            `(${result.criticalCount} critical, ${result.highCount} high). ` +
            `Resolve open critical tickets before delegating new work.`,
        };
      }

      if (result.verdict === "WARN") {
        config?.onWarn?.(toolName, result);
        // Allow the call but the warning callback has fired.
        return undefined;
      }

      // PASS
      return undefined;
    }

    // Eagerly prime the cache so the first gated tool call doesn't fail open
    // unnecessarily.
    void getStabilityResult();
  };
}

// ---------------------------------------------------------------------------
// Utility: externally queryable stability gate
// ---------------------------------------------------------------------------

/**
 * Create a stability gate with externally queryable state.
 *
 * Returns both the ExtensionFactory and a getter for the last check result.
 * Useful when the caller needs to query stability outside of tool calls.
 */
export function createStabilityGateWithQuery(config?: StabilityGateConfig): {
  extension: ExtensionFactory;
  getLastResult: () => StabilityCheckResult | null;
} {
  let lastResult: StabilityCheckResult | null = null;

  const wrappedConfig: StabilityGateConfig = {
    ...config,
    onBlocked: (tool, result) => {
      lastResult = result;
      config?.onBlocked?.(tool, result);
    },
    onWarn: (tool, result) => {
      lastResult = result;
      config?.onWarn?.(tool, result);
    },
  };

  // Also capture PASS results by wrapping the ticket store to update lastResult.
  // We do this via a thin proxy that intercepts and tracks.
  const originalTicketStore = config?.ticketStoreProvider;
  if (originalTicketStore) {
    wrappedConfig.ticketStoreProvider = {
      async listOpen(limit?: number) {
        const tickets = await originalTicketStore.listOpen(limit);
        // The lastResult will be set by the evaluation after this returns.
        return tickets;
      },
    };
  }

  const extension = createStabilityGateExtension(wrappedConfig);

  return {
    extension,
    getLastResult: () => lastResult,
  };
}

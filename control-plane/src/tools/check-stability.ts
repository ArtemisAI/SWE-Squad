/**
 * Tool: check_stability — Evaluate safety gates before starting new work.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";

export function createCheckStabilityTool(ctx: SWEContext) {
  return defineTool({
    name: "check_stability",
    label: "Check Stability",
    description:
      "Evaluate safety gates (circuit breaker, stability, budget). " +
      "Returns PASS, WARN, or BLOCK with counts of critical/high tickets.",
    parameters: Type.Object({}),
    async execute(_toolCallId, _params, _signal, _onUpdate, _extCtx) {
      try {
        // If guardrails coordinator is configured, use it
        if (ctx.guardrails) {
          const decision = ctx.guardrails.evaluate();
          return {
            content: [{
              type: "text" as const,
              text: decision.blocked
                ? `BLOCK: ${decision.reason} (gate: ${decision.gate})`
                : `PASS: All safety gates passed. Evaluated: ${decision.evaluatedGates.join(", ")}`,
            }],
            details: { allowed: decision.allowed, gate: decision.gate, reason: decision.reason },
          };
        }

        // Fallback: check circuit breaker directly
        if (ctx.circuitBreaker) {
          const paused = ctx.circuitBreaker.isPaused;
          const rate = ctx.circuitBreaker.failureRate;
          if (paused) {
            return {
              content: [{
                type: "text" as const,
                text: `BLOCK: Circuit breaker tripped (failure rate: ${(rate * 100).toFixed(1)}%)`,
              }],
              details: { verdict: "BLOCK", failureRate: rate },
            };
          }
        }

        // No ticket store = can't check ticket counts
        if (!ctx.ticketStore) {
          return {
            content: [{ type: "text" as const, text: "WARN: No ticket store configured, unable to check stability fully" }],
            details: { verdict: "WARN" },
          };
        }

        // Manual stability check via ticket counts
        const openTickets = await ctx.ticketStore.listOpen();
        const criticalCount = openTickets.filter((t) => t.severity === "critical").length;
        const highCount = openTickets.filter((t) => t.severity === "high").length;

        const maxCritical = ctx.config.governance.maxOpenCritical;
        const maxHigh = ctx.config.governance.maxOpenHigh;

        if (criticalCount > maxCritical) {
          return {
            content: [{
              type: "text" as const,
              text: `BLOCK: ${criticalCount} critical tickets (max: ${maxCritical})`,
            }],
            details: { verdict: "BLOCK", critical: criticalCount, high: highCount },
          };
        }

        if (highCount > maxHigh) {
          return {
            content: [{
              type: "text" as const,
              text: `WARN: ${highCount} high tickets (max: ${maxHigh}), ${criticalCount} critical`,
            }],
            details: { verdict: "WARN", critical: criticalCount, high: highCount },
          };
        }

        return {
          content: [{
            type: "text" as const,
            text: `PASS: ${criticalCount} critical, ${highCount} high tickets`,
          }],
          details: { verdict: "PASS", critical: criticalCount, high: highCount },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `WARN: Unable to check stability: ${err}` }],
          details: { verdict: "WARN" },
        };
      }
    },
  });
}

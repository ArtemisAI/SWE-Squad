/**
 * Tool: check_metrics -- Query outcome metrics for the current session.
 *
 * Provides the LLM with hard data about its own success/failure rates,
 * engine health, stalled tickets, and active alerts. This is the LLM's
 * self-monitoring capability -- it can query its own performance and
 * adjust behavior based on real metrics, not just prompt instructions.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";

export function createCheckMetricsTool(ctx: SWEContext) {
  return defineTool({
    name: "check_metrics",
    label: "Check Metrics",
    description:
      "Query outcome metrics for the current session. Returns success/failure rates " +
      "by tool, by engine, stalled tickets, and active alerts. Use this to monitor " +
      "your own performance and adjust behavior based on hard data.",
    parameters: Type.Object({
      scope: Type.Optional(
        Type.Union(
          [
            Type.Literal("overview"),
            Type.Literal("engines"),
            Type.Literal("tickets"),
            Type.Literal("alerts"),
            Type.Literal("full"),
          ],
          { description: "What to report on (default: overview)", default: "overview" },
        ),
      ),
      engine: Type.Optional(
        Type.String({ description: "Filter to a specific engine name" }),
      ),
      ticketId: Type.Optional(
        Type.String({ description: "Check if a specific ticket is exhausted" }),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _extCtx) {
      const tracker = ctx.outcomeTracker;
      if (!tracker) {
        return {
          content: [{
            type: "text" as const,
            text: "Outcome tracker not configured. No metrics available.",
          }],
          details: {},
        };
      }

      const scope = params.scope ?? "overview";
      const health = tracker.health();
      const parts: string[] = [];

      // Overview: overall stats
      if (scope === "overview" || scope === "full") {
        const o = health.overall;
        parts.push(
          `## System Health (last ${health.windowMinutes}min)`,
          "",
          `Total calls: ${o.totalCalls}`,
          `Success rate: ${(o.successRate * 100).toFixed(1)}%`,
          `Successes: ${o.successes}, Failures: ${o.failures}`,
          `Avg duration: ${o.avgDurationMs.toFixed(0)}ms`,
          `Total cost: $${o.totalCostUsd.toFixed(4)}`,
        );
        if (o.lastFailure) {
          parts.push(`Last failure: ${o.lastFailure.error} (${o.lastFailure.timestamp.toISOString()})`);
        }
        parts.push("");

        // Per-tool breakdown
        const tools = Object.entries(health.byTool);
        if (tools.length > 0) {
          parts.push("### By Tool", "");
          for (const [tool, stats] of tools) {
            parts.push(
              `- **${tool}**: ${stats.totalCalls} calls, ${(stats.successRate * 100).toFixed(0)}% success, $${stats.totalCostUsd.toFixed(4)}`,
            );
          }
          parts.push("");
        }
      }

      // Engines: per-engine metrics
      if (scope === "engines" || scope === "full") {
        const engines = Object.entries(health.byEngine);
        if (engines.length > 0) {
          parts.push("### Engine Health", "");
          for (const [name, metrics] of engines) {
            if (params.engine && name !== params.engine) continue;
            const status = metrics.isHealthy ? "HEALTHY" : "DEGRADED";
            parts.push(
              `- **${name}** [${status}]: ${metrics.calls} calls, ` +
                `${(metrics.successRate * 100).toFixed(0)}% success, ` +
                `${metrics.consecutiveFailures} consecutive failures, ` +
                `$${metrics.totalCostUsd.toFixed(4)}`,
            );
          }
          parts.push("");
        } else {
          parts.push("No engine metrics recorded yet.", "");
        }

        // Specific engine health check
        if (params.engine) {
          const isHealthy = tracker.isEngineHealthy(params.engine);
          parts.push(`Engine "${params.engine}" is ${isHealthy ? "HEALTHY" : "UNHEALTHY"}`, "");
        }
      }

      // Tickets: stalled tickets
      if (scope === "tickets" || scope === "full") {
        if (health.stalledTickets.length > 0) {
          parts.push("### Stalled Tickets", "");
          for (const id of health.stalledTickets) {
            parts.push(`- ${id} (exceeded retry limit)`);
          }
          parts.push("");
        } else {
          parts.push("No stalled tickets.", "");
        }

        // Specific ticket exhaustion check
        if (params.ticketId) {
          const exhausted = tracker.isTicketExhausted(params.ticketId);
          parts.push(
            `Ticket "${params.ticketId}" is ${exhausted ? "EXHAUSTED (stop retrying)" : "not exhausted"}`,
            "",
          );
        }
      }

      // Alerts
      if (scope === "alerts" || scope === "full") {
        const alerts = health.alerts;
        if (alerts.length > 0) {
          parts.push("### Active Alerts", "");
          for (const alert of alerts) {
            parts.push(
              `- [${alert.severity.toUpperCase()}] ${alert.type}: ${alert.message}`,
            );
          }
          parts.push("");
        } else {
          parts.push("No active alerts.", "");
        }
      }

      const text = parts.join("\n") || "No metrics data available.";

      return {
        content: [{ type: "text" as const, text }],
        details: {
          totalCalls: health.overall.totalCalls,
          successRate: health.overall.successRate,
          stalledTickets: health.stalledTickets,
          alertCount: health.alerts.length,
        },
      };
    },
  });
}

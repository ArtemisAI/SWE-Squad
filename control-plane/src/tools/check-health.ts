/**
 * Tool: check_health — Aggregate health snapshot of all subsystems.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";
import { resolveEngine, listEngines, hasEngine } from "../providers/engine/registry.js";

export function createCheckHealthTool(ctx: SWEContext) {
  return defineTool({
    name: "check_health",
    label: "Check Health",
    description:
      "Get a health snapshot of all subsystems: circuit breaker, Supabase, " +
      "engine availability, cost tracking, and uptime.",
    parameters: Type.Object({}),
    async execute(_toolCallId, _params, _signal, _onUpdate, _extCtx) {
      const health: Record<string, unknown> = {
        timestamp: new Date().toISOString(),
        teamId: ctx.config.teamId,
      };

      // Circuit breaker
      if (ctx.circuitBreaker) {
        health.circuitBreaker = {
          isPaused: ctx.circuitBreaker.isPaused,
          failureRate: ctx.circuitBreaker.failureRate,
        };
      } else {
        health.circuitBreaker = "not configured";
      }

      // Supabase
      if (ctx.ticketStore) {
        try {
          await ctx.ticketStore.keepAlive();
          health.supabase = "healthy";
        } catch (err) {
          health.supabase = `unhealthy: ${err}`;
        }
      } else {
        health.supabase = "not configured";
      }

      // Engines — check in parallel with 5s per-engine timeout
      const engineNames = listEngines();
      const engineChecks = await Promise.all(
        engineNames.map(async (name) => {
          try {
            const engine = resolveEngine(name);
            const result = await Promise.race([
              engine.healthCheck(),
              new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 2000)),
            ]);
            return [name, result] as const;
          } catch {
            return [name, false] as const;
          }
        }),
      );
      const engines: Record<string, boolean> = {};
      for (const [name, ok] of engineChecks) {
        engines[name] = ok;
      }
      health.engines = engines;

      // Delegation config
      health.delegation = Object.fromEntries(
        Object.entries(ctx.config.delegation).map(([role, entry]) => [
          role,
          { engine: entry.engine, registered: hasEngine(entry.engine) },
        ]),
      );

      // Guardrails
      if (ctx.guardrails) {
        health.guardrails = ctx.guardrails.health(ctx.config.teamId);
      } else {
        health.guardrails = "not configured";
      }

      // Ticket workload summary
      if (ctx.ticketStore) {
        try {
          const open = await ctx.ticketStore.listOpen();
          const byStatus: Record<string, number> = {};
          const bySeverity: Record<string, number> = {};
          for (const t of open) {
            byStatus[t.status] = (byStatus[t.status] ?? 0) + 1;
            bySeverity[t.severity] = (bySeverity[t.severity] ?? 0) + 1;
          }
          health.workload = { total: open.length, byStatus, bySeverity };
        } catch {
          health.workload = "failed to query";
        }
      }

      return {
        content: [{ type: "text" as const, text: JSON.stringify(health, null, 2) }],
        details: health,
      };
    },
  });
}

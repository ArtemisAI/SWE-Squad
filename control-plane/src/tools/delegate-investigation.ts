/**
 * Tool: delegate_investigation — Delegate investigation to a configured engine.
 *
 * Engine-agnostic: reads config.delegation.investigator.engine to resolve
 * which CodingEngine to use. Never imports an engine directly.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";
import { resolveEngineForRole, getDelegationConfig } from "../shared/engine-resolver.js";
import { buildInvestigationPrompt } from "../shared/prompt-builder.js";
import { withRetry } from "../utils/retry.js";

export function createDelegateInvestigationTool(ctx: SWEContext) {
  return defineTool({
    name: "delegate_investigation",
    label: "Delegate Investigation",
    description:
      "Delegate root-cause investigation of a ticket to the configured investigation engine. " +
      "Claims the ticket atomically, runs investigation via the engine, and stores the report.",
    parameters: Type.Object({
      ticketId: Type.String({ description: "The ticket ID to investigate" }),
      model: Type.Optional(
        Type.String({ description: "Model override (e.g. sonnet, haiku)" }),
      ),
      timeout: Type.Optional(
        Type.Number({ description: "Timeout in seconds (default 1800)", default: 1800 }),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _extCtx) {
      const startMs = Date.now();
      const store = ctx.ticketStore;
      if (!store) {
        return {
          content: [{ type: "text" as const, text: "Error: Supabase ticket store not configured" }],
          details: {},
        };
      }

      try {
        // 1. Get ticket
        const ticket = await store.get(params.ticketId);
        if (!ticket) {
          return {
            content: [{ type: "text" as const, text: `Ticket not found: ${params.ticketId}` }],
            details: {},
          };
        }

        // 1b. Check investigation attempt limit
        const meta0 = ticket.metadata as Record<string, unknown>;
        const attempts = (meta0.investigation_attempts as number) ?? 0;
        const maxAttempts = ctx.config.cycle?.maxReinvestigations != null
          ? ctx.config.cycle.maxReinvestigations + 1  // maxReinvestigations=1 means 2 total attempts
          : 3; // default: 3 total attempts
        if (attempts >= maxAttempts) {
          return {
            content: [{
              type: "text" as const,
              text: `Investigation attempt limit reached for ${params.ticketId} ` +
                `(${attempts}/${maxAttempts} attempts). ` +
                `Consider escalating this ticket or updating it to 'blocked'.`,
            }],
            details: { ticketId: params.ticketId, attempts, maxAttempts },
          };
        }

        // 2. Claim atomically
        const agentId = ctx.config.teamId;
        const claimed = await store.claimTicket(params.ticketId, agentId);
        if (!claimed) {
          return {
            content: [{ type: "text" as const, text: `Already claimed by another agent: ${params.ticketId}` }],
            details: {},
          };
        }

        // 3. Resolve engine from config
        let engine;
        try {
          engine = resolveEngineForRole("investigator", ctx.config);
        } catch (err) {
          await store.releaseTicket(params.ticketId, ticket.status);
          return {
            content: [{ type: "text" as const, text: `Engine resolution failed: ${err}` }],
            details: {},
          };
        }

        // 4. Health check
        const healthy = await engine.healthCheck();
        if (!healthy) {
          await store.releaseTicket(params.ticketId, ticket.status);
          return {
            content: [{ type: "text" as const, text: `Engine "${engine.name}" health check failed` }],
            details: {},
          };
        }

        // 5. Query memory for similar past investigations
        let memoryContext = "";
        if (ctx.memoryService) {
          try {
            const memories = await ctx.memoryService.query({
              tenantId: ctx.config.teamId,
              projectId: (ticket.metadata as Record<string, unknown>).repo as string ?? "default",
              types: ["investigation", "root_cause", "fix_pattern"],
              limit: 5,
            });
            if (memories.length > 0) {
              memoryContext = "\n\n## Relevant Past Investigations\n\n" +
                memories.map((m, i) =>
                  `### Memory ${i + 1} (confidence: ${m.confidence}, type: ${m.type})\n${m.content.slice(0, 500)}`
                ).join("\n\n");
              ctx.logger.info(
                `Memory: injected ${memories.length} past memories into investigation prompt for ${params.ticketId}`,
              );
              // Record hits for confidence tracking
              for (const m of memories) {
                ctx.memoryService.recordHit(m.id, ctx.config.teamId).catch(() => {});
              }
            } else {
              ctx.logger.debug(`Memory: no relevant memories found for ${params.ticketId}`);
            }
          } catch (e) {
            ctx.logger.warn(`Memory query failed (non-fatal): ${e}`);
          }
        }

        // 6. Build prompt and run
        const delegation = getDelegationConfig("investigator", ctx.config);
        const prompt = buildInvestigationPrompt(ticket) + memoryContext;
        const model = params.model ?? delegation.model;
        const timeout = params.timeout ?? delegation.timeout;

        ctx.logger.info(
          `Investigating ${params.ticketId} via ${engine.name} (model=${model ?? "default"}, timeout=${timeout}s)`,
        );

        // Resolve workspace from ticket repo → config repos → fallback to cwd
        let investigationCwd = ctx.cwd;
        const ticketRepo = (ticket.metadata as Record<string, unknown>)?.repo as string | undefined;
        if (ticketRepo) {
          const repoConfig = ctx.config.repos.find(
            (r: Record<string, unknown>) =>
              r.name === ticketRepo || (r.name as string)?.endsWith(`/${ticketRepo.split("/").pop()}`),
          );
          const localPath = repoConfig?.localPath ?? repoConfig?.local_path;
          if (localPath) {
            investigationCwd = String(localPath).replace("~", process.env.HOME ?? "/home/agent");
          }
        }

        const result = await engine.run(prompt, {
          cwd: investigationCwd,
          readOnly: true,
          model,
          timeout,
        });

        // 6. Increment attempt counter now that engine has completed
        {
          const meta = ticket.metadata as Record<string, unknown>;
          meta.investigation_attempts = (attempts + 1);
          ticket.metadata = meta;
        }

        // 6b. Check for engine failure
        if (result.returncode !== 0) {
          ticket.updatedAt = new Date().toISOString();
          const meta = ticket.metadata as Record<string, unknown>;
          meta.lastInvestigationError = result.stderr || "Engine returned non-zero";
          await withRetry(() => store.update(ticket), { label: "store.update after investigation failure" });
          await withRetry(
            () => store.releaseTicket(params.ticketId, ticket.status),
            { label: "releaseTicket after investigation failure" },
          );

          ctx.outcomeTracker?.record({
            tool: "delegate_investigation",
            ticketId: params.ticketId,
            engine: engine.name,
            success: false,
            error: result.stderr || "non-zero exit",
            durationMs: Date.now() - startMs,
            costUsd: result.costUsd ?? undefined,
            timestamp: new Date(),
          });

          return {
            content: [{
              type: "text" as const,
              text: `Investigation failed for ${params.ticketId}: ${result.stderr || "non-zero exit"}\n` +
                `Engine: ${engine.name}, returncode=${result.returncode}`,
            }],
            details: { ticketId: params.ticketId, returncode: result.returncode },
          };
        }

        // 6b. Update ticket with report (retry — connection may be stale after long engine.run)
        ticket.investigationReport = result.stdout || "Investigation produced no output";
        ticket.status = "investigation_complete";
        ticket.investigationSessionId = result.sessionId ?? null;
        ticket.updatedAt = new Date().toISOString();
        await withRetry(() => store.update(ticket), { label: "store.update after investigation" });

        // 7. Release claim (retry for same reason)
        await withRetry(
          () => store.releaseTicket(params.ticketId, "investigation_complete"),
          { label: "releaseTicket after investigation" },
        );

        // 8. Store investigation result in memory for future reuse
        if (ctx.memoryService && result.stdout) {
          try {
            await ctx.memoryService.store(
              ctx.config.teamId,
              (ticket.metadata as Record<string, unknown>).repo as string ?? "default",
              {
                agentId: ctx.config.teamId,
                engine: engine.name,
                type: "investigation",
                content: `Investigation for ${ticket.title} (${params.ticketId}):\n${result.stdout.slice(0, 2000)}`,
                tags: ["investigation", ticket.severity, params.ticketId],
              },
            );
            ctx.logger.info(`Memory: stored investigation result for ${params.ticketId}`);
          } catch (e) {
            ctx.logger.warn(`Memory store failed (non-fatal): ${e}`);
          }
        }

        // 9. Record outcome
        ctx.outcomeTracker?.record({
          tool: "delegate_investigation",
          ticketId: params.ticketId,
          engine: engine.name,
          success: true,
          durationMs: Date.now() - startMs,
          costUsd: result.costUsd ?? undefined,
          timestamp: new Date(),
        });

        return {
          content: [{
            type: "text" as const,
            text: `Investigation complete for ${params.ticketId}.\n\n` +
              `Engine: ${engine.name}, Cost: $${result.costUsd?.toFixed(4) ?? "unknown"}\n\n` +
              `Report summary:\n${(result.stdout || "").slice(0, 500)}`,
          }],
          details: {
            ticketId: params.ticketId,
            engine: engine.name,
            cost: result.costUsd,
            model: result.model,
          },
        };
      } catch (err) {
        // Record failure outcome
        ctx.outcomeTracker?.record({
          tool: "delegate_investigation",
          ticketId: params.ticketId,
          success: false,
          error: String(err),
          durationMs: Date.now() - startMs,
          timestamp: new Date(),
        });

        // Store error in ticket metadata so it's visible in ticket_list
        try {
          const ticket = await store.get(params.ticketId);
          if (ticket) {
            const meta = ticket.metadata as Record<string, unknown>;
            meta.lastInvestigationError = String(err).slice(0, 500);
            ticket.metadata = meta;
            ticket.updatedAt = new Date().toISOString();
            await store.update(ticket);
          }
        } catch { /* non-fatal — best effort error recording */ }

        // Release claim on error
        try {
          await store.releaseTicket(params.ticketId);
        } catch { /* non-fatal */ }

        return {
          content: [{ type: "text" as const, text: `Investigation failed for ${params.ticketId}: ${err}` }],
          details: {},
        };
      }
    },
  });
}

/**
 * Tool: ticket_list — List tickets from the store, filtered by status/severity.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";

export function createTicketListTool(ctx: SWEContext) {
  return defineTool({
    name: "ticket_list",
    label: "List Tickets",
    description:
      "List SWE tickets from the store. Filter by status, severity, repo, or limit results.",
    parameters: Type.Object({
      status: Type.Optional(
        Type.String({
          description:
            "Filter by ticket status. Single value (e.g. 'open') or comma-separated " +
            "(e.g. 'open,triaged,investigating'). Use 'pipeline' for all non-resolved statuses.",
        }),
      ),
      severity: Type.Optional(
        Type.String({ description: "Filter by severity (critical, high, medium, low)" }),
      ),
      repo: Type.Optional(
        Type.String({ description: "Filter by repository (owner/repo)" }),
      ),
      limit: Type.Optional(
        Type.Number({ description: "Max tickets to return (default 20)", default: 20 }),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _extCtx) {
      const store = ctx.ticketStore;
      if (!store) {
        return {
          content: [{ type: "text" as const, text: "Error: Supabase ticket store not configured" }],
          details: {},
        };
      }

      try {
        const limit = params.limit ?? 20;
        let tickets;

        if (params.status === "pipeline") {
          // Fetch all non-resolved statuses in one batch
          const pipelineStatuses = [
            "open", "triaged", "investigating", "investigation_complete",
            "in_development", "in_review", "testing",
          ];
          const allTickets = await Promise.all(
            pipelineStatuses.map((s) => store.listByStatus(s, limit)),
          );
          tickets = allTickets.flat();
        } else if (params.status && params.status.includes(",")) {
          // Comma-separated statuses — query each and merge
          const statuses = params.status.split(",").map((s) => s.trim());
          const allTickets = await Promise.all(
            statuses.map((s) => store.listByStatus(s, limit)),
          );
          tickets = allTickets.flat();
        } else if (params.status) {
          tickets = await store.listByStatus(params.status, limit);
        } else {
          // Default: show the full pipeline (all non-resolved statuses).
          // The LLM often omits the status param, so this must match
          // the "pipeline" behaviour to avoid hiding in_review/testing tickets.
          const defaultStatuses = [
            "open", "triaged", "investigating", "investigation_complete",
            "in_development", "in_review", "testing",
          ];
          const allTickets = await Promise.all(
            defaultStatuses.map((s) => store.listByStatus(s, limit)),
          );
          tickets = allTickets.flat();
        }

        // Apply client-side filters
        if (params.severity) {
          tickets = tickets.filter((t) => t.severity === params.severity);
        }
        if (params.repo) {
          tickets = tickets.filter((t) => {
            const meta = t.metadata as Record<string, unknown>;
            return meta?.repo === params.repo || meta?.source_repo === params.repo;
          });
        }

        // Sort by pipeline priority: tickets further in the pipeline appear first.
        // This prevents the limit from hiding actionable in_review/testing tickets
        // behind a wall of open tickets.
        const pipelinePriority: Record<string, number> = {
          testing: 0, in_review: 1, in_development: 2,
          investigation_complete: 3, investigating: 4, triaged: 5, open: 6,
        };
        tickets.sort((a, b) =>
          (pipelinePriority[a.status] ?? 9) - (pipelinePriority[b.status] ?? 9),
        );

        // Format for LLM consumption with pipeline-relevant metadata
        const summary = tickets.slice(0, limit).map((t) => {
          const meta = t.metadata as Record<string, unknown> | undefined;
          const entry: Record<string, unknown> = {
            id: t.ticketId,
            title: t.title,
            status: t.status,
            severity: t.severity,
            assignee: t.assignedTo,
            created: t.createdAt,
          };
          // Include pipeline-relevant fields so the LLM can make decisions
          if (meta?.prUrl) entry.prUrl = meta.prUrl;
          if (meta?.reviewVerdict) entry.reviewVerdict = meta.reviewVerdict;
          if (meta?.repo) entry.repo = meta.repo;
          if (meta?.devBranch) entry.devBranch = meta.devBranch;
          if (meta?.investigation_attempts) entry.investigationAttempts = meta.investigation_attempts;
          if (meta?.lastDevError) entry.lastDevError = meta.lastDevError;
          return entry;
        });

        return {
          content: [{
            type: "text" as const,
            text: tickets.length === 0
              ? "No tickets found matching the filters."
              : JSON.stringify(summary, null, 2),
          }],
          details: { count: summary.length },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `Error listing tickets: ${err}` }],
          details: {},
        };
      }
    },
  });
}

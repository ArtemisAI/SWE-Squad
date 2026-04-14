/**
 * Tool: ticket_update — Update a ticket's status, notes, or assignee.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";

export function createTicketUpdateTool(ctx: SWEContext) {
  return defineTool({
    name: "ticket_update",
    label: "Update Ticket",
    description:
      "Update a ticket's status, notes, or assignee. Enforces resolution audit when setting status to resolved.",
    parameters: Type.Object({
      ticketId: Type.String({ description: "The ticket ID to update" }),
      status: Type.Optional(
        Type.String({ description: "New status (e.g. investigating, in_development, resolved, failed)" }),
      ),
      notes: Type.Optional(
        Type.String({ description: "Notes to append to the ticket" }),
      ),
      assignee: Type.Optional(
        Type.String({ description: "Assignee agent/team name" }),
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
        const ticket = await store.get(params.ticketId);
        if (!ticket) {
          return {
            content: [{ type: "text" as const, text: `Ticket not found: ${params.ticketId}` }],
            details: {},
          };
        }

        // Resolution audit: verify ticket has investigation report + proposed fix
        if (params.status === "resolved") {
          if (!ticket.investigationReport && !ticket.proposedFix) {
            return {
              content: [{
                type: "text" as const,
                text: `Resolution audit failed for ${params.ticketId}: no investigation report or proposed fix`,
              }],
              details: {},
            };
          }
        }

        // Apply updates
        if (params.status) {
          ticket.status = params.status as typeof ticket.status;
        }
        if (params.assignee !== undefined) {
          ticket.assignedTo = params.assignee;
        }
        if (params.notes) {
          const meta = ticket.metadata as Record<string, unknown>;
          const existingNotes = (meta.notes as string) ?? "";
          meta.notes = existingNotes
            ? `${existingNotes}\n---\n${params.notes}`
            : params.notes;
        }
        ticket.updatedAt = new Date().toISOString();

        await store.update(ticket);

        return {
          content: [{
            type: "text" as const,
            text: `Updated ${params.ticketId}: status=${ticket.status}, assignee=${ticket.assignedTo ?? "unassigned"}`,
          }],
          details: { ticketId: params.ticketId, status: ticket.status },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `Error updating ticket: ${err}` }],
          details: {},
        };
      }
    },
  });
}

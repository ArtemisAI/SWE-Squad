/**
 * Tool: ticket_create — Create a new SWE ticket with fingerprint dedup.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";
import { createTicket } from "../models/ticket.js";

export function createTicketCreateTool(ctx: SWEContext) {
  return defineTool({
    name: "ticket_create",
    label: "Create Ticket",
    description:
      "Create a new SWE ticket. Deduplicates by fingerprint — returns existing ticket if duplicate.",
    parameters: Type.Object({
      title: Type.String({ description: "Ticket title" }),
      description: Type.String({ description: "Ticket description" }),
      severity: Type.String({ description: "Severity: critical, high, medium, low" }),
      repo: Type.Optional(
        Type.String({ description: "Repository (owner/repo)" }),
      ),
      source: Type.Optional(
        Type.String({ description: "Source of the ticket (e.g. github, monitor, manual)" }),
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
        // Generate fingerprint for dedup
        const fingerprint = `manual-${params.title.toLowerCase().replace(/\s+/g, "-").slice(0, 60)}`;

        // Check for duplicate
        const knownFps = await store.knownFingerprints;
        if (knownFps.has(fingerprint)) {
          return {
            content: [{
              type: "text" as const,
              text: `Duplicate: ticket with fingerprint "${fingerprint}" already exists`,
            }],
            details: { duplicate: true, fingerprint },
          };
        }

        const ticket = createTicket(params.title, params.description, {
          severity: params.severity as "critical" | "high" | "medium" | "low",
          metadata: {
            fingerprint,
            source: params.source ?? "manual",
            ...(params.repo ? { repo: params.repo } : {}),
          },
        });

        await store.add(ticket);

        return {
          content: [{
            type: "text" as const,
            text: `Ticket ${ticket.ticketId} created: "${ticket.title}" (${ticket.severity})`,
          }],
          details: { ticketId: ticket.ticketId, fingerprint },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `Error creating ticket: ${err}` }],
          details: {},
        };
      }
    },
  });
}

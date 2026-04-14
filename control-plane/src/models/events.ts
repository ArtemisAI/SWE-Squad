/**
 * SWE event model — TypeScript / Zod port.
 *
 * Defines the event envelope exchanged between SWE team agents
 * via the A2A hub or standalone dispatch.
 *
 * Ported from: src/swe_team/events.py
 */

import { z } from "zod";
import crypto from "node:crypto";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function randomHex(bytes: number): string {
  return crypto.randomBytes(bytes).toString("hex");
}

function nowISO(): string {
  return new Date().toISOString();
}

// ---------------------------------------------------------------------------
// Schema + Type
// ---------------------------------------------------------------------------

export const SWEEventSchema = z.object({
  eventId: z.string().default(() => randomHex(8)),
  eventType: z.string(),
  ticketId: z.string(),
  timestamp: z.string().default(nowISO),
  source: z.string(),
  data: z.record(z.string(), z.unknown()).default({}),
});

export type SWEEvent = z.infer<typeof SWEEventSchema>;

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

/**
 * Create a new SWEEvent with defaults applied.
 * Only `eventType`, `ticketId`, and `source` are required.
 */
export function createEvent(
  eventType: string,
  ticketId: string,
  source: string,
  data?: Record<string, unknown>,
): SWEEvent {
  return SWEEventSchema.parse({
    eventType,
    ticketId,
    source,
    ...(data !== undefined ? { data } : {}),
  });
}

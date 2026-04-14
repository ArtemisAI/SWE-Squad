/**
 * Pi-agent extension: Stability Gate — Block delegation when system is unstable.
 *
 * Discovered automatically by pi-agent from .pi/extensions/.
 *
 * Wires up the SupabaseTicketStore directly from env vars so the gate
 * can count open critical/high tickets. If Supabase is not configured,
 * the gate falls back to "fail open" (PASS).
 */

import { createStabilityGateExtension } from "../../src/extensions/stability-gate.js";
import { SupabaseClient } from "../../src/providers/supabase/client.js";
import { SupabaseTicketStore } from "../../src/providers/supabase/store.js";

// Build a ticket store from env vars (same source as main.ts)
let ticketStoreProvider: { listOpen(limit?: number): Promise<Array<{ severity: string }>> } | undefined;

const supabaseUrl = process.env.SUPABASE_URL;
const supabaseKey = process.env.SUPABASE_ANON_KEY;
const teamId = process.env.SWE_TEAM_ID ?? "swe-squad-1";

if (supabaseUrl && supabaseKey) {
  const client = new SupabaseClient({ url: supabaseUrl, key: supabaseKey });
  ticketStoreProvider = new SupabaseTicketStore({ client, teamId });
}

export default createStabilityGateExtension({
  ticketStoreProvider,
  cacheSeconds: 60,
  maxOpenCritical: 2,
  maxOpenHigh: 20,
  onBlocked: (tool, result) => {
    console.error(
      `[StabilityGate] Blocked ${tool}: ${result.reason} ` +
        `(${result.criticalCount} critical, ${result.highCount} high)`,
    );
  },
  onWarn: (tool, result) => {
    console.warn(
      `[StabilityGate] Warning for ${tool}: ${result.reason} ` +
        `(${result.criticalCount} critical, ${result.highCount} high)`,
    );
  },
});

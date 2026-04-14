/**
 * Supabase-backed ticket store for the Autonomous SWE Team.
 *
 * Drop-in replacement for an in-memory TicketStore that persists tickets
 * to Supabase PostgreSQL via the PostgREST API.
 *
 * Each team instance is scoped by `teamId` so multiple SWE teams can
 * share the same Supabase project without overlap.
 *
 * Ported from: src/swe_team/supabase_store.py
 */

import type { SWETicket } from "../../models/ticket.js";
import { SupabaseClient } from "./client.js";

// ---------------------------------------------------------------------------
// Statuses considered "closed" for listOpen() filtering.
// Matches the Python _CLOSED_STATUSES frozenset.
// ---------------------------------------------------------------------------

const CLOSED_STATUSES = new Set([
  "resolved",
  "closed",
  "acknowledged",
  "failed",
  "blocked",
]);

// Status mapping: Python-side states that lack a Supabase CHECK constraint
// equivalent are mapped before writing.
const STATUS_MAP: Record<string, string> = {
  failed: "closed",
  blocked: "acknowledged",
};

// ---------------------------------------------------------------------------
// camelCase <-> snake_case conversion helpers
// ---------------------------------------------------------------------------

/** Map of camelCase ticket field -> snake_case Supabase column. */
const CAMEL_TO_SNAKE: Record<string, string> = {
  ticketId: "ticket_id",
  createdAt: "created_at",
  updatedAt: "updated_at",
  assignedTo: "assigned_to",
  ticketType: "ticket_type",
  sourceModule: "source_module",
  errorLog: "error_log",
  relatedTickets: "related_tickets",
  blockedBy: "blocked_by",
  blocking: "blocking",
  investigationReport: "investigation_report",
  proposedFix: "proposed_fix",
  testResults: "test_results",
  deploymentId: "deployment_id",
  rollbackReason: "rollback_reason",
  investigationSessionId: "investigation_session_id",
  developmentSessionId: "development_session_id",
  projectId: "project_id",
  parentTicketId: "parent_ticket_id",
};

/** Inverse map: snake_case -> camelCase. */
const SNAKE_TO_CAMEL: Record<string, string> = Object.fromEntries(
  Object.entries(CAMEL_TO_SNAKE).map(([k, v]) => [v, k]),
);

// Fields stripped before sending to Supabase (not in the DDL schema).
const STRIP_FIELDS = new Set(["ticketType", "blockedBy", "blocking"]);
// Their snake_case equivalents (for safety during row building).
const STRIP_SNAKE = new Set(["ticket_type", "blocked_by", "blocking"]);

/**
 * Convert a camelCase SWETicket to a snake_case row dict for Supabase.
 */
export function ticketToRow(
  ticket: SWETicket,
  teamId: string,
): Record<string, unknown> {
  const row: Record<string, unknown> = {};

  for (const [key, value] of Object.entries(ticket)) {
    if (STRIP_FIELDS.has(key)) continue;
    const snakeKey = CAMEL_TO_SNAKE[key] ?? key;
    if (STRIP_SNAKE.has(snakeKey)) continue;
    row[snakeKey] = value;
  }

  row["team_id"] = teamId;

  // Map statuses not present in the Supabase CHECK constraint.
  const status = row["status"] as string | undefined;
  if (status && status in STATUS_MAP) {
    row["status"] = STATUS_MAP[status];
  }

  // Ensure JSONB fields are objects, not JSON strings.
  for (const jsonbKey of ["labels", "related_tickets", "metadata", "test_results"]) {
    const val = row[jsonbKey];
    if (typeof val === "string") {
      try {
        row[jsonbKey] = JSON.parse(val);
      } catch {
        // leave as-is
      }
    }
  }

  // Session ID fields: only include if populated (avoids 400 on older schemas).
  if (row["investigation_session_id"] == null) delete row["investigation_session_id"];
  if (row["development_session_id"] == null) delete row["development_session_id"];

  return row;
}

/**
 * Convert a snake_case Supabase row back to a camelCase SWETicket.
 */
export function rowToTicket(row: Record<string, unknown>): SWETicket {
  const ticket: Record<string, unknown> = {};

  for (const [key, value] of Object.entries(row)) {
    // Skip Supabase-only columns not in the TS model.
    if (key === "team_id" || key === "embedding" || key === "memory_confidence" || key === "memory_accessed_at") {
      continue;
    }
    const camelKey = SNAKE_TO_CAMEL[key] ?? key;
    ticket[camelKey] = value;
  }

  // Normalise metadata: PostgREST usually returns JSONB as objects, but
  // manual PATCH calls may store a JSON string.
  if (typeof ticket["metadata"] === "string") {
    try {
      ticket["metadata"] = JSON.parse(ticket["metadata"] as string);
    } catch {
      ticket["metadata"] = {};
    }
  }
  if (ticket["metadata"] == null) {
    ticket["metadata"] = {};
  }

  // Ensure session ID fields are present (graceful for rows predating these columns).
  ticket["investigationSessionId"] ??= null;
  ticket["developmentSessionId"] ??= null;

  // Default arrays for fields that may not come back from Supabase.
  ticket["blockedBy"] ??= [];
  ticket["blocking"] ??= [];
  ticket["labels"] ??= [];
  ticket["relatedTickets"] ??= [];
  ticket["ticketType"] ??= "unknown";

  return ticket as unknown as SWETicket;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export class SupabaseTicketStore {
  private readonly client: SupabaseClient;
  private readonly teamId: string;
  private fingerprintCache: Set<string> | null = null;

  constructor(options: { client: SupabaseClient; teamId?: string }) {
    this.client = options.client;
    this.teamId = options.teamId ?? "default";
  }

  // -----------------------------------------------------------------------
  // CRUD
  // -----------------------------------------------------------------------

  /** Upsert a ticket (insert or update on conflict). */
  async add(ticket: SWETicket): Promise<void> {
    const row = ticketToRow(ticket, this.teamId);
    await this.client.insert("swe_tickets", row);

    // Update fingerprint cache.
    const fp = (ticket.metadata as Record<string, unknown>)?.fingerprint;
    if (typeof fp === "string" && this.fingerprintCache != null) {
      this.fingerprintCache.add(fp);
    }
  }

  /** Return a ticket by ID, or `null`. */
  async get(ticketId: string): Promise<SWETicket | null> {
    const rows = await this.client.query<Record<string, unknown>>(
      "swe_tickets",
      {
        ticket_id: `eq.${ticketId}`,
        team_id: `eq.${this.teamId}`,
      },
    );
    if (rows.length === 0) return null;
    return rowToTicket(rows[0]);
  }

  /** Update an existing ticket (PATCH by ticket_id + team_id). */
  async update(ticket: SWETicket): Promise<void> {
    const row = ticketToRow(ticket, this.teamId);
    // Remove the primary key from the body; it goes in the filter.
    const { ticket_id: _, team_id: __, ...body } = row;

    await this.client.update(
      "swe_tickets",
      {
        ticket_id: `eq.${ticket.ticketId}`,
        team_id: `eq.${this.teamId}`,
      },
      body,
    );
  }

  // -----------------------------------------------------------------------
  // Queries
  // -----------------------------------------------------------------------

  /** Return all tickets for this team, newest first. */
  async listAll(limit = 500): Promise<SWETicket[]> {
    const rows = await this.client.query<Record<string, unknown>>(
      "swe_tickets",
      {
        team_id: `eq.${this.teamId}`,
        order: "created_at.desc",
        limit: String(limit),
      },
    );
    return rows.map(rowToTicket);
  }

  /**
   * Return all tickets that are not resolved, closed, acknowledged, failed,
   * or blocked.
   */
  async listOpen(limit = 500): Promise<SWETicket[]> {
    const closedList = [...CLOSED_STATUSES].join(",");
    const rows = await this.client.query<Record<string, unknown>>(
      "swe_tickets",
      {
        team_id: `eq.${this.teamId}`,
        status: `not.in.(${closedList})`,
        order: "created_at.desc",
        limit: String(limit),
      },
    );
    return rows.map(rowToTicket);
  }

  /** Return tickets with the given status. */
  async listByStatus(status: string, limit = 500): Promise<SWETicket[]> {
    const rows = await this.client.query<Record<string, unknown>>(
      "swe_tickets",
      {
        team_id: `eq.${this.teamId}`,
        status: `eq.${status}`,
        order: "created_at.desc",
        limit: String(limit),
      },
    );
    return rows.map(rowToTicket);
  }

  /** Return tickets resolved within the last N hours (default 24). */
  async listRecentlyResolved(hours = 24): Promise<SWETicket[]> {
    const cutoff = new Date(Date.now() - hours * 3600_000).toISOString();
    const rows = await this.client.query<Record<string, unknown>>(
      "swe_tickets",
      {
        team_id: `eq.${this.teamId}`,
        status: "eq.resolved",
        updated_at: `gte.${cutoff}`,
        order: "updated_at.desc",
      },
    );
    return rows.map(rowToTicket);
  }

  // -----------------------------------------------------------------------
  // Atomic checkout (RPC)
  // -----------------------------------------------------------------------

  /**
   * Atomically claim a ticket using Postgres row-level locking via RPC.
   *
   * Returns the agent ID if claimed successfully, or `null` if another
   * agent holds it or the ticket is in a non-claimable state.
   */
  async claimTicket(
    ticketId: string,
    agentId: string,
  ): Promise<string | null> {
    try {
      const result = await this.client.rpc<boolean>("claim_ticket", {
        p_ticket_id: ticketId,
        p_agent_id: agentId,
      });
      if (result) return agentId;
    } catch {
      // RPC error — fall through to REST fallback
    }

    // Fallback: claim via REST PATCH for non-open states (investigation_complete,
    // in_review, rework_requested) that the original RPC doesn't handle.
    // Less atomic than the RPC but sufficient for single-daemon operation.
    try {
      const tickets = await this.client.query<{ assigned_to: string | null; status: string }>(
        "swe_tickets",
        { "ticket_id": `eq.${ticketId}`, "team_id": `eq.${this.teamId}`, "select": "assigned_to,status" },
      );
      if (!tickets || tickets.length === 0) return null;
      const ticket = tickets[0];
      // Already claimed by someone else
      if (ticket.assigned_to && ticket.assigned_to !== agentId) return null;
      // Already ours
      if (ticket.assigned_to === agentId) return agentId;
      // Claim it
      await this.client.update(
        "swe_tickets",
        { "ticket_id": `eq.${ticketId}`, "team_id": `eq.${this.teamId}` },
        { assigned_to: agentId, updated_at: new Date().toISOString() },
      );
      return agentId;
    } catch {
      return null;
    }
  }

  /**
   * Release a ticket claim, resetting it to the given status (default "open").
   */
  async releaseTicket(
    ticketId: string,
    resetStatus = "open",
  ): Promise<void> {
    try {
      await this.client.rpc("release_ticket", {
        p_ticket_id: ticketId,
        p_reset_status: resetStatus,
      });
    } catch {
      // Non-fatal: log at caller level if needed.
    }
  }

  // -----------------------------------------------------------------------
  // Fingerprint dedup
  // -----------------------------------------------------------------------

  /**
   * Fingerprints of all stored tickets for this team (for dedup).
   *
   * Lazily loaded and cached. Returns a new Set each call (safe to mutate).
   */
  get knownFingerprints(): Promise<Set<string>> {
    return this.loadFingerprints();
  }

  private async loadFingerprints(): Promise<Set<string>> {
    if (this.fingerprintCache != null) return new Set(this.fingerprintCache);

    const rows = await this.client.query<Record<string, unknown>>(
      "swe_tickets",
      {
        team_id: `eq.${this.teamId}`,
        select: "metadata",
      },
    );

    const fps = new Set<string>();
    for (const row of rows) {
      let meta = row["metadata"] as Record<string, unknown> | string | null;
      if (typeof meta === "string") {
        try {
          meta = JSON.parse(meta) as Record<string, unknown>;
        } catch {
          continue;
        }
      }
      const fp = (meta as Record<string, unknown> | null)?.fingerprint;
      if (typeof fp === "string") fps.add(fp);
    }

    this.fingerprintCache = fps;
    return new Set(fps);
  }

  // -----------------------------------------------------------------------
  // Health / keep-alive
  // -----------------------------------------------------------------------

  /**
   * Lightweight keep-alive ping — issues a trivial SELECT to prevent
   * Supabase free-tier database pausing after 7 days of inactivity.
   */
  async keepAlive(): Promise<void> {
    await this.client.query("swe_tickets", {
      select: "ticket_id",
      limit: "1",
    });
  }
}

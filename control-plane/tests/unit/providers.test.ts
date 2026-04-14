/**
 * Unit tests for Supabase, GitHub, and Telegram providers.
 *
 * All external calls (fetch, execFileSync) are mocked.
 * No network access or real API keys required.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// ---------------------------------------------------------------------------
// Mock node:child_process for GitHub integration
// ---------------------------------------------------------------------------

vi.mock("node:child_process", () => ({
  execFileSync: vi.fn(),
}));

import { execFileSync } from "node:child_process";

// =========================================================================
// 1. SupabaseClient tests
// =========================================================================

import {
  SupabaseClient,
  SupabaseError,
} from "../../src/providers/supabase/client.js";

describe("SupabaseClient", () => {
  let client: SupabaseClient;
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);

    client = new SupabaseClient({
      url: "http://localhost:8000",
      key: "test-key-123",
      timeout: 5000,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // -----------------------------------------------------------------------
  // query()
  // -----------------------------------------------------------------------

  describe("query()", () => {
    it("builds correct URL with no params", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify([{ id: 1 }]),
      });

      await client.query("swe_tickets");

      const [url] = mockFetch.mock.calls[0];
      expect(url).toBe("http://localhost:8000/rest/v1/swe_tickets");
    });

    it("builds correct URL with PostgREST filter params", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify([]),
      });

      await client.query("swe_tickets", {
        status: "eq.open",
        team_id: "eq.default",
      });

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("status=eq.open");
      expect(url).toContain("team_id=eq.default");
    });

    it("sends correct headers with apikey and Bearer token", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => "[]",
      });

      await client.query("swe_tickets");

      const [, init] = mockFetch.mock.calls[0];
      expect(init.headers.apikey).toBe("test-key-123");
      expect(init.headers.Authorization).toBe("Bearer test-key-123");
      expect(init.headers.Prefer).toBe("return=representation");
    });

    it("returns parsed JSON array", async () => {
      const data = [{ ticket_id: "t-1", status: "open" }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify(data),
      });

      const result = await client.query("swe_tickets");
      expect(result).toEqual(data);
    });

    it("uses GET method", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => "[]",
      });

      await client.query("swe_tickets");

      const [, init] = mockFetch.mock.calls[0];
      expect(init.method).toBe("GET");
    });

    it("throws SupabaseError on non-2xx response", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 400,
        text: async () => '{"message":"Bad request"}',
      });

      await expect(client.query("swe_tickets")).rejects.toThrow(SupabaseError);
    });

    it("preserves PostgREST special characters in filter values", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => "[]",
      });

      await client.query("swe_tickets", {
        status: "not.in.(resolved,closed,acknowledged,failed,blocked)",
      });

      const [url] = mockFetch.mock.calls[0];
      // The parentheses, commas, and dots must NOT be percent-encoded
      expect(url).toContain("not.in.(resolved,closed,acknowledged,failed,blocked)");
    });
  });

  // -----------------------------------------------------------------------
  // insert()
  // -----------------------------------------------------------------------

  describe("insert()", () => {
    it("sends POST with correct headers", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify([{ ticket_id: "t-1" }]),
      });

      await client.insert("swe_tickets", { ticket_id: "t-1", status: "open" });

      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe("http://localhost:8000/rest/v1/swe_tickets");
      expect(init.method).toBe("POST");
      expect(init.headers.Prefer).toBe(
        "return=representation,resolution=merge-duplicates",
      );
    });

    it("sends JSON body", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify([{}]),
      });

      const data = { ticket_id: "t-1", status: "open", severity: "high" };
      await client.insert("swe_tickets", data);

      const [, init] = mockFetch.mock.calls[0];
      expect(JSON.parse(init.body)).toEqual(data);
    });

    it("returns inserted rows", async () => {
      const rows = [{ ticket_id: "t-1", status: "open" }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify(rows),
      });

      const result = await client.insert("swe_tickets", { ticket_id: "t-1" });
      expect(result).toEqual(rows);
    });
  });

  // -----------------------------------------------------------------------
  // update()
  // -----------------------------------------------------------------------

  describe("update()", () => {
    it("sends PATCH with filter params in URL", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify([{}]),
      });

      await client.update(
        "swe_tickets",
        { ticket_id: "eq.t-1", team_id: "eq.default" },
        { status: "resolved" },
      );

      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toContain("ticket_id=eq.t-1");
      expect(url).toContain("team_id=eq.default");
      expect(init.method).toBe("PATCH");
    });

    it("sends update data as JSON body", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify([{}]),
      });

      const updateData = { status: "resolved", updated_at: "2026-01-01T00:00:00Z" };
      await client.update(
        "swe_tickets",
        { ticket_id: "eq.t-1" },
        updateData,
      );

      const [, init] = mockFetch.mock.calls[0];
      expect(JSON.parse(init.body)).toEqual(updateData);
    });

    it("includes Prefer: return=representation header", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify([{}]),
      });

      await client.update("swe_tickets", { ticket_id: "eq.t-1" }, { status: "open" });

      const [, init] = mockFetch.mock.calls[0];
      expect(init.headers.Prefer).toBe("return=representation");
    });
  });

  // -----------------------------------------------------------------------
  // rpc()
  // -----------------------------------------------------------------------

  describe("rpc()", () => {
    it("sends POST to /rpc/{fnName}", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify(true),
      });

      await client.rpc("claim_ticket", {
        p_ticket_id: "t-1",
        p_agent_id: "agent-1",
      });

      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe("http://localhost:8000/rest/v1/rpc/claim_ticket");
      expect(init.method).toBe("POST");
    });

    it("sends params as JSON body", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify(true),
      });

      const params = { p_ticket_id: "t-1", p_agent_id: "agent-1" };
      await client.rpc("claim_ticket", params);

      const [, init] = mockFetch.mock.calls[0];
      expect(JSON.parse(init.body)).toEqual(params);
    });

    it("returns parsed result", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify(true),
      });

      const result = await client.rpc<boolean>("claim_ticket", {});
      expect(result).toBe(true);
    });

    it("sends request without body when params omitted", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => JSON.stringify({ count: 42 }),
      });

      await client.rpc("get_count");

      const [, init] = mockFetch.mock.calls[0];
      expect(init.body).toBeUndefined();
    });
  });

  // -----------------------------------------------------------------------
  // healthCheck()
  // -----------------------------------------------------------------------

  describe("healthCheck()", () => {
    it("returns true on 200 OK", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
      });

      const result = await client.healthCheck();
      expect(result).toBe(true);
    });

    it("returns true on 401 (service alive but auth required)", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 401,
      });

      const result = await client.healthCheck();
      expect(result).toBe(true);
    });

    it("returns false on 500 server error", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 500,
      });

      const result = await client.healthCheck();
      expect(result).toBe(false);
    });

    it("returns false on network error", async () => {
      mockFetch.mockRejectedValueOnce(new Error("ECONNREFUSED"));

      const result = await client.healthCheck();
      expect(result).toBe(false);
    });

    it("calls base URL (without /rest/v1)", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
      });

      await client.healthCheck();

      const [url] = mockFetch.mock.calls[0];
      expect(url).toBe("http://localhost:8000");
      expect(url).not.toContain("/rest/v1");
    });
  });

  // -----------------------------------------------------------------------
  // URL trailing slash normalization
  // -----------------------------------------------------------------------

  describe("URL normalization", () => {
    it("strips trailing slashes from base URL", async () => {
      const c = new SupabaseClient({
        url: "http://localhost:8000///",
        key: "k",
      });

      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => "[]",
      });

      await c.query("test");

      const [url] = mockFetch.mock.calls[0];
      expect(url).toBe("http://localhost:8000/rest/v1/test");
    });
  });

  // -----------------------------------------------------------------------
  // delete()
  // -----------------------------------------------------------------------

  describe("delete()", () => {
    it("sends DELETE with filter params", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: async () => "",
      });

      await client.delete("swe_tickets", { ticket_id: "eq.t-1" });

      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toContain("ticket_id=eq.t-1");
      expect(init.method).toBe("DELETE");
    });

    it("throws SupabaseError on non-2xx", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 404,
        text: async () => "not found",
      });

      await expect(
        client.delete("swe_tickets", { ticket_id: "eq.missing" }),
      ).rejects.toThrow(SupabaseError);
    });
  });
});

// =========================================================================
// 2. SupabaseTicketStore tests
// =========================================================================

import {
  SupabaseTicketStore,
  ticketToRow,
  rowToTicket,
} from "../../src/providers/supabase/store.js";

describe("SupabaseTicketStore", () => {
  let mockClient: {
    query: ReturnType<typeof vi.fn>;
    insert: ReturnType<typeof vi.fn>;
    update: ReturnType<typeof vi.fn>;
    rpc: ReturnType<typeof vi.fn>;
  };
  let store: SupabaseTicketStore;

  beforeEach(() => {
    mockClient = {
      query: vi.fn().mockResolvedValue([]),
      insert: vi.fn().mockResolvedValue([{}]),
      update: vi.fn().mockResolvedValue([{}]),
      rpc: vi.fn().mockResolvedValue(true),
    };

    store = new SupabaseTicketStore({
      client: mockClient as unknown as SupabaseClient,
      teamId: "test-team",
    });
  });

  // -----------------------------------------------------------------------
  // ticketToRow() helper
  // -----------------------------------------------------------------------

  describe("ticketToRow()", () => {
    it("converts camelCase fields to snake_case", () => {
      const row = ticketToRow(
        {
          ticketId: "t-1",
          title: "Test",
          description: "Desc",
          severity: "high",
          status: "open",
          createdAt: "2026-01-01T00:00:00Z",
          updatedAt: "2026-01-01T00:00:00Z",
          assignedTo: null,
          labels: [],
          ticketType: "bug",
          sourceModule: "auth",
          errorLog: null,
          relatedTickets: [],
          blockedBy: [],
          blocking: [],
          metadata: {},
          investigationReport: null,
          proposedFix: null,
          testResults: null,
          deploymentId: null,
          rollbackReason: null,
          investigationSessionId: null,
          developmentSessionId: null,
          projectId: null,
          parentTicketId: null,
          goal: null,
        },
        "team-a",
      );

      expect(row["ticket_id"]).toBe("t-1");
      expect(row["created_at"]).toBe("2026-01-01T00:00:00Z");
      expect(row["assigned_to"]).toBeNull();
      expect(row["source_module"]).toBe("auth");
      expect(row["team_id"]).toBe("team-a");
    });

    it("strips ticketType, blockedBy, blocking fields", () => {
      const row = ticketToRow(
        {
          ticketId: "t-1",
          title: "Test",
          description: "Desc",
          severity: "medium",
          status: "open",
          createdAt: "2026-01-01T00:00:00Z",
          updatedAt: "2026-01-01T00:00:00Z",
          assignedTo: null,
          labels: [],
          ticketType: "bug",
          sourceModule: null,
          errorLog: null,
          relatedTickets: [],
          blockedBy: ["t-2"],
          blocking: ["t-3"],
          metadata: {},
          investigationReport: null,
          proposedFix: null,
          testResults: null,
          deploymentId: null,
          rollbackReason: null,
          investigationSessionId: null,
          developmentSessionId: null,
          projectId: null,
          parentTicketId: null,
          goal: null,
        },
        "team-a",
      );

      expect(row).not.toHaveProperty("ticket_type");
      expect(row).not.toHaveProperty("ticketType");
      expect(row).not.toHaveProperty("blocked_by");
      expect(row).not.toHaveProperty("blockedBy");
      expect(row).not.toHaveProperty("blocking");
    });

    it("maps 'failed' status to 'closed'", () => {
      const row = ticketToRow(
        {
          ticketId: "t-1",
          title: "T",
          description: "D",
          severity: "medium",
          status: "failed",
          createdAt: "",
          updatedAt: "",
          assignedTo: null,
          labels: [],
          ticketType: "bug",
          sourceModule: null,
          errorLog: null,
          relatedTickets: [],
          blockedBy: [],
          blocking: [],
          metadata: {},
          investigationReport: null,
          proposedFix: null,
          testResults: null,
          deploymentId: null,
          rollbackReason: null,
          investigationSessionId: null,
          developmentSessionId: null,
          projectId: null,
          parentTicketId: null,
          goal: null,
        },
        "team-a",
      );

      expect(row["status"]).toBe("closed");
    });

    it("maps 'blocked' status to 'acknowledged'", () => {
      const row = ticketToRow(
        {
          ticketId: "t-1",
          title: "T",
          description: "D",
          severity: "medium",
          status: "blocked",
          createdAt: "",
          updatedAt: "",
          assignedTo: null,
          labels: [],
          ticketType: "bug",
          sourceModule: null,
          errorLog: null,
          relatedTickets: [],
          blockedBy: [],
          blocking: [],
          metadata: {},
          investigationReport: null,
          proposedFix: null,
          testResults: null,
          deploymentId: null,
          rollbackReason: null,
          investigationSessionId: null,
          developmentSessionId: null,
          projectId: null,
          parentTicketId: null,
          goal: null,
        },
        "team-a",
      );

      expect(row["status"]).toBe("acknowledged");
    });

    it("removes null session ID fields", () => {
      const row = ticketToRow(
        {
          ticketId: "t-1",
          title: "T",
          description: "D",
          severity: "medium",
          status: "open",
          createdAt: "",
          updatedAt: "",
          assignedTo: null,
          labels: [],
          ticketType: "bug",
          sourceModule: null,
          errorLog: null,
          relatedTickets: [],
          blockedBy: [],
          blocking: [],
          metadata: {},
          investigationReport: null,
          proposedFix: null,
          testResults: null,
          deploymentId: null,
          rollbackReason: null,
          investigationSessionId: null,
          developmentSessionId: null,
          projectId: null,
          parentTicketId: null,
          goal: null,
        },
        "team-a",
      );

      expect(row).not.toHaveProperty("investigation_session_id");
      expect(row).not.toHaveProperty("development_session_id");
    });
  });

  // -----------------------------------------------------------------------
  // rowToTicket() helper
  // -----------------------------------------------------------------------

  describe("rowToTicket()", () => {
    it("converts snake_case row to camelCase ticket", () => {
      const ticket = rowToTicket({
        ticket_id: "t-1",
        title: "Test",
        description: "Desc",
        severity: "high",
        status: "open",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        assigned_to: "worker-1",
        source_module: "auth",
        metadata: { fingerprint: "fp-1" },
      });

      expect(ticket.ticketId).toBe("t-1");
      expect(ticket.createdAt).toBe("2026-01-01T00:00:00Z");
      expect(ticket.assignedTo).toBe("worker-1");
      expect(ticket.sourceModule).toBe("auth");
    });

    it("skips Supabase-only columns (team_id, embedding, etc.)", () => {
      const ticket = rowToTicket({
        ticket_id: "t-1",
        title: "T",
        description: "D",
        team_id: "alpha",
        embedding: "[0.1,0.2]",
        memory_confidence: 1.5,
        memory_accessed_at: "2026-01-01T00:00:00Z",
      });

      expect(ticket).not.toHaveProperty("team_id");
      expect(ticket).not.toHaveProperty("embedding");
      expect(ticket).not.toHaveProperty("memory_confidence");
      expect(ticket).not.toHaveProperty("memory_accessed_at");
    });

    it("defaults missing arrays to empty arrays", () => {
      const ticket = rowToTicket({
        ticket_id: "t-1",
        title: "T",
        description: "D",
      });

      expect(ticket.blockedBy).toEqual([]);
      expect(ticket.blocking).toEqual([]);
      expect(ticket.labels).toEqual([]);
      expect(ticket.relatedTickets).toEqual([]);
    });

    it("defaults ticketType to unknown", () => {
      const ticket = rowToTicket({
        ticket_id: "t-1",
        title: "T",
        description: "D",
      });

      expect(ticket.ticketType).toBe("unknown");
    });

    it("parses string metadata as JSON", () => {
      const ticket = rowToTicket({
        ticket_id: "t-1",
        title: "T",
        description: "D",
        metadata: '{"fingerprint":"fp-1"}',
      });

      expect(ticket.metadata).toEqual({ fingerprint: "fp-1" });
    });

    it("defaults null metadata to empty object", () => {
      const ticket = rowToTicket({
        ticket_id: "t-1",
        title: "T",
        description: "D",
        metadata: null,
      });

      expect(ticket.metadata).toEqual({});
    });

    it("defaults session ID fields to null", () => {
      const ticket = rowToTicket({
        ticket_id: "t-1",
        title: "T",
        description: "D",
      });

      expect(ticket.investigationSessionId).toBeNull();
      expect(ticket.developmentSessionId).toBeNull();
    });
  });

  // -----------------------------------------------------------------------
  // Store CRUD
  // -----------------------------------------------------------------------

  describe("add()", () => {
    it("calls client.insert with snake_case row", async () => {
      await store.add({
        ticketId: "t-1",
        title: "Bug",
        description: "Broken",
        severity: "high",
        status: "open",
        createdAt: "2026-01-01T00:00:00Z",
        updatedAt: "2026-01-01T00:00:00Z",
        assignedTo: null,
        labels: [],
        ticketType: "bug",
        sourceModule: "auth",
        errorLog: null,
        relatedTickets: [],
        blockedBy: [],
        blocking: [],
        metadata: { fingerprint: "fp-1" },
        investigationReport: null,
        proposedFix: null,
        testResults: null,
        deploymentId: null,
        rollbackReason: null,
        investigationSessionId: null,
        developmentSessionId: null,
        projectId: null,
        parentTicketId: null,
        goal: null,
      });

      expect(mockClient.insert).toHaveBeenCalledTimes(1);
      const [table, row] = mockClient.insert.mock.calls[0];
      expect(table).toBe("swe_tickets");
      expect(row["ticket_id"]).toBe("t-1");
      expect(row["team_id"]).toBe("test-team");
    });
  });

  describe("get()", () => {
    it("returns ticket when found", async () => {
      mockClient.query.mockResolvedValueOnce([
        {
          ticket_id: "t-1",
          title: "Bug",
          description: "Desc",
          severity: "high",
          status: "open",
          metadata: {},
        },
      ]);

      const ticket = await store.get("t-1");
      expect(ticket).not.toBeNull();
      expect(ticket!.ticketId).toBe("t-1");

      const [table, params] = mockClient.query.mock.calls[0];
      expect(table).toBe("swe_tickets");
      expect(params.ticket_id).toBe("eq.t-1");
      expect(params.team_id).toBe("eq.test-team");
    });

    it("returns null when not found", async () => {
      mockClient.query.mockResolvedValueOnce([]);

      const ticket = await store.get("nonexistent");
      expect(ticket).toBeNull();
    });
  });

  // -----------------------------------------------------------------------
  // Store queries
  // -----------------------------------------------------------------------

  describe("listOpen()", () => {
    it("uses correct PostgREST filter to exclude closed statuses", async () => {
      mockClient.query.mockResolvedValueOnce([]);

      await store.listOpen();

      const [table, params] = mockClient.query.mock.calls[0];
      expect(table).toBe("swe_tickets");
      expect(params.team_id).toBe("eq.test-team");
      expect(params.status).toMatch(/^not\.in\.\(/);
      expect(params.status).toContain("resolved");
      expect(params.status).toContain("closed");
      expect(params.status).toContain("acknowledged");
      expect(params.status).toContain("failed");
      expect(params.status).toContain("blocked");
      expect(params.order).toBe("created_at.desc");
    });
  });

  describe("listByStatus()", () => {
    it("filters by the given status", async () => {
      mockClient.query.mockResolvedValueOnce([]);

      await store.listByStatus("investigating");

      const [, params] = mockClient.query.mock.calls[0];
      expect(params.status).toBe("eq.investigating");
      expect(params.team_id).toBe("eq.test-team");
    });

    it("respects custom limit", async () => {
      mockClient.query.mockResolvedValueOnce([]);

      await store.listByStatus("open", 10);

      const [, params] = mockClient.query.mock.calls[0];
      expect(params.limit).toBe("10");
    });
  });

  // -----------------------------------------------------------------------
  // claimTicket (RPC)
  // -----------------------------------------------------------------------

  describe("claimTicket()", () => {
    it("calls RPC with correct parameters", async () => {
      mockClient.rpc.mockResolvedValueOnce(true);

      const result = await store.claimTicket("t-1", "agent-1");
      expect(result).toBe("agent-1");

      expect(mockClient.rpc).toHaveBeenCalledWith("claim_ticket", {
        p_ticket_id: "t-1",
        p_agent_id: "agent-1",
      });
    });

    it("returns null when RPC returns false", async () => {
      mockClient.rpc.mockResolvedValueOnce(false);

      const result = await store.claimTicket("t-1", "agent-1");
      expect(result).toBeNull();
    });

    it("returns null on RPC error (fail-closed)", async () => {
      mockClient.rpc.mockRejectedValueOnce(new Error("RPC error"));

      const result = await store.claimTicket("t-1", "agent-1");
      expect(result).toBeNull();
    });
  });

  // -----------------------------------------------------------------------
  // knownFingerprints
  // -----------------------------------------------------------------------

  describe("knownFingerprints", () => {
    it("lazy loads fingerprints from query", async () => {
      mockClient.query.mockResolvedValueOnce([
        { metadata: { fingerprint: "fp-1" } },
        { metadata: { fingerprint: "fp-2" } },
        { metadata: {} },
      ]);

      const fps = await store.knownFingerprints;
      expect(fps.has("fp-1")).toBe(true);
      expect(fps.has("fp-2")).toBe(true);
      expect(fps.size).toBe(2);
    });

    it("caches fingerprints after first load", async () => {
      mockClient.query.mockResolvedValueOnce([
        { metadata: { fingerprint: "fp-1" } },
      ]);

      await store.knownFingerprints;
      await store.knownFingerprints;

      // Only called once because of caching
      expect(mockClient.query).toHaveBeenCalledTimes(1);
    });

    it("handles string metadata in rows", async () => {
      mockClient.query.mockResolvedValueOnce([
        { metadata: '{"fingerprint":"fp-json"}' },
      ]);

      const fps = await store.knownFingerprints;
      expect(fps.has("fp-json")).toBe(true);
    });

    it("skips rows with unparseable string metadata", async () => {
      mockClient.query.mockResolvedValueOnce([
        { metadata: "not-json" },
        { metadata: { fingerprint: "fp-ok" } },
      ]);

      const fps = await store.knownFingerprints;
      expect(fps.size).toBe(1);
      expect(fps.has("fp-ok")).toBe(true);
    });
  });
});

// =========================================================================
// 3. GitHub integration tests
// =========================================================================

import {
  createGitHubIssue,
  findCommentByText,
  postStatusComment,
  isCircuitOpen,
  recordFailure,
  recordSuccess,
  resetCircuit,
  runGh,
} from "../../src/providers/github/integration.js";

describe("GitHub Integration", () => {
  const mockExecFileSync = execFileSync as ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    resetCircuit();
  });

  // -----------------------------------------------------------------------
  // createGitHubIssue()
  // -----------------------------------------------------------------------

  describe("createGitHubIssue()", () => {
    it("creates issue for CRITICAL severity", async () => {
      mockExecFileSync.mockReturnValueOnce(
        "https://github.com/owner/repo/issues/42\n",
      );

      const result = await createGitHubIssue(
        {
          title: "Crash in auth module",
          description: "Full stack trace...",
          severity: "critical",
          ticketId: "t-1",
          sourceModule: "auth",
        },
        { repo: "owner/repo" },
      );

      expect(result).toBe(42);
    });

    it("creates issue for HIGH severity", async () => {
      mockExecFileSync.mockReturnValueOnce(
        "https://github.com/owner/repo/issues/99\n",
      );

      const result = await createGitHubIssue(
        {
          title: "Memory leak",
          description: "Desc",
          severity: "high",
          ticketId: "t-2",
        },
        { repo: "owner/repo" },
      );

      expect(result).toBe(99);
    });

    it("skips issue for MEDIUM severity", async () => {
      const result = await createGitHubIssue(
        {
          title: "Minor issue",
          description: "Desc",
          severity: "medium",
          ticketId: "t-3",
        },
        { repo: "owner/repo" },
      );

      expect(result).toBeNull();
      expect(mockExecFileSync).not.toHaveBeenCalled();
    });

    it("skips issue for LOW severity", async () => {
      const result = await createGitHubIssue(
        {
          title: "Cosmetic issue",
          description: "Desc",
          severity: "low",
          ticketId: "t-4",
        },
        { repo: "owner/repo" },
      );

      expect(result).toBeNull();
    });

    it("returns null when no repo configured", async () => {
      const result = await createGitHubIssue(
        {
          title: "Bug",
          description: "Desc",
          severity: "critical",
          ticketId: "t-5",
        },
        { repo: "" },
      );

      expect(result).toBeNull();
    });

    it("passes correct labels to gh CLI", async () => {
      mockExecFileSync.mockReturnValueOnce(
        "https://github.com/owner/repo/issues/1\n",
      );

      await createGitHubIssue(
        {
          title: "Bug",
          description: "Desc",
          severity: "critical",
          ticketId: "t-6",
        },
        { repo: "owner/repo" },
      );

      const args = mockExecFileSync.mock.calls[0][1] as string[];
      const labelIdx = args.indexOf("--label");
      expect(labelIdx).toBeGreaterThan(-1);
      const labelStr = args[labelIdx + 1];
      expect(labelStr).toContain("swe-team");
      expect(labelStr).toContain("auto-detected");
      expect(labelStr).toContain("severity: critical");
    });

    it("includes [SWE-AUTO] prefix in title", async () => {
      mockExecFileSync.mockReturnValueOnce(
        "https://github.com/owner/repo/issues/1\n",
      );

      await createGitHubIssue(
        {
          title: "Auth crash",
          description: "Desc",
          severity: "critical",
          ticketId: "t-7",
        },
        { repo: "owner/repo" },
      );

      const args = mockExecFileSync.mock.calls[0][1] as string[];
      const titleIdx = args.indexOf("--title");
      expect(args[titleIdx + 1]).toContain("[SWE-AUTO]");
    });

    it("severity check is case-insensitive", async () => {
      mockExecFileSync.mockReturnValueOnce(
        "https://github.com/owner/repo/issues/10\n",
      );

      const result = await createGitHubIssue(
        {
          title: "Bug",
          description: "Desc",
          severity: "CRITICAL",
          ticketId: "t-8",
        },
        { repo: "owner/repo" },
      );

      expect(result).toBe(10);
    });
  });

  // -----------------------------------------------------------------------
  // Circuit breaker
  // -----------------------------------------------------------------------

  describe("Circuit breaker", () => {
    it("is closed initially", () => {
      expect(isCircuitOpen()).toBe(false);
    });

    it("opens after 3 consecutive failures", () => {
      recordFailure("fail 1");
      recordFailure("fail 2");
      expect(isCircuitOpen()).toBe(false);
      recordFailure("fail 3");
      expect(isCircuitOpen()).toBe(true);
    });

    it("closes after recordSuccess()", () => {
      recordFailure("f1");
      recordFailure("f2");
      recordFailure("f3");
      expect(isCircuitOpen()).toBe(true);

      recordSuccess();
      // After recording success, resetCircuit-like behavior is not automatic
      // but isCircuitOpen checks pausedUntil which recordSuccess resets
      // Actually, recordSuccess resets pausedUntil to null
      expect(isCircuitOpen()).toBe(false);
    });

    it("resets with resetCircuit()", () => {
      recordFailure("f1");
      recordFailure("f2");
      recordFailure("f3");
      expect(isCircuitOpen()).toBe(true);

      resetCircuit();
      expect(isCircuitOpen()).toBe(false);
    });

    it("runGh returns null when circuit is open", () => {
      recordFailure("f1");
      recordFailure("f2");
      recordFailure("f3");

      const result = runGh(["issue", "list"]);
      expect(result).toBeNull();
    });
  });

  // -----------------------------------------------------------------------
  // findCommentByText()
  // -----------------------------------------------------------------------

  describe("findCommentByText()", () => {
    it("finds comment containing search text", async () => {
      const comments = [
        { id: 100, body: "Some other comment" },
        { id: 200, body: "Status update: Ticket ID: t-1 is open" },
      ];

      mockExecFileSync.mockReturnValueOnce(JSON.stringify(comments));

      const result = await findCommentByText(42, "Ticket ID:", {
        repo: "owner/repo",
      });

      expect(result).toBe(200);
    });

    it("returns null when no comment matches", async () => {
      const comments = [{ id: 100, body: "unrelated" }];
      mockExecFileSync.mockReturnValueOnce(JSON.stringify(comments));

      const result = await findCommentByText(42, "Ticket ID:", {
        repo: "owner/repo",
      });

      expect(result).toBeNull();
    });

    it("returns null when gh fails", async () => {
      mockExecFileSync.mockImplementation(() => {
        const err = new Error("gh failed") as Error & {
          status: number;
          stdout: string;
          stderr: string;
        };
        err.status = 1;
        err.stdout = "";
        err.stderr = "Not found";
        throw err;
      });

      // After the failure, circuit is open for next calls, but first call
      // should still execute
      const result = await findCommentByText(42, "Ticket ID:", {
        repo: "owner/repo",
      });

      expect(result).toBeNull();
    });

    it("returns null when no repo configured", async () => {
      const result = await findCommentByText(42, "Ticket ID:", {
        repo: "",
      });

      expect(result).toBeNull();
    });

    it("handles empty comment list", async () => {
      mockExecFileSync.mockReturnValueOnce("[]");

      const result = await findCommentByText(42, "Ticket ID:", {
        repo: "owner/repo",
      });

      expect(result).toBeNull();
    });
  });

  // -----------------------------------------------------------------------
  // postStatusComment()
  // -----------------------------------------------------------------------

  describe("postStatusComment()", () => {
    it("creates new comment when none exists", async () => {
      // First call: findCommentByText search (returns empty)
      mockExecFileSync.mockReturnValueOnce("[]");
      // Second call: create comment
      mockExecFileSync.mockReturnValueOnce("");

      const result = await postStatusComment(42, "Status: investigating", {
        repo: "owner/repo",
      });

      expect(result).toBe(true);
      // Should have been called twice (search + create)
      expect(mockExecFileSync).toHaveBeenCalledTimes(2);
    });

    it("updates existing comment when found", async () => {
      // First call: findCommentByText search
      const comments = [{ id: 300, body: "Ticket ID: t-1 - status" }];
      mockExecFileSync.mockReturnValueOnce(JSON.stringify(comments));
      // Second call: update (PATCH)
      mockExecFileSync.mockReturnValueOnce(JSON.stringify({ id: 300 }));

      const result = await postStatusComment(
        42,
        "Updated status: resolved",
        { repo: "owner/repo" },
      );

      expect(result).toBe(true);
    });

    it("returns false when no repo configured", async () => {
      const result = await postStatusComment(42, "text", { repo: "" });
      expect(result).toBe(false);
    });
  });
});

// =========================================================================
// 4. Telegram provider tests
// =========================================================================

import {
  sendMessage,
  sendNotification,
  escapeHtml,
  editMessage,
} from "../../src/providers/notification/telegram.js";

describe("Telegram Provider", () => {
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);
    // Clear rate limiting state by restoring timers
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  // -----------------------------------------------------------------------
  // escapeHtml()
  // -----------------------------------------------------------------------

  describe("escapeHtml()", () => {
    it("escapes & to &amp;", () => {
      expect(escapeHtml("a & b")).toBe("a &amp; b");
    });

    it("escapes < to &lt;", () => {
      expect(escapeHtml("a < b")).toBe("a &lt; b");
    });

    it("escapes > to &gt;", () => {
      expect(escapeHtml("a > b")).toBe("a &gt; b");
    });

    it('escapes " to &quot;', () => {
      expect(escapeHtml('say "hello"')).toBe("say &quot;hello&quot;");
    });

    it("escapes all special characters in combination", () => {
      expect(escapeHtml('<b>"A & B"</b>')).toBe(
        "&lt;b&gt;&quot;A &amp; B&quot;&lt;/b&gt;",
      );
    });

    it("returns unchanged string when no special characters", () => {
      expect(escapeHtml("hello world 123")).toBe("hello world 123");
    });

    it("handles empty string", () => {
      expect(escapeHtml("")).toBe("");
    });
  });

  // -----------------------------------------------------------------------
  // sendMessage()
  // -----------------------------------------------------------------------

  describe("sendMessage()", () => {
    it("calls correct Telegram API URL", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, result: { message_id: 1 } }),
      });

      await sendMessage("Hello", {
        config: { botToken: "test-token-123", chatId: "12345" },
      });

      const [url] = mockFetch.mock.calls[0];
      expect(url).toBe(
        "https://api.telegram.org/bottest-token-123/sendMessage",
      );
    });

    it("sends correct payload", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, result: { message_id: 1 } }),
      });

      await sendMessage("Hello <b>world</b>", {
        config: { botToken: "tok", chatId: "123" },
      });

      const [, init] = mockFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.chat_id).toBe("123");
      expect(body.text).toBe("Hello <b>world</b>");
      expect(body.parse_mode).toBe("HTML");
    });

    it("returns true on success", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, result: { message_id: 1 } }),
      });

      const result = await sendMessage("Hello", {
        config: { botToken: "tok", chatId: "123" },
      });

      expect(result).toBe(true);
    });

    it("returns false when API returns error", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
      });

      const result = await sendMessage("Hello", {
        config: { botToken: "tok", chatId: "123" },
      });

      expect(result).toBe(false);
    });

    it("returns false when no bot token configured", async () => {
      // Clear env
      const origToken = process.env.TELEGRAM_BOT_TOKEN;
      const origChat = process.env.TELEGRAM_CHAT_ID;
      delete process.env.TELEGRAM_BOT_TOKEN;
      delete process.env.TELEGRAM_CHAT_ID;

      const result = await sendMessage("Hello", {
        config: { botToken: "", chatId: "123" },
      });

      expect(result).toBe(false);
      expect(mockFetch).not.toHaveBeenCalled();

      // Restore
      if (origToken) process.env.TELEGRAM_BOT_TOKEN = origToken;
      if (origChat) process.env.TELEGRAM_CHAT_ID = origChat;
    });

    it("returns false when no chat ID configured", async () => {
      const origChat = process.env.TELEGRAM_CHAT_ID;
      delete process.env.TELEGRAM_CHAT_ID;

      const result = await sendMessage("Hello", {
        config: { botToken: "tok", chatId: "" },
      });

      expect(result).toBe(false);

      if (origChat) process.env.TELEGRAM_CHAT_ID = origChat;
    });

    it("returns false on network error", async () => {
      mockFetch.mockRejectedValueOnce(new Error("Network down"));

      const result = await sendMessage("Hello", {
        config: { botToken: "tok", chatId: "123" },
      });

      expect(result).toBe(false);
    });

    it("sends with custom parse mode", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, result: {} }),
      });

      await sendMessage("Hello", {
        parseMode: "Markdown",
        config: { botToken: "tok", chatId: "123" },
      });

      const [, init] = mockFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.parse_mode).toBe("Markdown");
    });

    it("includes reply_to_message_id when specified", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, result: {} }),
      });

      await sendMessage("Reply text", {
        replyToMessageId: 42,
        config: { botToken: "tok", chatId: "123" },
      });

      const [, init] = mockFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.reply_to_message_id).toBe(42);
    });
  });

  // -----------------------------------------------------------------------
  // sendNotification() — rate limiting
  // -----------------------------------------------------------------------

  describe("sendNotification()", () => {
    it("sends first notification for a type", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, result: { message_id: 1 } }),
      });

      const result = await sendNotification(
        "test_alert_unique_1",
        "Alert!",
        { config: { botToken: "tok", chatId: "123" } },
      );

      expect(result).toBe(true);
    });

    it("blocks rapid duplicate sends of same alert type", async () => {
      // First send succeeds
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, result: { message_id: 1 } }),
      });

      const result1 = await sendNotification(
        "test_alert_unique_2",
        "Alert!",
        { config: { botToken: "tok", chatId: "123" } },
      );
      expect(result1).toBe(true);

      // Second send of same type within cooldown should be blocked
      const result2 = await sendNotification(
        "test_alert_unique_2",
        "Alert again!",
        { config: { botToken: "tok", chatId: "123" } },
      );
      expect(result2).toBe(false);
      // fetch should only have been called once
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });

    it("allows different alert types concurrently", async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: async () => ({ ok: true, result: { message_id: 1 } }),
      });

      const r1 = await sendNotification(
        "type_a_unique",
        "Alert A",
        { config: { botToken: "tok", chatId: "123" } },
      );
      const r2 = await sendNotification(
        "type_b_unique",
        "Alert B",
        { config: { botToken: "tok", chatId: "123" } },
      );

      expect(r1).toBe(true);
      expect(r2).toBe(true);
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });

    it("allows same type after cooldown expires", async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: async () => ({ ok: true, result: { message_id: 1 } }),
      });

      await sendNotification("type_cooldown_unique", "first", {
        config: { botToken: "tok", chatId: "123" },
      });

      // Advance time past 5-minute cooldown
      vi.advanceTimersByTime(301_000);

      const result = await sendNotification("type_cooldown_unique", "second", {
        config: { botToken: "tok", chatId: "123" },
      });

      expect(result).toBe(true);
    });
  });

  // -----------------------------------------------------------------------
  // editMessage()
  // -----------------------------------------------------------------------

  describe("editMessage()", () => {
    it("calls editMessageText API endpoint", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, result: { message_id: 42 } }),
      });

      const result = await editMessage("123", 42, "Updated text", {
        config: { botToken: "tok" },
      });

      expect(result).toBe(true);
      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("/editMessageText");
    });

    it("sends correct payload for edit", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, result: {} }),
      });

      await editMessage("chat-1", 55, "New text", {
        config: { botToken: "tok" },
      });

      const [, init] = mockFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.chat_id).toBe("chat-1");
      expect(body.message_id).toBe(55);
      expect(body.text).toBe("New text");
    });

    it("returns false on API failure", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
      });

      const result = await editMessage("123", 42, "text", {
        config: { botToken: "tok" },
      });

      expect(result).toBe(false);
    });
  });
});

// =========================================================================
// 5. EmbeddingService tests
// =========================================================================

import {
  EmbeddingService,
  vectorLiteral,
} from "../../src/providers/supabase/embeddings.js";

describe("EmbeddingService", () => {
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe("vectorLiteral()", () => {
    it("formats float array as pgvector text literal", () => {
      expect(vectorLiteral([0.1, 0.2, 0.3])).toBe("[0.1,0.2,0.3]");
    });

    it("handles empty array", () => {
      expect(vectorLiteral([])).toBe("[]");
    });

    it("handles single element", () => {
      expect(vectorLiteral([1.5])).toBe("[1.5]");
    });
  });

  describe("embed()", () => {
    it("calls correct embedding endpoint", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          data: [{ embedding: [0.1, 0.2, 0.3] }],
        }),
      });

      const service = new EmbeddingService({
        apiUrl: "http://proxy:8000/v1",
        apiKey: "test-key",
      });

      await service.embed("test text");

      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe("http://proxy:8000/v1/embeddings");
      expect(init.method).toBe("POST");
    });

    it("sends correct model and input", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          data: [{ embedding: [0.1] }],
        }),
      });

      const service = new EmbeddingService({
        apiUrl: "http://proxy:8000/v1",
        apiKey: "key",
        model: "custom-model",
      });

      await service.embed("hello");

      const [, init] = mockFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.input).toBe("hello");
      expect(body.model).toBe("custom-model");
    });

    it("returns embedding vector", async () => {
      const embedding = [0.1, 0.2, 0.3, 0.4];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          data: [{ embedding }],
        }),
      });

      const service = new EmbeddingService({
        apiUrl: "http://proxy:8000/v1",
        apiKey: "key",
      });

      const result = await service.embed("test");
      expect(result).toEqual(embedding);
    });

    it("throws on API error", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 500,
        text: async () => "Internal server error",
      });

      const service = new EmbeddingService({
        apiUrl: "http://proxy:8000/v1",
        apiKey: "key",
      });

      await expect(service.embed("test")).rejects.toThrow(/Embedding API error/);
    });

    it("throws when no data returned", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ data: [] }),
      });

      const service = new EmbeddingService({
        apiUrl: "http://proxy:8000/v1",
        apiKey: "key",
      });

      await expect(service.embed("test")).rejects.toThrow(/no data/);
    });

    it("uses bge-m3 as default model", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ data: [{ embedding: [0.1] }] }),
      });

      const service = new EmbeddingService({
        apiUrl: "http://proxy:8000/v1",
        apiKey: "key",
      });

      await service.embed("test");

      const [, init] = mockFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.model).toBe("bge-m3");
    });

    it("sends Bearer token in Authorization header", async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ data: [{ embedding: [0.1] }] }),
      });

      const service = new EmbeddingService({
        apiUrl: "http://proxy:8000/v1",
        apiKey: "my-secret-key",
      });

      await service.embed("test");

      const [, init] = mockFetch.mock.calls[0];
      expect(init.headers.Authorization).toBe("Bearer my-secret-key");
    });
  });
});

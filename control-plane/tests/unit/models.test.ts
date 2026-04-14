/**
 * Unit tests for SWE-Squad TypeScript data models.
 *
 * Covers: ticket.ts enums, createTicket(), isBlocked(), resolutionAudit(),
 *         OPEN_STATUSES, RESOLUTION_BYPASS_REASONS, SWETicketSchema validation,
 *         and events.ts createEvent().
 */

import { describe, it, expect } from "vitest";

import {
  TicketSeverity,
  TicketStatus,
  TicketType,
  AgentRole,
  GovernanceVerdict,
  EdgeType,
  SWETicketSchema,
  createTicket,
  isBlocked,
  resolutionAudit,
  OPEN_STATUSES,
  RESOLUTION_BYPASS_REASONS,
} from "../../src/models/ticket.js";

import { SWEEventSchema, createEvent } from "../../src/models/events.js";

// ---------------------------------------------------------------------------
// 1. Enum values
// ---------------------------------------------------------------------------

describe("TicketSeverity enum", () => {
  it("has exactly 4 values", () => {
    expect(Object.keys(TicketSeverity)).toHaveLength(4);
  });

  it("maps CRITICAL -> 'critical'", () => {
    expect(TicketSeverity.CRITICAL).toBe("critical");
  });

  it("maps HIGH -> 'high'", () => {
    expect(TicketSeverity.HIGH).toBe("high");
  });

  it("maps MEDIUM -> 'medium'", () => {
    expect(TicketSeverity.MEDIUM).toBe("medium");
  });

  it("maps LOW -> 'low'", () => {
    expect(TicketSeverity.LOW).toBe("low");
  });
});

describe("TicketStatus enum", () => {
  it("has exactly 18 values", () => {
    expect(Object.keys(TicketStatus)).toHaveLength(18);
  });

  it("maps OPEN -> 'open'", () => {
    expect(TicketStatus.OPEN).toBe("open");
  });

  it("maps RESOLVED -> 'resolved'", () => {
    expect(TicketStatus.RESOLVED).toBe("resolved");
  });

  it("maps VERIFYING -> 'verifying'", () => {
    expect(TicketStatus.VERIFYING).toBe("verifying");
  });

  it("maps FAILED -> 'failed'", () => {
    expect(TicketStatus.FAILED).toBe("failed");
  });
});

describe("TicketType enum", () => {
  it("has exactly 9 values", () => {
    expect(Object.keys(TicketType)).toHaveLength(9);
  });

  it("includes all expected types", () => {
    const expected = [
      "bug",
      "feature",
      "enhancement",
      "infrastructure",
      "documentation",
      "question",
      "security",
      "regression",
      "unknown",
    ];
    expect(Object.values(TicketType).sort()).toEqual(expected.sort());
  });
});

describe("AgentRole enum", () => {
  it("has exactly 10 values", () => {
    expect(Object.keys(AgentRole)).toHaveLength(10);
  });

  it("includes all expected roles", () => {
    const expected = [
      "monitor",
      "triage",
      "investigator",
      "developer",
      "reviewer",
      "qa",
      "tester",
      "deployer",
      "documenter",
      "creative",
    ];
    expect(Object.values(AgentRole).sort()).toEqual(expected.sort());
  });
});

describe("GovernanceVerdict enum", () => {
  it("has exactly 3 values", () => {
    expect(Object.keys(GovernanceVerdict)).toHaveLength(3);
  });

  it("maps PASS/BLOCK/WARN correctly", () => {
    expect(GovernanceVerdict.PASS).toBe("pass");
    expect(GovernanceVerdict.BLOCK).toBe("block");
    expect(GovernanceVerdict.WARN).toBe("warn");
  });
});

describe("EdgeType enum", () => {
  it("has exactly 6 values", () => {
    expect(Object.keys(EdgeType)).toHaveLength(6);
  });

  it("includes all expected edge types", () => {
    const expected = [
      "similar",
      "touches_module",
      "blocks",
      "resolves",
      "conflicts_with",
      "caused_regression",
    ];
    expect(Object.values(EdgeType).sort()).toEqual(expected.sort());
  });
});

// ---------------------------------------------------------------------------
// 2. createTicket()
// ---------------------------------------------------------------------------

describe("createTicket()", () => {
  it("creates a ticket with only title and description", () => {
    const t = createTicket("Test bug", "Something broke");
    expect(t.title).toBe("Test bug");
    expect(t.description).toBe("Something broke");
  });

  it("generates a 12-character hex ticketId by default", () => {
    const t = createTicket("A", "B");
    expect(t.ticketId).toMatch(/^[0-9a-f]{12}$/);
  });

  it("defaults severity to medium", () => {
    const t = createTicket("A", "B");
    expect(t.severity).toBe("medium");
  });

  it("defaults status to open", () => {
    const t = createTicket("A", "B");
    expect(t.status).toBe("open");
  });

  it("sets createdAt and updatedAt to ISO timestamps", () => {
    const before = new Date().toISOString();
    const t = createTicket("A", "B");
    const after = new Date().toISOString();
    // ISO format check
    expect(t.createdAt).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
    expect(t.updatedAt).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
    // Within time window
    expect(t.createdAt >= before).toBe(true);
    expect(t.createdAt <= after).toBe(true);
  });

  it("defaults nullable fields to null", () => {
    const t = createTicket("A", "B");
    expect(t.assignedTo).toBeNull();
    expect(t.sourceModule).toBeNull();
    expect(t.errorLog).toBeNull();
    expect(t.investigationReport).toBeNull();
    expect(t.proposedFix).toBeNull();
    expect(t.testResults).toBeNull();
    expect(t.deploymentId).toBeNull();
    expect(t.rollbackReason).toBeNull();
    expect(t.investigationSessionId).toBeNull();
    expect(t.developmentSessionId).toBeNull();
    expect(t.projectId).toBeNull();
    expect(t.parentTicketId).toBeNull();
    expect(t.goal).toBeNull();
  });

  it("defaults array fields to empty arrays", () => {
    const t = createTicket("A", "B");
    expect(t.labels).toEqual([]);
    expect(t.relatedTickets).toEqual([]);
    expect(t.blockedBy).toEqual([]);
    expect(t.blocking).toEqual([]);
  });

  it("defaults metadata to empty object", () => {
    const t = createTicket("A", "B");
    expect(t.metadata).toEqual({});
  });

  it("defaults ticketType to unknown", () => {
    const t = createTicket("A", "B");
    expect(t.ticketType).toBe("unknown");
  });

  it("allows overriding severity", () => {
    const t = createTicket("A", "B", { severity: "critical" });
    expect(t.severity).toBe("critical");
  });

  it("allows overriding status", () => {
    const t = createTicket("A", "B", { status: "investigating" });
    expect(t.status).toBe("investigating");
  });

  it("allows overriding ticketId", () => {
    const t = createTicket("A", "B", { ticketId: "custom-id-123" });
    expect(t.ticketId).toBe("custom-id-123");
  });

  it("allows overriding labels and metadata", () => {
    const t = createTicket("A", "B", {
      labels: ["urgent", "backend"],
      metadata: { source: "scanner" },
    });
    expect(t.labels).toEqual(["urgent", "backend"]);
    expect(t.metadata).toEqual({ source: "scanner" });
  });

  it("generates unique ticketIds across calls", () => {
    const ids = new Set(
      Array.from({ length: 20 }, () => createTicket("A", "B").ticketId),
    );
    expect(ids.size).toBe(20);
  });
});

// ---------------------------------------------------------------------------
// 3. isBlocked()
// ---------------------------------------------------------------------------

describe("isBlocked()", () => {
  it("returns false when blockedBy is empty", () => {
    const t = createTicket("A", "B");
    expect(isBlocked(t)).toBe(false);
  });

  it("returns true when blockedBy has items", () => {
    const t = createTicket("A", "B", { blockedBy: ["ticket-abc"] });
    expect(isBlocked(t)).toBe(true);
  });

  it("returns true when blockedBy has multiple items", () => {
    const t = createTicket("A", "B", {
      blockedBy: ["ticket-1", "ticket-2", "ticket-3"],
    });
    expect(isBlocked(t)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 4. resolutionAudit()
// ---------------------------------------------------------------------------

describe("resolutionAudit()", () => {
  it("passes with a recognized bypass reason in metadata.resolution_note", () => {
    const t = createTicket("A", "B", {
      metadata: { resolution_note: "false_regression" },
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(true);
    expect(reason).toContain("bypass");
  });

  it("passes for each recognized bypass reason", () => {
    for (const bypass of RESOLUTION_BYPASS_REASONS) {
      const t = createTicket("A", "B", {
        metadata: { resolution_note: bypass },
      });
      const [ok] = resolutionAudit(t);
      expect(ok).toBe(true);
    }
  });

  it("bypass matching is case-insensitive", () => {
    const t = createTicket("A", "B", {
      metadata: { resolution_note: "FALSE_REGRESSION" },
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });

  it("fails when investigation report is too short", () => {
    const t = createTicket("A", "B", {
      investigationReport: "too short",
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("investigation_report too short");
    expect(reason).toContain("9 chars");
  });

  it("fails when investigation report is null", () => {
    const t = createTicket("A", "B");
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("investigation_report too short");
    expect(reason).toContain("0 chars");
  });

  it("fails for HIGH severity without attempts", () => {
    const longReport = "x".repeat(200);
    const t = createTicket("A", "B", {
      severity: "high",
      investigationReport: longReport,
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("HIGH ticket requires >=1 fix attempt");
  });

  it("fails for CRITICAL severity without attempts", () => {
    const longReport = "x".repeat(200);
    const t = createTicket("A", "B", {
      severity: "critical",
      investigationReport: longReport,
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("CRITICAL ticket requires >=1 fix attempt");
  });

  it("passes for HIGH severity with attempts", () => {
    const longReport = "x".repeat(200);
    const t = createTicket("A", "B", {
      severity: "high",
      investigationReport: longReport,
      metadata: { attempts: [{ id: 1 }] },
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  it("passes for MEDIUM severity with long enough report and no attempts", () => {
    const longReport = "x".repeat(200);
    const t = createTicket("A", "B", {
      severity: "medium",
      investigationReport: longReport,
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  it("passes for LOW severity with sufficient report", () => {
    const longReport = "x".repeat(250);
    const t = createTicket("A", "B", {
      severity: "low",
      investigationReport: longReport,
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  it("report at exactly 200 chars passes the length check", () => {
    const t = createTicket("A", "B", {
      severity: "medium",
      investigationReport: "y".repeat(200),
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });

  it("report at 199 chars fails the length check", () => {
    const t = createTicket("A", "B", {
      severity: "medium",
      investigationReport: "y".repeat(199),
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("199 chars");
  });

  it("fails for HIGH with empty attempts array", () => {
    const longReport = "x".repeat(200);
    const t = createTicket("A", "B", {
      severity: "high",
      investigationReport: longReport,
      metadata: { attempts: [] },
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("Attempts list is empty");
  });

  it("bypass note takes priority over short report", () => {
    const t = createTicket("A", "B", {
      severity: "critical",
      investigationReport: null,
      metadata: { resolution_note: "duplicate" },
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 5. OPEN_STATUSES
// ---------------------------------------------------------------------------

describe("OPEN_STATUSES", () => {
  it("contains exactly 14 statuses", () => {
    expect(OPEN_STATUSES.size).toBe(14);
  });

  it("includes all expected open statuses", () => {
    const expected = [
      "open",
      "triaged",
      "needs_info",
      "blocked",
      "acknowledged",
      "investigating",
      "investigation_complete",
      "in_development",
      "in_review",
      "rework_requested",
      "testing",
      "deploying",
      "monitoring",
      "verifying",
    ];
    for (const s of expected) {
      expect(OPEN_STATUSES.has(s as TicketStatus)).toBe(true);
    }
  });

  it("does not include terminal statuses", () => {
    expect(OPEN_STATUSES.has(TicketStatus.RESOLVED)).toBe(false);
    expect(OPEN_STATUSES.has(TicketStatus.CLOSED)).toBe(false);
    expect(OPEN_STATUSES.has(TicketStatus.FAILED)).toBe(false);
    expect(OPEN_STATUSES.has(TicketStatus.ROLLED_BACK)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 6. RESOLUTION_BYPASS_REASONS
// ---------------------------------------------------------------------------

describe("RESOLUTION_BYPASS_REASONS", () => {
  it("contains exactly 7 reasons", () => {
    expect(RESOLUTION_BYPASS_REASONS.size).toBe(7);
  });

  it("includes all expected bypass reasons", () => {
    const expected = [
      "false_regression",
      "duplicate",
      "already_fixed_externally",
      "not_reproducible",
      "wont_fix_approved",
      "manual_override",
      "fix_succeeded",
    ];
    for (const r of expected) {
      expect(RESOLUTION_BYPASS_REASONS.has(r)).toBe(true);
    }
  });

  it("is a ReadonlySet (cannot be mutated at type level)", () => {
    // Runtime check: the Set instance exists and has .has()
    expect(typeof RESOLUTION_BYPASS_REASONS.has).toBe("function");
    expect(typeof RESOLUTION_BYPASS_REASONS.size).toBe("number");
  });
});

// ---------------------------------------------------------------------------
// 7. Schema validation (SWETicketSchema)
// ---------------------------------------------------------------------------

describe("SWETicketSchema", () => {
  it("parses valid minimal input", () => {
    const result = SWETicketSchema.parse({
      title: "Bug",
      description: "Broken",
    });
    expect(result.title).toBe("Bug");
    expect(result.description).toBe("Broken");
    expect(result.severity).toBe("medium");
    expect(result.status).toBe("open");
  });

  it("parses valid full input", () => {
    const result = SWETicketSchema.parse({
      ticketId: "abc123def456",
      title: "Full ticket",
      description: "With all fields",
      severity: "critical",
      status: "investigating",
      createdAt: "2026-01-01T00:00:00.000Z",
      updatedAt: "2026-01-01T00:00:00.000Z",
      assignedTo: "developer-1",
      labels: ["urgent"],
      ticketType: "bug",
      sourceModule: "auth",
      errorLog: "Error: crash",
      relatedTickets: ["t-001"],
      blockedBy: [],
      blocking: ["t-002"],
      metadata: { key: "value" },
      investigationReport: "Found the issue",
      proposedFix: "Change X to Y",
      testResults: { passed: true },
      deploymentId: "deploy-42",
      rollbackReason: null,
      investigationSessionId: "sess-1",
      developmentSessionId: "sess-2",
      projectId: "proj-1",
      parentTicketId: "parent-1",
      goal: "Fix auth module",
    });
    expect(result.ticketId).toBe("abc123def456");
    expect(result.severity).toBe("critical");
    expect(result.labels).toEqual(["urgent"]);
    expect(result.projectId).toBe("proj-1");
  });

  it("rejects missing title", () => {
    expect(() =>
      SWETicketSchema.parse({ description: "No title" }),
    ).toThrow();
  });

  it("rejects missing description", () => {
    expect(() => SWETicketSchema.parse({ title: "No desc" })).toThrow();
  });

  it("rejects invalid severity", () => {
    expect(() =>
      SWETicketSchema.parse({
        title: "A",
        description: "B",
        severity: "extreme",
      }),
    ).toThrow();
  });

  it("rejects invalid status", () => {
    expect(() =>
      SWETicketSchema.parse({
        title: "A",
        description: "B",
        status: "nonexistent",
      }),
    ).toThrow();
  });

  it("rejects invalid ticketType", () => {
    expect(() =>
      SWETicketSchema.parse({
        title: "A",
        description: "B",
        ticketType: "epic",
      }),
    ).toThrow();
  });
});

// ---------------------------------------------------------------------------
// 8. createEvent() and SWEEventSchema
// ---------------------------------------------------------------------------

describe("createEvent()", () => {
  it("creates an event with required fields", () => {
    const e = createEvent("ticket.created", "t-001", "monitor");
    expect(e.eventType).toBe("ticket.created");
    expect(e.ticketId).toBe("t-001");
    expect(e.source).toBe("monitor");
  });

  it("generates a 16-character hex eventId by default", () => {
    const e = createEvent("ticket.created", "t-001", "monitor");
    expect(e.eventId).toMatch(/^[0-9a-f]{16}$/);
  });

  it("generates a valid ISO timestamp", () => {
    const before = new Date().toISOString();
    const e = createEvent("ticket.created", "t-001", "monitor");
    const after = new Date().toISOString();
    expect(e.timestamp).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
    expect(e.timestamp >= before).toBe(true);
    expect(e.timestamp <= after).toBe(true);
  });

  it("defaults data to empty object when not provided", () => {
    const e = createEvent("ticket.created", "t-001", "monitor");
    expect(e.data).toEqual({});
  });

  it("accepts custom data payload", () => {
    const e = createEvent("ticket.created", "t-001", "monitor", {
      severity: "critical",
      labels: ["urgent"],
    });
    expect(e.data).toEqual({ severity: "critical", labels: ["urgent"] });
  });

  it("generates unique eventIds across calls", () => {
    const ids = new Set(
      Array.from({ length: 20 }, () =>
        createEvent("x", "t", "s").eventId,
      ),
    );
    expect(ids.size).toBe(20);
  });
});

describe("SWEEventSchema", () => {
  it("parses valid input with all fields", () => {
    const result = SWEEventSchema.parse({
      eventId: "abcdef0123456789",
      eventType: "ticket.resolved",
      ticketId: "t-042",
      timestamp: "2026-04-12T12:00:00.000Z",
      source: "developer",
      data: { branch: "fix/auth" },
    });
    expect(result.eventId).toBe("abcdef0123456789");
    expect(result.data).toEqual({ branch: "fix/auth" });
  });

  it("rejects missing eventType", () => {
    expect(() =>
      SWEEventSchema.parse({
        ticketId: "t-1",
        source: "monitor",
      }),
    ).toThrow();
  });

  it("rejects missing ticketId", () => {
    expect(() =>
      SWEEventSchema.parse({
        eventType: "ticket.created",
        source: "monitor",
      }),
    ).toThrow();
  });

  it("rejects missing source", () => {
    expect(() =>
      SWEEventSchema.parse({
        eventType: "ticket.created",
        ticketId: "t-1",
      }),
    ).toThrow();
  });
});

/**
 * Regression test suite: Python/TypeScript behavioral parity.
 *
 * Every test in this file verifies that the TypeScript port produces IDENTICAL
 * outputs to the Python original for the same inputs. This is NOT a "does it
 * run" suite -- it is a behavioral contract suite. Each test comment explains
 * which Python behavior it locks down.
 *
 * Covers:
 *   1. Model/ticket creation parity
 *   2. Resolution audit parity (all branches)
 *   3. Circuit breaker parity (threshold math)
 *   4. Throttle adapter parity (capacity/demand/compound math)
 *   5. Ralph Wiggum stability gate parity (verdict logic)
 *   6. Governance complexity checker parity
 *   7. Config schema default parity (Zod vs Python dataclass)
 *   8. Guardrails gate ordering parity
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  TicketSeverity,
  TicketStatus,
  TicketType,
  GovernanceVerdict,
  SWETicketSchema,
  createTicket,
  resolutionAudit,
  RESOLUTION_BYPASS_REASONS,
  type SWETicket,
} from "../../src/models/ticket.js";

import {
  GovernanceConfigSchema,
  CycleConfigSchema,
  ModelConfigSchema,
  ThrottleConfigSchema,
  type GovernanceConfig,
  type CycleConfig,
  type ThrottleConfig,
} from "../../src/config/schemas.js";

import { snakeToCamel } from "../../src/config/loader.js";

import { CircuitBreaker } from "../../src/safety/circuit-breaker.js";
import { RalphWiggumGate } from "../../src/safety/ralph-wiggum.js";
import { checkFixComplexity } from "../../src/safety/governance.js";
import {
  ThrottlePolicy,
  CapacityAdapter,
  DemandAdapter,
  type ThrottleAdapter,
  type ThrottleContext,
} from "../../src/safety/throttle.js";
import { GuardrailsCoordinator } from "../../src/safety/guardrails.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tmpDir: string;

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "regression-test-"));
});

afterEach(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

function tmpPath(filename: string = "state.json"): string {
  return path.join(tmpDir, filename);
}

/** Build a ticket with given severity and status for gate tests. */
function openTicket(severity: string, status: string = "open"): SWETicket {
  return createTicket("Test ticket", "desc", {
    severity: severity as SWETicket["severity"],
    status: status as SWETicket["status"],
  });
}

/** Default governance config with gate enabled. */
function govConfig(overrides: Partial<GovernanceConfig> = {}): GovernanceConfig {
  return {
    maxOpenCritical: 0,
    maxOpenHigh: 3,
    maxFailingTests: 0,
    requireCiGreen: true,
    checkIntervalHours: 6,
    enabled: true,
    ...overrides,
  };
}

/** Default cycle config. */
function cycleConfig(overrides: Partial<CycleConfig> = {}): CycleConfig {
  return {
    maxNewTicketsPerCycle: 20,
    maxInvestigationsPerCycle: 5,
    maxDevelopmentsPerCycle: 2,
    maxOpenInvestigating: 3,
    severityFilter: "high",
    maxInvestigationWorkers: 8,
    maxReinvestigations: 1,
    blockedTicketTimeoutHours: 4,
    blockedTicketEscalationHours: 24,
    ...overrides,
  };
}

/** Default throttle config. */
function throttleConfig(overrides: Partial<ThrottleConfig> = {}): ThrottleConfig {
  return {
    enabled: true,
    weeklyBudgetUsd: 500,
    backlogSurgeThreshold: 200,
    criticalSurgeThreshold: 20,
    timeBands: {},
    capacityWarningPct: 0.8,
    capacityWarningDaysRemaining: 2.0,
    capacityWarningMultiplier: 0.5,
    capacityCriticalPct: 0.95,
    capacityCriticalMultiplier: 0.1,
    backlogSurgeMultiplier: 1.5,
    criticalSurgeMultiplier: 2.0,
    ...overrides,
  };
}

/** Default throttle context with safe values. */
function baseContext(overrides: Partial<ThrottleContext> = {}): ThrottleContext {
  return {
    nowUtc: new Date("2026-04-12T12:00:00Z"),
    apiUsagePct: 0.3,
    apiDaysToReset: 4,
    backlogSize: 10,
    backlogCritical: 0,
    isPreRelease: false,
    rateLimitCooling: false,
    ...overrides,
  };
}

// ===========================================================================
// 1. Model Parity Tests
// ===========================================================================

describe("REGRESSION: Model Parity", () => {
  // Python: SWETicket(title="x", description="y") defaults severity="medium"
  it("default severity matches Python SWETicket(severity=TicketSeverity.MEDIUM)", () => {
    const t = createTicket("x", "y");
    expect(t.severity).toBe("medium");
  });

  // Python: default status is TicketStatus.OPEN = "open"
  it("default status matches Python SWETicket(status=TicketStatus.OPEN)", () => {
    const t = createTicket("x", "y");
    expect(t.status).toBe("open");
  });

  // Python: default ticket_type is TicketType.UNKNOWN = "unknown"
  it("default ticketType matches Python SWETicket(ticket_type=TicketType.UNKNOWN)", () => {
    const t = createTicket("x", "y");
    expect(t.ticketType).toBe("unknown");
  });

  // Python: ticket_id = secrets.token_hex(6) -> 12 hex chars
  it("ticketId is 12-char hex string matching Python secrets.token_hex(6)", () => {
    const t = createTicket("x", "y");
    expect(t.ticketId).toMatch(/^[0-9a-f]{12}$/);
  });

  // Python: created_at = datetime.utcnow().isoformat() -> ISO 8601 UTC
  it("timestamps are ISO 8601 UTC format matching Python datetime.utcnow().isoformat()", () => {
    const t = createTicket("x", "y");
    // Python isoformat() produces "2026-04-12T12:00:00.123456" (no Z suffix in some cases),
    // but JS .toISOString() always produces "...Z". Both are valid ISO 8601.
    expect(t.createdAt).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    expect(t.updatedAt).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  });

  // Python: all Optional fields default to None (null)
  it("all nullable fields default to null matching Python None defaults", () => {
    const t = createTicket("x", "y");
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

  // Python: all List fields default to field(default_factory=list) -> []
  it("all array fields default to [] matching Python field(default_factory=list)", () => {
    const t = createTicket("x", "y");
    expect(t.labels).toEqual([]);
    expect(t.relatedTickets).toEqual([]);
    expect(t.blockedBy).toEqual([]);
    expect(t.blocking).toEqual([]);
  });

  // Python: metadata defaults to field(default_factory=dict) -> {}
  it("metadata defaults to {} matching Python field(default_factory=dict)", () => {
    const t = createTicket("x", "y");
    expect(t.metadata).toEqual({});
  });

  // Python: two tickets created in sequence have different ticket_ids
  it("ticketIds are unique across calls matching Python secrets.token_hex uniqueness", () => {
    const ids = new Set(
      Array.from({ length: 50 }, () => createTicket("x", "y").ticketId),
    );
    expect(ids.size).toBe(50);
  });
});

// ===========================================================================
// 2. Resolution Audit Parity Tests
// ===========================================================================

describe("REGRESSION: Resolution Audit Parity", () => {
  // Python: RESOLUTION_BYPASS_REASONS has exactly 7 items
  it("RESOLUTION_BYPASS_REASONS has exactly 7 items matching Python set", () => {
    expect(RESOLUTION_BYPASS_REASONS.size).toBe(7);
  });

  // Python: each bypass reason in the set is recognized individually
  it("bypass 'false_regression' -> audit passes", () => {
    const t = createTicket("x", "y", {
      metadata: { resolution_note: "false_regression" },
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(true);
    expect(reason).toContain("bypass");
  });

  it("bypass 'duplicate' -> audit passes", () => {
    const t = createTicket("x", "y", {
      metadata: { resolution_note: "duplicate" },
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });

  it("bypass 'already_fixed_externally' -> audit passes", () => {
    const t = createTicket("x", "y", {
      metadata: { resolution_note: "already_fixed_externally" },
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });

  it("bypass 'not_reproducible' -> audit passes", () => {
    const t = createTicket("x", "y", {
      metadata: { resolution_note: "not_reproducible" },
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });

  it("bypass 'wont_fix_approved' -> audit passes", () => {
    const t = createTicket("x", "y", {
      metadata: { resolution_note: "wont_fix_approved" },
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });

  it("bypass 'manual_override' -> audit passes", () => {
    const t = createTicket("x", "y", {
      metadata: { resolution_note: "manual_override" },
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });

  it("bypass 'fix_succeeded' -> audit passes", () => {
    const t = createTicket("x", "y", {
      metadata: { resolution_note: "fix_succeeded" },
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });

  // Python: report < 200 chars fails
  it("report too short (<200 chars) -> fail, matching Python len(report) < 200", () => {
    const t = createTicket("x", "y", {
      investigationReport: "short",
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("investigation_report too short");
    expect(reason).toContain("5 chars");
  });

  // Python: boundary: exactly 200 chars passes the length check
  it("report exactly 200 chars -> pass (boundary), matching Python len(report) >= 200", () => {
    const t = createTicket("x", "y", {
      severity: "medium",
      investigationReport: "a".repeat(200),
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  // Python: boundary: 199 chars fails
  it("report 199 chars -> fail (boundary), matching Python len(report) < 200", () => {
    const t = createTicket("x", "y", {
      severity: "medium",
      investigationReport: "a".repeat(199),
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("199 chars");
  });

  // Python: HIGH severity with no attempts -> fail
  it("HIGH severity with no attempts -> fail, matching Python severity check", () => {
    const t = createTicket("x", "y", {
      severity: "high",
      investigationReport: "x".repeat(200),
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("HIGH ticket requires >=1 fix attempt");
  });

  // Python: HIGH severity with attempts -> pass
  it("HIGH severity with attempts -> pass, matching Python attempts check", () => {
    const t = createTicket("x", "y", {
      severity: "high",
      investigationReport: "x".repeat(200),
      metadata: { attempts: [{ id: 1 }] },
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  // Python: CRITICAL severity with no attempts -> fail
  it("CRITICAL severity with no attempts -> fail, matching Python severity check", () => {
    const t = createTicket("x", "y", {
      severity: "critical",
      investigationReport: "x".repeat(300),
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("CRITICAL ticket requires >=1 fix attempt");
  });

  // Python: CRITICAL severity with attempts -> pass
  it("CRITICAL severity with attempts -> pass", () => {
    const t = createTicket("x", "y", {
      severity: "critical",
      investigationReport: "x".repeat(300),
      metadata: { attempts: [{ id: 1, result: "fail" }] },
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  // Python: MEDIUM severity needs only sufficient report, no attempt needed
  it("MEDIUM severity with sufficient report only -> pass (no attempt needed)", () => {
    const t = createTicket("x", "y", {
      severity: "medium",
      investigationReport: "x".repeat(200),
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  // Python: LOW severity needs only sufficient report
  it("LOW severity with sufficient report only -> pass (no attempt needed)", () => {
    const t = createTicket("x", "y", {
      severity: "low",
      investigationReport: "x".repeat(250),
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  // Python: bypass reason overrides short report entirely
  it("bypass reason overrides short report for CRITICAL ticket", () => {
    const t = createTicket("x", "y", {
      severity: "critical",
      investigationReport: null,
      metadata: { resolution_note: "duplicate" },
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });

  // Python: bypass matching uses str.lower() and "in" operator (substring match)
  it("bypass matching is case-insensitive matching Python .lower()", () => {
    const t = createTicket("x", "y", {
      metadata: { resolution_note: "FALSE_REGRESSION" },
    });
    const [ok] = resolutionAudit(t);
    expect(ok).toBe(true);
  });

  // Python: empty attempts list still fails for HIGH
  it("HIGH with empty attempts array -> fail, matching Python len(attempts) == 0", () => {
    const t = createTicket("x", "y", {
      severity: "high",
      investigationReport: "x".repeat(200),
      metadata: { attempts: [] },
    });
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("Attempts list is empty");
  });

  // Python: null report -> report length is 0
  it("null investigation report -> 0 chars, matching Python (report or '')", () => {
    const t = createTicket("x", "y");
    const [ok, reason] = resolutionAudit(t);
    expect(ok).toBe(false);
    expect(reason).toContain("0 chars");
  });
});

// ===========================================================================
// 3. Circuit Breaker Parity Tests
// ===========================================================================

describe("REGRESSION: Circuit Breaker Parity", () => {
  // Python: failure_rate = sum(1 for r in results if not r) / len(results)
  // [T,T,F,F,F,F,F,T,T,T] -> 5 failures / 10 = 0.5
  it("failure rate [T,T,F,F,F,F,F,T,T,T] = 0.5 (NOT tripped), matching Python math", () => {
    const cb = new CircuitBreaker(tmpPath("cb1.json"), 10, 0.8, 30);
    const results = [true, true, false, false, false, false, false, true, true, true];
    for (const r of results) {
      cb.recordResult(r);
    }
    expect(cb.failureRate).toBeCloseTo(0.5, 5);
    expect(cb.isPaused).toBe(false);
  });

  // Python: [F,F,F,F,F,F,F,F,T,T] -> 8/10 = 0.8, trips at exactly 80%
  it("[F,F,F,F,F,F,F,F,T,T] = 0.8 (tripped at exactly 80%), matching Python threshold >=", () => {
    const cb = new CircuitBreaker(tmpPath("cb2.json"), 10, 0.8, 30);
    const results = [false, false, false, false, false, false, false, false, true, true];
    for (const r of results) {
      cb.recordResult(r);
    }
    expect(cb.failureRate).toBeCloseTo(0.8, 5);
    expect(cb.isPaused).toBe(true);
  });

  // Python: window overflow: 15 results but window=10 -> only last 10 count
  it("window overflow: 15 results, window=10 -> only last 10 count, matching Python deque(maxlen=10)", () => {
    const cb = new CircuitBreaker(tmpPath("cb3.json"), 10, 0.8, 30);
    // First 5 successes (will be evicted)
    for (let i = 0; i < 5; i++) cb.recordResult(true);
    // Then 10 more: 8 failures + 2 successes -> only these 10 count
    for (let i = 0; i < 8; i++) cb.recordResult(false);
    cb.recordResult(true);
    cb.recordResult(true);
    // Last 10: [F,F,F,F,F,F,F,F,T,T] -> 8/10 = 0.8
    expect(cb.failureRate).toBeCloseTo(0.8, 5);
    expect(cb.isPaused).toBe(true);
  });

  // Python: fewer than 5 results -> never trips regardless of rate
  it("fewer than 5 results -> never trips, matching Python len(results) >= 5 guard", () => {
    const cb = new CircuitBreaker(tmpPath("cb4.json"), 10, 0.8, 30);
    // 4 failures: rate = 4/4 = 1.0 but len < 5
    for (let i = 0; i < 4; i++) cb.recordResult(false);
    expect(cb.failureRate).toBeCloseTo(1.0, 5);
    expect(cb.isPaused).toBe(false);
  });

  // Python: exactly 5 failures -> 5/5 = 1.0, trips
  it("exactly 5 failures -> 5/5=1.0 (trips), matching Python threshold check at len==5", () => {
    const cb = new CircuitBreaker(tmpPath("cb5.json"), 10, 0.8, 30);
    for (let i = 0; i < 5; i++) cb.recordResult(false);
    expect(cb.failureRate).toBeCloseTo(1.0, 5);
    expect(cb.isPaused).toBe(true);
  });

  // Python: 4 failures -> 4/4=1.0 but len<5 does NOT trip
  it("4 failures -> 4/4=1.0 but len<5 (does NOT trip), matching Python minimum window", () => {
    const cb = new CircuitBreaker(tmpPath("cb6.json"), 10, 0.8, 30);
    for (let i = 0; i < 4; i++) cb.recordResult(false);
    expect(cb.failureRate).toBeCloseTo(1.0, 5);
    expect(cb.isPaused).toBe(false);
  });

  // Python: clearPause resets the pause state
  it("clearPause resets paused state, matching Python clear_pause()", () => {
    const cb = new CircuitBreaker(tmpPath("cb7.json"), 10, 0.8, 30);
    for (let i = 0; i < 5; i++) cb.recordResult(false);
    expect(cb.isPaused).toBe(true);
    cb.clearPause();
    expect(cb.isPaused).toBe(false);
  });

  // Python: recordSkip does NOT affect failure rate
  it("recordSkip does not affect failure rate, matching Python record_skip() semantics", () => {
    const cb = new CircuitBreaker(tmpPath("cb8.json"), 10, 0.8, 30);
    cb.recordResult(false);
    cb.recordResult(false);
    cb.recordSkip(); // should not affect anything
    expect(cb.failureRate).toBeCloseTo(1.0, 5); // 2/2
    // Still only 2 results, not 3
  });

  // Python: empty results -> failureRate = 0.0
  it("empty results -> failureRate = 0.0, matching Python empty deque case", () => {
    const cb = new CircuitBreaker(tmpPath("cb9.json"), 10, 0.8, 30);
    expect(cb.failureRate).toBe(0.0);
    expect(cb.isPaused).toBe(false);
  });
});

// ===========================================================================
// 4. Throttle Parity Tests
// ===========================================================================

describe("REGRESSION: Throttle Parity", () => {
  // Python: CapacityAdapter at exactly 95% -> critical multiplier (0.1)
  it("CapacityAdapter at exactly 95% -> critical multiplier, matching Python >= 0.95", () => {
    const config = throttleConfig();
    const adapter = new CapacityAdapter(config);
    const ctx = baseContext({ apiUsagePct: 0.95 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(0.1);
    expect(result.severityOverride).toBe("critical");
  });

  // Python: CapacityAdapter at 94.99% -> check warning threshold (not critical)
  it("CapacityAdapter at 94.99% -> not critical tier, matching Python < 0.95 branch", () => {
    const config = throttleConfig();
    const adapter = new CapacityAdapter(config);
    // 94.99% with 4 days to reset -> should hit warning (>= 0.8, >= 2 days)
    const ctx = baseContext({ apiUsagePct: 0.9499, apiDaysToReset: 4 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(0.5); // warning multiplier
    expect(result.severityOverride).toBe("critical");
    expect(result.reason).toContain("warning");
  });

  // Python: CapacityAdapter at 79% -> ok (no warning)
  it("CapacityAdapter at 79% -> ok, matching Python < 0.8 branch", () => {
    const config = throttleConfig();
    const adapter = new CapacityAdapter(config);
    const ctx = baseContext({ apiUsagePct: 0.79, apiDaysToReset: 4 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(1.0);
    expect(result.severityOverride).toBeNull();
  });

  // Python: DemandAdapter backlog=200, critical=20 -> critical_surge_multiplier
  it("DemandAdapter: backlog=200, critical=20 -> critical_surge_multiplier, matching Python critical-mass", () => {
    const config = throttleConfig();
    const adapter = new DemandAdapter(config);
    const ctx = baseContext({ backlogSize: 200, backlogCritical: 20 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(2.0);
    expect(result.reason).toContain("critical-mass");
  });

  // Python: DemandAdapter backlog=200, critical=19 -> backlog_surge_multiplier (not critical)
  it("DemandAdapter: backlog=200, critical=19 -> backlog_surge (not critical), matching Python boundary", () => {
    const config = throttleConfig();
    const adapter = new DemandAdapter(config);
    const ctx = baseContext({ backlogSize: 200, backlogCritical: 19 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(1.5);
    expect(result.reason).toContain("surge");
    expect(result.reason).not.toContain("critical-mass");
  });

  // Python: Compound: Time(0.5) x Capacity(0.1) = 0.05 -> clamped to 0.1
  it("compound: 0.5 * 0.1 = 0.05 -> clamped to 0.1, matching Python max(MIN_MULTIPLIER, ...)", () => {
    const halfAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 0.5, severityOverride: null, reason: "half" }),
    };
    const tenthAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 0.1, severityOverride: null, reason: "tenth" }),
    };
    const policy = new ThrottlePolicy(cycleConfig(), [halfAdapter, tenthAdapter]);
    const result = policy.resolve(baseContext());
    // 0.5 * 0.1 = 0.05, clamped to 0.1
    expect(result.effectiveMultiplier).toBe(0.1);
  });

  // Python: Compound: Demand(2.0) x Demand(2.0) = 4.0 -> clamped to 4.0 (at max)
  it("compound: 2.0 * 2.0 = 4.0 -> clamped to 4.0, matching Python min(MAX_MULTIPLIER, ...)", () => {
    const doubleAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 2.0, severityOverride: null, reason: "double" }),
    };
    const policy = new ThrottlePolicy(cycleConfig(), [doubleAdapter, doubleAdapter]);
    const result = policy.resolve(baseContext());
    expect(result.effectiveMultiplier).toBe(4.0);
  });

  // Python: Compound: 3.0 * 3.0 = 9.0 -> clamped to 4.0
  it("compound: 3.0 * 3.0 = 9.0 -> clamped to 4.0, matching Python MAX_MULTIPLIER=4.0", () => {
    const tripleAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 3.0, severityOverride: null, reason: "triple" }),
    };
    const policy = new ThrottlePolicy(cycleConfig(), [tripleAdapter, tripleAdapter]);
    const result = policy.resolve(baseContext());
    expect(result.effectiveMultiplier).toBe(4.0);
  });

  // Python: all limits floored at 1: 5 * 0.1 = 0.5 -> floored to 1
  it("all limits floored at 1: base=5, multiplier=0.1 -> floor(5*0.1)=0 -> max(1,0)=1, matching Python", () => {
    const tinyAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 0.1, severityOverride: null, reason: "tiny" }),
    };
    const policy = new ThrottlePolicy(
      cycleConfig({ maxInvestigationsPerCycle: 5 }),
      [tinyAdapter],
    );
    const result = policy.resolve(baseContext());
    // 5 * 0.1 = 0.5, floor = 0, max(1, 0) = 1
    expect(result.maxInvestigationsPerCycle).toBe(1);
  });

  // Python: maxDevelopmentsPerCycle=2, multiplier=0.1 -> floor(2*0.1)=0 -> 1
  it("maxDevelopmentsPerCycle=2 * 0.1 = 0.2 -> floored to 1, matching Python max(1, ...)", () => {
    const tinyAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 0.1, severityOverride: null, reason: "tiny" }),
    };
    const policy = new ThrottlePolicy(cycleConfig(), [tinyAdapter]);
    const result = policy.resolve(baseContext());
    // 2 * 0.1 = 0.2, floor = 0, max(1, 0) = 1
    expect(result.maxDevelopmentsPerCycle).toBe(1);
  });

  // Python: severity override uses the MOST restrictive (highest rank)
  it("severity override takes highest rank, matching Python max(sev_rank[...]) logic", () => {
    const highAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 1.0, severityOverride: "high", reason: "high" }),
    };
    const critAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 1.0, severityOverride: "critical", reason: "crit" }),
    };
    const policy = new ThrottlePolicy(cycleConfig(), [highAdapter, critAdapter]);
    const result = policy.resolve(baseContext());
    expect(result.severityFilter).toBe("critical");
  });

  // Python: severity override does not downgrade from base config
  it("severity override does not downgrade below base, matching Python 'only if more restrictive'", () => {
    const lowAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 1.0, severityOverride: "low", reason: "low" }),
    };
    // base severityFilter is "high" (rank 2), "low" is rank 0 -> no override
    const policy = new ThrottlePolicy(cycleConfig(), [lowAdapter]);
    const result = policy.resolve(baseContext());
    expect(result.severityFilter).toBe("high"); // unchanged
  });

  // Python: adapter error -> fallback to 1.0 multiplier
  it("adapter error -> fallback 1.0x, matching Python try/except with 1.0 fallback", () => {
    const badAdapter: ThrottleAdapter = {
      evaluate: () => { throw new Error("boom"); },
    };
    const policy = new ThrottlePolicy(cycleConfig(), [badAdapter]);
    const result = policy.resolve(baseContext());
    expect(result.effectiveMultiplier).toBe(1.0);
  });
});

// ===========================================================================
// 5. Ralph Wiggum Parity Tests
// ===========================================================================

describe("REGRESSION: Ralph Wiggum Parity", () => {
  // Python: 0 critical, 0 high -> PASS
  it("0 critical, 0 high -> PASS, matching Python all_checks_passed path", () => {
    const gate = new RalphWiggumGate(govConfig());
    const report = gate.evaluate([]);
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
    expect(report.openCritical).toBe(0);
    expect(report.openHigh).toBe(0);
  });

  // Python: 1 critical with max_open_critical=0 -> BLOCK
  it("1 critical (max_open_critical=0) -> BLOCK, matching Python open_critical > max", () => {
    const gate = new RalphWiggumGate(govConfig({ maxOpenCritical: 0 }));
    const tickets = [openTicket("critical")];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.openCritical).toBe(1);
    expect(report.details).toContain("critical");
  });

  // Python: 3 high with max_open_high=3 -> PASS (not exceeding, uses >)
  it("3 high (max_open_high=3) -> PASS (not exceeding), matching Python open_high > max (strict >)", () => {
    const gate = new RalphWiggumGate(govConfig({ maxOpenHigh: 3 }));
    const tickets = [
      openTicket("high"),
      openTicket("high"),
      openTicket("high"),
    ];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
    expect(report.openHigh).toBe(3);
  });

  // Python: 4 high with max_open_high=3 -> BLOCK (exceeding)
  it("4 high (max_open_high=3) -> BLOCK (exceeding), matching Python open_high > max", () => {
    const gate = new RalphWiggumGate(govConfig({ maxOpenHigh: 3 }));
    const tickets = [
      openTicket("high"),
      openTicket("high"),
      openTicket("high"),
      openTicket("high"),
    ];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.openHigh).toBe(4);
  });

  // Python: CI red + requireCiGreen=true -> BLOCK
  it("CI red + requireCiGreen=true -> BLOCK, matching Python ci_check branch", () => {
    const gate = new RalphWiggumGate(govConfig({ requireCiGreen: true }));
    const report = gate.evaluate([], { ciGreen: false });
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.ciStatus).toBe("red");
    expect(report.details).toContain("CI");
  });

  // Python: CI red + requireCiGreen=false -> PASS
  it("CI red + requireCiGreen=false -> PASS, matching Python skip ci_check", () => {
    const gate = new RalphWiggumGate(govConfig({ requireCiGreen: false }));
    const report = gate.evaluate([], { ciGreen: false });
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
  });

  // Python: 5% test failures -> WARN (warnPct=5)
  it("5% test failures -> WARN, matching Python fail_pct >= warn_pct", () => {
    const gate = new RalphWiggumGate(govConfig());
    const report = gate.evaluate([], { failingTests: 5, totalTests: 100 });
    expect(report.verdict).toBe(GovernanceVerdict.WARN);
    expect(report.details).toContain("WARN");
  });

  // Python: 10% test failures -> BLOCK (hardBlockPct=10)
  it("10% test failures -> BLOCK, matching Python fail_pct >= hard_block_pct", () => {
    const gate = new RalphWiggumGate(govConfig());
    const report = gate.evaluate([], { failingTests: 10, totalTests: 100 });
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.details).toContain("failing tests");
  });

  // Python: 4.99% test failures -> PASS (below 5% threshold)
  it("4.99% test failures -> PASS, matching Python fail_pct < warn_pct", () => {
    const gate = new RalphWiggumGate(govConfig());
    // 4.99 / 100 = 4.99%
    const report = gate.evaluate([], { failingTests: 4, totalTests: 100 });
    // 4/100 = 4% which is < 5%, should PASS
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
  });

  // Python: exactly 4.99% via fractional tests (need totalTests that gives exactly 4.99%)
  it("fractional: 499 failing / 10000 total = 4.99% -> PASS, matching Python exact boundary", () => {
    const gate = new RalphWiggumGate(govConfig());
    const report = gate.evaluate([], { failingTests: 499, totalTests: 10000 });
    // 499/10000 = 4.99% < 5%
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
  });

  // Python: resolved/closed tickets NOT counted as open
  it("resolved tickets not counted, matching Python OPEN_STATUSES filter", () => {
    const gate = new RalphWiggumGate(govConfig({ maxOpenCritical: 0 }));
    const tickets = [
      openTicket("critical", "resolved"),
      openTicket("critical", "closed"),
      openTicket("critical", "failed"),
    ];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
    expect(report.openCritical).toBe(0);
  });

  // Python: gate disabled -> always PASS
  it("gate disabled -> PASS regardless of tickets, matching Python enabled check", () => {
    const gate = new RalphWiggumGate(govConfig({ enabled: false }));
    const tickets = [openTicket("critical"), openTicket("critical"), openTicket("critical")];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
    expect(report.details).toContain("disabled");
  });

  // Python: OPEN_STATUSES includes investigating, in_development etc.
  it("investigating status counts as open, matching Python OPEN_STATUSES set", () => {
    const gate = new RalphWiggumGate(govConfig({ maxOpenCritical: 0 }));
    const tickets = [openTicket("critical", "investigating")];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.openCritical).toBe(1);
  });

  it("in_development status counts as open, matching Python OPEN_STATUSES set", () => {
    const gate = new RalphWiggumGate(govConfig({ maxOpenHigh: 0 }));
    const tickets = [openTicket("high", "in_development")];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.openHigh).toBe(1);
  });
});

// ===========================================================================
// 6. Governance Parity Tests
// ===========================================================================

describe("REGRESSION: Governance Parity", () => {
  // Python: 5 files, 200 lines -> valid (at limit)
  it("5 files, 200 lines -> valid (at limit), matching Python max_files=5, max_lines=200", () => {
    const files = ["src/a/1.ts", "src/a/2.ts", "src/a/3.ts", "src/a/4.ts", "src/a/5.ts"];
    const result = checkFixComplexity(files, 200, { maxFiles: 5, maxLines: 200 });
    expect(result.valid).toBe(true);
    expect(result.reason).toBe("ok");
  });

  // Python: 6 files, 200 lines -> invalid (over file limit)
  it("6 files, 200 lines -> invalid (over file limit), matching Python len(files) > max_files", () => {
    const files = ["src/a/1.ts", "src/a/2.ts", "src/a/3.ts", "src/a/4.ts", "src/a/5.ts", "src/a/6.ts"];
    const result = checkFixComplexity(files, 200, { maxFiles: 5, maxLines: 200 });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Too many files");
    expect(result.reason).toContain("6 > 5");
  });

  // Python: 5 files, 201 lines -> invalid (over line limit)
  it("5 files, 201 lines -> invalid (over line limit), matching Python lines > max_lines", () => {
    const files = ["src/a/1.ts", "src/a/2.ts", "src/a/3.ts", "src/a/4.ts", "src/a/5.ts"];
    const result = checkFixComplexity(files, 201, { maxFiles: 5, maxLines: 200 });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Too many lines");
    expect(result.reason).toContain("201 > 200");
  });

  // Python: requirements.txt + allowDependencyChanges=false -> invalid
  it("requirements.txt + allowDependencyChanges=false -> invalid, matching Python DEPENDENCY_FILES check", () => {
    const result = checkFixComplexity(["requirements.txt"], 10, {
      allowDependencyChanges: false,
    });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Dependency changes");
  });

  // Python: requirements.txt + allowDependencyChanges=true -> valid
  it("requirements.txt + allowDependencyChanges=true -> valid, matching Python bypass", () => {
    const result = checkFixComplexity(["requirements.txt"], 10, {
      allowDependencyChanges: true,
    });
    expect(result.valid).toBe(true);
  });

  // Python: all known dependency files are recognized
  it("all Python DEPENDENCY_FILES are recognized", () => {
    const depFiles = [
      "requirements.txt", "requirements.in", "pyproject.toml", "poetry.lock",
      "Pipfile", "Pipfile.lock", "setup.cfg", "setup.py",
      "package.json", "package-lock.json", "pnpm-lock.yaml",
    ];
    for (const f of depFiles) {
      const result = checkFixComplexity([f], 5, { allowDependencyChanges: false });
      expect(result.valid).toBe(false);
      expect(result.reason).toContain("Dependency changes");
    }
  });

  // Python: cross-module detection with allowedModules set
  it("cross-module with allowedModules -> blocks extra modules, matching Python set diff", () => {
    const result = checkFixComplexity(
      ["src/alpha/a.ts", "src/beta/b.ts", "src/gamma/c.ts"],
      30,
      { allowedModules: new Set(["alpha", "beta"]) },
    );
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Cross-module");
    expect(result.reason).toContain("gamma");
  });

  // Python: allowedModules always includes "tests" implicitly
  it("tests/ module is always allowed, matching Python allowed.add('tests')", () => {
    const result = checkFixComplexity(
      ["src/alpha/a.ts", "tests/test_alpha.ts"],
      30,
      { allowedModules: new Set(["alpha"]) },
    );
    expect(result.valid).toBe(true);
  });

  // Python: no allowedModules, multi-module -> detected
  it("no allowedModules, multi-module -> cross-module detected, matching Python auto-detection", () => {
    const result = checkFixComplexity(
      ["src/alpha/a.ts", "src/beta/b.ts"],
      30,
    );
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Cross-module");
  });

  // Python: empty file list -> invalid
  it("empty file list -> invalid, matching Python len(files_changed) == 0", () => {
    const result = checkFixComplexity([], 0);
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("No files");
  });

  // Python: single module with tests -> valid
  it("single module + tests -> valid, matching Python test exclusion from cross-module", () => {
    const result = checkFixComplexity(
      ["src/foo/bar.ts", "tests/test_foo.ts"],
      50,
    );
    expect(result.valid).toBe(true);
    expect(result.reason).toBe("ok");
  });
});

// ===========================================================================
// 7. Config Schema Parity Tests
// ===========================================================================

describe("REGRESSION: Config Schema Parity", () => {
  // Python: GovernanceConfig defaults match exactly
  it("GovernanceConfig defaults match Python dataclass defaults", () => {
    const result = GovernanceConfigSchema.parse({});
    expect(result.maxOpenCritical).toBe(0);
    expect(result.maxOpenHigh).toBe(3);
    expect(result.enabled).toBe(false);
    expect(result.requireCiGreen).toBe(true);
    expect(result.maxFailingTests).toBe(0);
    expect(result.checkIntervalHours).toBe(6);
  });

  // Python: CycleConfig has exactly 9 default fields
  it("CycleConfig all 9 defaults match Python dataclass", () => {
    const result = CycleConfigSchema.parse({});
    expect(result.maxNewTicketsPerCycle).toBe(20);
    expect(result.maxInvestigationsPerCycle).toBe(5);
    expect(result.maxDevelopmentsPerCycle).toBe(2);
    expect(result.maxOpenInvestigating).toBe(3);
    expect(result.severityFilter).toBe("high");
    expect(result.maxInvestigationWorkers).toBe(8);
    expect(result.maxReinvestigations).toBe(1);
    expect(result.blockedTicketTimeoutHours).toBe(4);
    expect(result.blockedTicketEscalationHours).toBe(24);
  });

  // Python: ModelConfig: t1_heavy="opus", t2_standard="sonnet", t3_fast="haiku"
  it("ModelConfig defaults match Python ModelTiers(t1_heavy='opus', t2_standard='sonnet', t3_fast='haiku')", () => {
    const result = ModelConfigSchema.parse({});
    expect(result.t1Heavy).toBe("opus");
    expect(result.t2Standard).toBe("sonnet");
    expect(result.t3Fast).toBe("haiku");
  });

  // Python: ThrottleConfig all multiplier defaults
  it("ThrottleConfig all multiplier defaults match Python dataclass", () => {
    const result = ThrottleConfigSchema.parse({});
    expect(result.enabled).toBe(false);
    expect(result.weeklyBudgetUsd).toBe(500.0);
    expect(result.backlogSurgeThreshold).toBe(200);
    expect(result.criticalSurgeThreshold).toBe(20);
    expect(result.capacityWarningPct).toBe(0.8);
    expect(result.capacityWarningDaysRemaining).toBe(2.0);
    expect(result.capacityWarningMultiplier).toBe(0.5);
    expect(result.capacityCriticalPct).toBe(0.95);
    expect(result.capacityCriticalMultiplier).toBe(0.1);
    expect(result.backlogSurgeMultiplier).toBe(1.5);
    expect(result.criticalSurgeMultiplier).toBe(2.0);
    expect(result.timeBands).toEqual({});
  });

  // Python: snakeToCamel "a2a_hub_url" -> "a2aHubUrl"
  it("snakeToCamel: 'a2a_hub_url' -> 'a2aHubUrl', matching Python config loader", () => {
    const result = snakeToCamel({ a2a_hub_url: "http://example.com" });
    expect(result).toEqual({ a2aHubUrl: "http://example.com" });
  });

  // Python: snakeToCamel handles numeric segments
  it("snakeToCamel: numeric segments like 'max_retries_on_429' -> 'maxRetriesOn429'", () => {
    const result = snakeToCamel({ max_retries_on_429: 5 });
    expect(result).toEqual({ maxRetriesOn429: 5 });
  });

  // Edge case: "__double__" underscores
  it("snakeToCamel: '__double__' handling - leading underscores preserved", () => {
    // The regex /_([a-z0-9])/g only matches _ followed by [a-z0-9].
    // "__double__": the _d at position 1 is matched -> _D, the trailing __ are not
    // matched because _ is followed by _ (not [a-z0-9]) then end-of-string.
    // Result: "_Double__"
    const result = snakeToCamel({ __double__: "value" });
    expect(result).toHaveProperty("_Double__");
  });

  // Python: snakeToCamel with no underscores -> passthrough
  it("snakeToCamel: already camelCase -> passthrough, matching Python no-op", () => {
    const result = snakeToCamel({ alreadyCamel: "ok" });
    expect(result).toEqual({ alreadyCamel: "ok" });
  });

  // Python: snakeToCamel handles nested objects recursively
  it("snakeToCamel: nested objects converted recursively", () => {
    const result = snakeToCamel({
      outer_key: {
        inner_key: {
          deep_key: "value",
        },
      },
    });
    expect(result).toEqual({
      outerKey: {
        innerKey: {
          deepKey: "value",
        },
      },
    });
  });

  // Python: snakeToCamel handles arrays
  it("snakeToCamel: arrays of objects converted, matching Python recursive walk", () => {
    const result = snakeToCamel([
      { first_name: "Alice" },
      { last_name: "Bob" },
    ]);
    expect(result).toEqual([{ firstName: "Alice" }, { lastName: "Bob" }]);
  });

  // Python: snakeToCamel passes through primitives
  it("snakeToCamel: primitives pass through unchanged", () => {
    expect(snakeToCamel("hello")).toBe("hello");
    expect(snakeToCamel(42)).toBe(42);
    expect(snakeToCamel(true)).toBe(true);
    expect(snakeToCamel(null)).toBe(null);
  });
});

// ===========================================================================
// 8. Guardrails Gate Ordering Parity
// ===========================================================================

describe("REGRESSION: Guardrails Gate Ordering Parity", () => {
  // Python: Gate 1 (circuit breaker) blocks -> evaluatedGates = ["circuit_breaker"]
  it("circuit breaker blocks -> evaluatedGates = ['circuit_breaker'], matching Python order", () => {
    const coord = new GuardrailsCoordinator();
    const cb = new CircuitBreaker(tmpPath("g1.json"), 10, 0.8, 30);
    for (let i = 0; i < 5; i++) cb.recordResult(false);
    coord.setCircuitBreaker(cb);

    const decision = coord.evaluate();
    expect(decision.allowed).toBe(false);
    expect(decision.gate).toBe("circuit_breaker");
    expect(decision.evaluatedGates).toEqual(["circuit_breaker"]);
  });

  // Python: Gate 2 (budget) blocks -> evaluatedGates = ["circuit_breaker", "budget_gate"]
  it("budget gate blocks -> evaluatedGates includes circuit_breaker then budget_gate, matching Python order", () => {
    const coord = new GuardrailsCoordinator();
    // Add a healthy circuit breaker so gate 1 passes
    const cb = new CircuitBreaker(tmpPath("g2.json"), 10, 0.8, 30);
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);
    // Set cost tracker that blocks
    coord.setCostTracker(
      {
        check_budget: () => ({
          is_over_budget: true,
          is_warning: false,
          status: "over_budget",
          percent_used: 120,
          daily_spent: 6000,
          daily_limit: 5000,
          monthly_spent: 50000,
          monthly_limit: 40000,
        }),
      },
      "team-x",
    );

    const decision = coord.evaluate("investigate", "MEDIUM", 0, "team-x");
    expect(decision.allowed).toBe(false);
    expect(decision.gate).toBe("budget_gate");
    expect(decision.evaluatedGates).toEqual(["circuit_breaker", "budget_gate"]);
  });

  // Python: Gate 3 (usage governor) blocks -> evaluatedGates has 3 items
  it("usage governor blocks -> evaluatedGates = [circuit_breaker, budget_gate, usage_governor]", () => {
    const coord = new GuardrailsCoordinator();
    const cb = new CircuitBreaker(tmpPath("g3.json"), 10, 0.8, 30);
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);
    coord.setCostTracker(
      {
        check_budget: () => ({
          is_over_budget: false,
          is_warning: false,
          status: "ok",
          percent_used: 50,
          daily_spent: 2500,
          daily_limit: 5000,
          monthly_spent: 20000,
          monthly_limit: 40000,
        }),
      },
      "team-y",
    );
    coord.setUsageGovernor({
      get_concurrency_decision: () => ({
        allow_new_work: false,
        priority_floor: 0,
        audit_trail: "rate limited",
      }),
    });

    const decision = coord.evaluate("investigate", "MEDIUM", 0, "team-y");
    expect(decision.allowed).toBe(false);
    expect(decision.gate).toBe("usage_governor");
    expect(decision.evaluatedGates).toEqual([
      "circuit_breaker",
      "budget_gate",
      "usage_governor",
    ]);
  });

  // Python: Gate 4 (stability) blocks on deploy task ->
  // evaluatedGates = ["circuit_breaker", "budget_gate?", "usage_governor?", "stability_gate"]
  it("stability gate blocks deploy -> includes stability_gate in evaluatedGates, matching Python", () => {
    const coord = new GuardrailsCoordinator();
    const cb = new CircuitBreaker(tmpPath("g4.json"), 10, 0.8, 30);
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);
    coord.setUsageGovernor({
      get_concurrency_decision: () => ({
        allow_new_work: true,
        max_agents: 10,
        priority_floor: 4,
        audit_trail: "ok",
      }),
    });
    coord.setStabilityGate({
      evaluate: () => ({ verdict: "BLOCK", details: "too many bugs", open_critical: 5, open_high: 0 }),
    });

    // deploy task triggers stability gate
    const decision = coord.evaluate("deploy", "MEDIUM", 0);
    expect(decision.allowed).toBe(false);
    expect(decision.gate).toBe("stability_gate");
    expect(decision.evaluatedGates).toContain("circuit_breaker");
    expect(decision.evaluatedGates).toContain("usage_governor");
    expect(decision.evaluatedGates).toContain("stability_gate");
  });

  // Python: stability gate is NOT checked for "investigate" tasks
  it("stability gate NOT checked for investigate tasks, matching Python task_type filter", () => {
    const coord = new GuardrailsCoordinator();
    coord.setStabilityGate({
      evaluate: () => ({ verdict: "BLOCK", details: "bugs", open_critical: 5, open_high: 0 }),
    });

    const decision = coord.evaluate("investigate");
    expect(decision.allowed).toBe(true);
    expect(decision.evaluatedGates).not.toContain("stability_gate");
  });

  // Python: All pass -> evaluatedGates includes "all_clear" and gate="all_clear"
  it("all gates pass -> gate='all_clear', matching Python all_clear sentinel", () => {
    const coord = new GuardrailsCoordinator();
    const cb = new CircuitBreaker(tmpPath("g5.json"), 10, 0.8, 30);
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);
    coord.setUsageGovernor({
      get_concurrency_decision: () => ({
        allow_new_work: true,
        max_agents: 10,
        priority_floor: 4,
        audit_trail: "ok",
      }),
    });
    coord.setStabilityGate({
      evaluate: () => ({ verdict: "pass", details: "ok", open_critical: 0, open_high: 0 }),
    });
    const throttlePolicy = new ThrottlePolicy(cycleConfig(), []);
    coord.setThrottle(throttlePolicy);

    // deploy task -> checks all gates including stability
    const decision = coord.evaluate("deploy", "MEDIUM", 0);
    expect(decision.allowed).toBe(true);
    expect(decision.gate).toBe("all_clear");
    expect(decision.evaluatedGates).toContain("circuit_breaker");
    expect(decision.evaluatedGates).toContain("usage_governor");
    expect(decision.evaluatedGates).toContain("stability_gate");
    expect(decision.evaluatedGates).toContain("throttle");
    expect(decision.evaluatedGates).toContain("all_clear");
    // 5 entries: circuit_breaker, usage_governor, stability_gate, throttle, all_clear
    expect(decision.evaluatedGates.length).toBe(5);
  });

  // Python: blocked property is inverse of allowed
  it("blocked = !allowed, matching Python @property blocked", () => {
    const coord = new GuardrailsCoordinator();
    const decision = coord.evaluate();
    expect(decision.blocked).toBe(!decision.allowed);
    expect(decision.allowed).toBe(true);
    expect(decision.blocked).toBe(false);
  });

  // Python: blocked property works when blocked
  it("blocked=true when circuit breaker trips, matching Python @property blocked", () => {
    const coord = new GuardrailsCoordinator();
    const cb = new CircuitBreaker(tmpPath("g6.json"), 10, 0.8, 30);
    for (let i = 0; i < 5; i++) cb.recordResult(false);
    coord.setCircuitBreaker(cb);

    const decision = coord.evaluate();
    expect(decision.allowed).toBe(false);
    expect(decision.blocked).toBe(true);
  });

  // Python: canProceed is alias for evaluate
  it("canProceed is alias for evaluate, matching Python backward compat", () => {
    const coord = new GuardrailsCoordinator();
    const d1 = coord.evaluate("investigate", "MEDIUM", 0);
    const d2 = coord.canProceed("investigate", "MEDIUM", 0);
    expect(d1.allowed).toBe(d2.allowed);
    expect(d1.gate).toBe(d2.gate);
  });

  // Python: team-scoped circuit breakers are independent
  it("team-scoped circuit breakers are independent, matching Python team_circuit_breakers dict", () => {
    const coord = new GuardrailsCoordinator();
    const cbA = new CircuitBreaker(tmpPath("teamA.json"), 10, 0.8, 30);
    const cbB = new CircuitBreaker(tmpPath("teamB.json"), 10, 0.8, 30);

    // Trip team A
    for (let i = 0; i < 5; i++) cbA.recordResult(false);

    coord.setCircuitBreaker(cbA, "team-a");
    coord.setCircuitBreaker(cbB, "team-b");

    const decA = coord.evaluate("investigate", "MEDIUM", 0, "team-a");
    expect(decA.allowed).toBe(false);
    expect(decA.gate).toBe("circuit_breaker");

    const decB = coord.evaluate("investigate", "MEDIUM", 0, "team-b");
    expect(decB.allowed).toBe(true);
  });

  // Python: decision includes timestamp
  it("decision includes timestamp, matching Python time.time() in decision", () => {
    const coord = new GuardrailsCoordinator();
    const before = Date.now();
    const decision = coord.evaluate();
    const after = Date.now();
    expect(decision.timestamp).toBeGreaterThanOrEqual(before);
    expect(decision.timestamp).toBeLessThanOrEqual(after);
  });

  // Python: no gates configured -> minimal evaluatedGates = ["all_clear"]
  it("no gates configured -> evaluatedGates = ['all_clear'], matching Python empty coordinator", () => {
    const coord = new GuardrailsCoordinator();
    const decision = coord.evaluate();
    expect(decision.evaluatedGates).toEqual(["all_clear"]);
    expect(decision.gate).toBe("all_clear");
    expect(decision.allowed).toBe(true);
  });
});

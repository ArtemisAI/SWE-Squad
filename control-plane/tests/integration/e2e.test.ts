/**
 * End-to-end integration tests for the SWE-Squad control plane.
 *
 * Exercises the FULL pipeline from config load through to status output,
 * mocking only external services (Supabase, GitHub, Telegram, Claude CLI).
 *
 * Covers:
 *   1. Full pipeline: config -> ticket -> triage -> safety -> engine
 *   2. Ticket lifecycle transitions and resolution audit
 *   3. Safety gate cascade (all 5 gates wired together)
 *   4. Throttle pipeline with all 3 adapters
 *   5. Engine registry + CodingEngine interface compliance
 *   6. Config loading with env overrides
 *   7. Governance complexity checks and deployment governor lifecycle
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Config
import { loadConfig } from "../../src/config/loader.js";
import type {
  SWETeamConfig,
  GovernanceConfig,
  CycleConfig,
  ThrottleConfig,
} from "../../src/config/schemas.js";

// Models
import {
  type SWETicket,
  TicketSeverity,
  TicketStatus,
  GovernanceVerdict,
  createTicket,
  resolutionAudit,
  isBlocked,
  OPEN_STATUSES,
} from "../../src/models/ticket.js";
import { createEvent, type SWEEvent } from "../../src/models/events.js";

// Safety
import { CircuitBreaker } from "../../src/safety/circuit-breaker.js";
import { RalphWiggumGate } from "../../src/safety/ralph-wiggum.js";
import { GuardrailsCoordinator } from "../../src/safety/guardrails.js";
import {
  ThrottlePolicy,
  TimeBasedAdapter,
  CapacityAdapter,
  DemandAdapter,
  daysUntilWeeklyReset,
  type ThrottleContext,
  type ThrottleAdapter,
} from "../../src/safety/throttle.js";

// Governance
import {
  checkFixComplexity,
  DeploymentGovernor,
  DEPENDENCY_FILES,
} from "../../src/safety/governance.js";

// Engine
import {
  type CodingEngine,
  type EngineResult,
  type RunOptions,
  createEngineResult,
  isSuccess,
  classifyError,
} from "../../src/providers/engine/base.js";
import {
  registerEngine,
  resolveEngine,
  hasEngine,
  listEngines,
} from "../../src/providers/engine/registry.js";

// Supabase store helpers (row conversion only -- no network)
import {
  ticketToRow,
  rowToTicket,
} from "../../src/providers/supabase/store.js";

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

let tmpDir: string;

function freshTmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "e2e-test-"));
}

function tmpPath(filename: string): string {
  return path.join(tmpDir, filename);
}

/** Build a minimal open ticket. */
function openTicket(
  severity: string,
  status: string = "open",
  overrides?: Partial<SWETicket>,
): SWETicket {
  return createTicket("Test ticket", "Test description", {
    severity: severity as SWETicket["severity"],
    status: status as SWETicket["status"],
    ...overrides,
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

/** Default throttle context. */
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

// Environment variable management
const ENV_KEYS = [
  "SWE_TEAM_CONFIG",
  "SWE_TEAM_ENABLED",
  "SWE_TEAM_ID",
  "SWE_GITHUB_ACCOUNT",
  "SWE_MODEL_T1",
  "SWE_MODEL_T2",
  "SWE_MODEL_T3",
  "T1_MODEL",
  "T2_MODEL",
  "T3_MODEL",
];

let savedEnv: Record<string, string | undefined> = {};

function saveEnv(): void {
  for (const key of ENV_KEYS) {
    savedEnv[key] = process.env[key];
    delete process.env[key];
  }
}

function restoreEnv(): void {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = savedEnv[key];
    }
  }
}

// ===========================================================================
// 1. Full pipeline: config -> ticket -> triage -> safety -> engine
// ===========================================================================

describe("E2E: Full pipeline - config -> ticket -> triage -> safety -> engine", () => {
  beforeEach(() => {
    tmpDir = freshTmpDir();
    saveEnv();
  });

  afterEach(() => {
    restoreEnv();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("loads config with defaults and creates tickets at different severities", () => {
    const config = loadConfig(tmpPath("nonexistent.yaml"));
    expect(config.enabled).toBe(false);
    expect(config.teamId).toBe("default");
    expect(config.governance.maxOpenCritical).toBe(0);

    const critical = openTicket("critical");
    const high = openTicket("high");
    const medium = openTicket("medium");
    const low = openTicket("low");

    expect(critical.severity).toBe(TicketSeverity.CRITICAL);
    expect(high.severity).toBe(TicketSeverity.HIGH);
    expect(medium.severity).toBe(TicketSeverity.MEDIUM);
    expect(low.severity).toBe(TicketSeverity.LOW);

    // All start as OPEN
    for (const t of [critical, high, medium, low]) {
      expect(t.status).toBe(TicketStatus.OPEN);
      expect(OPEN_STATUSES.has(t.status)).toBe(true);
    }
  });

  it("RalphWiggum evaluates ticket mix and blocks on critical count", () => {
    const config = loadConfig(tmpPath("nonexistent.yaml"));
    const gate = new RalphWiggumGate(govConfig({ maxOpenCritical: 0 }));

    const tickets = [
      openTicket("critical"),
      openTicket("high"),
      openTicket("medium"),
    ];

    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.openCritical).toBe(1);
    expect(report.openHigh).toBe(1);
    expect(report.details).toContain("critical");
  });

  it("GuardrailsCoordinator blocks deploy when stability gate fails", () => {
    const cb = new CircuitBreaker(tmpPath("pipeline-cb.json"), 10, 0.8, 30);
    cb.recordResult(true); // healthy breaker

    const gate = new RalphWiggumGate(govConfig({ maxOpenCritical: 0, enabled: true }));

    const coord = new GuardrailsCoordinator();
    coord.setCircuitBreaker(cb);
    // Wrap RalphWiggumGate as a StabilityGate interface
    const tickets = [openTicket("critical")];
    coord.setStabilityGate({
      evaluate: () => {
        const r = gate.evaluate(tickets);
        return {
          verdict: r.verdict.toUpperCase(),
          details: r.details,
          open_critical: r.openCritical,
          open_high: r.openHigh,
        };
      },
    });

    // Deploy should be blocked
    const deployDecision = coord.evaluate("deploy", "MEDIUM", 0);
    expect(deployDecision.allowed).toBe(false);
    expect(deployDecision.gate).toBe("stability_gate");

    // Investigate should still be allowed (stability gate only blocks deploy/creative)
    const investigateDecision = coord.evaluate("investigate", "MEDIUM", 0);
    expect(investigateDecision.allowed).toBe(true);
  });

  it("GuardrailsCoordinator allows deploy when all gates pass", () => {
    const cb = new CircuitBreaker(tmpPath("pipeline-pass-cb.json"), 10, 0.8, 30);
    cb.recordResult(true);

    const gate = new RalphWiggumGate(govConfig({ maxOpenCritical: 1, enabled: true }));

    const coord = new GuardrailsCoordinator();
    coord.setCircuitBreaker(cb);

    // Only 1 critical, threshold is 1 -- should PASS
    const tickets = [openTicket("critical")];
    coord.setStabilityGate({
      evaluate: () => {
        const r = gate.evaluate(tickets);
        return {
          verdict: r.verdict.toUpperCase(),
          details: r.details,
          open_critical: r.openCritical,
          open_high: r.openHigh,
        };
      },
    });

    const decision = coord.evaluate("deploy", "MEDIUM", 0);
    expect(decision.allowed).toBe(true);
    expect(decision.gate).toBe("all_clear");
  });

  it("full pipeline: config -> create tickets -> stability gate -> guardrails -> engine registry", () => {
    // Step 1: Load config
    const yamlContent = `
enabled: true
team_id: e2e-test
governance:
  max_open_critical: 1
  max_open_high: 5
  enabled: true
`;
    const configPath = tmpPath("pipeline.yaml");
    fs.writeFileSync(configPath, yamlContent, "utf-8");
    const config = loadConfig(configPath);
    expect(config.enabled).toBe(true);
    expect(config.teamId).toBe("e2e-test");

    // Step 2: Create tickets
    const tickets: SWETicket[] = [
      openTicket("high"),
      openTicket("medium"),
      openTicket("low"),
    ];
    expect(tickets).toHaveLength(3);

    // Step 3: Stability gate
    const gate = new RalphWiggumGate(config.governance);
    const stabilityReport = gate.evaluate(tickets);
    expect(stabilityReport.verdict).toBe(GovernanceVerdict.PASS);

    // Step 4: Guardrails
    const cb = new CircuitBreaker(tmpPath("full-cb.json"));
    cb.recordResult(true);
    const coord = new GuardrailsCoordinator();
    coord.setCircuitBreaker(cb);
    coord.setStabilityGate({
      evaluate: () => ({
        verdict: stabilityReport.verdict,
        details: stabilityReport.details,
        open_critical: stabilityReport.openCritical,
        open_high: stabilityReport.openHigh,
      }),
    });

    const decision = coord.evaluate("deploy", "HIGH", 0);
    expect(decision.allowed).toBe(true);

    // Step 5: Engine registry - register a mock engine
    const mockEngineName = "e2e-mock-engine";
    registerEngine(mockEngineName, () => ({
      name: mockEngineName,
      run: async (prompt: string) =>
        createEngineResult({ stdout: `Processed: ${prompt}`, returncode: 0 }),
      healthCheck: async () => true,
    }));
    expect(hasEngine(mockEngineName)).toBe(true);

    const engine = resolveEngine(mockEngineName);
    expect(engine.name).toBe(mockEngineName);
  });
});

// ===========================================================================
// 2. Ticket lifecycle
// ===========================================================================

describe("E2E: Ticket lifecycle", () => {
  it("ticket starts in OPEN status", () => {
    const ticket = createTicket("Lifecycle bug", "Testing lifecycle");
    expect(ticket.status).toBe(TicketStatus.OPEN);
  });

  it("transitions OPEN -> TRIAGED -> INVESTIGATING -> INVESTIGATION_COMPLETE -> IN_DEVELOPMENT -> IN_REVIEW -> RESOLVED", () => {
    // Simulate the full lifecycle by creating a ticket and mutating status
    const ticket = createTicket("Lifecycle bug", "Testing lifecycle", {
      severity: "high",
    });

    const transitions: Array<[string, () => void]> = [
      [
        TicketStatus.TRIAGED,
        () => {
          ticket.status = TicketStatus.TRIAGED;
          ticket.assignedTo = "triage-agent";
        },
      ],
      [
        TicketStatus.INVESTIGATING,
        () => {
          ticket.status = TicketStatus.INVESTIGATING;
          ticket.assignedTo = "investigator-agent";
        },
      ],
      [
        TicketStatus.INVESTIGATION_COMPLETE,
        () => {
          ticket.status = TicketStatus.INVESTIGATION_COMPLETE;
          ticket.investigationReport = "x".repeat(250);
        },
      ],
      [
        TicketStatus.IN_DEVELOPMENT,
        () => {
          ticket.status = TicketStatus.IN_DEVELOPMENT;
          ticket.assignedTo = "developer-agent";
        },
      ],
      [
        TicketStatus.IN_REVIEW,
        () => {
          ticket.status = TicketStatus.IN_REVIEW;
          (ticket.metadata as Record<string, unknown>).attempts = [
            { id: 1, branch: "fix/lifecycle-bug" },
          ];
        },
      ],
      [
        TicketStatus.RESOLVED,
        () => {
          ticket.status = TicketStatus.RESOLVED;
          ticket.updatedAt = new Date().toISOString();
        },
      ],
    ];

    for (const [expectedStatus, transition] of transitions) {
      transition();
      expect(ticket.status).toBe(expectedStatus);
    }

    // Final check: resolution audit should pass (report >= 200 chars, attempts present)
    const [ok, reason] = resolutionAudit(ticket);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  it("resolution audit blocks RESOLVED without proper investigation report", () => {
    const ticket = createTicket("No report bug", "Missing report", {
      severity: "medium",
      status: "resolved",
    });

    const [ok, reason] = resolutionAudit(ticket);
    expect(ok).toBe(false);
    expect(reason).toContain("investigation_report too short");
  });

  it("resolution audit blocks HIGH ticket without attempts even with long report", () => {
    const ticket = createTicket("High severity bug", "Needs attempts", {
      severity: "high",
      status: "resolved",
      investigationReport: "x".repeat(300),
    });

    const [ok, reason] = resolutionAudit(ticket);
    expect(ok).toBe(false);
    expect(reason).toContain("requires >=1 fix attempt");
  });

  it("resolution audit passes CRITICAL ticket with report and attempts", () => {
    const ticket = createTicket("Critical fix", "Fixed it", {
      severity: "critical",
      status: "resolved",
      investigationReport: "x".repeat(250),
      metadata: { attempts: [{ id: 1, branch: "fix/critical" }] },
    });

    const [ok, reason] = resolutionAudit(ticket);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  it("resolution audit allows bypass via resolution_note", () => {
    const ticket = createTicket("Bypass bug", "Not a real bug", {
      severity: "critical",
      status: "resolved",
      metadata: { resolution_note: "false_regression" },
    });

    const [ok, reason] = resolutionAudit(ticket);
    expect(ok).toBe(true);
    expect(reason).toContain("bypass");
  });

  it("resolution audit passes LOW severity with only a long report (no attempts needed)", () => {
    const ticket = createTicket("Low sev bug", "Trivial fix", {
      severity: "low",
      status: "resolved",
      investigationReport: "y".repeat(200),
    });

    const [ok, reason] = resolutionAudit(ticket);
    expect(ok).toBe(true);
    expect(reason).toBe("audit passed");
  });

  it("isBlocked returns true when blockedBy is populated", () => {
    const ticket = createTicket("Blocked ticket", "Waiting", {
      blockedBy: ["dep-ticket-1"],
    });
    expect(isBlocked(ticket)).toBe(true);
  });

  it("isBlocked returns false when blockedBy is empty", () => {
    const ticket = createTicket("Unblocked ticket", "Good to go");
    expect(isBlocked(ticket)).toBe(false);
  });

  it("events are created for each lifecycle transition", () => {
    const ticketId = "lifecycle-test-001";
    const events: SWEEvent[] = [];

    events.push(createEvent("ticket.created", ticketId, "monitor"));
    events.push(createEvent("ticket.triaged", ticketId, "triage"));
    events.push(createEvent("ticket.investigating", ticketId, "investigator"));
    events.push(
      createEvent("ticket.investigation_complete", ticketId, "investigator", {
        rootCause: "null pointer",
      }),
    );
    events.push(createEvent("ticket.in_development", ticketId, "developer"));
    events.push(createEvent("ticket.in_review", ticketId, "developer"));
    events.push(
      createEvent("ticket.resolved", ticketId, "developer", {
        branch: "fix/null-ptr",
      }),
    );

    expect(events).toHaveLength(7);
    // All events share the same ticketId
    for (const e of events) {
      expect(e.ticketId).toBe(ticketId);
      expect(e.eventId).toMatch(/^[0-9a-f]{16}$/);
      expect(e.timestamp).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    }

    // Each event has a unique ID
    const ids = new Set(events.map((e) => e.eventId));
    expect(ids.size).toBe(7);
  });
});

// ===========================================================================
// 3. Safety gate cascade
// ===========================================================================

describe("E2E: Safety gate cascade", () => {
  beforeEach(() => {
    tmpDir = freshTmpDir();
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("all clear: every gate passes -> allowed", () => {
    const coord = new GuardrailsCoordinator();

    // Wire circuit breaker (healthy)
    const cb = new CircuitBreaker(tmpPath("cascade-cb.json"), 10, 0.8, 30);
    cb.recordResult(true);
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);

    // Wire stability gate (passing)
    coord.setStabilityGate({
      evaluate: () => ({
        verdict: "pass",
        details: "All clear",
        open_critical: 0,
        open_high: 0,
      }),
    });

    // Wire usage governor (allowing)
    coord.setUsageGovernor({
      get_concurrency_decision: () => ({
        allow_new_work: true,
        max_agents: 10,
        priority_floor: 4,
        audit_trail: "ok",
      }),
    });

    // Wire cost tracker (within budget)
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
      "test-team",
    );

    const decision = coord.evaluate("deploy", "MEDIUM", 0, "test-team");
    expect(decision.allowed).toBe(true);
    expect(decision.blocked).toBe(false);
    expect(decision.gate).toBe("all_clear");
    expect(decision.evaluatedGates).toContain("circuit_breaker");
    expect(decision.evaluatedGates).toContain("budget_gate");
    expect(decision.evaluatedGates).toContain("usage_governor");
    expect(decision.evaluatedGates).toContain("stability_gate");
  });

  it("circuit breaker tripped -> blocked at gate 1", () => {
    const coord = new GuardrailsCoordinator();

    const cb = new CircuitBreaker(tmpPath("cascade-trip-cb.json"), 10, 0.8, 30);
    // Trip: 5 consecutive failures
    for (let i = 0; i < 5; i++) cb.recordResult(false);
    expect(cb.isPaused).toBe(true);

    coord.setCircuitBreaker(cb);

    // Other gates wired but should not be reached
    coord.setStabilityGate({
      evaluate: () => ({
        verdict: "pass",
        details: "ok",
        open_critical: 0,
        open_high: 0,
      }),
    });

    const decision = coord.evaluate("deploy", "MEDIUM", 0);
    expect(decision.allowed).toBe(false);
    expect(decision.blocked).toBe(true);
    expect(decision.gate).toBe("circuit_breaker");
    // Only circuit_breaker should be in evaluatedGates (short-circuits)
    expect(decision.evaluatedGates).toContain("circuit_breaker");
    expect(decision.evaluatedGates).not.toContain("stability_gate");
    expect(decision.evaluatedGates).not.toContain("usage_governor");
  });

  it("budget gate blocks at gate 2 when over budget", () => {
    const coord = new GuardrailsCoordinator();

    // Healthy circuit breaker
    const cb = new CircuitBreaker(tmpPath("cascade-budget-cb.json"));
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);

    // Over budget
    coord.setCostTracker(
      {
        check_budget: () => ({
          is_over_budget: true,
          is_warning: false,
          status: "over_budget",
          percent_used: 110,
          daily_spent: 5500,
          daily_limit: 5000,
          monthly_spent: 45000,
          monthly_limit: 40000,
        }),
      },
      "expensive-team",
    );

    const decision = coord.evaluate("investigate", "MEDIUM", 0, "expensive-team");
    expect(decision.allowed).toBe(false);
    expect(decision.gate).toBe("budget_gate");
    expect(decision.evaluatedGates).toContain("circuit_breaker");
    expect(decision.evaluatedGates).toContain("budget_gate");
    // Should not reach usage governor
    expect(decision.evaluatedGates).not.toContain("usage_governor");
  });

  it("usage governor blocks at gate 3 when work not allowed", () => {
    const coord = new GuardrailsCoordinator();

    // Healthy circuit breaker
    const cb = new CircuitBreaker(tmpPath("cascade-usage-cb.json"));
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);

    // Usage governor blocks
    coord.setUsageGovernor({
      get_concurrency_decision: () => ({
        allow_new_work: false,
        priority_floor: 0,
        audit_trail: "rate limited",
      }),
    });

    const decision = coord.evaluate("investigate", "MEDIUM", 0);
    expect(decision.allowed).toBe(false);
    expect(decision.gate).toBe("usage_governor");
    expect(decision.evaluatedGates).toContain("circuit_breaker");
    expect(decision.evaluatedGates).toContain("usage_governor");
  });

  it("stability gate blocks deploy at gate 4 with too many critical tickets", () => {
    const coord = new GuardrailsCoordinator();

    // Healthy circuit breaker
    const cb = new CircuitBreaker(tmpPath("cascade-stab-cb.json"));
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);

    // Passing usage governor
    coord.setUsageGovernor({
      get_concurrency_decision: () => ({
        allow_new_work: true,
        max_agents: 10,
        priority_floor: 4,
        audit_trail: "ok",
      }),
    });

    // Stability gate blocks
    coord.setStabilityGate({
      evaluate: () => ({
        verdict: "BLOCK",
        details: "5 critical bugs open",
        open_critical: 5,
        open_high: 3,
      }),
    });

    const decision = coord.evaluate("deploy", "MEDIUM", 0);
    expect(decision.allowed).toBe(false);
    expect(decision.gate).toBe("stability_gate");
    expect(decision.evaluatedGates).toContain("circuit_breaker");
    expect(decision.evaluatedGates).toContain("usage_governor");
    expect(decision.evaluatedGates).toContain("stability_gate");
  });

  it("circuit breaker clear but stability blocked -> blocked at gate 4 for deploy", () => {
    const coord = new GuardrailsCoordinator();

    const cb = new CircuitBreaker(tmpPath("cascade-mixed-cb.json"));
    cb.recordResult(true);
    cb.recordResult(true);
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);

    // Stability blocks
    coord.setStabilityGate({
      evaluate: () => ({
        verdict: "BLOCK",
        reason: "Too many bugs",
        details: "Too many bugs",
        open_critical: 10,
        open_high: 20,
      }),
    });

    // Deploy blocked by stability
    const deployDecision = coord.evaluate("deploy", "HIGH", 0);
    expect(deployDecision.allowed).toBe(false);
    expect(deployDecision.gate).toBe("stability_gate");

    // Creative also blocked by stability
    const creativeDecision = coord.evaluate("creative", "HIGH", 0);
    expect(creativeDecision.allowed).toBe(false);
    expect(creativeDecision.gate).toBe("stability_gate");

    // Investigate NOT blocked by stability (gate only applies to deploy/creative)
    const investigateDecision = coord.evaluate("investigate", "HIGH", 0);
    expect(investigateDecision.allowed).toBe(true);
  });

  it("health snapshot reflects all gate states", () => {
    const coord = new GuardrailsCoordinator();

    const cb = new CircuitBreaker(tmpPath("health-cb.json"), 10, 0.8, 30);
    cb.recordResult(true);
    cb.recordResult(false);
    coord.setCircuitBreaker(cb);

    coord.setStabilityGate({
      evaluate: () => ({
        verdict: "warn",
        details: "minor issues",
        open_critical: 0,
        open_high: 2,
      }),
    });

    const health = coord.health();
    expect(health.circuitBreakerPaused).toBe(false);
    expect(health.circuitBreakerFailureRate).toBe(0.5); // 1 fail / 2 total
    expect(health.stabilityVerdict).toBe("warn");
  });

  it("usage governor blocks by severity priority floor", () => {
    const coord = new GuardrailsCoordinator();

    // Healthy breaker
    const cb = new CircuitBreaker(tmpPath("sev-floor-cb.json"));
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);

    // Priority floor 1 = only CRITICAL (priority 0) and HIGH (priority 1) allowed
    coord.setUsageGovernor({
      get_concurrency_decision: () => ({
        allow_new_work: true,
        max_agents: 10,
        priority_floor: 1,
        audit_trail: "ok",
      }),
    });

    // MEDIUM = priority 2, above floor 1 -> blocked
    const mediumDecision = coord.evaluate("investigate", "MEDIUM", 0);
    expect(mediumDecision.allowed).toBe(false);
    expect(mediumDecision.gate).toBe("usage_governor");

    // HIGH = priority 1, at floor -> allowed
    const highDecision = coord.evaluate("investigate", "HIGH", 0);
    expect(highDecision.allowed).toBe(true);

    // CRITICAL = priority 0, below floor -> allowed
    const critDecision = coord.evaluate("investigate", "CRITICAL", 0);
    expect(critDecision.allowed).toBe(true);
  });

  it("usage governor blocks by max agent count", () => {
    const coord = new GuardrailsCoordinator();

    const cb = new CircuitBreaker(tmpPath("max-agents-cb.json"));
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);

    coord.setUsageGovernor({
      get_concurrency_decision: () => ({
        allow_new_work: true,
        max_agents: 3,
        priority_floor: 4,
        audit_trail: "ok",
      }),
    });

    // 3 agents running, max 3 -> blocked
    const decision = coord.evaluate("investigate", "MEDIUM", 3);
    expect(decision.allowed).toBe(false);
    expect(decision.gate).toBe("usage_governor");
    expect(decision.reason).toContain("3 agents running");

    // 2 agents running, max 3 -> allowed
    const okDecision = coord.evaluate("investigate", "MEDIUM", 2);
    expect(okDecision.allowed).toBe(true);
  });
});

// ===========================================================================
// 4. Throttle pipeline
// ===========================================================================

describe("E2E: Throttle pipeline", () => {
  it("creates ThrottlePolicy with all 3 adapters and resolves normal context", () => {
    const tConfig = throttleConfig();
    const adapters: ThrottleAdapter[] = [
      new TimeBasedAdapter(tConfig),
      new CapacityAdapter(tConfig),
      new DemandAdapter(tConfig),
    ];
    const policy = new ThrottlePolicy(cycleConfig(), adapters);

    const result = policy.resolve(baseContext());
    expect(result.effectiveMultiplier).toBe(1.0);
    expect(result.maxNewTicketsPerCycle).toBe(20);
    expect(result.maxInvestigationsPerCycle).toBe(5);
    expect(result.maxDevelopmentsPerCycle).toBe(2);
    expect(result.maxOpenInvestigating).toBe(3);
    expect(result.severityFilter).toBe("high");
    expect(result.reasons.length).toBeGreaterThanOrEqual(3);
  });

  it("time-based throttling reduces capacity during off-peak hours", () => {
    const tConfig = throttleConfig({
      timeBands: {
        offPeak: { startHour: 0, endHour: 6, multiplier: 0.3, timezone: "UTC" },
      },
    });
    const adapters: ThrottleAdapter[] = [
      new TimeBasedAdapter(tConfig),
      new CapacityAdapter(tConfig),
      new DemandAdapter(tConfig),
    ];
    const policy = new ThrottlePolicy(cycleConfig(), adapters);

    // 3 AM UTC should hit off-peak
    const ctx = baseContext({ nowUtc: new Date("2026-04-12T03:00:00Z") });
    const result = policy.resolve(ctx);

    expect(result.effectiveMultiplier).toBe(0.3);
    // 20 * 0.3 = 6
    expect(result.maxNewTicketsPerCycle).toBe(6);
    // 5 * 0.3 = 1.5, floored to 1
    expect(result.maxInvestigationsPerCycle).toBe(1);
  });

  it("capacity throttling with high API usage overrides severity", () => {
    const tConfig = throttleConfig();
    const adapters: ThrottleAdapter[] = [
      new TimeBasedAdapter(tConfig),
      new CapacityAdapter(tConfig),
      new DemandAdapter(tConfig),
    ];
    const policy = new ThrottlePolicy(cycleConfig(), adapters);

    // 96% usage -> critical capacity
    const ctx = baseContext({ apiUsagePct: 0.96 });
    const result = policy.resolve(ctx);

    expect(result.effectiveMultiplier).toBe(0.1);
    expect(result.severityFilter).toBe("critical");
    // All limits at floor of 1 (20 * 0.1 = 2, but 5 * 0.1 = 0.5 -> floor 1)
    expect(result.maxNewTicketsPerCycle).toBe(2);
    expect(result.maxInvestigationsPerCycle).toBeGreaterThanOrEqual(1);
    expect(result.maxDevelopmentsPerCycle).toBeGreaterThanOrEqual(1);
  });

  it("demand surge with large backlog increases capacity", () => {
    const tConfig = throttleConfig({ backlogSurgeThreshold: 100 });
    const adapters: ThrottleAdapter[] = [
      new TimeBasedAdapter(tConfig),
      new CapacityAdapter(tConfig),
      new DemandAdapter(tConfig),
    ];
    const policy = new ThrottlePolicy(cycleConfig(), adapters);

    const ctx = baseContext({ backlogSize: 250, backlogCritical: 0 });
    const result = policy.resolve(ctx);

    expect(result.effectiveMultiplier).toBe(1.5);
    // 20 * 1.5 = 30
    expect(result.maxNewTicketsPerCycle).toBe(30);
    // 5 * 1.5 = 7.5 -> 7
    expect(result.maxInvestigationsPerCycle).toBe(7);
  });

  it("compound multiplier clamped to max 4.0", () => {
    // Both time and demand boost
    const tConfig = throttleConfig({
      timeBands: {
        boost: { startHour: 0, endHour: 24, multiplier: 3.0 },
      },
      backlogSurgeThreshold: 50,
      criticalSurgeThreshold: 5,
    });
    const adapters: ThrottleAdapter[] = [
      new TimeBasedAdapter(tConfig),
      new CapacityAdapter(tConfig),
      new DemandAdapter(tConfig),
    ];
    const policy = new ThrottlePolicy(cycleConfig(), adapters);

    // 3.0 (time) * 1.0 (capacity ok) * 2.0 (critical mass) = 6.0, clamped to 4.0
    const ctx = baseContext({ backlogSize: 100, backlogCritical: 10 });
    const result = policy.resolve(ctx);

    expect(result.effectiveMultiplier).toBe(4.0);
  });

  it("compound multiplier clamped to min 0.1", () => {
    // Off-peak + critical capacity
    const tConfig = throttleConfig({
      timeBands: {
        dead: { startHour: 0, endHour: 24, multiplier: 0.2 },
      },
    });
    const adapters: ThrottleAdapter[] = [
      new TimeBasedAdapter(tConfig),
      new CapacityAdapter(tConfig),
      new DemandAdapter(tConfig),
    ];
    const policy = new ThrottlePolicy(cycleConfig(), adapters);

    // 0.2 (time) * 0.1 (capacity critical) * 1.0 (demand normal) = 0.02, clamped to 0.1
    const ctx = baseContext({ apiUsagePct: 0.96 });
    const result = policy.resolve(ctx);

    expect(result.effectiveMultiplier).toBe(0.1);
    // All limits floored at 1
    expect(result.maxNewTicketsPerCycle).toBeGreaterThanOrEqual(1);
    expect(result.maxInvestigationsPerCycle).toBeGreaterThanOrEqual(1);
    expect(result.maxDevelopmentsPerCycle).toBeGreaterThanOrEqual(1);
    expect(result.maxOpenInvestigating).toBeGreaterThanOrEqual(1);
  });

  it("pre-release flag triggers demand surge even with small backlog", () => {
    const tConfig = throttleConfig({ backlogSurgeThreshold: 200 });
    const adapters: ThrottleAdapter[] = [
      new TimeBasedAdapter(tConfig),
      new CapacityAdapter(tConfig),
      new DemandAdapter(tConfig),
    ];
    const policy = new ThrottlePolicy(cycleConfig(), adapters);

    const ctx = baseContext({ backlogSize: 5, isPreRelease: true });
    const result = policy.resolve(ctx);

    expect(result.effectiveMultiplier).toBe(1.5);
  });

  it("capacity warning with days to reset triggers throttle", () => {
    const tConfig = throttleConfig();
    const adapter = new CapacityAdapter(tConfig);

    const ctx = baseContext({ apiUsagePct: 0.85, apiDaysToReset: 5 });
    const result = adapter.evaluate(ctx, cycleConfig());

    expect(result.multiplier).toBe(0.5);
    expect(result.severityOverride).toBe("critical");
  });

  it("daysUntilWeeklyReset returns positive value", () => {
    const days = daysUntilWeeklyReset(new Date("2026-04-12T12:00:00Z"));
    expect(days).toBeGreaterThan(0);
    expect(days).toBeLessThanOrEqual(7);
  });

  it("overnight time band window works correctly", () => {
    const tConfig = throttleConfig({
      timeBands: {
        overnight: { startHour: 22, endHour: 6, multiplier: 0.4 },
      },
    });
    const adapter = new TimeBasedAdapter(tConfig);

    // 23:00 UTC should be in the overnight window
    const ctx = baseContext({ nowUtc: new Date("2026-04-12T23:00:00Z") });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(0.4);

    // 12:00 UTC should NOT be in the overnight window
    const midday = baseContext({ nowUtc: new Date("2026-04-12T12:00:00Z") });
    const middayResult = adapter.evaluate(midday, cycleConfig());
    expect(middayResult.multiplier).toBe(1.0);
  });
});

// ===========================================================================
// 5. Engine registry + CodingEngine interface compliance
// ===========================================================================

describe("E2E: Engine registry + CodingEngine interface", () => {
  const testEngineName = `test-engine-${Date.now()}`;

  it("registers, resolves, and calls a mock CodingEngine", async () => {
    const mockEngine: CodingEngine = {
      name: testEngineName,
      run: async (prompt: string, options?: RunOptions): Promise<EngineResult> => {
        return createEngineResult({
          stdout: `Mock output for: ${prompt}`,
          stderr: "",
          returncode: 0,
          model: options?.model ?? "mock-model",
          costUsd: 0.01,
          inputTokens: 100,
          outputTokens: 50,
          numTurns: 1,
          durationApiMs: 500,
          sessionId: "mock-session-1",
          metadata: { engine: testEngineName },
        });
      },
      healthCheck: async () => true,
    };

    registerEngine(testEngineName, () => mockEngine);
    expect(hasEngine(testEngineName)).toBe(true);
    expect(listEngines()).toContain(testEngineName);

    const engine = resolveEngine(testEngineName);
    expect(engine.name).toBe(testEngineName);

    const result = await engine.run("Fix the bug in auth module", {
      model: "sonnet",
      timeout: 120,
    });

    expect(result.stdout).toContain("Fix the bug");
    expect(result.returncode).toBe(0);
    expect(isSuccess(result)).toBe(true);
    expect(result.model).toBe("sonnet");
    expect(result.costUsd).toBe(0.01);
    expect(result.inputTokens).toBe(100);
    expect(result.outputTokens).toBe(50);
    expect(result.numTurns).toBe(1);
    expect(result.durationApiMs).toBe(500);
    expect(result.sessionId).toBe("mock-session-1");
    expect(result.metadata).toEqual({ engine: testEngineName });
  });

  it("createEngineResult sets sensible defaults", () => {
    const result = createEngineResult();
    expect(result.stdout).toBe("");
    expect(result.stderr).toBe("");
    expect(result.returncode).toBe(0);
    expect(result.costUsd).toBeNull();
    expect(result.model).toBeNull();
    expect(result.inputTokens).toBeNull();
    expect(result.outputTokens).toBeNull();
    expect(result.cacheReadTokens).toBeNull();
    expect(result.cacheCreationTokens).toBeNull();
    expect(result.numTurns).toBeNull();
    expect(result.durationApiMs).toBeNull();
    expect(result.sessionId).toBeNull();
    expect(result.metadata).toEqual({});
    expect(isSuccess(result)).toBe(true);
  });

  it("createEngineResult with failure has isSuccess=false", () => {
    const result = createEngineResult({
      returncode: 1,
      stderr: "Something failed",
    });
    expect(isSuccess(result)).toBe(false);
    expect(result.stderr).toBe("Something failed");
  });

  it("classifyError identifies rate limit errors", () => {
    expect(classifyError("HTTP 429 rate limit exceeded", 1)).toBe("rate_limit");
    expect(classifyError("rate_limit reached", 1)).toBe("rate_limit");
  });

  it("classifyError identifies auth errors", () => {
    expect(classifyError("HTTP 401 unauthorized", 1)).toBe("auth_error");
    expect(classifyError("403 forbidden", 1)).toBe("auth_error");
  });

  it("classifyError identifies timeout", () => {
    expect(classifyError("timeout exceeded", 1)).toBe("timeout");
    expect(classifyError("some error", -1)).toBe("timeout");
  });

  it("classifyError identifies overloaded", () => {
    expect(classifyError("HTTP 529 overloaded", 1)).toBe("overloaded");
    expect(classifyError("at capacity", 1)).toBe("overloaded");
  });

  it("classifyError identifies server errors", () => {
    expect(classifyError("HTTP 500 internal server error", 1)).toBe("server_error");
  });

  it("classifyError identifies model not found", () => {
    expect(classifyError("model not found: opus-99", 1)).toBe("model_not_found");
    expect(classifyError("HTTP 404", 1)).toBe("model_not_found");
  });

  it("classifyError returns unknown for unrecognized errors", () => {
    expect(classifyError("something weird", 1)).toBe("unknown");
  });

  it("resolveEngine throws for unregistered engine", () => {
    expect(() => resolveEngine("nonexistent-engine-xyz")).toThrow(
      /not registered/,
    );
  });

  it("healthCheck works on mock engine", async () => {
    const mockEngine: CodingEngine = {
      name: "health-test-engine",
      run: async () => createEngineResult(),
      healthCheck: async () => true,
    };
    registerEngine("health-test-engine", () => mockEngine);

    const engine = resolveEngine("health-test-engine");
    const healthy = await engine.healthCheck();
    expect(healthy).toBe(true);
  });

  it("engine with run-only (no cwd/env options) still works", async () => {
    const simpleEngine: CodingEngine = {
      name: "simple-engine",
      run: async (prompt: string) =>
        createEngineResult({ stdout: prompt.toUpperCase(), returncode: 0 }),
      healthCheck: async () => true,
    };
    registerEngine("simple-engine", () => simpleEngine);

    const engine = resolveEngine("simple-engine");
    const result = await engine.run("hello world");
    expect(result.stdout).toBe("HELLO WORLD");
    expect(isSuccess(result)).toBe(true);
  });
});

// ===========================================================================
// 6. Config loading with env overrides
// ===========================================================================

describe("E2E: Config loading with env overrides", () => {
  beforeEach(() => {
    tmpDir = freshTmpDir();
    saveEnv();
  });

  afterEach(() => {
    restoreEnv();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("loads defaults when YAML file does not exist", () => {
    const config = loadConfig(tmpPath("missing.yaml"));
    expect(config.enabled).toBe(false);
    expect(config.teamId).toBe("default");
    expect(config.githubAccount).toBe("");
    expect(config.models.t1Heavy).toBe("opus");
    expect(config.models.t2Standard).toBe("sonnet");
    expect(config.models.t3Fast).toBe("haiku");
    expect(config.governance.maxOpenCritical).toBe(0);
    expect(config.cycle.maxNewTicketsPerCycle).toBe(20);
  });

  it("writes temp YAML config and loads it with overrides", () => {
    const yaml = `
enabled: false
team_id: yaml-team
github_account: yaml-bot
governance:
  max_open_critical: 2
  max_open_high: 8
  enabled: true
models:
  t1_heavy: yaml-opus
  t2_standard: yaml-sonnet
cycle:
  max_new_tickets_per_cycle: 50
  max_investigations_per_cycle: 10
memory:
  embedding_model: custom-embed
  top_k: 10
throttle:
  enabled: true
  weekly_budget_usd: 1000
`;
    const configPath = tmpPath("env-override.yaml");
    fs.writeFileSync(configPath, yaml, "utf-8");

    // Set env var overrides
    process.env.SWE_TEAM_ENABLED = "true";
    process.env.SWE_TEAM_ID = "env-team";
    process.env.SWE_GITHUB_ACCOUNT = "env-bot";
    process.env.SWE_MODEL_T1 = "env-opus";
    process.env.SWE_MODEL_T2 = "env-sonnet";
    process.env.SWE_MODEL_T3 = "env-haiku";

    const config = loadConfig(configPath);

    // Env overrides take precedence
    expect(config.enabled).toBe(true); // env overrides YAML false
    expect(config.teamId).toBe("env-team"); // env overrides YAML "yaml-team"
    expect(config.githubAccount).toBe("env-bot"); // env overrides YAML "yaml-bot"
    expect(config.models.t1Heavy).toBe("env-opus"); // env overrides YAML "yaml-opus"
    expect(config.models.t2Standard).toBe("env-sonnet");
    expect(config.models.t3Fast).toBe("env-haiku");

    // YAML values preserved where no env override
    expect(config.governance.maxOpenCritical).toBe(2);
    expect(config.governance.maxOpenHigh).toBe(8);
    expect(config.governance.enabled).toBe(true);
    expect(config.cycle.maxNewTicketsPerCycle).toBe(50);
    expect(config.cycle.maxInvestigationsPerCycle).toBe(10);
    expect(config.memory.embeddingModel).toBe("custom-embed");
    expect(config.memory.topK).toBe(10);
    expect(config.throttle.enabled).toBe(true);
    expect(config.throttle.weeklyBudgetUsd).toBe(1000);

    // Nested defaults still present
    expect(config.governance.requireCiGreen).toBe(true);
    expect(config.rateLimits.maxRetriesOn429).toBe(5);
    expect(config.staleTicketTimeouts.investigatingHours).toBe(4);
  });

  it("SWE_TEAM_CONFIG env var sets the config path", () => {
    const configPath = tmpPath("from-env-var.yaml");
    fs.writeFileSync(configPath, "team_id: env-path-team\n", "utf-8");
    process.env.SWE_TEAM_CONFIG = configPath;

    const config = loadConfig();
    expect(config.teamId).toBe("env-path-team");
  });

  it("explicit path argument overrides SWE_TEAM_CONFIG", () => {
    const envPath = tmpPath("env-config.yaml");
    const argPath = tmpPath("arg-config.yaml");
    fs.writeFileSync(envPath, "team_id: from-env\n", "utf-8");
    fs.writeFileSync(argPath, "team_id: from-arg\n", "utf-8");
    process.env.SWE_TEAM_CONFIG = envPath;

    const config = loadConfig(argPath);
    expect(config.teamId).toBe("from-arg");
  });

  it("T*_MODEL fallback works when SWE_MODEL_T* is not set", () => {
    process.env.T1_MODEL = "fallback-opus";
    process.env.T2_MODEL = "fallback-sonnet";
    process.env.T3_MODEL = "fallback-haiku";

    const config = loadConfig(tmpPath("missing.yaml"));
    expect(config.models.t1Heavy).toBe("fallback-opus");
    expect(config.models.t2Standard).toBe("fallback-sonnet");
    expect(config.models.t3Fast).toBe("fallback-haiku");
  });

  it("SWE_MODEL_T* takes precedence over T*_MODEL", () => {
    process.env.T1_MODEL = "old-opus";
    process.env.SWE_MODEL_T1 = "new-opus";

    const config = loadConfig(tmpPath("missing.yaml"));
    expect(config.models.t1Heavy).toBe("new-opus");
  });

  it("throws on invalid YAML schema", () => {
    const configPath = tmpPath("invalid.yaml");
    fs.writeFileSync(
      configPath,
      "governance:\n  max_open_critical: not-a-number\n",
      "utf-8",
    );

    expect(() => loadConfig(configPath)).toThrow(/Invalid SWE team config/);
  });

  it("snake_case YAML keys are properly converted to camelCase", () => {
    const yaml = `
stale_ticket_timeouts:
  investigating_hours: 12
  in_development_hours: 8
  in_review_hours: 72
rate_limits:
  max_retries_on_429: 15
  initial_backoff_seconds: 120
  max_backoff_seconds: 1800
`;
    const configPath = tmpPath("snake-case.yaml");
    fs.writeFileSync(configPath, yaml, "utf-8");

    const config = loadConfig(configPath);
    expect(config.staleTicketTimeouts.investigatingHours).toBe(12);
    expect(config.staleTicketTimeouts.inDevelopmentHours).toBe(8);
    expect(config.staleTicketTimeouts.inReviewHours).toBe(72);
    expect(config.rateLimits.maxRetriesOn429).toBe(15);
    expect(config.rateLimits.initialBackoffSeconds).toBe(120);
    expect(config.rateLimits.maxBackoffSeconds).toBe(1800);
  });

  it("config with teams section parses correctly", () => {
    const yaml = `
teams:
  alpha:
    name: alpha
    vm: worker-1
    github_account: your-bot-alpha
    role: full
    max_concurrent: 5
    cost_budget_daily: 100.0
    specialization:
      - qa
      - merge
  beta:
    name: beta
    vm: worker-2
    role: developer
    max_concurrent: 3
`;
    const configPath = tmpPath("teams.yaml");
    fs.writeFileSync(configPath, yaml, "utf-8");

    const config = loadConfig(configPath);
    expect(config.teams.alpha).toBeDefined();
    expect(config.teams.alpha.name).toBe("alpha");
    expect(config.teams.alpha.role).toBe("full");
    expect(config.teams.alpha.maxConcurrent).toBe(5);
    expect(config.teams.alpha.costBudgetDaily).toBe(100.0);
    expect(config.teams.alpha.specialization).toEqual(["qa", "merge"]);
    expect(config.teams.beta.role).toBe("developer");
    expect(config.teams.beta.maxConcurrent).toBe(3);
  });
});

// ===========================================================================
// 7. Governance complexity checks
// ===========================================================================

describe("E2E: Governance complexity checks", () => {
  it("valid fix within default limits passes", () => {
    const result = checkFixComplexity(["src/swe_team/monitor.ts"], 50);
    expect(result.valid).toBe(true);
    expect(result.reason).toBe("ok");
  });

  it("fix touching too many files is blocked", () => {
    const files = Array.from({ length: 8 }, (_, i) => `src/swe_team/f${i}.ts`);
    const result = checkFixComplexity(files, 100, { maxFiles: 5 });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Too many files");
  });

  it("fix changing too many lines is blocked", () => {
    const result = checkFixComplexity(["src/swe_team/big.ts"], 500, {
      maxLines: 200,
    });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Too many lines");
  });

  it("dependency file changes are blocked by default", () => {
    const result = checkFixComplexity(["package.json"], 5);
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Dependency changes");
  });

  it("all known dependency files are caught", () => {
    for (const depFile of DEPENDENCY_FILES) {
      const result = checkFixComplexity([depFile], 5);
      expect(result.valid).toBe(false);
    }
  });

  it("dependency changes allowed when explicitly enabled", () => {
    const result = checkFixComplexity(["package.json"], 5, {
      allowDependencyChanges: true,
    });
    expect(result.valid).toBe(true);
  });

  it("cross-module changes detected and blocked", () => {
    const result = checkFixComplexity(
      ["src/alpha/a.ts", "src/beta/b.ts"],
      30,
    );
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Cross-module");
  });

  it("cross-module allowed when modules are in allowed set", () => {
    const result = checkFixComplexity(
      ["src/alpha/a.ts", "src/beta/b.ts"],
      30,
      { allowedModules: new Set(["alpha", "beta"]) },
    );
    expect(result.valid).toBe(true);
  });

  it("test files do not count as a separate module", () => {
    const result = checkFixComplexity(
      ["src/swe_team/monitor.ts", "tests/monitor.test.ts"],
      80,
    );
    expect(result.valid).toBe(true);
  });

  it("empty file list is rejected", () => {
    const result = checkFixComplexity([], 0);
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("No files");
  });
});

describe("E2E: Deployment governor lifecycle", () => {
  it("canDeploy returns true when stability gate passes", () => {
    const gov = new DeploymentGovernor();
    const report = {
      verdict: GovernanceVerdict.PASS as const,
      openCritical: 0,
      openHigh: 0,
      failingTests: 0,
      ciStatus: "green",
      details: "All clear",
      checkedAt: new Date().toISOString(),
    };

    expect(gov.canDeploy(report)).toBe(true);
  });

  it("canDeploy returns false when stability gate blocks", () => {
    const gov = new DeploymentGovernor();
    const report = {
      verdict: GovernanceVerdict.BLOCK as const,
      openCritical: 3,
      openHigh: 5,
      failingTests: 10,
      ciStatus: "red",
      details: "BLOCKED: 3 critical tickets",
      checkedAt: new Date().toISOString(),
    };

    expect(gov.canDeploy(report)).toBe(false);
  });

  it("canDeploy allows deployment on WARN verdict", () => {
    const gov = new DeploymentGovernor();
    const report = {
      verdict: GovernanceVerdict.WARN as const,
      openCritical: 0,
      openHigh: 2,
      failingTests: 3,
      ciStatus: "green",
      details: "WARN: some failing tests",
      checkedAt: new Date().toISOString(),
    };

    expect(gov.canDeploy(report)).toBe(true);
  });

  it("full lifecycle: start -> complete deployment", () => {
    const gov = new DeploymentGovernor();
    expect(gov.records).toHaveLength(0);

    // Start deployment
    const rec = gov.startDeployment("ticket-001", "fix/auth-bug");
    expect(rec.ticketId).toBe("ticket-001");
    expect(rec.branch).toBe("fix/auth-bug");
    expect(rec.status).toBe("deploying");
    expect(rec.deploymentId).toBeTruthy();
    expect(rec.completedAt).toBeNull();

    expect(gov.records).toHaveLength(1);

    // Complete deployment
    const completed = gov.completeDeployment(rec.deploymentId, {
      tests: "passed",
      coverage: 95,
    });
    expect(completed).not.toBeNull();
    expect(completed!.status).toBe("deployed");
    expect(completed!.completedAt).not.toBeNull();
    expect(completed!.testResults).toEqual({ tests: "passed", coverage: 95 });
  });

  it("full lifecycle: start -> rollback deployment", () => {
    const gov = new DeploymentGovernor();

    const rec = gov.startDeployment("ticket-002", "fix/api-crash");
    expect(rec.status).toBe("deploying");

    const rolledBack = gov.rollback(rec.deploymentId, "Regression detected in /api/v1");
    expect(rolledBack).not.toBeNull();
    expect(rolledBack!.status).toBe("rolled_back");
    expect(rolledBack!.rollbackReason).toBe("Regression detected in /api/v1");
    expect(rolledBack!.completedAt).not.toBeNull();
  });

  it("completeDeployment returns null for unknown deploymentId", () => {
    const gov = new DeploymentGovernor();
    const result = gov.completeDeployment("nonexistent-id");
    expect(result).toBeNull();
  });

  it("rollback returns null for unknown deploymentId", () => {
    const gov = new DeploymentGovernor();
    const result = gov.rollback("nonexistent-id", "reason");
    expect(result).toBeNull();
  });

  it("tracks multiple deployments independently", () => {
    const gov = new DeploymentGovernor();

    const rec1 = gov.startDeployment("ticket-a", "fix/a");
    const rec2 = gov.startDeployment("ticket-b", "fix/b");
    const rec3 = gov.startDeployment("ticket-c", "fix/c");

    expect(gov.records).toHaveLength(3);

    gov.completeDeployment(rec1.deploymentId);
    gov.rollback(rec2.deploymentId, "Failed smoke test");
    // rec3 still deploying

    const records = gov.records;
    const r1 = records.find((r) => r.ticketId === "ticket-a");
    const r2 = records.find((r) => r.ticketId === "ticket-b");
    const r3 = records.find((r) => r.ticketId === "ticket-c");

    expect(r1!.status).toBe("deployed");
    expect(r2!.status).toBe("rolled_back");
    expect(r3!.status).toBe("deploying");
  });
});

// ===========================================================================
// Bonus: Supabase store row conversion (no network, pure logic)
// ===========================================================================

describe("E2E: Supabase store row conversion", () => {
  it("ticketToRow converts camelCase to snake_case", () => {
    const ticket = createTicket("Row test", "Testing row conversion", {
      severity: "high",
      assignedTo: "agent-1",
      sourceModule: "auth",
      investigationReport: "Found the issue",
    });

    const row = ticketToRow(ticket, "test-team");

    expect(row["ticket_id"]).toBe(ticket.ticketId);
    expect(row["title"]).toBe("Row test");
    expect(row["description"]).toBe("Testing row conversion");
    expect(row["severity"]).toBe("high");
    expect(row["assigned_to"]).toBe("agent-1");
    expect(row["source_module"]).toBe("auth");
    expect(row["investigation_report"]).toBe("Found the issue");
    expect(row["team_id"]).toBe("test-team");
    expect(row["status"]).toBe("high".includes("fail") ? "closed" : "open");
  });

  it("rowToTicket converts snake_case back to camelCase", () => {
    const row: Record<string, unknown> = {
      ticket_id: "abc123",
      title: "From row",
      description: "Testing reverse conversion",
      severity: "critical",
      status: "investigating",
      created_at: "2026-04-12T00:00:00.000Z",
      updated_at: "2026-04-12T01:00:00.000Z",
      assigned_to: "agent-2",
      source_module: "payments",
      error_log: "Error: boom",
      labels: ["urgent"],
      related_tickets: ["t-001"],
      metadata: { fingerprint: "fp-123" },
      investigation_report: "Investigated the issue",
      proposed_fix: "Apply patch",
      test_results: { passed: true },
      deployment_id: "deploy-42",
      rollback_reason: null,
      investigation_session_id: "sess-1",
      development_session_id: "sess-2",
      project_id: "proj-1",
      parent_ticket_id: "parent-1",
      goal: "Fix payments",
      team_id: "test-team",
    };

    const ticket = rowToTicket(row);

    expect(ticket.ticketId).toBe("abc123");
    expect(ticket.title).toBe("From row");
    expect(ticket.severity).toBe("critical");
    expect(ticket.status).toBe("investigating");
    expect(ticket.assignedTo).toBe("agent-2");
    expect(ticket.sourceModule).toBe("payments");
    expect(ticket.errorLog).toBe("Error: boom");
    expect(ticket.labels).toEqual(["urgent"]);
    expect(ticket.relatedTickets).toEqual(["t-001"]);
    expect(ticket.metadata).toEqual({ fingerprint: "fp-123" });
    expect(ticket.investigationReport).toBe("Investigated the issue");
    expect(ticket.proposedFix).toBe("Apply patch");
    expect(ticket.projectId).toBe("proj-1");
    expect(ticket.parentTicketId).toBe("parent-1");
    expect(ticket.goal).toBe("Fix payments");
    expect(ticket.investigationSessionId).toBe("sess-1");
    expect(ticket.developmentSessionId).toBe("sess-2");
  });

  it("rowToTicket handles missing optional fields gracefully", () => {
    const row: Record<string, unknown> = {
      ticket_id: "minimal",
      title: "Minimal",
      description: "Just the basics",
      severity: "low",
      status: "open",
      created_at: "2026-04-12T00:00:00.000Z",
      updated_at: "2026-04-12T00:00:00.000Z",
      team_id: "test",
    };

    const ticket = rowToTicket(row);
    expect(ticket.ticketId).toBe("minimal");
    expect(ticket.blockedBy).toEqual([]);
    expect(ticket.blocking).toEqual([]);
    expect(ticket.labels).toEqual([]);
    expect(ticket.relatedTickets).toEqual([]);
    expect(ticket.ticketType).toBe("unknown");
    expect(ticket.metadata).toEqual({});
    expect(ticket.investigationSessionId).toBeNull();
    expect(ticket.developmentSessionId).toBeNull();
  });

  it("ticketToRow strips fields not in DDL", () => {
    const ticket = createTicket("Strip test", "Testing field stripping", {
      ticketType: "bug",
      blockedBy: ["t-001"],
      blocking: ["t-002"],
    });

    const row = ticketToRow(ticket, "test-team");

    // These fields should be stripped (not in Supabase DDL)
    expect(row["ticket_type"]).toBeUndefined();
    expect(row["blocked_by"]).toBeUndefined();
    expect(row["blocking"]).toBeUndefined();
  });

  it("ticketToRow maps failed status to closed", () => {
    const ticket = createTicket("Failed ticket", "This failed", {
      status: "failed" as SWETicket["status"],
    });

    const row = ticketToRow(ticket, "test-team");
    expect(row["status"]).toBe("closed");
  });

  it("ticketToRow maps blocked status to acknowledged", () => {
    const ticket = createTicket("Blocked ticket", "This is blocked", {
      status: "blocked" as SWETicket["status"],
    });

    const row = ticketToRow(ticket, "test-team");
    expect(row["status"]).toBe("acknowledged");
  });

  it("round-trip: ticket -> row -> ticket preserves core fields", () => {
    const original = createTicket("Round trip", "Testing round trip", {
      severity: "high",
      status: "investigating",
      assignedTo: "agent-1",
      sourceModule: "auth",
      labels: ["urgent", "backend"],
      investigationReport: "Found root cause",
      proposedFix: "Apply patch",
      metadata: { fingerprint: "fp-999", custom: "data" },
      projectId: "proj-42",
      goal: "Fix auth flow",
    });

    const row = ticketToRow(original, "round-trip-team");
    // Add team_id which would be present from Supabase
    row["team_id"] = "round-trip-team";

    const restored = rowToTicket(row);

    expect(restored.ticketId).toBe(original.ticketId);
    expect(restored.title).toBe(original.title);
    expect(restored.description).toBe(original.description);
    expect(restored.severity).toBe(original.severity);
    expect(restored.status).toBe(original.status);
    expect(restored.assignedTo).toBe(original.assignedTo);
    expect(restored.sourceModule).toBe(original.sourceModule);
    expect(restored.labels).toEqual(original.labels);
    expect(restored.investigationReport).toBe(original.investigationReport);
    expect(restored.proposedFix).toBe(original.proposedFix);
    expect(restored.metadata).toEqual(original.metadata);
    expect(restored.projectId).toBe(original.projectId);
    expect(restored.goal).toBe(original.goal);
  });
});

// ===========================================================================
// Bonus: Circuit breaker integration
// ===========================================================================

describe("E2E: Circuit breaker integration", () => {
  beforeEach(() => {
    tmpDir = freshTmpDir();
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("trips after threshold exceeded and persists state", () => {
    const statePath = tmpPath("cb-persist.json");
    const cb = new CircuitBreaker(statePath, 10, 0.8, 30);

    // 5 failures = 100% failure rate
    for (let i = 0; i < 5; i++) {
      cb.recordResult(false);
    }

    expect(cb.isPaused).toBe(true);
    expect(cb.failureRate).toBe(1.0);

    // Verify persisted to disk
    expect(fs.existsSync(statePath)).toBe(true);
    const saved = JSON.parse(fs.readFileSync(statePath, "utf-8"));
    expect(saved.is_paused).toBe(true);
    expect(saved.failure_rate).toBe(1.0);
  });

  it("does not trip with mixed results below threshold", () => {
    const cb = new CircuitBreaker(tmpPath("cb-mixed.json"), 10, 0.8, 30);

    // 3 failures, 2 successes = 60% failure rate (below 80%)
    cb.recordResult(false);
    cb.recordResult(false);
    cb.recordResult(false);
    cb.recordResult(true);
    cb.recordResult(true);

    expect(cb.isPaused).toBe(false);
    expect(cb.failureRate).toBe(0.6);
  });

  it("clearPause resets the paused state", () => {
    const cb = new CircuitBreaker(tmpPath("cb-clear.json"), 10, 0.8, 30);

    for (let i = 0; i < 5; i++) cb.recordResult(false);
    expect(cb.isPaused).toBe(true);

    cb.clearPause();
    expect(cb.isPaused).toBe(false);
  });

  it("recordSkip does not affect failure rate", () => {
    const cb = new CircuitBreaker(tmpPath("cb-skip.json"), 10, 0.8, 30);

    cb.recordResult(true);
    cb.recordResult(true);
    cb.recordSkip();
    cb.recordSkip();

    // Only 2 results recorded, both successes
    expect(cb.failureRate).toBe(0.0);
    expect(cb.isPaused).toBe(false);
  });

  it("window size limits tracked results and old results slide out", () => {
    // Window size 5: only the most recent 5 results are kept.
    // With threshold 0.8, we need >= 5 results and >= 80% failures to trip.
    const cb = new CircuitBreaker(tmpPath("cb-window.json"), 5, 0.8, 30);

    // Start clean: 4 successes
    for (let i = 0; i < 4; i++) cb.recordResult(true);
    expect(cb.failureRate).toBe(0.0);
    expect(cb.isPaused).toBe(false);

    // Add 1 failure: [t, t, t, t, f] = 20% failure, no trip
    cb.recordResult(false);
    expect(cb.failureRate).toBe(0.2);
    expect(cb.isPaused).toBe(false);

    // Add successes to push the failure out of the 5-slot window.
    // The failure is at position 5 (newest), so we need 5 successes
    // to fully slide it out: [t,t,t,f,t] -> [t,t,f,t,t] -> [t,f,t,t,t]
    // -> [f,t,t,t,t] -> [t,t,t,t,t]
    // While the failure is in the window, rate stays at 0.2 (1/5).
    for (let i = 0; i < 4; i++) {
      cb.recordResult(true);
      expect(cb.failureRate).toBe(0.2);
    }

    // 5th success pushes the failure out completely
    cb.recordResult(true);
    expect(cb.failureRate).toBe(0.0);
    expect(cb.isPaused).toBe(false);
  });
});

// ===========================================================================
// Bonus: RalphWiggum + Guardrails integration with real ticket data
// ===========================================================================

describe("E2E: RalphWiggum + Guardrails with real ticket scenarios", () => {
  beforeEach(() => {
    tmpDir = freshTmpDir();
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("realistic scenario: mixed ticket severities with thresholds", () => {
    const gate = new RalphWiggumGate(
      govConfig({ maxOpenCritical: 2, maxOpenHigh: 5, enabled: true }),
    );

    const tickets: SWETicket[] = [
      openTicket("critical"),
      openTicket("critical"),
      openTicket("high"),
      openTicket("high"),
      openTicket("high"),
      openTicket("medium"),
      openTicket("medium"),
      openTicket("low"),
    ];

    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
    expect(report.openCritical).toBe(2);
    expect(report.openHigh).toBe(3);
  });

  it("realistic scenario: one more critical pushes over the edge", () => {
    const gate = new RalphWiggumGate(
      govConfig({ maxOpenCritical: 2, maxOpenHigh: 5, enabled: true }),
    );

    const tickets: SWETicket[] = [
      openTicket("critical"),
      openTicket("critical"),
      openTicket("critical"), // one too many
      openTicket("high"),
    ];

    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.openCritical).toBe(3);
  });

  it("resolved tickets do not count toward thresholds", () => {
    const gate = new RalphWiggumGate(
      govConfig({ maxOpenCritical: 0, enabled: true }),
    );

    const tickets: SWETicket[] = [
      openTicket("critical", "resolved"),
      openTicket("critical", "closed"),
      openTicket("critical", "failed"),
    ];

    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
    expect(report.openCritical).toBe(0);
  });

  it("full guardrails integration with RalphWiggum fed live tickets", () => {
    const tickets: SWETicket[] = [
      openTicket("critical"),
      openTicket("critical"),
      openTicket("critical"),
    ];

    const ralphGate = new RalphWiggumGate(
      govConfig({ maxOpenCritical: 2, enabled: true }),
    );

    const coord = new GuardrailsCoordinator();

    const cb = new CircuitBreaker(tmpPath("rw-guard-cb.json"));
    cb.recordResult(true);
    coord.setCircuitBreaker(cb);

    coord.setStabilityGate({
      evaluate: () => {
        const r = ralphGate.evaluate(tickets);
        return {
          verdict: r.verdict === "block" ? "BLOCK" : r.verdict.toUpperCase(),
          details: r.details,
          open_critical: r.openCritical,
          open_high: r.openHigh,
        };
      },
    });

    // Deploy blocked because 3 critical > threshold of 2
    const deployDecision = coord.evaluate("deploy", "HIGH", 0);
    expect(deployDecision.allowed).toBe(false);
    expect(deployDecision.gate).toBe("stability_gate");

    // Resolve one ticket
    tickets[0].status = TicketStatus.RESOLVED;

    // Now only 2 critical open = exactly at threshold -> PASS
    const retryDecision = coord.evaluate("deploy", "HIGH", 0);
    expect(retryDecision.allowed).toBe(true);
  });
});

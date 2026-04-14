/**
 * Tests for safety gate modules: RalphWiggumGate, checkFixComplexity,
 * ThrottlePolicy + adapters, and GuardrailsCoordinator.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { RalphWiggumGate } from "../../src/safety/ralph-wiggum.js";
import { checkFixComplexity } from "../../src/safety/governance.js";
import {
  ThrottlePolicy,
  TimeBasedAdapter,
  CapacityAdapter,
  DemandAdapter,
  type ThrottleContext,
  type ThrottleAdapter,
  type ThrottleResult,
} from "../../src/safety/throttle.js";
import { GuardrailsCoordinator } from "../../src/safety/guardrails.js";
import { CircuitBreaker } from "../../src/safety/circuit-breaker.js";

import {
  type SWETicket,
  TicketSeverity,
  TicketStatus,
  GovernanceVerdict,
  createTicket,
} from "../../src/models/ticket.js";

import type { GovernanceConfig, CycleConfig, ThrottleConfig } from "../../src/config/schemas.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tmpDir: string;

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "safety-test-"));
});

afterEach(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

function tmpPath(filename: string = "state.json"): string {
  return path.join(tmpDir, filename);
}

/** Build a minimal open ticket with a given severity. */
function openTicket(severity: string, status: string = "open"): SWETicket {
  return createTicket("Test ticket", "desc", {
    severity: severity as SWETicket["severity"],
    status: status as SWETicket["status"],
  });
}

/** Default governance config with the gate enabled. */
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

/** Default cycle config for throttle tests. */
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
// RalphWiggumGate
// ===========================================================================

describe("RalphWiggumGate", () => {
  it("PASS when no critical or high tickets", () => {
    const gate = new RalphWiggumGate(govConfig());
    const report = gate.evaluate([]);
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
    expect(report.openCritical).toBe(0);
    expect(report.openHigh).toBe(0);
  });

  it("PASS with only low/medium tickets", () => {
    const gate = new RalphWiggumGate(govConfig());
    const tickets = [
      openTicket("low"),
      openTicket("medium"),
      openTicket("low"),
    ];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
  });

  it("BLOCK when critical tickets exceed max", () => {
    const gate = new RalphWiggumGate(govConfig({ maxOpenCritical: 0 }));
    const tickets = [openTicket("critical")];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.openCritical).toBe(1);
    expect(report.details).toContain("critical");
  });

  it("BLOCK when high tickets exceed max", () => {
    const gate = new RalphWiggumGate(govConfig({ maxOpenHigh: 1 }));
    const tickets = [openTicket("high"), openTicket("high")];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.openHigh).toBe(2);
  });

  it("BLOCK when CI is not green and requireCiGreen=true", () => {
    const gate = new RalphWiggumGate(govConfig({ requireCiGreen: true }));
    const report = gate.evaluate([], { ciGreen: false });
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.details).toContain("CI");
    expect(report.ciStatus).toBe("red");
  });

  it("PASS when CI is not green but requireCiGreen=false", () => {
    const gate = new RalphWiggumGate(govConfig({ requireCiGreen: false }));
    const report = gate.evaluate([], { ciGreen: false });
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
  });

  it("WARN when test failures >= 5% but < 10%", () => {
    const gate = new RalphWiggumGate(govConfig());
    // 7 failing out of 100 = 7%
    const report = gate.evaluate([], { failingTests: 7, totalTests: 100 });
    expect(report.verdict).toBe(GovernanceVerdict.WARN);
    expect(report.details).toContain("WARN");
  });

  it("BLOCK when test failures >= 10%", () => {
    const gate = new RalphWiggumGate(govConfig());
    // 10 failing out of 100 = 10%
    const report = gate.evaluate([], { failingTests: 10, totalTests: 100 });
    expect(report.verdict).toBe(GovernanceVerdict.BLOCK);
    expect(report.details).toContain("failing tests");
  });

  it("PASS when test failures < 5%", () => {
    const gate = new RalphWiggumGate(govConfig());
    // 4 failing out of 100 = 4%
    const report = gate.evaluate([], { failingTests: 4, totalTests: 100 });
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
  });

  it("does not count resolved tickets as open", () => {
    const gate = new RalphWiggumGate(govConfig({ maxOpenCritical: 0 }));
    const tickets = [openTicket("critical", "resolved")];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
    expect(report.openCritical).toBe(0);
  });

  it("PASS when gate is disabled", () => {
    const gate = new RalphWiggumGate(govConfig({ enabled: false }));
    const tickets = [openTicket("critical"), openTicket("critical")];
    const report = gate.evaluate(tickets);
    expect(report.verdict).toBe(GovernanceVerdict.PASS);
    expect(report.details).toContain("disabled");
  });
});

// ===========================================================================
// checkFixComplexity
// ===========================================================================

describe("checkFixComplexity", () => {
  it("valid fix within limits", () => {
    const result = checkFixComplexity(["src/foo/bar.ts"], 50);
    expect(result.valid).toBe(true);
    expect(result.reason).toBe("ok");
  });

  it("rejects when too many files changed", () => {
    const files = Array.from({ length: 8 }, (_, i) => `src/mod/f${i}.ts`);
    const result = checkFixComplexity(files, 50, { maxFiles: 5 });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Too many files");
  });

  it("rejects when too many lines changed", () => {
    const result = checkFixComplexity(["src/foo.ts"], 500, { maxLines: 200 });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Too many lines");
  });

  it("blocks dependency file changes by default", () => {
    const result = checkFixComplexity(["package.json"], 10);
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Dependency changes");
  });

  it("allows dependency changes when explicitly enabled", () => {
    const result = checkFixComplexity(["package.json"], 10, {
      allowDependencyChanges: true,
    });
    expect(result.valid).toBe(true);
  });

  it("detects cross-module changes", () => {
    const result = checkFixComplexity(
      ["src/alpha/a.ts", "src/beta/b.ts"],
      30,
    );
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("Cross-module");
  });

  it("allows cross-module when modules are in allowed set", () => {
    const result = checkFixComplexity(
      ["src/alpha/a.ts", "src/beta/b.ts"],
      30,
      { allowedModules: new Set(["alpha", "beta"]) },
    );
    expect(result.valid).toBe(true);
  });

  it("test files do not count as a separate module", () => {
    const result = checkFixComplexity(
      ["src/foo/bar.ts", "tests/foo.test.ts"],
      50,
    );
    expect(result.valid).toBe(true);
  });

  it("rejects empty file list", () => {
    const result = checkFixComplexity([], 0);
    expect(result.valid).toBe(false);
    expect(result.reason).toContain("No files");
  });
});

// ===========================================================================
// ThrottlePolicy & Adapters
// ===========================================================================

describe("ThrottlePolicy", () => {
  it("default multiplier is 1.0 with no adapters", () => {
    const policy = new ThrottlePolicy(cycleConfig(), []);
    const result = policy.resolve(baseContext());
    expect(result.effectiveMultiplier).toBe(1.0);
    expect(result.maxNewTicketsPerCycle).toBe(20);
  });

  it("all limits floored at 1", () => {
    // Use a multiplier of 0.01 via a custom adapter
    const tiny: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 0.01, severityOverride: null, reason: "tiny" }),
    };
    const policy = new ThrottlePolicy(cycleConfig(), [tiny]);
    const result = policy.resolve(baseContext());
    expect(result.maxNewTicketsPerCycle).toBeGreaterThanOrEqual(1);
    expect(result.maxInvestigationsPerCycle).toBeGreaterThanOrEqual(1);
    expect(result.maxDevelopmentsPerCycle).toBeGreaterThanOrEqual(1);
    expect(result.maxOpenInvestigating).toBeGreaterThanOrEqual(1);
  });
});

describe("TimeBasedAdapter", () => {
  it("applies band multiplier when hour matches", () => {
    const config = throttleConfig({
      timeBands: {
        offPeak: { startHour: 0, endHour: 6, multiplier: 0.5 },
      },
    });
    const adapter = new TimeBasedAdapter(config);
    const ctx = baseContext({ nowUtc: new Date("2026-04-12T03:00:00Z") });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(0.5);
    expect(result.reason).toContain("offPeak");
  });

  it("returns 1.0 when no band matches", () => {
    const config = throttleConfig({
      timeBands: {
        offPeak: { startHour: 0, endHour: 6, multiplier: 0.5 },
      },
    });
    const adapter = new TimeBasedAdapter(config);
    const ctx = baseContext({ nowUtc: new Date("2026-04-12T12:00:00Z") });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(1.0);
  });

  it("returns 1.0 when no time bands configured", () => {
    const config = throttleConfig({ timeBands: {} });
    const adapter = new TimeBasedAdapter(config);
    const result = adapter.evaluate(baseContext(), cycleConfig());
    expect(result.multiplier).toBe(1.0);
    expect(result.reason).toContain("no windows configured");
  });
});

describe("CapacityAdapter", () => {
  it("returns critical multiplier at >= 95% usage", () => {
    const config = throttleConfig();
    const adapter = new CapacityAdapter(config);
    const ctx = baseContext({ apiUsagePct: 0.96 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(0.1);
    expect(result.severityOverride).toBe("critical");
    expect(result.reason).toContain("critical");
  });

  it("returns warning multiplier at >= 80% usage with >= 2 days left", () => {
    const config = throttleConfig();
    const adapter = new CapacityAdapter(config);
    const ctx = baseContext({ apiUsagePct: 0.85, apiDaysToReset: 3 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(0.5);
    expect(result.severityOverride).toBe("critical");
    expect(result.reason).toContain("warning");
  });

  it("returns 1.0 at normal usage", () => {
    const config = throttleConfig();
    const adapter = new CapacityAdapter(config);
    const ctx = baseContext({ apiUsagePct: 0.5, apiDaysToReset: 3 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(1.0);
    expect(result.severityOverride).toBeNull();
  });

  it("does not warn at 80% usage with < 2 days to reset", () => {
    const config = throttleConfig();
    const adapter = new CapacityAdapter(config);
    const ctx = baseContext({ apiUsagePct: 0.85, apiDaysToReset: 1 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(1.0);
  });
});

describe("DemandAdapter", () => {
  it("surge when backlog exceeds threshold", () => {
    const config = throttleConfig({ backlogSurgeThreshold: 100 });
    const adapter = new DemandAdapter(config);
    const ctx = baseContext({ backlogSize: 150, backlogCritical: 0 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(1.5);
    expect(result.reason).toContain("surge");
  });

  it("critical-mass when backlog AND critical both exceed thresholds", () => {
    const config = throttleConfig({
      backlogSurgeThreshold: 100,
      criticalSurgeThreshold: 10,
    });
    const adapter = new DemandAdapter(config);
    const ctx = baseContext({ backlogSize: 200, backlogCritical: 15 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(2.0);
    expect(result.reason).toContain("critical-mass");
  });

  it("returns 1.0 with normal backlog", () => {
    const config = throttleConfig({ backlogSurgeThreshold: 200 });
    const adapter = new DemandAdapter(config);
    const ctx = baseContext({ backlogSize: 10 });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(1.0);
    expect(result.reason).toContain("normal");
  });

  it("surge when pre-release even with small backlog", () => {
    const config = throttleConfig({ backlogSurgeThreshold: 200 });
    const adapter = new DemandAdapter(config);
    const ctx = baseContext({ backlogSize: 5, isPreRelease: true });
    const result = adapter.evaluate(ctx, cycleConfig());
    expect(result.multiplier).toBe(1.5);
    expect(result.reason).toContain("pre-release");
  });
});

describe("ThrottlePolicy combined", () => {
  it("combined multipliers are clamped to max 4.0", () => {
    const bigAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 3.0, severityOverride: null, reason: "big" }),
    };
    const policy = new ThrottlePolicy(cycleConfig(), [bigAdapter, bigAdapter]);
    const result = policy.resolve(baseContext());
    // 3.0 * 3.0 = 9.0, clamped to 4.0
    expect(result.effectiveMultiplier).toBe(4.0);
  });

  it("combined multipliers are clamped to min 0.1", () => {
    const tinyAdapter: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 0.05, severityOverride: null, reason: "tiny" }),
    };
    const policy = new ThrottlePolicy(cycleConfig(), [tinyAdapter, tinyAdapter]);
    const result = policy.resolve(baseContext());
    // 0.05 * 0.05 = 0.0025, clamped to 0.1
    expect(result.effectiveMultiplier).toBe(0.1);
  });

  it("uses the most restrictive severity override", () => {
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

  it("collects reasons from all adapters", () => {
    const a1: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 1.0, severityOverride: null, reason: "reason-a" }),
    };
    const a2: ThrottleAdapter = {
      evaluate: () => ({ multiplier: 1.0, severityOverride: null, reason: "reason-b" }),
    };
    const policy = new ThrottlePolicy(cycleConfig(), [a1, a2]);
    const result = policy.resolve(baseContext());
    expect(result.reasons).toContain("reason-a");
    expect(result.reasons).toContain("reason-b");
  });

  it("handles adapter errors gracefully with 1.0 fallback", () => {
    const badAdapter: ThrottleAdapter = {
      evaluate: () => { throw new Error("boom"); },
    };
    const policy = new ThrottlePolicy(cycleConfig(), [badAdapter]);
    const result = policy.resolve(baseContext());
    expect(result.effectiveMultiplier).toBe(1.0);
  });
});

// ===========================================================================
// GuardrailsCoordinator
// ===========================================================================

describe("GuardrailsCoordinator", () => {
  it("all gates clear -> allowed", () => {
    const coord = new GuardrailsCoordinator();
    const decision = coord.evaluate();
    expect(decision.allowed).toBe(true);
    expect(decision.blocked).toBe(false);
    expect(decision.gate).toBe("all_clear");
  });

  it("circuit breaker paused -> blocked", () => {
    const coord = new GuardrailsCoordinator();
    const cb = new CircuitBreaker(tmpPath("coord-cb.json"), 10, 0.8, 30);
    for (let i = 0; i < 5; i++) {
      cb.recordResult(false);
    }
    coord.setCircuitBreaker(cb);
    const decision = coord.evaluate();
    expect(decision.allowed).toBe(false);
    expect(decision.blocked).toBe(true);
    expect(decision.gate).toBe("circuit_breaker");
    expect(decision.reason).toContain("Circuit breaker");
  });

  it("evaluate returns correct gate name on circuit breaker block", () => {
    const coord = new GuardrailsCoordinator();
    const cb = new CircuitBreaker(tmpPath("gate-cb.json"), 10, 0.8, 30);
    for (let i = 0; i < 5; i++) {
      cb.recordResult(false);
    }
    coord.setCircuitBreaker(cb);
    const decision = coord.evaluate();
    expect(decision.evaluatedGates).toContain("circuit_breaker");
    expect(decision.gate).toBe("circuit_breaker");
  });

  it("stability gate block only applies to deploy/creative tasks", () => {
    const coord = new GuardrailsCoordinator();
    coord.setStabilityGate({
      evaluate: () => ({ verdict: "BLOCK", details: "bugs", open_critical: 5, open_high: 0 }),
    });
    // "investigate" should NOT trigger stability gate
    const investigateDecision = coord.evaluate("investigate");
    expect(investigateDecision.allowed).toBe(true);

    // "deploy" SHOULD trigger stability gate
    const deployDecision = coord.evaluate("deploy");
    expect(deployDecision.allowed).toBe(false);
    expect(deployDecision.gate).toBe("stability_gate");
  });

  it("canProceed is an alias for evaluate", () => {
    const coord = new GuardrailsCoordinator();
    const d1 = coord.evaluate("investigate", "MEDIUM", 0);
    const d2 = coord.canProceed("investigate", "MEDIUM", 0);
    expect(d1.allowed).toBe(d2.allowed);
    expect(d1.gate).toBe(d2.gate);
  });

  it("usage governor blocks when allow_new_work is false", () => {
    const coord = new GuardrailsCoordinator();
    coord.setUsageGovernor({
      get_concurrency_decision: () => ({
        allow_new_work: false,
        priority_floor: 0,
        audit_trail: "rate limited",
      }),
    });
    const decision = coord.evaluate();
    expect(decision.allowed).toBe(false);
    expect(decision.gate).toBe("usage_governor");
  });

  it("budget gate blocks when over budget", () => {
    const coord = new GuardrailsCoordinator();
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
      "team-a",
    );
    const decision = coord.evaluate("investigate", "MEDIUM", 0, "team-a");
    expect(decision.allowed).toBe(false);
    expect(decision.gate).toBe("budget_gate");
  });

  it("team-scoped circuit breakers work independently", () => {
    const coord = new GuardrailsCoordinator();
    const cbA = new CircuitBreaker(tmpPath("team-a.json"), 10, 0.8, 30);
    const cbB = new CircuitBreaker(tmpPath("team-b.json"), 10, 0.8, 30);

    // Trip team A's breaker
    for (let i = 0; i < 5; i++) cbA.recordResult(false);

    coord.setCircuitBreaker(cbA, "team-a");
    coord.setCircuitBreaker(cbB, "team-b");

    const decisionA = coord.evaluate("investigate", "MEDIUM", 0, "team-a");
    expect(decisionA.allowed).toBe(false);

    const decisionB = coord.evaluate("investigate", "MEDIUM", 0, "team-b");
    expect(decisionB.allowed).toBe(true);
  });

  it("evaluatedGates tracks all gates that were checked", () => {
    const coord = new GuardrailsCoordinator();
    const cb = new CircuitBreaker(tmpPath("eval-gates.json"));
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
    const decision = coord.evaluate("investigate", "MEDIUM", 0);
    expect(decision.evaluatedGates).toContain("circuit_breaker");
    expect(decision.evaluatedGates).toContain("usage_governor");
    expect(decision.evaluatedGates).toContain("all_clear");
  });
});

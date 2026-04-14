/**
 * Unified Guardrails Coordinator.
 *
 * Single entry point for all safety gates: circuit breaker, stability gate,
 * usage governor, throttle, and budget. Eliminates fragmented gate evaluation
 * by centralizing the decision into one call.
 *
 * Ported from: src/swe_team/guardrails.py
 */

import type { CircuitBreaker } from "./circuit-breaker.js";
import type { ThrottlePolicy } from "./throttle.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Result of a unified guardrail evaluation. */
export interface GuardrailDecision {
  allowed: boolean;
  reason: string;
  /** Which gate blocked (or "all_clear"). */
  gate: string;
  details: Record<string, unknown>;
  evaluatedGates: string[];
  timestamp: number;
  /** Convenience property: true when not allowed. */
  readonly blocked: boolean;
}

/** Create a GuardrailDecision with the `blocked` getter. */
function makeDecision(
  fields: Omit<GuardrailDecision, "blocked">,
): GuardrailDecision {
  return Object.defineProperty({ ...fields }, "blocked", {
    get(this: GuardrailDecision) {
      return !this.allowed;
    },
    enumerable: true,
    configurable: false,
  }) as GuardrailDecision;
}

/** Health snapshot of all guardrail components. */
export interface GuardrailHealth {
  circuitBreakerPaused: boolean;
  circuitBreakerFailureRate: number;
  stabilityVerdict: string;
  budgetStatus: string;
  budgetPercentUsed: number;
  throttleMultiplier: number;
  queueDepth: number;
}

// ---------------------------------------------------------------------------
// Minimal interfaces for external gates (avoids hard coupling)
// ---------------------------------------------------------------------------

/** Minimal interface for a stability gate usable by the coordinator. */
interface StabilityGate {
  evaluate(...args: unknown[]): { verdict: string; reason?: string; details?: string; open_critical?: number; open_high?: number };
}

/** Minimal interface for a usage governor usable by the coordinator. */
interface UsageGovernor {
  get_concurrency_decision(): {
    allow_new_work: boolean;
    max_agents?: number;
    max_parallel_agents?: number;
    priority_floor: number;
    audit_trail: string;
  };
}

/** Minimal interface for a cost tracker usable by the coordinator. */
interface CostTracker {
  check_budget(teamId: string): {
    is_over_budget: boolean;
    is_warning: boolean;
    status: string;
    percent_used: number;
    daily_spent: number;
    daily_limit: number;
    monthly_spent: number;
    monthly_limit: number;
  };
}

/** Minimal interface for a queued dispatcher for health reporting. */
interface QueuedDispatcher {
  health(): Record<string, number>;
}

// ---------------------------------------------------------------------------
// Severity priority map
// ---------------------------------------------------------------------------

const SEVERITY_PRIORITY: Record<string, number> = {
  CRITICAL: 0,
  HIGH: 1,
  MEDIUM: 2,
  LOW: 3,
  INFO: 4,
};

// ---------------------------------------------------------------------------
// GuardrailsCoordinator
// ---------------------------------------------------------------------------

/**
 * Unified coordinator for all safety gates.
 *
 * Evaluates gates in strict priority order:
 * 1. Circuit breaker (hard block if paused -- system is unhealthy)
 * 2. Budget gate (hard-stop if dollar budget exceeded)
 * 3. Usage governor (quota/concurrency limits)
 * 4. Stability gate (bug count thresholds)
 * 5. Throttle (time/capacity/demand adjustments -- informational)
 *
 * Each gate is optional -- if not set, it's skipped. This allows
 * incremental adoption.
 */
export class GuardrailsCoordinator {
  private _circuitBreaker: CircuitBreaker | null = null;
  private _teamCircuitBreakers: Map<string, CircuitBreaker> = new Map();
  private _stabilityGate: StabilityGate | null = null;
  private _usageGovernor: UsageGovernor | null = null;
  private _throttlePolicy: ThrottlePolicy | null = null;
  private _queuedDispatcher: QueuedDispatcher | null = null;
  private _costTracker: CostTracker | null = null;
  private _teamId: string = "";

  // -----------------------------------------------------------------------
  // Setters
  // -----------------------------------------------------------------------

  setCircuitBreaker(cb: CircuitBreaker, teamId?: string): void {
    if (teamId) {
      this._teamCircuitBreakers.set(teamId, cb);
      return;
    }
    this._circuitBreaker = cb;
  }

  setStabilityGate(gate: StabilityGate): void {
    this._stabilityGate = gate;
  }

  setUsageGovernor(gov: UsageGovernor): void {
    this._usageGovernor = gov;
  }

  setThrottle(policy: ThrottlePolicy): void {
    this._throttlePolicy = policy;
  }

  setQueuedDispatcher(dispatcher: QueuedDispatcher): void {
    this._queuedDispatcher = dispatcher;
  }

  setCostTracker(tracker: CostTracker, teamId: string = ""): void {
    this._costTracker = tracker;
    this._teamId = teamId;
  }

  // -----------------------------------------------------------------------
  // evaluate
  // -----------------------------------------------------------------------

  /**
   * Run all gates in priority order and return a unified decision.
   *
   * @param taskType        - "investigate", "develop", "deploy", "creative"
   * @param ticketSeverity  - "CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"
   * @param currentAgents   - Number of agents currently running
   * @param teamId          - Optional team scope
   */
  evaluate(
    taskType: string = "investigate",
    ticketSeverity: string = "MEDIUM",
    currentAgents: number = 0,
    teamId?: string,
  ): GuardrailDecision {
    const activeTeamId = teamId ?? this._teamId;
    const circuitBreaker =
      (activeTeamId
        ? this._teamCircuitBreakers.get(activeTeamId)
        : undefined) ?? this._circuitBreaker;

    const evaluated: string[] = [];

    // -- Gate 1: Circuit Breaker ------------------------------------------
    if (circuitBreaker != null) {
      evaluated.push("circuit_breaker");
      if (circuitBreaker.isPaused) {
        return makeDecision({
          allowed: false,
          reason: `Circuit breaker paused (failure rate ${(circuitBreaker.failureRate * 100).toFixed(0)}%)`,
          gate: "circuit_breaker",
          details: {
            failure_rate: circuitBreaker.failureRate,
          },
          evaluatedGates: [...evaluated],
          timestamp: Date.now(),
        });
      }
    }

    // -- Gate 2: Budget Gate ----------------------------------------------
    if (this._costTracker != null && activeTeamId) {
      evaluated.push("budget_gate");
      try {
        const budgetStatus = this._costTracker.check_budget(activeTeamId);
        if (budgetStatus.is_over_budget) {
          return makeDecision({
            allowed: false,
            reason:
              `Budget hard-stop: ${budgetStatus.percent_used.toFixed(1)}% of budget used ` +
              `(daily $${(budgetStatus.daily_spent / 100).toFixed(2)}/` +
              `$${(budgetStatus.daily_limit / 100).toFixed(2)}, ` +
              `monthly $${(budgetStatus.monthly_spent / 100).toFixed(2)}/` +
              `$${(budgetStatus.monthly_limit / 100).toFixed(2)})`,
            gate: "budget_gate",
            details: {
              status: budgetStatus.status,
              percent_used: budgetStatus.percent_used,
              daily_spent_cents: budgetStatus.daily_spent,
              daily_limit_cents: budgetStatus.daily_limit,
              monthly_spent_cents: budgetStatus.monthly_spent,
              monthly_limit_cents: budgetStatus.monthly_limit,
            },
            evaluatedGates: [...evaluated],
            timestamp: Date.now(),
          });
        }
      } catch (err) {
        console.warn("Budget gate check failed -- failing open:", err);
      }
    }

    // -- Gate 3: Usage Governor -------------------------------------------
    if (this._usageGovernor != null) {
      evaluated.push("usage_governor");
      try {
        const decision = this._usageGovernor.get_concurrency_decision();
        if (!decision.allow_new_work) {
          return makeDecision({
            allowed: false,
            reason: `Usage governor: new work blocked (${decision.audit_trail})`,
            gate: "usage_governor",
            details: {
              max_agents: decision.max_agents ?? decision.max_parallel_agents ?? 0,
              priority_floor: decision.priority_floor,
              audit_trail: decision.audit_trail,
            },
            evaluatedGates: [...evaluated],
            timestamp: Date.now(),
          });
        }

        // Check if severity meets priority floor
        const sevNum = SEVERITY_PRIORITY[ticketSeverity] ?? 2;
        if (sevNum > decision.priority_floor) {
          return makeDecision({
            allowed: false,
            reason: `Usage governor: ticket severity ${ticketSeverity} below priority floor ${decision.priority_floor}`,
            gate: "usage_governor",
            details: { priority_floor: decision.priority_floor },
            evaluatedGates: [...evaluated],
            timestamp: Date.now(),
          });
        }

        // Check agent count
        const maxAgents = decision.max_agents ?? decision.max_parallel_agents ?? 5;
        if (currentAgents >= maxAgents) {
          return makeDecision({
            allowed: false,
            reason: `Usage governor: ${currentAgents} agents running (max ${maxAgents})`,
            gate: "usage_governor",
            details: { current: currentAgents, max: maxAgents },
            evaluatedGates: [...evaluated],
            timestamp: Date.now(),
          });
        }
      } catch (err) {
        console.warn("Usage governor check failed -- failing open:", err);
      }
    }

    // -- Gate 4: Stability Gate -------------------------------------------
    if (
      this._stabilityGate != null &&
      (taskType === "deploy" || taskType === "creative")
    ) {
      evaluated.push("stability_gate");
      try {
        const report = this._stabilityGate.evaluate();
        if (report.verdict === "BLOCK" || report.verdict === "block") {
          return makeDecision({
            allowed: false,
            reason: `Stability gate BLOCK: ${report.reason ?? report.details ?? ""}`,
            gate: "stability_gate",
            details: {
              verdict: report.verdict,
              reason: report.reason ?? report.details ?? "",
              open_critical: report.open_critical ?? 0,
              open_high: report.open_high ?? 0,
            },
            evaluatedGates: [...evaluated],
            timestamp: Date.now(),
          });
        }
      } catch (err) {
        console.warn("Stability gate check failed:", err);
      }
    }

    // -- Gate 5: Throttle -------------------------------------------------
    if (this._throttlePolicy != null) {
      evaluated.push("throttle");
      // Throttle adjusts limits but doesn't hard-block; it's informational.
      // The actual enforcement happens via adjusted cycle config.
    }

    evaluated.push("all_clear");
    return makeDecision({
      allowed: true,
      reason: "All guardrails passed",
      gate: "all_clear",
      details: {},
      evaluatedGates: [...evaluated],
      timestamp: Date.now(),
    });
  }

  /**
   * Backward-compatible wrapper around evaluate().
   */
  canProceed(
    taskType: string = "investigate",
    ticketSeverity: string = "MEDIUM",
    currentAgents: number = 0,
    teamId?: string,
  ): GuardrailDecision {
    return this.evaluate(taskType, ticketSeverity, currentAgents, teamId);
  }

  // -----------------------------------------------------------------------
  // Health
  // -----------------------------------------------------------------------

  /** Return a health snapshot of all guardrail components. */
  health(teamId?: string): GuardrailHealth {
    const activeTeamId = teamId ?? this._teamId;
    const circuitBreaker =
      (activeTeamId
        ? this._teamCircuitBreakers.get(activeTeamId)
        : undefined) ?? this._circuitBreaker;

    const h: GuardrailHealth = {
      circuitBreakerPaused: false,
      circuitBreakerFailureRate: 0.0,
      stabilityVerdict: "unknown",
      budgetStatus: "unconfigured",
      budgetPercentUsed: 0.0,
      throttleMultiplier: 1.0,
      queueDepth: 0,
    };

    if (circuitBreaker != null) {
      h.circuitBreakerPaused = circuitBreaker.isPaused;
      h.circuitBreakerFailureRate = circuitBreaker.failureRate;
    }

    if (this._stabilityGate != null) {
      try {
        const report = this._stabilityGate.evaluate();
        h.stabilityVerdict = report.verdict;
      } catch {
        // Keep "unknown"
      }
    }

    if (this._queuedDispatcher != null) {
      try {
        const qh = this._queuedDispatcher.health();
        h.queueDepth =
          (qh.investigate_depth ?? 0) + (qh.develop_depth ?? 0);
      } catch {
        // Keep 0
      }
    }

    if (this._costTracker != null && activeTeamId) {
      try {
        const budget = this._costTracker.check_budget(activeTeamId);
        h.budgetStatus = budget.status;
        h.budgetPercentUsed = budget.percent_used;
      } catch {
        h.budgetStatus = "error";
      }
    }

    return h;
  }
}

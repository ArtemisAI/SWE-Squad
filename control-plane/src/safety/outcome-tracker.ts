/**
 * Outcome Tracker — monitors tool execution results for anomalies.
 *
 * Tracks success/failure rates per tool, per engine, per ticket.
 * Detects patterns: repeated failures, degraded engines, stalled tickets.
 * Emits structured metrics for dashboards and alerting.
 *
 * This is CODE-LEVEL monitoring — not a prompt suggestion. It runs
 * independently of LLM decisions and provides hard data for enforcement.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ToolOutcome {
  tool: string;
  ticketId?: string;
  engine?: string;
  success: boolean;
  error?: string;
  durationMs: number;
  costUsd?: number;
  timestamp: Date;
}

export interface OutcomeStats {
  totalCalls: number;
  successes: number;
  failures: number;
  successRate: number;
  avgDurationMs: number;
  totalCostUsd: number;
  lastFailure?: { error: string; timestamp: Date };
}

export interface EngineMetrics {
  engine: string;
  calls: number;
  successes: number;
  failures: number;
  successRate: number;
  avgDurationMs: number;
  totalCostUsd: number;
  consecutiveFailures: number;
  isHealthy: boolean;
}

export interface TicketMetrics {
  ticketId: string;
  investigationAttempts: number;
  developmentAttempts: number;
  reviewAttempts: number;
  testAttempts: number;
  totalFailures: number;
  isStalled: boolean;
  stalledSinceMs?: number;
}

export interface SystemHealth {
  timestamp: string;
  windowMinutes: number;
  overall: OutcomeStats;
  byTool: Record<string, OutcomeStats>;
  byEngine: Record<string, EngineMetrics>;
  stalledTickets: string[];
  alerts: Alert[];
}

export interface Alert {
  type: "engine_degraded" | "ticket_stalled" | "high_failure_rate" | "budget_warning";
  severity: "info" | "warn" | "critical";
  message: string;
  timestamp: Date;
}

export interface OutcomeTrackerOptions {
  /** Rolling window size in minutes. Default: 60 */
  windowMinutes?: number;
  /** Max consecutive engine failures before marking unhealthy. Default: 3 */
  maxConsecutiveFailures?: number;
  /** Max ticket attempts before marking stalled. Default: 3 */
  maxTicketAttempts?: number;
  /** Overall failure rate threshold for alert. Default: 0.5 */
  failureRateThreshold?: number;
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

export class OutcomeTracker {
  private outcomes: ToolOutcome[] = [];
  private engineConsecutiveFailures: Map<string, number> = new Map();
  private ticketAttempts: Map<string, TicketMetrics> = new Map();
  private alerts: Alert[] = [];
  private readonly windowMs: number;
  private readonly maxConsecutiveFailures: number;
  private readonly maxTicketAttempts: number;
  private readonly failureRateThreshold: number;

  constructor(options?: OutcomeTrackerOptions) {
    this.windowMs = (options?.windowMinutes ?? 60) * 60 * 1000;
    this.maxConsecutiveFailures = options?.maxConsecutiveFailures ?? 3;
    this.maxTicketAttempts = options?.maxTicketAttempts ?? 3;
    this.failureRateThreshold = options?.failureRateThreshold ?? 0.5;
  }

  /**
   * Record a tool execution outcome.
   */
  record(outcome: ToolOutcome): void {
    this.outcomes.push(outcome);
    this.pruneOldOutcomes();

    // Track engine consecutive failures
    if (outcome.engine) {
      if (outcome.success) {
        this.engineConsecutiveFailures.set(outcome.engine, 0);
      } else {
        const current = this.engineConsecutiveFailures.get(outcome.engine) ?? 0;
        this.engineConsecutiveFailures.set(outcome.engine, current + 1);

        if (current + 1 >= this.maxConsecutiveFailures) {
          this.addAlert({
            type: "engine_degraded",
            severity: "critical",
            message: `Engine "${outcome.engine}" has ${current + 1} consecutive failures`,
            timestamp: new Date(),
          });
        }
      }
    }

    // Track ticket attempts
    if (outcome.ticketId) {
      this.trackTicketAttempt(outcome);
    }

    // Check overall failure rate
    this.checkOverallHealth();
  }

  /**
   * Get comprehensive system health metrics.
   */
  health(): SystemHealth {
    this.pruneOldOutcomes();
    const windowMinutes = this.windowMs / 60_000;

    return {
      timestamp: new Date().toISOString(),
      windowMinutes,
      overall: this.computeStats(this.outcomes),
      byTool: this.computeStatsByTool(),
      byEngine: this.computeEngineMetrics(),
      stalledTickets: this.getStalledTickets(),
      alerts: [...this.alerts].slice(-20), // Last 20 alerts
    };
  }

  /**
   * Check if a specific engine is healthy.
   */
  isEngineHealthy(engine: string): boolean {
    const failures = this.engineConsecutiveFailures.get(engine) ?? 0;
    return failures < this.maxConsecutiveFailures;
  }

  /**
   * Check if a ticket has exceeded retry limits.
   */
  isTicketExhausted(ticketId: string): boolean {
    const metrics = this.ticketAttempts.get(ticketId);
    if (!metrics) return false;
    return metrics.totalFailures >= this.maxTicketAttempts;
  }

  /**
   * Get recent alerts (optionally filtered by severity).
   */
  getAlerts(severity?: Alert["severity"]): Alert[] {
    if (severity) {
      return this.alerts.filter((a) => a.severity === severity);
    }
    return [...this.alerts];
  }

  /**
   * Clear alerts (after they've been acknowledged/notified).
   */
  clearAlerts(): void {
    this.alerts = [];
  }

  // -----------------------------------------------------------------------
  // Internals
  // -----------------------------------------------------------------------

  private pruneOldOutcomes(): void {
    const cutoff = Date.now() - this.windowMs;
    this.outcomes = this.outcomes.filter((o) => o.timestamp.getTime() > cutoff);
  }

  private computeStats(outcomes: ToolOutcome[]): OutcomeStats {
    if (outcomes.length === 0) {
      return {
        totalCalls: 0,
        successes: 0,
        failures: 0,
        successRate: 1,
        avgDurationMs: 0,
        totalCostUsd: 0,
      };
    }

    const successes = outcomes.filter((o) => o.success).length;
    const failures = outcomes.length - successes;
    const totalDuration = outcomes.reduce((sum, o) => sum + o.durationMs, 0);
    const totalCost = outcomes.reduce((sum, o) => sum + (o.costUsd ?? 0), 0);

    const lastFail = outcomes
      .filter((o) => !o.success)
      .sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime())[0];

    return {
      totalCalls: outcomes.length,
      successes,
      failures,
      successRate: successes / outcomes.length,
      avgDurationMs: totalDuration / outcomes.length,
      totalCostUsd: totalCost,
      lastFailure: lastFail
        ? { error: lastFail.error ?? "unknown", timestamp: lastFail.timestamp }
        : undefined,
    };
  }

  private computeStatsByTool(): Record<string, OutcomeStats> {
    const byTool: Record<string, ToolOutcome[]> = {};
    for (const o of this.outcomes) {
      (byTool[o.tool] ??= []).push(o);
    }
    const result: Record<string, OutcomeStats> = {};
    for (const [tool, outcomes] of Object.entries(byTool)) {
      result[tool] = this.computeStats(outcomes);
    }
    return result;
  }

  private computeEngineMetrics(): Record<string, EngineMetrics> {
    const byEngine: Record<string, ToolOutcome[]> = {};
    for (const o of this.outcomes) {
      if (o.engine) {
        (byEngine[o.engine] ??= []).push(o);
      }
    }

    const result: Record<string, EngineMetrics> = {};
    for (const [engine, outcomes] of Object.entries(byEngine)) {
      const stats = this.computeStats(outcomes);
      const consecutiveFailures =
        this.engineConsecutiveFailures.get(engine) ?? 0;
      result[engine] = {
        engine,
        calls: stats.totalCalls,
        successes: stats.successes,
        failures: stats.failures,
        successRate: stats.successRate,
        avgDurationMs: stats.avgDurationMs,
        totalCostUsd: stats.totalCostUsd,
        consecutiveFailures,
        isHealthy: consecutiveFailures < this.maxConsecutiveFailures,
      };
    }
    return result;
  }

  private trackTicketAttempt(outcome: ToolOutcome): void {
    const ticketId = outcome.ticketId!;
    let metrics = this.ticketAttempts.get(ticketId);
    if (!metrics) {
      metrics = {
        ticketId,
        investigationAttempts: 0,
        developmentAttempts: 0,
        reviewAttempts: 0,
        testAttempts: 0,
        totalFailures: 0,
        isStalled: false,
      };
      this.ticketAttempts.set(ticketId, metrics);
    }

    // Count by tool type
    if (outcome.tool === "delegate_investigation") {
      metrics.investigationAttempts++;
    } else if (outcome.tool === "delegate_development") {
      metrics.developmentAttempts++;
    } else if (outcome.tool === "delegate_review") {
      metrics.reviewAttempts++;
    } else if (outcome.tool === "run_tests") {
      metrics.testAttempts++;
    }

    if (!outcome.success) {
      metrics.totalFailures++;
    }

    // Check if stalled
    if (metrics.totalFailures >= this.maxTicketAttempts) {
      metrics.isStalled = true;
      metrics.stalledSinceMs = Date.now();
      this.addAlert({
        type: "ticket_stalled",
        severity: "warn",
        message: `Ticket ${ticketId} stalled after ${metrics.totalFailures} failures`,
        timestamp: new Date(),
      });
    }
  }

  private getStalledTickets(): string[] {
    return Array.from(this.ticketAttempts.entries())
      .filter(([, m]) => m.isStalled)
      .map(([id]) => id);
  }

  private checkOverallHealth(): void {
    if (this.outcomes.length < 5) return; // Need minimum sample
    const stats = this.computeStats(this.outcomes);
    if (stats.successRate < this.failureRateThreshold) {
      this.addAlert({
        type: "high_failure_rate",
        severity: "critical",
        message: `Overall success rate ${(stats.successRate * 100).toFixed(0)}% below threshold ${(this.failureRateThreshold * 100).toFixed(0)}%`,
        timestamp: new Date(),
      });
    }
  }

  private addAlert(alert: Alert): void {
    // Dedup: don't add if same type+message within last 5 minutes
    const fiveMinAgo = Date.now() - 5 * 60_000;
    const isDuplicate = this.alerts.some(
      (a) =>
        a.type === alert.type &&
        a.message === alert.message &&
        a.timestamp.getTime() > fiveMinAgo,
    );
    if (!isDuplicate) {
      this.alerts.push(alert);
    }
  }
}

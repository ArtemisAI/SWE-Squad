/**
 * Ralph-Wiggum Stability Loop.
 *
 * Implements the stability-first governance pattern: fix bugs before
 * building new features. Before any new feature work is allowed, this
 * gate checks:
 *
 * 1. Open critical / high bug count against thresholds
 * 2. CI / test-suite status
 * 3. Test failure severity tiers
 *
 * The gate produces a StabilityReport with verdict: pass | block | warn.
 *
 * Ported from: src/swe_team/ralph_wiggum.py
 */

import type { GovernanceConfig } from "../config/schemas.js";
import {
  type SWETicket,
  type StabilityReport,
  GovernanceVerdict,
  TicketSeverity,
  TicketStatus,
  type TicketSeverity as TicketSeverityType,
} from "../models/ticket.js";

// ---------------------------------------------------------------------------
// Open statuses -- tickets counted by the stability gate
// ---------------------------------------------------------------------------

/** Statuses that count as "open" for the stability gate. */
const OPEN_STATUSES: ReadonlySet<string> = new Set([
  TicketStatus.OPEN,
  TicketStatus.TRIAGED,
  TicketStatus.INVESTIGATING,
  TicketStatus.INVESTIGATION_COMPLETE,
  TicketStatus.IN_DEVELOPMENT,
  TicketStatus.IN_REVIEW,
  TicketStatus.TESTING,
  TicketStatus.DEPLOYING,
  TicketStatus.MONITORING,
]);

// ---------------------------------------------------------------------------
// Evaluate options
// ---------------------------------------------------------------------------

export interface EvaluateOptions {
  /** Whether the most recent CI run passed. */
  ciGreen?: boolean;
  /** Number of failing tests in the latest suite run. */
  failingTests?: number;
  /** Total number of tests in the suite. */
  totalTests?: number;
}

// ---------------------------------------------------------------------------
// RalphWiggumGate
// ---------------------------------------------------------------------------

/**
 * Stability gate that blocks new work when the codebase is unhealthy.
 *
 * Named after the "Ralph Wiggum Loop" concept:
 * Stop building on top of failing, unstable, or insecure code.
 */
export class RalphWiggumGate {
  private readonly _config: GovernanceConfig;

  constructor(config: GovernanceConfig) {
    this._config = config;
  }

  /**
   * Run the stability check and return a verdict.
   *
   * @param tickets - All currently tracked SWETicket objects.
   * @param options - Optional CI/test status signals.
   */
  evaluate(tickets: SWETicket[], options?: EvaluateOptions): StabilityReport {
    const ciGreen = options?.ciGreen ?? true;
    const failingTests = options?.failingTests ?? 0;
    const totalTests = options?.totalTests ?? 0;

    if (!this._config.enabled) {
      return {
        verdict: GovernanceVerdict.PASS,
        openCritical: 0,
        openHigh: 0,
        failingTests: 0,
        ciStatus: "unknown",
        details: "Gate disabled",
        checkedAt: new Date().toISOString(),
      };
    }

    const openCritical = RalphWiggumGate._countOpen(
      tickets,
      TicketSeverity.CRITICAL,
    );
    const openHigh = RalphWiggumGate._countOpen(
      tickets,
      TicketSeverity.HIGH,
    );

    const reasons: string[] = [];
    const warnReasons: string[] = [];

    // Rule 1: No critical bugs allowed beyond threshold
    if (openCritical > this._config.maxOpenCritical) {
      reasons.push(
        `${openCritical} open critical ticket(s) ` +
          `(max ${this._config.maxOpenCritical})`,
      );
    }

    // Rule 2: High bug ceiling
    if (openHigh > this._config.maxOpenHigh) {
      reasons.push(
        `${openHigh} open high ticket(s) ` +
          `(max ${this._config.maxOpenHigh})`,
      );
    }

    // Rule 3: CI must be green
    if (this._config.requireCiGreen && !ciGreen) {
      reasons.push("CI is not green");
    }

    // Rule 4: Flexible test failure gate
    // Thresholds: hardBlockPct (default 10%), warnPct (default 5%)
    const hardBlockPct = 10;
    const warnPct = 5;

    if (failingTests > 0) {
      const failPct =
        totalTests > 0 ? (failingTests / totalTests) * 100 : 100;

      if (failPct >= hardBlockPct) {
        reasons.push(
          `${failingTests}/${totalTests || "?"} failing tests ` +
            `(${failPct.toFixed(1)}% >= hard block threshold ${hardBlockPct}%)`,
        );
      } else if (failPct >= warnPct) {
        warnReasons.push(
          `${failingTests} failing test(s) ` +
            `(${failPct.toFixed(1)}% -- warn threshold ${warnPct}%)`,
        );
      }
      // Below warn threshold: isolated failure, not blocking
    }

    let verdict: typeof GovernanceVerdict.PASS | typeof GovernanceVerdict.BLOCK | typeof GovernanceVerdict.WARN;
    let details: string;

    if (reasons.length > 0) {
      verdict = GovernanceVerdict.BLOCK;
      details = "BLOCKED: " + reasons.join("; ");
    } else if (warnReasons.length > 0) {
      verdict = GovernanceVerdict.WARN;
      details = "WARN (not blocking): " + warnReasons.join("; ");
    } else {
      verdict = GovernanceVerdict.PASS;
      details = "All stability checks passed";
    }

    return {
      verdict,
      openCritical,
      openHigh,
      failingTests,
      ciStatus: ciGreen ? "green" : "red",
      details,
      checkedAt: new Date().toISOString(),
    };
  }

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------

  /** Count tickets with the given severity that are still open. */
  private static _countOpen(
    tickets: SWETicket[],
    severity: TicketSeverityType,
  ): number {
    return tickets.filter(
      (t) => t.severity === severity && OPEN_STATUSES.has(t.status),
    ).length;
  }
}

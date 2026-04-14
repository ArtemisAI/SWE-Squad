/**
 * Dynamic throttle system for SWE-Squad cycle limits.
 *
 * Replaces hardcoded cycle config values with dynamically computed limits
 * based on time-of-day, API capacity, and backlog demand signals.
 *
 * Ported from: src/swe_team/throttle.py
 */

import type { CycleConfig, ThrottleConfig, TimeBand } from "../config/schemas.js";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Severity ranking used for override comparison. */
const SEV_RANK: Record<string, number> = {
  low: 0,
  medium: 1,
  high: 2,
  critical: 3,
};

/** Multiplier bounds -- prevent runaway scaling in either direction. */
const MIN_MULTIPLIER = 0.1;
const MAX_MULTIPLIER = 4.0;

// ---------------------------------------------------------------------------
// Context and result types
// ---------------------------------------------------------------------------

/** Input signals gathered at cycle start for throttle evaluation. */
export interface ThrottleContext {
  /** Current UTC timestamp. */
  nowUtc: Date;
  /** 0.0-1.0, weekly API usage fraction. */
  apiUsagePct: number;
  /** Days until weekly usage resets. */
  apiDaysToReset: number;
  /** Count of OPEN+TRIAGED tickets. */
  backlogSize: number;
  /** Critical tickets in backlog. */
  backlogCritical: number;
  /** Manual flag for release pressure. */
  isPreRelease: boolean;
  /** From RateLimitTracker.isCoolingDown(). */
  rateLimitCooling: boolean;
}

/** Output from a single throttle adapter. */
export interface ThrottleResult {
  multiplier: number;
  severityOverride: string | null;
  reason: string;
}

/** Dynamically computed cycle limits -- duck-type compatible with CycleConfig. */
export interface ResolvedCycleConfig {
  maxNewTicketsPerCycle: number;
  maxInvestigationsPerCycle: number;
  maxDevelopmentsPerCycle: number;
  maxOpenInvestigating: number;
  severityFilter: string;
  effectiveMultiplier: number;
  reasons: string[];
}

// ---------------------------------------------------------------------------
// Adapter interface
// ---------------------------------------------------------------------------

/** Base interface for throttle strategy adapters. */
export interface ThrottleAdapter {
  /**
   * Evaluate this adapter's throttle signal.
   *
   * @param context - Signals gathered at cycle start.
   * @param base    - The static CycleConfig from YAML (for reference values).
   * @returns ThrottleResult with multiplier and optional severity override.
   */
  evaluate(context: ThrottleContext, base: CycleConfig): ThrottleResult;
}

// ---------------------------------------------------------------------------
// Time-based adapter
// ---------------------------------------------------------------------------

/**
 * Return true when `hour` falls in [start, end), supporting overnight windows.
 */
function hourInWindow(hour: number, start: number, end: number): boolean {
  if (start === 0 && end === 24) {
    return true;
  }
  if (start <= end) {
    return hour >= start && hour < end;
  }
  // Overnight window (e.g. 22 -> 6)
  return hour >= start || hour < end;
}

/**
 * Get the hour in a given IANA timezone.
 *
 * Uses Intl.DateTimeFormat to resolve timezone offsets portably
 * (no dependency on a ZoneInfo library).
 */
function getHourInTimezone(date: Date, timezone: string): number {
  const formatter = new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    hour12: false,
    timeZone: timezone,
  });
  return parseInt(formatter.format(date), 10);
}

/**
 * Adjust capacity using configured time windows.
 *
 * Rules are loaded from `throttle.timeBands` and evaluated in config order.
 * Each window supports `startHour`, `endHour`, `multiplier`, and optional
 * `timezone` (default UTC). No band names are hardcoded.
 */
export class TimeBasedAdapter implements ThrottleAdapter {
  private readonly _timeBands: Record<string, TimeBand>;

  constructor(config: ThrottleConfig) {
    this._timeBands = config.timeBands;
  }

  evaluate(context: ThrottleContext): ThrottleResult {
    if (
      !this._timeBands ||
      Object.keys(this._timeBands).length === 0
    ) {
      return {
        multiplier: 1.0,
        severityOverride: null,
        reason: "time=no windows configured -> 1.0x",
      };
    }

    const nowUtc = context.nowUtc;

    for (const [bandName, band] of Object.entries(this._timeBands)) {
      const tz = band.timezone ?? "UTC";
      const start = band.startHour;
      const end = band.endHour;
      const multiplier = band.multiplier;

      const localHour = getHourInTimezone(nowUtc, tz);
      if (hourInWindow(localHour, start, end)) {
        return {
          multiplier,
          severityOverride: null,
          reason:
            `time=${bandName} (${localHour}:00 ${tz}, ` +
            `window=${start}-${end}) -> ${multiplier}x`,
        };
      }
    }

    return {
      multiplier: 1.0,
      severityOverride: null,
      reason: "time=no active window -> 1.0x",
    };
  }
}

// ---------------------------------------------------------------------------
// Capacity-based adapter
// ---------------------------------------------------------------------------

/**
 * Adjusts capacity based on API budget consumption.
 *
 * When weekly API usage is high and days-to-reset are far away,
 * throttles down to preserve budget for critical work only.
 */
export class CapacityAdapter implements ThrottleAdapter {
  private readonly _config: ThrottleConfig;

  constructor(config: ThrottleConfig) {
    this._config = config;
  }

  evaluate(context: ThrottleContext): ThrottleResult {
    const pct = context.apiUsagePct;
    const days = context.apiDaysToReset;

    // Emergency: >= 95% used regardless of days to reset
    if (pct >= this._config.capacityCriticalPct) {
      return {
        multiplier: this._config.capacityCriticalMultiplier,
        severityOverride: "critical",
        reason:
          `capacity=critical (${(pct * 100).toFixed(0)}% used) -> ` +
          `${this._config.capacityCriticalMultiplier}x, critical-only`,
      };
    }

    // Warning: >= 80% used with >= 2 days until reset
    if (
      pct >= this._config.capacityWarningPct &&
      days >= this._config.capacityWarningDaysRemaining
    ) {
      return {
        multiplier: this._config.capacityWarningMultiplier,
        severityOverride: "critical",
        reason:
          `capacity=warning (${(pct * 100).toFixed(0)}% used, ${days.toFixed(1)}d to reset) -> ` +
          `${this._config.capacityWarningMultiplier}x, critical-only`,
      };
    }

    return {
      multiplier: 1.0,
      severityOverride: null,
      reason: `capacity=ok (${(pct * 100).toFixed(0)}% used, ${days.toFixed(1)}d to reset)`,
    };
  }
}

// ---------------------------------------------------------------------------
// Demand-based adapter
// ---------------------------------------------------------------------------

/**
 * Adjusts capacity based on backlog pressure and release deadlines.
 */
export class DemandAdapter implements ThrottleAdapter {
  private readonly _config: ThrottleConfig;

  constructor(config: ThrottleConfig) {
    this._config = config;
  }

  evaluate(context: ThrottleContext): ThrottleResult {
    const surge = this._config.backlogSurgeThreshold;
    const critSurge = this._config.criticalSurgeThreshold;

    // Critical mass: large backlog AND many critical tickets
    if (
      context.backlogSize >= surge &&
      context.backlogCritical >= critSurge
    ) {
      return {
        multiplier: this._config.criticalSurgeMultiplier,
        severityOverride: null,
        reason:
          `demand=critical-mass (backlog=${context.backlogSize}, ` +
          `critical=${context.backlogCritical}) -> ` +
          `${this._config.criticalSurgeMultiplier}x`,
      };
    }

    // High pressure: large backlog or pre-release
    if (context.backlogSize >= surge || context.isPreRelease) {
      const reasonParts: string[] = [];
      if (context.backlogSize >= surge) {
        reasonParts.push(`backlog=${context.backlogSize}`);
      }
      if (context.isPreRelease) {
        reasonParts.push("pre-release");
      }
      return {
        multiplier: this._config.backlogSurgeMultiplier,
        severityOverride: null,
        reason:
          `demand=surge (${reasonParts.join(", ")}) -> ` +
          `${this._config.backlogSurgeMultiplier}x`,
      };
    }

    return {
      multiplier: 1.0,
      severityOverride: null,
      reason: `demand=normal (backlog=${context.backlogSize})`,
    };
  }
}

// ---------------------------------------------------------------------------
// ThrottlePolicy -- orchestrator
// ---------------------------------------------------------------------------

/**
 * Combines multiple throttle adapters to produce resolved cycle limits.
 *
 * The policy evaluates each adapter, multiplies their multipliers together
 * (clamped to [0.1, 4.0]), and applies the most restrictive severity
 * override. All numeric limits are floored at 1 (never fully stop).
 */
export class ThrottlePolicy {
  private readonly _base: CycleConfig;
  private readonly _adapters: ThrottleAdapter[];

  constructor(baseConfig: CycleConfig, adapters: ThrottleAdapter[]) {
    this._base = baseConfig;
    this._adapters = adapters;
  }

  /** Evaluate all adapters and compute effective cycle limits. */
  resolve(context: ThrottleContext): ResolvedCycleConfig {
    const results: ThrottleResult[] = [];

    for (const adapter of this._adapters) {
      try {
        const result = adapter.evaluate(context, this._base);
        results.push(result);
      } catch (err) {
        console.error(
          `Throttle adapter ${adapter.constructor.name} failed -- using 1.0x:`,
          err,
        );
        results.push({
          multiplier: 1.0,
          severityOverride: null,
          reason: `${adapter.constructor.name}: error fallback`,
        });
      }
    }

    // Combine multipliers (product, clamped)
    let combined = 1.0;
    for (const r of results) {
      combined *= r.multiplier;
    }
    combined = Math.max(MIN_MULTIPLIER, Math.min(MAX_MULTIPLIER, combined));

    // Severity: use the most restrictive override
    let severity = this._base.severityFilter;
    for (const r of results) {
      if (r.severityOverride != null) {
        const overrideRank = SEV_RANK[r.severityOverride] ?? 0;
        const currentRank = SEV_RANK[severity] ?? 0;
        // Only apply if more restrictive
        if (overrideRank > currentRank) {
          severity = r.severityOverride;
        }
      }
    }

    const reasons = results
      .filter((r) => r.reason)
      .map((r) => r.reason);

    return {
      maxNewTicketsPerCycle: Math.max(
        1,
        Math.floor(this._base.maxNewTicketsPerCycle * combined),
      ),
      maxInvestigationsPerCycle: Math.max(
        1,
        Math.floor(this._base.maxInvestigationsPerCycle * combined),
      ),
      maxDevelopmentsPerCycle: Math.max(
        1,
        Math.floor(this._base.maxDevelopmentsPerCycle * combined),
      ),
      maxOpenInvestigating: Math.max(
        1,
        Math.floor(this._base.maxOpenInvestigating * combined),
      ),
      severityFilter: severity,
      effectiveMultiplier: Math.round(combined * 1000) / 1000,
      reasons,
    };
  }
}

// ---------------------------------------------------------------------------
// Utility: days until weekly reset (Monday 00:00 UTC)
// ---------------------------------------------------------------------------

/**
 * Calculate days until next Monday 00:00 UTC (weekly API reset).
 *
 * @param nowUtc - Current UTC date. Defaults to now.
 */
export function daysUntilWeeklyReset(nowUtc?: Date): number {
  const now = nowUtc ?? new Date();
  // JS getUTCDay(): 0 = Sunday, 1 = Monday, ... 6 = Saturday
  // Python weekday(): 0 = Monday ... 6 = Sunday
  // Convert JS day to Python-style weekday for the same logic:
  const jsDay = now.getUTCDay();
  const pyDay = jsDay === 0 ? 6 : jsDay - 1; // 0=Mon, 1=Tue, ..., 6=Sun

  let daysAhead = (7 - pyDay) % 7;
  if (daysAhead === 0) {
    daysAhead = 7; // If it's Monday, next reset is next Monday
  }

  const nextMonday = new Date(
    Date.UTC(
      now.getUTCFullYear(),
      now.getUTCMonth(),
      now.getUTCDate(),
      0,
      0,
      0,
      0,
    ),
  );
  nextMonday.setUTCDate(nextMonday.getUTCDate() + daysAhead);

  const deltaMs = nextMonday.getTime() - now.getTime();
  return deltaMs / (86400 * 1000);
}

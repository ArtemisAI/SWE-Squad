/**
 * Engine rate-limiter / health tracker.
 *
 * Tracks per-engine failure state so the orchestrator can route work
 * to healthy engines and avoid hammering rate-limited or exhausted ones.
 */

import type { EngineErrorType } from "../providers/engine/base.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type EngineHealthState =
  | "healthy"
  | "rate_limited"
  | "overloaded"
  | "monthly_exhausted"
  | "errored";

export interface EngineHealth {
  state: EngineHealthState;
  failureCount: number;
  lastFailure: number | null; // epoch ms
  cooldownUntil: number | null; // epoch ms
}

export interface RateLimiterOptions {
  /** Cooldown after rate_limit (ms). Default: 60_000. */
  rateLimitCooldownMs?: number;
  /** Cooldown after overloaded (ms). Default: 120_000. */
  overloadedCooldownMs?: number;
  /** Cooldown after generic error (ms). Default: 30_000. */
  errorCooldownMs?: number;
  /** Cooldown after monthly_exhausted (ms). Default: 3_600_000 (1h). */
  exhaustedCooldownMs?: number;
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

export class EngineRateLimiter {
  private readonly _engines: Map<string, EngineHealth> = new Map();
  private readonly _rateLimitCooldownMs: number;
  private readonly _overloadedCooldownMs: number;
  private readonly _errorCooldownMs: number;
  private readonly _exhaustedCooldownMs: number;

  constructor(options?: RateLimiterOptions) {
    this._rateLimitCooldownMs = options?.rateLimitCooldownMs ?? 60_000;
    this._overloadedCooldownMs = options?.overloadedCooldownMs ?? 120_000;
    this._errorCooldownMs = options?.errorCooldownMs ?? 30_000;
    this._exhaustedCooldownMs = options?.exhaustedCooldownMs ?? 3_600_000;
  }

  /** Get or create health record for an engine. */
  getHealth(engineName: string): EngineHealth {
    let h = this._engines.get(engineName);
    if (!h) {
      h = { state: "healthy", failureCount: 0, lastFailure: null, cooldownUntil: null };
      this._engines.set(engineName, h);
    }
    return h;
  }

  /** Record a failure for an engine. */
  markFailure(engineName: string, errorType: EngineErrorType): void {
    const h = this.getHealth(engineName);
    h.failureCount += 1;
    h.lastFailure = Date.now();

    switch (errorType) {
      case "rate_limit":
        h.state = "rate_limited";
        h.cooldownUntil = Date.now() + this._rateLimitCooldownMs;
        break;
      case "overloaded":
        h.state = "overloaded";
        h.cooldownUntil = Date.now() + this._overloadedCooldownMs;
        break;
      case "auth_error":
        h.state = "monthly_exhausted";
        h.cooldownUntil = Date.now() + this._exhaustedCooldownMs;
        break;
      default:
        h.state = "errored";
        h.cooldownUntil = Date.now() + this._errorCooldownMs;
        break;
    }
  }

  /** Record a success — resets state to healthy. */
  markSuccess(engineName: string): void {
    const h = this.getHealth(engineName);
    h.state = "healthy";
    h.failureCount = 0;
    h.lastFailure = null;
    h.cooldownUntil = null;
  }

  /** Whether the engine should be used right now. */
  shouldUse(engineName: string): boolean {
    const h = this.getHealth(engineName);
    if (h.state === "healthy") return true;
    if (h.cooldownUntil !== null && Date.now() >= h.cooldownUntil) {
      // Cooldown expired — tentatively allow
      return true;
    }
    return false;
  }

  /**
   * Pick the best engine from a preference-ordered list.
   * Returns the first engine that is currently usable, or null if all are down.
   */
  getBestEngine(preferred: string[]): string | null {
    for (const name of preferred) {
      if (this.shouldUse(name)) return name;
    }
    return null;
  }

  /** Snapshot of all tracked engines. */
  snapshot(): Record<string, EngineHealth> {
    const out: Record<string, EngineHealth> = {};
    for (const [k, v] of this._engines) {
      out[k] = { ...v };
    }
    return out;
  }
}

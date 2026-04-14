/**
 * Circuit breaker for SWE-Squad agents.
 *
 * Tracks rolling failure rates and provides a mechanism to pause processing
 * when failures exceed a defined threshold.
 *
 * Ported from: src/swe_team/circuit_breaker.py
 */

import fs from "node:fs";
import path from "node:path";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface CircuitBreakerState {
  results: boolean[];
  paused_until: string | null;
  failure_rate: number;
  is_paused: boolean;
}

// ---------------------------------------------------------------------------
// CircuitBreaker
// ---------------------------------------------------------------------------

export class CircuitBreaker {
  private readonly _path: string;
  private readonly _windowSize: number;
  private readonly _threshold: number;
  private readonly _pauseDurationMinutes: number;

  private _results: boolean[];
  private _pausedUntil: string | null;

  constructor(
    statePath: string = "data/swe_team/circuit_breaker.json",
    windowSize: number = 10,
    failureThreshold: number = 0.8,
    pauseDurationMinutes: number = 30,
  ) {
    this._path = statePath;
    this._windowSize = windowSize;
    this._threshold = failureThreshold;
    this._pauseDurationMinutes = pauseDurationMinutes;

    // Load state
    const state = this._load();
    this._results = (state.results ?? []).slice(-windowSize);
    this._pausedUntil = state.paused_until ?? null;
  }

  // -----------------------------------------------------------------------
  // Persistence
  // -----------------------------------------------------------------------

  private _load(): Partial<CircuitBreakerState> {
    try {
      if (!fs.existsSync(this._path)) {
        return {};
      }
      const raw = fs.readFileSync(this._path, "utf-8");
      return JSON.parse(raw) as Partial<CircuitBreakerState>;
    } catch {
      return {};
    }
  }

  private _save(): void {
    try {
      const dir = path.dirname(this._path);
      fs.mkdirSync(dir, { recursive: true });
      const data: CircuitBreakerState = {
        results: this._results,
        paused_until: this._pausedUntil,
        failure_rate: this.failureRate,
        is_paused: this.isPaused,
      };
      fs.writeFileSync(this._path, JSON.stringify(data, null, 2), "utf-8");
    } catch (err) {
      console.warn("Failed to save circuit breaker state:", err);
    }
  }

  // -----------------------------------------------------------------------
  // Properties
  // -----------------------------------------------------------------------

  /** Rolling failure rate: failures / window length. */
  get failureRate(): number {
    if (this._results.length === 0) {
      return 0.0;
    }
    const failures = this._results.filter((r) => !r).length;
    return failures / this._results.length;
  }

  /** Whether the circuit breaker is currently in a paused (tripped) state. */
  get isPaused(): boolean {
    if (this._pausedUntil == null) {
      return false;
    }
    try {
      const pausedUntil = new Date(this._pausedUntil).getTime();
      return Date.now() < pausedUntil;
    } catch {
      return false;
    }
  }

  // -----------------------------------------------------------------------
  // Methods
  // -----------------------------------------------------------------------

  /**
   * Record a single result (true for success, false for failure).
   *
   * When the window has at least 5 results and the failure rate exceeds the
   * threshold, the circuit breaker trips and sets a pause until timestamp.
   */
  recordResult(success: boolean): void {
    this._results.push(success);
    if (this._results.length > this._windowSize) {
      this._results.shift();
    }

    // Check if threshold reached
    if (this._results.length >= 5 && this.failureRate >= this._threshold) {
      const until = new Date(
        Date.now() + this._pauseDurationMinutes * 60 * 1000,
      );
      this._pausedUntil = until.toISOString();
      console.error(
        `Circuit breaker tripped: failure rate ${(this.failureRate * 100).toFixed(1)}% ` +
          `(threshold ${(this._threshold * 100).toFixed(1)}%). ` +
          `Pausing for ${this._pauseDurationMinutes} min.`,
      );
    }

    this._save();
  }

  /**
   * Record a skip -- a ticket that was bypassed without a genuine attempt.
   *
   * Skips do NOT affect the failure rate. They cover two scenarios:
   * 1. A ticket whose dev/investigation attempts are already exhausted (3/3 cap).
   * 2. A rate-limit pause where no attempt was made at all.
   *
   * Recording these as failures would inflate the failure rate, causing the
   * circuit breaker to trip and creating a death spiral.
   */
  recordSkip(): void {
    // No change to _results -- just log, no save needed.
  }

  /** Manually clear the pause state. */
  clearPause(): void {
    this._pausedUntil = null;
    this._save();
  }
}

/**
 * Unit tests for the engine rate-limiter / health tracker.
 *
 * Tests cover state transitions, cooldown logic, engine selection,
 * failure counting, and custom cooldown durations.
 * Uses vi.useFakeTimers() for deterministic time control.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  EngineRateLimiter,
  type EngineHealth,
  type EngineHealthState,
} from "../../../src/utils/rate-limiter.js";
import type { EngineErrorType } from "../../../src/providers/engine/base.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let limiter: EngineRateLimiter;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-04-12T12:00:00Z"));
  limiter = new EngineRateLimiter();
});

afterEach(() => {
  vi.useRealTimers();
});

// ===========================================================================
// 1. Initial state
// ===========================================================================

describe("Initial state", () => {
  it("unknown engine starts as healthy", () => {
    const h = limiter.getHealth("claude-cli");
    expect(h.state).toBe("healthy");
  });

  it("unknown engine has zero failure count", () => {
    const h = limiter.getHealth("claude-cli");
    expect(h.failureCount).toBe(0);
  });

  it("unknown engine has null lastFailure", () => {
    const h = limiter.getHealth("claude-cli");
    expect(h.lastFailure).toBeNull();
  });

  it("unknown engine has null cooldownUntil", () => {
    const h = limiter.getHealth("claude-cli");
    expect(h.cooldownUntil).toBeNull();
  });

  it("shouldUse returns true for unknown engine", () => {
    expect(limiter.shouldUse("never-seen")).toBe(true);
  });
});

// ===========================================================================
// 2. markFailure state transitions
// ===========================================================================

describe("markFailure state transitions", () => {
  it("rate_limit sets state to rate_limited", () => {
    limiter.markFailure("eng", "rate_limit");
    expect(limiter.getHealth("eng").state).toBe("rate_limited");
  });

  it("rate_limit makes shouldUse return false", () => {
    limiter.markFailure("eng", "rate_limit");
    expect(limiter.shouldUse("eng")).toBe(false);
  });

  it("overloaded sets state to overloaded", () => {
    limiter.markFailure("eng", "overloaded");
    expect(limiter.getHealth("eng").state).toBe("overloaded");
  });

  it("overloaded makes shouldUse return false", () => {
    limiter.markFailure("eng", "overloaded");
    expect(limiter.shouldUse("eng")).toBe(false);
  });

  it("auth_error sets state to monthly_exhausted", () => {
    limiter.markFailure("eng", "auth_error");
    expect(limiter.getHealth("eng").state).toBe("monthly_exhausted");
  });

  it("auth_error makes shouldUse return false", () => {
    limiter.markFailure("eng", "auth_error");
    expect(limiter.shouldUse("eng")).toBe(false);
  });

  it("server_error sets state to errored", () => {
    limiter.markFailure("eng", "server_error");
    expect(limiter.getHealth("eng").state).toBe("errored");
  });

  it("timeout sets state to errored", () => {
    limiter.markFailure("eng", "timeout");
    expect(limiter.getHealth("eng").state).toBe("errored");
  });

  it("unknown error type sets state to errored", () => {
    limiter.markFailure("eng", "unknown");
    expect(limiter.getHealth("eng").state).toBe("errored");
  });

  it("model_not_found sets state to errored", () => {
    limiter.markFailure("eng", "model_not_found");
    expect(limiter.getHealth("eng").state).toBe("errored");
  });
});

// ===========================================================================
// 3. markSuccess resets state
// ===========================================================================

describe("markSuccess resets state", () => {
  it("resets state to healthy after failure", () => {
    limiter.markFailure("eng", "rate_limit");
    expect(limiter.getHealth("eng").state).toBe("rate_limited");
    limiter.markSuccess("eng");
    expect(limiter.getHealth("eng").state).toBe("healthy");
  });

  it("resets failure count to 0", () => {
    limiter.markFailure("eng", "rate_limit");
    limiter.markFailure("eng", "rate_limit");
    expect(limiter.getHealth("eng").failureCount).toBe(2);
    limiter.markSuccess("eng");
    expect(limiter.getHealth("eng").failureCount).toBe(0);
  });

  it("clears lastFailure", () => {
    limiter.markFailure("eng", "rate_limit");
    expect(limiter.getHealth("eng").lastFailure).not.toBeNull();
    limiter.markSuccess("eng");
    expect(limiter.getHealth("eng").lastFailure).toBeNull();
  });

  it("clears cooldownUntil", () => {
    limiter.markFailure("eng", "rate_limit");
    expect(limiter.getHealth("eng").cooldownUntil).not.toBeNull();
    limiter.markSuccess("eng");
    expect(limiter.getHealth("eng").cooldownUntil).toBeNull();
  });

  it("shouldUse returns true after markSuccess", () => {
    limiter.markFailure("eng", "rate_limit");
    expect(limiter.shouldUse("eng")).toBe(false);
    limiter.markSuccess("eng");
    expect(limiter.shouldUse("eng")).toBe(true);
  });
});

// ===========================================================================
// 4. Cooldown expiry
// ===========================================================================

describe("Cooldown expiry", () => {
  it("shouldUse returns true after rate_limit cooldown expires", () => {
    limiter.markFailure("eng", "rate_limit");
    expect(limiter.shouldUse("eng")).toBe(false);
    // Advance past default 60s cooldown
    vi.advanceTimersByTime(60_001);
    expect(limiter.shouldUse("eng")).toBe(true);
  });

  it("shouldUse returns false before rate_limit cooldown expires", () => {
    limiter.markFailure("eng", "rate_limit");
    vi.advanceTimersByTime(59_000);
    expect(limiter.shouldUse("eng")).toBe(false);
  });

  it("shouldUse returns true after overloaded cooldown expires", () => {
    limiter.markFailure("eng", "overloaded");
    expect(limiter.shouldUse("eng")).toBe(false);
    // Default overloaded cooldown: 120s
    vi.advanceTimersByTime(120_001);
    expect(limiter.shouldUse("eng")).toBe(true);
  });

  it("shouldUse returns true after error cooldown expires", () => {
    limiter.markFailure("eng", "server_error");
    expect(limiter.shouldUse("eng")).toBe(false);
    // Default error cooldown: 30s
    vi.advanceTimersByTime(30_001);
    expect(limiter.shouldUse("eng")).toBe(true);
  });

  it("shouldUse returns true after exhausted cooldown expires", () => {
    limiter.markFailure("eng", "auth_error");
    expect(limiter.shouldUse("eng")).toBe(false);
    // Default exhausted cooldown: 1h
    vi.advanceTimersByTime(3_600_001);
    expect(limiter.shouldUse("eng")).toBe(true);
  });
});

// ===========================================================================
// 5. getBestEngine
// ===========================================================================

describe("getBestEngine", () => {
  it("returns preferred engine when healthy", () => {
    const best = limiter.getBestEngine(["claude-cli", "gemini", "opencode"]);
    expect(best).toBe("claude-cli");
  });

  it("returns first healthy fallback if preferred is down", () => {
    limiter.markFailure("claude-cli", "rate_limit");
    const best = limiter.getBestEngine(["claude-cli", "gemini", "opencode"]);
    expect(best).toBe("gemini");
  });

  it("skips multiple down engines", () => {
    limiter.markFailure("claude-cli", "rate_limit");
    limiter.markFailure("gemini", "overloaded");
    const best = limiter.getBestEngine(["claude-cli", "gemini", "opencode"]);
    expect(best).toBe("opencode");
  });

  it("returns null if all engines are down", () => {
    limiter.markFailure("claude-cli", "rate_limit");
    limiter.markFailure("gemini", "overloaded");
    limiter.markFailure("opencode", "auth_error");
    const best = limiter.getBestEngine(["claude-cli", "gemini", "opencode"]);
    expect(best).toBeNull();
  });

  it("returns null for empty preference list", () => {
    const best = limiter.getBestEngine([]);
    expect(best).toBeNull();
  });

  it("returns recovered engine after cooldown", () => {
    limiter.markFailure("claude-cli", "rate_limit");
    vi.advanceTimersByTime(60_001);
    const best = limiter.getBestEngine(["claude-cli", "gemini"]);
    expect(best).toBe("claude-cli");
  });

  it("uses fallback during cooldown, then preferred after", () => {
    limiter.markFailure("claude-cli", "rate_limit");
    // During cooldown: fallback
    expect(limiter.getBestEngine(["claude-cli", "gemini"])).toBe("gemini");
    // After cooldown: preferred
    vi.advanceTimersByTime(60_001);
    expect(limiter.getBestEngine(["claude-cli", "gemini"])).toBe("claude-cli");
  });
});

// ===========================================================================
// 6. Failure count tracking
// ===========================================================================

describe("Failure count tracking", () => {
  it("failure count increments on repeated failures", () => {
    limiter.markFailure("eng", "rate_limit");
    limiter.markFailure("eng", "rate_limit");
    limiter.markFailure("eng", "rate_limit");
    expect(limiter.getHealth("eng").failureCount).toBe(3);
  });

  it("failure count increments across different error types", () => {
    limiter.markFailure("eng", "rate_limit");
    limiter.markFailure("eng", "overloaded");
    limiter.markFailure("eng", "server_error");
    expect(limiter.getHealth("eng").failureCount).toBe(3);
  });

  it("lastFailure timestamp is updated on each failure", () => {
    limiter.markFailure("eng", "rate_limit");
    const first = limiter.getHealth("eng").lastFailure;
    vi.advanceTimersByTime(5000);
    limiter.markFailure("eng", "rate_limit");
    const second = limiter.getHealth("eng").lastFailure;
    expect(second).toBeGreaterThan(first!);
  });
});

// ===========================================================================
// 7. Custom cooldown durations
// ===========================================================================

describe("Custom cooldown durations", () => {
  it("custom rateLimitCooldownMs is respected", () => {
    const custom = new EngineRateLimiter({ rateLimitCooldownMs: 10_000 });
    custom.markFailure("eng", "rate_limit");
    vi.advanceTimersByTime(9_000);
    expect(custom.shouldUse("eng")).toBe(false);
    vi.advanceTimersByTime(1_001);
    expect(custom.shouldUse("eng")).toBe(true);
  });

  it("custom overloadedCooldownMs is respected", () => {
    const custom = new EngineRateLimiter({ overloadedCooldownMs: 5_000 });
    custom.markFailure("eng", "overloaded");
    vi.advanceTimersByTime(4_000);
    expect(custom.shouldUse("eng")).toBe(false);
    vi.advanceTimersByTime(1_001);
    expect(custom.shouldUse("eng")).toBe(true);
  });

  it("custom errorCooldownMs is respected", () => {
    const custom = new EngineRateLimiter({ errorCooldownMs: 2_000 });
    custom.markFailure("eng", "timeout");
    vi.advanceTimersByTime(1_500);
    expect(custom.shouldUse("eng")).toBe(false);
    vi.advanceTimersByTime(501);
    expect(custom.shouldUse("eng")).toBe(true);
  });

  it("custom exhaustedCooldownMs is respected", () => {
    const custom = new EngineRateLimiter({ exhaustedCooldownMs: 300_000 });
    custom.markFailure("eng", "auth_error");
    vi.advanceTimersByTime(299_000);
    expect(custom.shouldUse("eng")).toBe(false);
    vi.advanceTimersByTime(1_001);
    expect(custom.shouldUse("eng")).toBe(true);
  });
});

// ===========================================================================
// 8. Snapshot
// ===========================================================================

describe("snapshot()", () => {
  it("returns empty object when no engines tracked", () => {
    const snap = limiter.snapshot();
    expect(snap).toEqual({});
  });

  it("returns copies of engine health (not references)", () => {
    limiter.getHealth("eng");
    const snap = limiter.snapshot();
    snap["eng"].failureCount = 999;
    expect(limiter.getHealth("eng").failureCount).toBe(0);
  });

  it("includes all tracked engines", () => {
    limiter.getHealth("a");
    limiter.getHealth("b");
    limiter.markFailure("c", "rate_limit");
    const snap = limiter.snapshot();
    expect(Object.keys(snap).sort()).toEqual(["a", "b", "c"]);
  });
});

// ===========================================================================
// 9. Independent engine tracking
// ===========================================================================

describe("Independent engine tracking", () => {
  it("failure on one engine does not affect another", () => {
    limiter.markFailure("eng-a", "rate_limit");
    expect(limiter.shouldUse("eng-a")).toBe(false);
    expect(limiter.shouldUse("eng-b")).toBe(true);
  });

  it("success on one engine does not reset another", () => {
    limiter.markFailure("eng-a", "rate_limit");
    limiter.markFailure("eng-b", "overloaded");
    limiter.markSuccess("eng-a");
    expect(limiter.shouldUse("eng-a")).toBe(true);
    expect(limiter.shouldUse("eng-b")).toBe(false);
  });
});

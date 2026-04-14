/**
 * Tests for CircuitBreaker safety gate.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { CircuitBreaker } from "../../src/safety/circuit-breaker.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tmpDir: string;

function tmpPath(filename: string = "cb.json"): string {
  return path.join(tmpDir, filename);
}

// ---------------------------------------------------------------------------
// Suite
// ---------------------------------------------------------------------------

describe("CircuitBreaker", () => {
  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "cb-test-"));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  // -----------------------------------------------------------------------
  // Default properties
  // -----------------------------------------------------------------------

  it("has failureRate=0 when freshly created", () => {
    const cb = new CircuitBreaker(tmpPath());
    expect(cb.failureRate).toBe(0);
  });

  it("has isPaused=false when freshly created", () => {
    const cb = new CircuitBreaker(tmpPath());
    expect(cb.isPaused).toBe(false);
  });

  // -----------------------------------------------------------------------
  // recordResult – success
  // -----------------------------------------------------------------------

  it("keeps failure rate at 0 when all results are successes", () => {
    const cb = new CircuitBreaker(tmpPath());
    cb.recordResult(true);
    cb.recordResult(true);
    cb.recordResult(true);
    expect(cb.failureRate).toBe(0);
    expect(cb.isPaused).toBe(false);
  });

  // -----------------------------------------------------------------------
  // recordResult – failure
  // -----------------------------------------------------------------------

  it("increases failure rate when failures are recorded", () => {
    const cb = new CircuitBreaker(tmpPath());
    cb.recordResult(false);
    cb.recordResult(true);
    // 1 failure out of 2 results => 0.5
    expect(cb.failureRate).toBe(0.5);
  });

  it("calculates failure rate correctly with mixed results", () => {
    const cb = new CircuitBreaker(tmpPath());
    cb.recordResult(true);
    cb.recordResult(false);
    cb.recordResult(false);
    cb.recordResult(true);
    // 2 failures out of 4 => 0.5
    expect(cb.failureRate).toBe(0.5);
  });

  // -----------------------------------------------------------------------
  // Trip condition: 5+ failures at 80%+ rate
  // -----------------------------------------------------------------------

  it("trips when 5+ results at 80%+ failure rate", () => {
    const cb = new CircuitBreaker(tmpPath());
    // Record 5 failures in a row
    for (let i = 0; i < 5; i++) {
      cb.recordResult(false);
    }
    expect(cb.failureRate).toBe(1.0);
    expect(cb.isPaused).toBe(true);
  });

  it("trips at exactly 80% failure rate with 5 results", () => {
    const cb = new CircuitBreaker(tmpPath());
    // 4 failures + 1 success = 80%
    cb.recordResult(false);
    cb.recordResult(false);
    cb.recordResult(false);
    cb.recordResult(false);
    cb.recordResult(true); // 4/5 = 0.8 — but this last one is success, rate after = 0.8
    // After recording `true`, rate is 4/5 = 0.8 which >= 0.8 threshold
    expect(cb.failureRate).toBe(0.8);
    expect(cb.isPaused).toBe(true);
  });

  // -----------------------------------------------------------------------
  // Does NOT trip with < 5 results
  // -----------------------------------------------------------------------

  it("does NOT trip with only 4 failures (< 5 results)", () => {
    const cb = new CircuitBreaker(tmpPath());
    for (let i = 0; i < 4; i++) {
      cb.recordResult(false);
    }
    expect(cb.failureRate).toBe(1.0);
    expect(cb.isPaused).toBe(false);
  });

  it("does NOT trip with 3 results even at 100% failure", () => {
    const cb = new CircuitBreaker(tmpPath());
    cb.recordResult(false);
    cb.recordResult(false);
    cb.recordResult(false);
    expect(cb.failureRate).toBe(1.0);
    expect(cb.isPaused).toBe(false);
  });

  // -----------------------------------------------------------------------
  // recordSkip
  // -----------------------------------------------------------------------

  it("recordSkip does not affect failure rate", () => {
    const cb = new CircuitBreaker(tmpPath());
    cb.recordResult(true);
    cb.recordResult(false);
    const rateBefore = cb.failureRate;
    cb.recordSkip();
    cb.recordSkip();
    cb.recordSkip();
    expect(cb.failureRate).toBe(rateBefore);
  });

  // -----------------------------------------------------------------------
  // clearPause
  // -----------------------------------------------------------------------

  it("clearPause resets the pause state", () => {
    const cb = new CircuitBreaker(tmpPath());
    for (let i = 0; i < 5; i++) {
      cb.recordResult(false);
    }
    expect(cb.isPaused).toBe(true);
    cb.clearPause();
    expect(cb.isPaused).toBe(false);
  });

  // -----------------------------------------------------------------------
  // Pause expiry
  // -----------------------------------------------------------------------

  it("pause expires after the configured duration", () => {
    // Use a very short pause duration (0.001 minutes ≈ 60ms)
    const cb = new CircuitBreaker(tmpPath(), 10, 0.8, 0);
    for (let i = 0; i < 5; i++) {
      cb.recordResult(false);
    }
    // With 0 minute pause, the paused_until is effectively now
    // so isPaused should be false immediately (Date.now() >= pausedUntil)
    expect(cb.isPaused).toBe(false);
  });

  // -----------------------------------------------------------------------
  // State persistence
  // -----------------------------------------------------------------------

  it("persists state to disk and reloads it", () => {
    const p = tmpPath("persist.json");
    const cb1 = new CircuitBreaker(p);
    cb1.recordResult(true);
    cb1.recordResult(false);
    cb1.recordResult(true);

    // Create a new instance from the same file
    const cb2 = new CircuitBreaker(p);
    expect(cb2.failureRate).toBeCloseTo(1 / 3, 5);
    expect(cb2.isPaused).toBe(false);
  });

  it("persists paused state across instances", () => {
    const p = tmpPath("paused.json");
    const cb1 = new CircuitBreaker(p, 10, 0.8, 30);
    for (let i = 0; i < 5; i++) {
      cb1.recordResult(false);
    }
    expect(cb1.isPaused).toBe(true);

    const cb2 = new CircuitBreaker(p, 10, 0.8, 30);
    expect(cb2.isPaused).toBe(true);
  });

  it("creates parent directories for state file", () => {
    const deep = path.join(tmpDir, "a", "b", "c", "cb.json");
    const cb = new CircuitBreaker(deep);
    cb.recordResult(true);
    expect(fs.existsSync(deep)).toBe(true);
  });

  // -----------------------------------------------------------------------
  // Window size limiting
  // -----------------------------------------------------------------------

  it("only keeps the last N results (window size)", () => {
    const windowSize = 5;
    const cb = new CircuitBreaker(tmpPath(), windowSize);
    // Fill with 5 failures
    for (let i = 0; i < 5; i++) {
      cb.recordResult(false);
    }
    expect(cb.failureRate).toBe(1.0);

    // Now push 5 successes — old failures should be evicted
    for (let i = 0; i < 5; i++) {
      cb.recordResult(true);
    }
    expect(cb.failureRate).toBe(0);
  });

  it("window eviction reduces failure rate over time", () => {
    const cb = new CircuitBreaker(tmpPath(), 4);
    cb.recordResult(false); // [F] -> 1.0
    cb.recordResult(false); // [F,F] -> 1.0
    cb.recordResult(true);  // [F,F,T] -> 2/3
    cb.recordResult(true);  // [F,F,T,T] -> 0.5
    cb.recordResult(true);  // [F,T,T,T] -> 0.25 (first F evicted)
    expect(cb.failureRate).toBe(0.25);
  });

  // -----------------------------------------------------------------------
  // Edge cases
  // -----------------------------------------------------------------------

  it("handles corrupt state file gracefully", () => {
    const p = tmpPath("corrupt.json");
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, "NOT VALID JSON{{{", "utf-8");
    const cb = new CircuitBreaker(p);
    expect(cb.failureRate).toBe(0);
    expect(cb.isPaused).toBe(false);
  });

  it("works when state file does not exist", () => {
    const cb = new CircuitBreaker(tmpPath("nonexistent.json"));
    expect(cb.failureRate).toBe(0);
    expect(cb.isPaused).toBe(false);
  });
});

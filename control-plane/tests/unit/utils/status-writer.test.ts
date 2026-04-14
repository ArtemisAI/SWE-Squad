/**
 * Unit tests for the StatusWriter (utils/status-writer.ts).
 *
 * Tests cover atomic file writes, cycle result mapping, read/write
 * round-trips, uptime calculation, and error handling.
 * Uses real temp directories for filesystem operations.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  StatusWriter,
  type StatusSnapshot,
  type CycleResultLike,
} from "../../../src/utils/status-writer.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tmpDir: string;

function statusPath(filename: string = "status.json"): string {
  return path.join(tmpDir, filename);
}

function mkCycleResult(overrides?: Partial<CycleResultLike>): CycleResultLike {
  return {
    timestamp: new Date().toISOString(),
    durationMs: 1234,
    ticketsScanned: 5,
    ticketsTriaged: 3,
    ticketsInvestigated: 2,
    ticketsDeveloped: 1,
    ticketsVerified: 1,
    stabilityVerdict: "pass",
    errors: [],
    ...overrides,
  };
}

/** Minimal StatusSnapshot with all Python-compatible fields populated. */
function mkSnapshot(overrides?: Partial<StatusSnapshot>): StatusSnapshot {
  return {
    // Python-compatible fields
    last_cycle: new Date().toISOString(),
    tickets_open: 0,
    tickets_investigating: 0,
    gate_verdict: "pass",
    next_cycle: null,
    engine_health: [],
    // TS-native fields
    pid: process.pid,
    startedAt: new Date().toISOString(),
    uptimeSeconds: 0,
    lastCycleAt: null,
    lastCycleDurationMs: null,
    cyclesCompleted: 0,
    ticketsResolved: 0,
    stabilityVerdict: "unknown",
    errors: [],
    ...overrides,
  };
}

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "status-test-"));
});

afterEach(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  vi.restoreAllMocks();
});

// ===========================================================================
// 1. write()
// ===========================================================================

describe("write()", () => {
  it("creates file at the configured path", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.write(mkSnapshot());
    expect(fs.existsSync(p)).toBe(true);
  });

  it("writes valid JSON", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.write(mkSnapshot({ uptimeSeconds: 10, stabilityVerdict: "pass", gate_verdict: "pass" }));
    const raw = fs.readFileSync(p, "utf-8");
    const parsed = JSON.parse(raw);
    expect(parsed.pid).toBe(process.pid);
    expect(parsed.stabilityVerdict).toBe("pass");
  });

  it("creates parent directories if they do not exist", () => {
    const deep = path.join(tmpDir, "a", "b", "c", "status.json");
    const writer = new StatusWriter(deep);
    writer.write(mkSnapshot({ pid: 1, startedAt: new Date().toISOString() }));
    expect(fs.existsSync(deep)).toBe(true);
  });

  it("atomic write: tmp file does not linger on success", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.write(mkSnapshot({ pid: 1 }));
    expect(fs.existsSync(p + ".tmp")).toBe(false);
    expect(fs.existsSync(p)).toBe(true);
  });

  it("overwrites existing file", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.write(mkSnapshot({ pid: 1 }));
    writer.write(mkSnapshot({ pid: 2, uptimeSeconds: 99, cyclesCompleted: 5, tickets_open: 3, ticketsResolved: 7, stabilityVerdict: "pass" }));
    const raw = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(raw.pid).toBe(2);
    expect(raw.cyclesCompleted).toBe(5);
  });
});

// ===========================================================================
// 2. writeFromCycleResult()
// ===========================================================================

describe("writeFromCycleResult()", () => {
  it("maps stabilityVerdict from cycle result", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult({ stabilityVerdict: "block" }), 2, 5);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.stabilityVerdict).toBe("block");
  });

  it("maps errors from cycle result", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult({ errors: ["err1", "err2"] }), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.errors).toEqual(["err1", "err2"]);
  });

  it("maps lastCycleDurationMs from cycle result", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult({ durationMs: 4567 }), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.lastCycleDurationMs).toBe(4567);
  });

  it("maps lastCycleAt from cycle result timestamp", () => {
    const ts = "2026-04-12T10:00:00.000Z";
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult({ timestamp: ts }), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.lastCycleAt).toBe(ts);
  });

  it("increments cyclesCompleted on each call", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult(), 0, 0);
    writer.writeFromCycleResult(mkCycleResult(), 0, 0);
    writer.writeFromCycleResult(mkCycleResult(), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.cyclesCompleted).toBe(3);
  });

  it("sets tickets_open from argument", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult(), 42, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.tickets_open).toBe(42);
  });

  it("sets ticketsResolved from argument", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult(), 0, 99);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.ticketsResolved).toBe(99);
  });

  // ── Python-compatible fields ──

  it("maps last_cycle from cycle result timestamp", () => {
    const ts = "2026-04-12T10:00:00.000Z";
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult({ timestamp: ts }), 5, 3);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.last_cycle).toBe(ts);
  });

  it("maps tickets_open from openCount argument", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult(), 42, 5);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.tickets_open).toBe(42);
  });

  it("maps tickets_investigating from ticketsInvestigated in cycle result", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult({ ticketsInvestigated: 7 }), 10, 5);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.tickets_investigating).toBe(7);
  });

  it("maps gate_verdict from stabilityVerdict", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult({ stabilityVerdict: "block" }), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.gate_verdict).toBe("block");
  });

  it("maps engine_health from errors", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult({ errors: ["engine-a: degraded"] }), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.engine_health).toEqual(["engine-a: degraded"]);
  });

  it("computes next_cycle when intervalSeconds > 0", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-12T12:00:00Z"));
    const p = statusPath();
    const writer = new StatusWriter(p, 300); // 5-minute interval
    writer.writeFromCycleResult(mkCycleResult({ timestamp: "2026-04-12T12:00:00Z" }), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.next_cycle).toBe("2026-04-12T12:05:00.000Z");
    vi.useRealTimers();
  });

  it("sets next_cycle to null when intervalSeconds is 0", () => {
    const p = statusPath();
    const writer = new StatusWriter(p); // default intervalSeconds = 0
    writer.writeFromCycleResult(mkCycleResult(), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.next_cycle).toBeNull();
  });
});

// ===========================================================================
// 3. PID and uptime
// ===========================================================================

describe("PID and uptime", () => {
  it("pid is process.pid", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult(), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.pid).toBe(process.pid);
  });

  it("startedAt is a valid ISO timestamp", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult(), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    const parsed = new Date(snap.startedAt);
    expect(parsed.getTime()).not.toBeNaN();
  });

  it("uptimeSeconds is non-negative", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult(), 0, 0);
    const snap = JSON.parse(fs.readFileSync(p, "utf-8"));
    expect(snap.uptimeSeconds).toBeGreaterThanOrEqual(0);
  });

  it("uptimeSeconds increases over time", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-12T12:00:00Z"));
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult(), 0, 0);
    const snap1 = JSON.parse(fs.readFileSync(p, "utf-8"));

    vi.advanceTimersByTime(30_000);
    writer.writeFromCycleResult(mkCycleResult(), 0, 0);
    const snap2 = JSON.parse(fs.readFileSync(p, "utf-8"));

    expect(snap2.uptimeSeconds).toBeGreaterThan(snap1.uptimeSeconds);
    vi.useRealTimers();
  });
});

// ===========================================================================
// 4. read()
// ===========================================================================

describe("StatusWriter.read()", () => {
  it("returns null for missing file", () => {
    const result = StatusWriter.read(path.join(tmpDir, "nonexistent.json"));
    expect(result).toBeNull();
  });

  it("returns StatusSnapshot for existing file", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(mkCycleResult(), 3, 7);
    const result = StatusWriter.read(p);
    expect(result).not.toBeNull();
    expect(result!.tickets_open).toBe(3);
    expect(result!.ticketsResolved).toBe(7);
  });

  it("returns null for corrupt JSON file", () => {
    const p = statusPath();
    fs.writeFileSync(p, "NOT VALID JSON{{{", "utf-8");
    const result = StatusWriter.read(p);
    expect(result).toBeNull();
  });

  it("round-trips all fields correctly", () => {
    const p = statusPath();
    const writer = new StatusWriter(p);
    writer.writeFromCycleResult(
      mkCycleResult({
        timestamp: "2026-04-12T10:30:00Z",
        durationMs: 2222,
        stabilityVerdict: "warn",
        errors: ["test-err"],
      }),
      10,
      20,
    );
    const snap = StatusWriter.read(p);
    expect(snap).not.toBeNull();
    // TS-native fields
    expect(snap!.lastCycleAt).toBe("2026-04-12T10:30:00Z");
    expect(snap!.lastCycleDurationMs).toBe(2222);
    expect(snap!.stabilityVerdict).toBe("warn");
    expect(snap!.errors).toEqual(["test-err"]);
    expect(snap!.tickets_open).toBe(10);
    expect(snap!.ticketsResolved).toBe(20);
    expect(snap!.cyclesCompleted).toBe(1);
    // Python-compatible fields
    expect(snap!.last_cycle).toBe("2026-04-12T10:30:00Z");
    expect(snap!.gate_verdict).toBe("warn");
    expect(snap!.engine_health).toEqual(["test-err"]);
  });
});

/**
 * Status file writer — atomic JSON snapshot of daemon health.
 *
 * Writes `data/swe_team/status.json` atomically (write to .tmp, rename).
 * The CLI and dashboard read this file for at-a-glance health.
 */

import fs from "node:fs";
import path from "node:path";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface StatusSnapshot {
  // ── Python-compatible fields (read by dashboard_data.py::_load_status) ──
  last_cycle: string | null;        // ISO timestamp of last completed cycle
  tickets_open: number;            // count of open tickets
  tickets_investigating: number;  // count of tickets in investigating state
  gate_verdict: string;            // pass / warn / block
  next_cycle: string | null;       // ISO timestamp of next scheduled cycle
  engine_health: string[];         // per-engine health status lines

  // ── TS-control-plane-native fields ──
  pid: number;
  startedAt: string;
  uptimeSeconds: number;
  lastCycleAt: string | null;
  lastCycleDurationMs: number | null;
  cyclesCompleted: number;
  ticketsResolved: number;
  stabilityVerdict: string;
  errors: string[];
}

export interface CycleResultLike {
  timestamp: string;
  durationMs: number;
  ticketsScanned: number;
  ticketsTriaged: number;
  ticketsInvestigated: number;
  ticketsDeveloped: number;
  ticketsVerified: number;
  stabilityVerdict: string;
  errors: string[];
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

export class StatusWriter {
  private readonly _path: string;
  private readonly _startedAt: string;
  private _cyclesCompleted: number = 0;
  private _ticketsOpen: number = 0;
  private _ticketsResolved: number = 0;
  private _ticketsInvestigating: number = 0;
  private _intervalSeconds: number;

  constructor(statusPath: string, intervalSeconds: number = 0) {
    this._path = statusPath;
    this._startedAt = new Date().toISOString();
    this._intervalSeconds = intervalSeconds;
  }

  /** Write a raw StatusSnapshot to disk atomically. */
  write(snapshot: StatusSnapshot): void {
    const dir = path.dirname(this._path);
    fs.mkdirSync(dir, { recursive: true });
    const tmpPath = this._path + ".tmp";
    fs.writeFileSync(tmpPath, JSON.stringify(snapshot, null, 2), "utf-8");
    fs.renameSync(tmpPath, this._path);
  }

  /** Update from a cycle result and write. */
  writeFromCycleResult(result: CycleResultLike, openCount: number, resolvedCount: number): void {
    this._cyclesCompleted += 1;
    this._ticketsOpen = openCount;
    this._ticketsResolved = resolvedCount;
    this._ticketsInvestigating = result.ticketsInvestigated;

    const now = Date.now();
    const started = new Date(this._startedAt).getTime();
    const uptimeSeconds = Math.round((now - started) / 1000);

    // Compute next_cycle timestamp (Python-compatible field)
    let nextCycle: string | null = null;
    if (this._intervalSeconds > 0) {
      nextCycle = new Date(now + this._intervalSeconds * 1000).toISOString();
    }

    this.write({
      // Python-compatible fields (read by dashboard_data.py::_load_status)
      last_cycle: result.timestamp,
      tickets_open: this._ticketsOpen,
      tickets_investigating: this._ticketsInvestigating,
      gate_verdict: result.stabilityVerdict,
      next_cycle: nextCycle,
      engine_health: result.errors,

      // TS-control-plane-native fields
      pid: process.pid,
      startedAt: this._startedAt,
      uptimeSeconds,
      lastCycleAt: result.timestamp,
      lastCycleDurationMs: result.durationMs,
      cyclesCompleted: this._cyclesCompleted,
      ticketsResolved: this._ticketsResolved,
      stabilityVerdict: result.stabilityVerdict,
      errors: result.errors,
    });
  }

  /** Read the current status file, or null if not found. */
  static read(statusPath: string): StatusSnapshot | null {
    try {
      const raw = fs.readFileSync(statusPath, "utf-8");
      return JSON.parse(raw) as StatusSnapshot;
    } catch {
      return null;
    }
  }
}

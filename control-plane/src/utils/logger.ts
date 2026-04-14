/**
 * Structured logger for the SWE-Squad control plane.
 *
 * Provides levelled logging (debug, info, warn, error) with timestamps,
 * prefixed output, hierarchical child loggers, and optional file output.
 *
 * When `logFile` is set, all messages are appended to the file in addition
 * to stdout/stderr. This ensures daemon logs survive even when running
 * outside systemd (e.g., manual `npx tsx src/main.ts --daemon`).
 */

import fs from "node:fs";
import path from "node:path";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type LogLevel = "debug" | "info" | "warn" | "error";

export interface Logger {
  debug(msg: string, ...args: unknown[]): void;
  info(msg: string, ...args: unknown[]): void;
  warn(msg: string, ...args: unknown[]): void;
  error(msg: string, ...args: unknown[]): void;
  child(prefix: string): Logger;
}

// ---------------------------------------------------------------------------
// Level ordering (lower = more verbose)
// ---------------------------------------------------------------------------

const LEVEL_ORDER: Record<LogLevel, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
};

// ---------------------------------------------------------------------------
// Timestamp helper
// ---------------------------------------------------------------------------

function timestamp(): string {
  return new Date().toISOString();
}

// ---------------------------------------------------------------------------
// File writer (shared singleton — all loggers append to the same file)
// ---------------------------------------------------------------------------

let _logFd: number | null = null;

function ensureLogFile(filePath: string): void {
  if (_logFd !== null) return;
  try {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    _logFd = fs.openSync(filePath, "a");
  } catch {
    // File logging is best-effort — don't crash the daemon
  }
}

function writeToFile(line: string): void {
  if (_logFd === null) return;
  try {
    fs.writeSync(_logFd, line + "\n");
  } catch {
    // Silently ignore write errors
  }
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

class PrefixedLogger implements Logger {
  private readonly _prefix: string;
  private readonly _level: number;

  constructor(prefix: string, level: LogLevel) {
    this._prefix = prefix;
    this._level = LEVEL_ORDER[level];
  }

  debug(msg: string, ...args: unknown[]): void {
    if (this._level <= LEVEL_ORDER.debug) {
      const line = `${timestamp()} [DEBUG] [${this._prefix}] ${msg}`;
      console.log(line, ...args);
      writeToFile(line + (args.length ? " " + args.map(String).join(" ") : ""));
    }
  }

  info(msg: string, ...args: unknown[]): void {
    if (this._level <= LEVEL_ORDER.info) {
      const line = `${timestamp()} [INFO] [${this._prefix}] ${msg}`;
      console.log(line, ...args);
      writeToFile(line + (args.length ? " " + args.map(String).join(" ") : ""));
    }
  }

  warn(msg: string, ...args: unknown[]): void {
    if (this._level <= LEVEL_ORDER.warn) {
      const line = `${timestamp()} [WARN] [${this._prefix}] ${msg}`;
      console.warn(line, ...args);
      writeToFile(line + (args.length ? " " + args.map(String).join(" ") : ""));
    }
  }

  error(msg: string, ...args: unknown[]): void {
    if (this._level <= LEVEL_ORDER.error) {
      const line = `${timestamp()} [ERROR] [${this._prefix}] ${msg}`;
      console.error(line, ...args);
      writeToFile(line + (args.length ? " " + args.map(String).join(" ") : ""));
    }
  }

  child(prefix: string): Logger {
    return new PrefixedLogger(`${this._prefix}:${prefix}`, this._levelName());
  }

  private _levelName(): LogLevel {
    for (const [name, val] of Object.entries(LEVEL_ORDER)) {
      if (val === this._level) return name as LogLevel;
    }
    return "info";
  }
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

export interface CreateLoggerOptions {
  prefix?: string;
  level?: LogLevel;
  logFile?: string;
}

/**
 * Create a new Logger instance.
 *
 * @param options.prefix  - Prefix shown in every log line (default: "swe")
 * @param options.level   - Minimum log level (default: "info")
 * @param options.logFile - Optional file path for persistent log output
 */
export function createLogger(options?: CreateLoggerOptions): Logger {
  const prefix = options?.prefix ?? "swe";
  const level = options?.level ?? "info";
  if (options?.logFile) {
    ensureLogFile(options.logFile);
  }
  return new PrefixedLogger(prefix, level);
}

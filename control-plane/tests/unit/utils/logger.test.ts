/**
 * Unit tests for the structured logger (utils/logger.ts).
 *
 * Tests cover factory function, level filtering, output formatting,
 * prefix handling, and child logger chaining.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createLogger, type Logger, type LogLevel } from "../../../src/utils/logger.js";

// ---------------------------------------------------------------------------
// Spies
// ---------------------------------------------------------------------------

let logSpy: ReturnType<typeof vi.spyOn>;
let warnSpy: ReturnType<typeof vi.spyOn>;
let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
  warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ===========================================================================
// 1. createLogger factory
// ===========================================================================

describe("createLogger()", () => {
  it("returns an object with all expected methods", () => {
    const log = createLogger();
    expect(typeof log.debug).toBe("function");
    expect(typeof log.info).toBe("function");
    expect(typeof log.warn).toBe("function");
    expect(typeof log.error).toBe("function");
    expect(typeof log.child).toBe("function");
  });

  it("returns a new instance on each call", () => {
    const a = createLogger();
    const b = createLogger();
    expect(a).not.toBe(b);
  });

  it("accepts no arguments (uses defaults)", () => {
    const log = createLogger();
    log.info("test");
    expect(logSpy).toHaveBeenCalledTimes(1);
  });

  it("accepts custom prefix", () => {
    const log = createLogger({ prefix: "myapp" });
    log.info("hello");
    const output = logSpy.mock.calls[0][0] as string;
    expect(output).toContain("[myapp]");
  });

  it("uses default prefix 'swe' when not specified", () => {
    const log = createLogger();
    log.info("hello");
    const output = logSpy.mock.calls[0][0] as string;
    expect(output).toContain("[swe]");
  });
});

// ===========================================================================
// 2. Level filtering
// ===========================================================================

describe("Level filtering", () => {
  it("debug is suppressed at level=info", () => {
    const log = createLogger({ level: "info" });
    log.debug("should not appear");
    expect(logSpy).not.toHaveBeenCalled();
  });

  it("debug is shown at level=debug", () => {
    const log = createLogger({ level: "debug" });
    log.debug("visible");
    expect(logSpy).toHaveBeenCalledTimes(1);
    const output = logSpy.mock.calls[0][0] as string;
    expect(output).toContain("visible");
  });

  it("info is shown at level=info", () => {
    const log = createLogger({ level: "info" });
    log.info("msg");
    expect(logSpy).toHaveBeenCalledTimes(1);
  });

  it("info is suppressed at level=warn", () => {
    const log = createLogger({ level: "warn" });
    log.info("should not appear");
    expect(logSpy).not.toHaveBeenCalled();
  });

  it("warn is shown at level=warn", () => {
    const log = createLogger({ level: "warn" });
    log.warn("warning");
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  it("warn is suppressed at level=error", () => {
    const log = createLogger({ level: "error" });
    log.warn("should not appear");
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it("error is always shown (even at level=error)", () => {
    const log = createLogger({ level: "error" });
    log.error("critical");
    expect(errorSpy).toHaveBeenCalledTimes(1);
  });

  it("all levels shown at level=debug", () => {
    const log = createLogger({ level: "debug" });
    log.debug("d");
    log.info("i");
    log.warn("w");
    log.error("e");
    expect(logSpy).toHaveBeenCalledTimes(2); // debug + info go to console.log
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(errorSpy).toHaveBeenCalledTimes(1);
  });
});

// ===========================================================================
// 3. Output formatting
// ===========================================================================

describe("Output formatting", () => {
  it("info output contains [INFO] tag", () => {
    const log = createLogger();
    log.info("test");
    const output = logSpy.mock.calls[0][0] as string;
    expect(output).toContain("[INFO]");
  });

  it("debug output contains [DEBUG] tag", () => {
    const log = createLogger({ level: "debug" });
    log.debug("test");
    const output = logSpy.mock.calls[0][0] as string;
    expect(output).toContain("[DEBUG]");
  });

  it("warn output contains [WARN] tag", () => {
    const log = createLogger();
    log.warn("test");
    const output = warnSpy.mock.calls[0][0] as string;
    expect(output).toContain("[WARN]");
  });

  it("error output contains [ERROR] tag", () => {
    const log = createLogger();
    log.error("test");
    const output = errorSpy.mock.calls[0][0] as string;
    expect(output).toContain("[ERROR]");
  });

  it("output contains ISO timestamp", () => {
    const log = createLogger();
    log.info("test");
    const output = logSpy.mock.calls[0][0] as string;
    // ISO timestamp pattern: YYYY-MM-DDTHH:MM:SS
    expect(output).toMatch(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  });

  it("output contains the message text", () => {
    const log = createLogger();
    log.info("hello world 42");
    const output = logSpy.mock.calls[0][0] as string;
    expect(output).toContain("hello world 42");
  });

  it("prefix appears in output", () => {
    const log = createLogger({ prefix: "runner" });
    log.info("test");
    const output = logSpy.mock.calls[0][0] as string;
    expect(output).toContain("[runner]");
  });
});

// ===========================================================================
// 4. child() loggers
// ===========================================================================

describe("child()", () => {
  it("child creates a logger with nested prefix", () => {
    const parent = createLogger({ prefix: "swe" });
    const child = parent.child("monitor");
    child.info("scanning");
    const output = logSpy.mock.calls[0][0] as string;
    expect(output).toContain("[swe:monitor]");
  });

  it("multiple children chain prefixes", () => {
    const root = createLogger({ prefix: "swe" });
    const child = root.child("monitor");
    const grandchild = child.child("scan");
    grandchild.info("found issue");
    const output = logSpy.mock.calls[0][0] as string;
    expect(output).toContain("[swe:monitor:scan]");
  });

  it("child inherits parent level", () => {
    const parent = createLogger({ prefix: "swe", level: "warn" });
    const child = parent.child("quiet");
    child.info("should be suppressed");
    expect(logSpy).not.toHaveBeenCalled();
    child.warn("should show");
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  it("child does not affect parent output", () => {
    const parent = createLogger({ prefix: "swe" });
    const _child = parent.child("sub");
    parent.info("parent msg");
    const output = logSpy.mock.calls[0][0] as string;
    expect(output).toContain("[swe]");
    expect(output).not.toContain("[swe:sub]");
  });

  it("child is a full Logger (has all methods)", () => {
    const parent = createLogger();
    const child = parent.child("test");
    expect(typeof child.debug).toBe("function");
    expect(typeof child.info).toBe("function");
    expect(typeof child.warn).toBe("function");
    expect(typeof child.error).toBe("function");
    expect(typeof child.child).toBe("function");
  });
});

// ===========================================================================
// 5. Extra arguments
// ===========================================================================

describe("Extra arguments", () => {
  it("passes extra arguments to console.log", () => {
    const log = createLogger();
    log.info("count:", 42);
    expect(logSpy).toHaveBeenCalledWith(expect.any(String), 42);
  });

  it("passes extra arguments to console.warn", () => {
    const log = createLogger();
    log.warn("obj:", { key: "val" });
    expect(warnSpy).toHaveBeenCalledWith(expect.any(String), { key: "val" });
  });

  it("passes extra arguments to console.error", () => {
    const log = createLogger();
    const err = new Error("boom");
    log.error("failed:", err);
    expect(errorSpy).toHaveBeenCalledWith(expect.any(String), err);
  });
});

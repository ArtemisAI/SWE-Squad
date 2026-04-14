/**
 * Unit tests for the engine layer: base.ts, registry.ts, claude-cli.ts.
 *
 * All subprocess calls are mocked via vi.mock("node:child_process").
 * No actual binaries are invoked.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { EventEmitter } from "node:events";

// ---------------------------------------------------------------------------
// Mock spawn helper — creates a fake ChildProcess that emits given output
// ---------------------------------------------------------------------------

interface MockSpawnOptions {
  stdout?: string;
  stderr?: string;
  exitCode?: number;
  signal?: string | null;
  /** If set, emit an 'error' event on the child process instead of 'close'. */
  error?: { message: string; code?: string };
}

/**
 * Returns a mock `spawn` function. When called, it creates a fake child
 * process backed by EventEmitter that emits the configured stdout/stderr
 * and then closes with the given exit code (or fires an error event).
 *
 * The returned function also exposes `.lastArgs` so tests can inspect
 * what binary + args were passed to spawn.
 */
function createMockSpawn(opts: MockSpawnOptions = {}) {
  const {
    stdout = "",
    stderr = "",
    exitCode = 0,
    signal = null,
    error,
  } = opts;

  const mockSpawnFn = vi.fn((_binary: string, _args: string[], _spawnOpts?: unknown) => {
    const child = new EventEmitter() as EventEmitter & {
      stdin: { write: ReturnType<typeof vi.fn>; end: ReturnType<typeof vi.fn> };
      stdout: EventEmitter & { setEncoding: ReturnType<typeof vi.fn> };
      stderr: EventEmitter & { setEncoding: ReturnType<typeof vi.fn> };
      kill: ReturnType<typeof vi.fn>;
      killed: boolean;
    };

    const stdoutEmitter = new EventEmitter();
    (stdoutEmitter as typeof child.stdout).setEncoding = vi.fn();
    child.stdout = stdoutEmitter as typeof child.stdout;

    const stderrEmitter = new EventEmitter();
    (stderrEmitter as typeof child.stderr).setEncoding = vi.fn();
    child.stderr = stderrEmitter as typeof child.stderr;

    child.stdin = { write: vi.fn(), end: vi.fn() };
    child.killed = false;
    child.kill = vi.fn(() => { child.killed = true; });

    // Schedule async emission so the caller can attach listeners first
    queueMicrotask(() => {
      if (error) {
        const err = new Error(error.message) as Error & { code?: string };
        if (error.code) err.code = error.code;
        child.emit("error", err);
        return;
      }
      if (stdout) child.stdout.emit("data", stdout);
      if (stderr) child.stderr.emit("data", stderr);
      child.emit("close", exitCode, signal);
    });

    return child;
  });

  return mockSpawnFn;
}

/** Module-level mock spawn — replaced per-test via mockSpawnImpl */
let mockSpawnImpl: ReturnType<typeof createMockSpawn> = createMockSpawn();

// ---------------------------------------------------------------------------
// Mocks — must be declared before imports that reference them
// ---------------------------------------------------------------------------

vi.mock("node:child_process", () => ({
  execFileSync: vi.fn(),
  spawn: (...args: unknown[]) => (mockSpawnImpl as Function)(...args),
}));

vi.mock("node:fs", () => ({
  existsSync: vi.fn(() => false),
}));

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";

import {
  createEngineResult,
  isSuccess,
  classifyError,
  type EngineResult,
} from "../../src/providers/engine/base.js";

// Registry is imported dynamically per test group to isolate module state
// We need the types for reference though
import type { CodingEngine } from "../../src/providers/engine/base.js";

// =========================================================================
// 1. base.ts — createEngineResult, isSuccess, classifyError
// =========================================================================

describe("createEngineResult()", () => {
  it("returns correct defaults with no overrides", () => {
    const r = createEngineResult();
    expect(r.stdout).toBe("");
    expect(r.stderr).toBe("");
    expect(r.returncode).toBe(0);
    expect(r.costUsd).toBeNull();
    expect(r.model).toBeNull();
    expect(r.inputTokens).toBeNull();
    expect(r.outputTokens).toBeNull();
    expect(r.cacheReadTokens).toBeNull();
    expect(r.cacheCreationTokens).toBeNull();
    expect(r.numTurns).toBeNull();
    expect(r.durationApiMs).toBeNull();
    expect(r.sessionId).toBeNull();
    expect(r.metadata).toEqual({});
  });

  it("overrides stdout", () => {
    const r = createEngineResult({ stdout: "hello" });
    expect(r.stdout).toBe("hello");
    expect(r.stderr).toBe("");
  });

  it("overrides returncode", () => {
    const r = createEngineResult({ returncode: 1 });
    expect(r.returncode).toBe(1);
  });

  it("overrides costUsd", () => {
    const r = createEngineResult({ costUsd: 0.05 });
    expect(r.costUsd).toBe(0.05);
  });

  it("overrides model and token counts", () => {
    const r = createEngineResult({
      model: "sonnet",
      inputTokens: 1000,
      outputTokens: 500,
    });
    expect(r.model).toBe("sonnet");
    expect(r.inputTokens).toBe(1000);
    expect(r.outputTokens).toBe(500);
  });

  it("overrides metadata", () => {
    const r = createEngineResult({ metadata: { errorType: "timeout" } });
    expect(r.metadata).toEqual({ errorType: "timeout" });
  });

  it("overrides sessionId", () => {
    const r = createEngineResult({ sessionId: "sess-abc" });
    expect(r.sessionId).toBe("sess-abc");
  });

  it("overrides multiple fields simultaneously", () => {
    const r = createEngineResult({
      stdout: "output",
      stderr: "err",
      returncode: 2,
      costUsd: 0.10,
      model: "opus",
      numTurns: 3,
      durationApiMs: 5000,
    });
    expect(r.stdout).toBe("output");
    expect(r.stderr).toBe("err");
    expect(r.returncode).toBe(2);
    expect(r.costUsd).toBe(0.10);
    expect(r.model).toBe("opus");
    expect(r.numTurns).toBe(3);
    expect(r.durationApiMs).toBe(5000);
  });

  it("overrides cache token fields", () => {
    const r = createEngineResult({
      cacheReadTokens: 200,
      cacheCreationTokens: 100,
    });
    expect(r.cacheReadTokens).toBe(200);
    expect(r.cacheCreationTokens).toBe(100);
  });
});

describe("isSuccess()", () => {
  it("returns true for returncode === 0", () => {
    const r = createEngineResult({ returncode: 0 });
    expect(isSuccess(r)).toBe(true);
  });

  it("returns false for returncode === 1", () => {
    const r = createEngineResult({ returncode: 1 });
    expect(isSuccess(r)).toBe(false);
  });

  it("returns false for returncode === -1", () => {
    const r = createEngineResult({ returncode: -1 });
    expect(isSuccess(r)).toBe(false);
  });

  it("returns false for returncode === 127", () => {
    const r = createEngineResult({ returncode: 127 });
    expect(isSuccess(r)).toBe(false);
  });
});

describe("classifyError()", () => {
  it("returns timeout for returncode -1", () => {
    expect(classifyError("", -1)).toBe("timeout");
  });

  it("returns timeout for stderr containing 'timeout'", () => {
    expect(classifyError("Process timed out: Timeout", 1)).toBe("timeout");
  });

  it("returns rate_limit for stderr containing '429'", () => {
    expect(classifyError("HTTP error 429 too many requests", 1)).toBe("rate_limit");
  });

  it("returns rate_limit for stderr containing 'rate limit'", () => {
    expect(classifyError("Rate limit exceeded", 1)).toBe("rate_limit");
  });

  it("returns rate_limit for stderr containing 'rate_limit'", () => {
    expect(classifyError("Error: rate_limit", 1)).toBe("rate_limit");
  });

  it("returns overloaded for stderr containing '529'", () => {
    expect(classifyError("HTTP 529", 1)).toBe("overloaded");
  });

  it("returns overloaded for stderr containing 'overloaded'", () => {
    expect(classifyError("API is overloaded", 1)).toBe("overloaded");
  });

  it("returns overloaded for stderr containing 'capacity'", () => {
    expect(classifyError("At capacity", 1)).toBe("overloaded");
  });

  it("returns server_error for stderr containing '500'", () => {
    expect(classifyError("HTTP error 500", 1)).toBe("server_error");
  });

  it("returns server_error for stderr containing 'internal server error'", () => {
    expect(classifyError("internal server error occurred", 1)).toBe("server_error");
  });

  it("returns server_error for stderr containing 'server error'", () => {
    expect(classifyError("server error", 1)).toBe("server_error");
  });

  it("returns auth_error for stderr containing '401'", () => {
    expect(classifyError("HTTP 401 Unauthorized", 1)).toBe("auth_error");
  });

  it("returns auth_error for stderr containing '403'", () => {
    expect(classifyError("HTTP 403 Forbidden", 1)).toBe("auth_error");
  });

  it("returns auth_error for stderr containing 'unauthorized'", () => {
    expect(classifyError("Unauthorized access", 1)).toBe("auth_error");
  });

  it("returns auth_error for stderr containing 'forbidden'", () => {
    expect(classifyError("forbidden", 1)).toBe("auth_error");
  });

  it("returns model_not_found for stderr containing 'model not found'", () => {
    expect(classifyError("model not found: claude-x", 1)).toBe("model_not_found");
  });

  it("returns model_not_found for stderr containing '404'", () => {
    expect(classifyError("HTTP 404", 1)).toBe("model_not_found");
  });

  it("returns unknown for unrecognized stderr", () => {
    expect(classifyError("something went wrong", 1)).toBe("unknown");
  });

  it("returns unknown for empty stderr with non-zero returncode", () => {
    expect(classifyError("", 1)).toBe("unknown");
  });

  it("timeout takes priority over other patterns (returncode -1)", () => {
    // returncode -1 should classify as timeout even if stderr has other patterns
    expect(classifyError("rate limit 429", -1)).toBe("timeout");
  });

  it("is case-insensitive", () => {
    expect(classifyError("RATE LIMIT exceeded", 1)).toBe("rate_limit");
    expect(classifyError("TIMEOUT", 1)).toBe("timeout");
    expect(classifyError("OVERLOADED", 1)).toBe("overloaded");
  });
});

// =========================================================================
// 2. registry.ts — registerEngine, resolveEngine, listEngines, hasEngine
// =========================================================================

// Import registry functions. The module registers built-ins at load time.
import {
  registerEngine,
  resolveEngine,
  listEngines,
  hasEngine,
} from "../../src/providers/engine/registry.js";

describe("Engine Registry", () => {
  it("has built-in claude-cli engine registered", () => {
    expect(hasEngine("claude-cli")).toBe(true);
  });

  it("listEngines includes claude-cli as built-in engine", () => {
    const engines = listEngines();
    expect(engines).toContain("claude-cli");
    expect(engines.length).toBeGreaterThanOrEqual(1);
  });

  it("hasEngine returns false for unknown engine", () => {
    expect(hasEngine("nonexistent-engine-xyz")).toBe(false);
  });

  it("resolveEngine throws for unknown engine", () => {
    expect(() => resolveEngine("nonexistent-engine-xyz")).toThrow(
      /not registered/,
    );
  });

  it("resolveEngine error message lists available engines", () => {
    expect(() => resolveEngine("nonexistent")).toThrow(/claude-cli/);
  });

  it("registerEngine + resolveEngine round trip with custom engine", () => {
    const mockEngine: CodingEngine = {
      name: "test-engine",
      run: vi.fn(),
      healthCheck: vi.fn(),
    };

    registerEngine("test-engine", () => mockEngine);
    expect(hasEngine("test-engine")).toBe(true);

    const resolved = resolveEngine("test-engine");
    expect(resolved.name).toBe("test-engine");
  });

  it("registerEngine overwrites existing factory", () => {
    const engine1: CodingEngine = {
      name: "overwrite-v1",
      run: vi.fn(),
      healthCheck: vi.fn(),
    };
    const engine2: CodingEngine = {
      name: "overwrite-v2",
      run: vi.fn(),
      healthCheck: vi.fn(),
    };

    registerEngine("overwrite-test", () => engine1);
    expect(resolveEngine("overwrite-test").name).toBe("overwrite-v1");

    registerEngine("overwrite-test", () => engine2);
    expect(resolveEngine("overwrite-test").name).toBe("overwrite-v2");
  });

  it("resolveEngine passes config to factory", () => {
    const factory = vi.fn(() => ({
      name: "config-test",
      run: vi.fn(),
      healthCheck: vi.fn(),
    }));

    registerEngine("config-test", factory);
    resolveEngine("config-test", { myOption: "value" });

    expect(factory).toHaveBeenCalledWith({ myOption: "value" });
  });

  it("resolveEngine passes empty object when no config provided", () => {
    const factory = vi.fn(() => ({
      name: "no-config-test",
      run: vi.fn(),
      healthCheck: vi.fn(),
    }));

    registerEngine("no-config-test", factory);
    resolveEngine("no-config-test");

    expect(factory).toHaveBeenCalledWith({});
  });

  it("claude-cli engine has correct name", () => {
    // Mock findBinary so constructor doesn't throw
    (execFileSync as ReturnType<typeof vi.fn>).mockReturnValueOnce("/usr/bin/claude\n");
    const engine = resolveEngine("claude-cli");
    expect(engine.name).toBe("claude-cli");
  });
});

// =========================================================================
// 3. claude-cli.ts — ClaudeCliEngine (mocked subprocess)
// =========================================================================

import { ClaudeCliEngine } from "../../src/providers/engine/claude-cli.js";

describe("ClaudeCliEngine", () => {
  const mockExecFileSync = execFileSync as ReturnType<typeof vi.fn>;
  const mockExistsSync = existsSync as ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    // Default: findBinary for "which" returns a path
    mockExecFileSync.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === "which") return "/usr/bin/claude\n";
      return "";
    });
    mockExistsSync.mockReturnValue(false);
    // Reset spawn to a default that emits empty stdout and exits 0
    mockSpawnImpl = createMockSpawn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // -----------------------------------------------------------------------
  // Constructor
  // -----------------------------------------------------------------------

  describe("constructor", () => {
    it("uses custom binary when provided", () => {
      const engine = new ClaudeCliEngine({ binary: "/opt/claude" });
      expect(engine.name).toBe("claude-cli");
    });

    it("defaults to finding claude on PATH", () => {
      const engine = new ClaudeCliEngine();
      expect(engine.name).toBe("claude-cli");
    });

    it("falls back to 'claude' string when binary not found", () => {
      mockExecFileSync.mockImplementation((cmd: string) => {
        if (cmd === "which") throw new Error("not found");
        return "";
      });
      // Does not throw — defers error to run()/healthCheck()
      const engine = new ClaudeCliEngine();
      expect(engine.name).toBe("claude-cli");
    });
  });

  // -----------------------------------------------------------------------
  // run() — command building
  // -----------------------------------------------------------------------

  describe("run() command building", () => {
    it("builds correct command with default options", async () => {
      const jsonOutput = JSON.stringify({
        type: "result",
        subtype: "success",
        result: "Hello!",
        cost_usd: 0.01,
        session_id: "sess-123",
      });

      mockSpawnImpl = createMockSpawn({ stdout: jsonOutput });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      await engine.run("test prompt");

      expect(mockSpawnImpl).toHaveBeenCalled();
      const [binary, args] = mockSpawnImpl.mock.calls[0];
      expect(binary).toBe("/usr/bin/claude");
      expect(args).toContain("--model");
      expect(args).toContain("sonnet"); // default model
      expect(args).toContain("--print");
      expect(args).toContain("--output-format");
      expect(args).toContain("json");
    });

    it("includes --dangerously-skip-permissions when allowedTools set", async () => {
      const jsonOutput = JSON.stringify({ result: "ok" });
      mockSpawnImpl = createMockSpawn({ stdout: jsonOutput });

      const engine = new ClaudeCliEngine({
        binary: "/usr/bin/claude",
        allowedTools: "Read,Edit,Bash",
      });
      await engine.run("test prompt");

      const args = mockSpawnImpl.mock.calls[0][1] as string[];
      expect(args).toContain("--dangerously-skip-permissions");
      expect(args).toContain("--allowedTools");
      expect(args).toContain("Read,Edit,Bash");
    });

    it("does not include --dangerously-skip-permissions in strict mode", async () => {
      const jsonOutput = JSON.stringify({ result: "ok" });
      mockSpawnImpl = createMockSpawn({ stdout: jsonOutput });

      const engine = new ClaudeCliEngine({
        binary: "/usr/bin/claude",
        allowedTools: "Read,Edit",
        permissionMode: "strict",
      });
      await engine.run("test prompt");

      const args = mockSpawnImpl.mock.calls[0][1] as string[];
      expect(args).not.toContain("--dangerously-skip-permissions");
    });

    it("uses model override from run options", async () => {
      const jsonOutput = JSON.stringify({ result: "ok" });
      mockSpawnImpl = createMockSpawn({ stdout: jsonOutput });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      await engine.run("prompt", { model: "opus" });

      const args = mockSpawnImpl.mock.calls[0][1] as string[];
      const modelIdx = args.indexOf("--model");
      expect(args[modelIdx + 1]).toBe("opus");
    });
  });

  // -----------------------------------------------------------------------
  // run() — JSON output parsing
  // -----------------------------------------------------------------------

  describe("run() JSON output parsing", () => {
    it("parses JSON output correctly", async () => {
      const jsonOutput = JSON.stringify({
        type: "result",
        subtype: "success",
        result: "Hello world!",
        cost_usd: 0.065,
        duration_ms: 2380,
        duration_api_ms: 2300,
        num_turns: 1,
        session_id: "abc-def-123",
        usage: {
          input_tokens: 500,
          output_tokens: 100,
          cache_read_input_tokens: 50,
          cache_creation_input_tokens: 20,
        },
      });

      mockSpawnImpl = createMockSpawn({ stdout: jsonOutput });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      const result = await engine.run("test");

      expect(result.stdout).toBe("Hello world!");
      expect(result.returncode).toBe(0);
      expect(result.costUsd).toBe(0.065);
      expect(result.numTurns).toBe(1);
      expect(result.durationApiMs).toBe(2300);
      expect(result.sessionId).toBe("abc-def-123");
      expect(result.inputTokens).toBe(500);
      expect(result.outputTokens).toBe(100);
      expect(result.cacheReadTokens).toBe(50);
      expect(result.cacheCreationTokens).toBe(20);
    });

    it("handles non-JSON output gracefully", async () => {
      mockSpawnImpl = createMockSpawn({ stdout: "plain text output\n" });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      const result = await engine.run("test");

      expect(result.stdout).toBe("plain text output");
      expect(result.returncode).toBe(0);
    });

    it("handles empty output", async () => {
      mockSpawnImpl = createMockSpawn({ stdout: "" });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      const result = await engine.run("test");

      expect(result.stdout).toBe("");
      expect(result.returncode).toBe(0);
    });
  });

  // -----------------------------------------------------------------------
  // run() — error handling
  // -----------------------------------------------------------------------

  describe("run() error handling", () => {
    it("handles timeout errors", async () => {
      // Timeout in spawn-based exec: child is killed with SIGTERM
      mockSpawnImpl = createMockSpawn({ exitCode: null as unknown as number, signal: "SIGTERM" });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      const result = await engine.run("test");

      expect(result.returncode).toBe(-1);
      expect(result.stderr).toContain("Timeout");
      expect(result.metadata.errorType).toBe("timeout");
    });

    it("handles binary not found (ENOENT)", async () => {
      // ENOENT comes as an 'error' event on the child process
      mockSpawnImpl = createMockSpawn({ error: { message: "spawn ENOENT", code: "ENOENT" } });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      const result = await engine.run("test");

      expect(result.returncode).toBe(-1);
      expect(result.stderr).toContain("Binary not found");
      expect(result.metadata.errorType).toBe("unknown");
    });

    it("handles non-zero exit with stderr classification", async () => {
      mockSpawnImpl = createMockSpawn({
        stdout: JSON.stringify({ result: "partial" }),
        stderr: "HTTP 429 rate limit exceeded",
        exitCode: 1,
      });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      const result = await engine.run("test");

      expect(result.returncode).toBe(1);
      expect(result.metadata.errorType).toBe("rate_limit");
    });

    it("handles non-zero exit with auth error classification", async () => {
      mockSpawnImpl = createMockSpawn({
        stdout: "",
        stderr: "401 Unauthorized",
        exitCode: 1,
      });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      const result = await engine.run("test");

      expect(result.returncode).toBe(1);
      expect(result.metadata.errorType).toBe("auth_error");
    });

    it("handles unknown errors", async () => {
      // Generic error event (no special code)
      mockSpawnImpl = createMockSpawn({ error: { message: "Something unexpected" } });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      const result = await engine.run("test");

      expect(result.returncode).toBe(-1);
      expect(result.stderr).toContain("Something unexpected");
      expect(result.metadata.errorType).toBe("unknown");
    });
  });

  // -----------------------------------------------------------------------
  // resume()
  // -----------------------------------------------------------------------

  describe("resume()", () => {
    it("adds --resume flag with valid UUID session ID", async () => {
      const jsonOutput = JSON.stringify({ result: "resumed" });
      mockSpawnImpl = createMockSpawn({ stdout: jsonOutput });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      const uuid = "12345678-1234-1234-1234-123456789abc";
      await engine.resume(uuid, "continue");

      const args = mockSpawnImpl.mock.calls[0][1] as string[];
      expect(args).toContain("--resume");
      expect(args).toContain(uuid);
    });

    it("adds --resume without session ID when UUID is invalid", async () => {
      const jsonOutput = JSON.stringify({ result: "resumed" });
      mockSpawnImpl = createMockSpawn({ stdout: jsonOutput });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      await engine.resume("not-a-uuid", "continue");

      const args = mockSpawnImpl.mock.calls[0][1] as string[];
      expect(args).toContain("--resume");
      // Invalid UUID should not appear as an argument after --resume
      expect(args).not.toContain("not-a-uuid");
    });
  });

  // -----------------------------------------------------------------------
  // healthCheck()
  // -----------------------------------------------------------------------

  describe("healthCheck()", () => {
    it("returns true when binary exists on PATH", async () => {
      mockExecFileSync.mockImplementation((cmd: string) => {
        if (cmd === "which") return "/usr/bin/claude\n";
        return "";
      });

      const engine = new ClaudeCliEngine({ binary: "/usr/bin/claude" });
      const healthy = await engine.healthCheck();
      expect(healthy).toBe(true);
    });

    it("returns false when binary not found", async () => {
      mockExecFileSync.mockImplementation((cmd: string) => {
        if (cmd === "which") throw new Error("not found");
        return "";
      });
      mockExistsSync.mockReturnValue(false);

      const engine = new ClaudeCliEngine({ binary: "/nonexistent/claude" });
      const healthy = await engine.healthCheck();
      expect(healthy).toBe(false);
    });

    it("returns true when fallback path exists", async () => {
      mockExecFileSync.mockImplementation((cmd: string) => {
        if (cmd === "which") throw new Error("not found");
        return "";
      });
      mockExistsSync.mockReturnValue(true);

      const engine = new ClaudeCliEngine({ binary: "/usr/local/bin/claude" });
      const healthy = await engine.healthCheck();
      expect(healthy).toBe(true);
    });
  });

  // -----------------------------------------------------------------------
  // Static methods
  // -----------------------------------------------------------------------

  describe("parseJsonOutput (static)", () => {
    it("parses valid JSON output", () => {
      const data = ClaudeCliEngine.parseJsonOutput(
        JSON.stringify({ result: "test", cost_usd: 0.01 }),
      );
      expect(data.result).toBe("test");
      expect(data.cost_usd).toBe(0.01);
    });

    it("returns {result: text} for non-JSON output", () => {
      const data = ClaudeCliEngine.parseJsonOutput("plain text");
      expect(data.result).toBe("plain text");
    });

    it("returns {result: ''} for empty string", () => {
      const data = ClaudeCliEngine.parseJsonOutput("");
      expect(data.result).toBe("");
    });

    it("returns {result: text} for JSON array output", () => {
      const data = ClaudeCliEngine.parseJsonOutput("[1,2,3]");
      expect(data.result).toBe("[1,2,3]");
    });
  });

  describe("parseCostLegacy (static)", () => {
    it("extracts cost from $ notation", () => {
      const cost = ClaudeCliEngine.parseCostLegacy("Total cost: $0.123");
      expect(cost).toBe(0.123);
    });

    it("extracts cost with commas", () => {
      const cost = ClaudeCliEngine.parseCostLegacy("Cost: $1,234.56");
      expect(cost).toBe(1234.56);
    });

    it("returns null when no cost found", () => {
      const cost = ClaudeCliEngine.parseCostLegacy("no cost info here");
      expect(cost).toBeNull();
    });

    it("returns null for empty text", () => {
      const cost = ClaudeCliEngine.parseCostLegacy("");
      expect(cost).toBeNull();
    });
  });
});

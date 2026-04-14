/**
 * Claude Code CLI engine -- concrete CodingEngine implementation.
 *
 * Wraps /usr/bin/claude (or a custom binary path) as a subprocess.
 * Registered in swe_team.yaml under providers.coding_engine.provider: claude.
 *
 * Ported from: src/swe_team/providers/coding_engine/claude.py
 */

import { execFileSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";

import type { CodingEngine, EngineResult, RunOptions } from "./base.js";
import { classifyError, createEngineResult } from "./base.js";

// ---------------------------------------------------------------------------
// Binary resolution helper
// ---------------------------------------------------------------------------

/**
 * Locate a binary on PATH, falling back to a known absolute path.
 *
 * Uses `which` (Unix) or `where` (Windows) to resolve the binary name.
 * Returns the first successful resolution, or the fallback path.
 */
function findBinary(name: string, fallback: string): string {
  try {
    const resolved = execFileSync("which", [name], {
      encoding: "utf-8",
      timeout: 5_000,
    }).trim();
    if (resolved) return resolved;
  } catch {
    // `which` not found or binary not on PATH -- try fallback
  }
  if (existsSync(fallback)) {
    return fallback;
  }
  throw new Error(
    `Binary "${name}" not found on PATH and fallback "${fallback}" does not exist`,
  );
}

// ---------------------------------------------------------------------------
// Claude CLI JSON output shape
// ---------------------------------------------------------------------------

/** Shape of `--output-format json --print` output from Claude CLI. */
interface ClaudeJsonOutput {
  type?: string;
  subtype?: string;
  result?: string;
  cost_usd?: number;
  duration_ms?: number;
  duration_api_ms?: number;
  num_turns?: number;
  session_id?: string;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    cache_read_input_tokens?: number;
    cache_read_tokens?: number;
    cache_creation_input_tokens?: number;
    cache_creation_tokens?: number;
  };
}

// ---------------------------------------------------------------------------
// ClaudeCliEngine
// ---------------------------------------------------------------------------

export interface ClaudeCliEngineOptions {
  /** Path to the claude binary. Resolved via PATH if omitted. */
  binary?: string;
  /** Default model name (e.g. "sonnet"). */
  defaultModel?: string;
  /** Default timeout in seconds. */
  defaultTimeout?: number;
  /** Comma-separated list of allowed tools (e.g. "Read,Edit,Bash"). */
  allowedTools?: string | null;
  /**
   * Permission mode for --dangerously-skip-permissions.
   *   - "strict": never skip (safest).
   *   - "auto": skip only when allowedTools is set (default).
   *   - "bypass": always skip (requires explicit opt-in).
   */
  permissionMode?: "strict" | "auto" | "bypass";
}

export class ClaudeCliEngine implements CodingEngine {
  readonly name = "claude-cli";

  private readonly binary: string;
  private readonly defaultModel: string;
  private readonly defaultTimeout: number;
  private readonly allowedTools: string | null;
  private readonly permissionMode: "strict" | "auto" | "bypass";

  constructor(options?: ClaudeCliEngineOptions) {
    const opts = options ?? {};

    this.defaultModel = opts.defaultModel ?? "sonnet";
    this.defaultTimeout = opts.defaultTimeout ?? 300;
    this.allowedTools = opts.allowedTools ?? null;

    // Default to "bypass" — this engine runs in automation (--print mode),
    // where interactive permission prompts can't be answered.
    const mode = opts.permissionMode ?? "bypass";
    if (mode !== "strict" && mode !== "auto" && mode !== "bypass") {
      throw new Error(
        `permissionMode must be "strict", "auto", or "bypass"; got "${mode as string}"`,
      );
    }
    this.permissionMode = mode;

    // Resolve binary -- allow constructor to throw if truly not found
    if (opts.binary) {
      this.binary = opts.binary;
    } else {
      try {
        this.binary = findBinary("claude", "/usr/bin/claude");
      } catch {
        // Defer the error to run() / healthCheck() so the registry can
        // still instantiate engines on machines without the binary.
        this.binary = "claude";
      }
    }
  }

  // -----------------------------------------------------------------------
  // CodingEngine protocol
  // -----------------------------------------------------------------------

  async run(
    prompt: string,
    options?: RunOptions,
  ): Promise<EngineResult> {
    const model = options?.model ?? this.defaultModel;
    const timeout = (options?.timeout ?? this.defaultTimeout) * 1_000; // ms
    const cwd = options?.cwd;
    const env = options?.env
      ? { ...process.env, ...options.env }
      : undefined;

    const cmd = this.buildCmd(model, {
      sessionId: options?.sessionId,
      resume: false,
      readOnly: options?.readOnly,
    });

    return this.exec(cmd, prompt, { timeout, cwd, env, model });
  }

  /**
   * Resume an existing Claude Code session.
   *
   * Adds --resume <sessionId> so Claude Code continues from the
   * previous conversation state.
   */
  async resume(
    sessionId: string,
    prompt: string,
    options?: RunOptions,
  ): Promise<EngineResult> {
    const model = options?.model ?? this.defaultModel;
    const timeout = (options?.timeout ?? this.defaultTimeout) * 1_000;
    const cwd = options?.cwd;
    const env = options?.env
      ? { ...process.env, ...options.env }
      : undefined;

    const cmd = this.buildCmd(model, {
      sessionId,
      resume: true,
      readOnly: options?.readOnly,
    });

    return this.exec(cmd, prompt, { timeout, cwd, env, model });
  }

  async healthCheck(): Promise<boolean> {
    try {
      findBinary("claude", this.binary);
      return true;
    } catch {
      return false;
    }
  }

  // -----------------------------------------------------------------------
  // Command builder
  // -----------------------------------------------------------------------

  private shouldSkipPermissions(): boolean {
    if (this.permissionMode === "bypass") return true;
    if (this.permissionMode === "auto") return Boolean(this.allowedTools);
    return false; // "strict"
  }

  private buildCmd(
    model: string,
    opts: {
      sessionId?: string;
      resume?: boolean;
      readOnly?: boolean;
    },
  ): string[] {
    const cmd: string[] = [this.binary];

    if (this.shouldSkipPermissions()) {
      cmd.push("--dangerously-skip-permissions");
    }

    cmd.push("--model", model);

    if (this.allowedTools) {
      cmd.push("--allowedTools", this.allowedTools);
    }

    // Session handling -- Claude CLI requires valid UUIDs
    const sid = opts.sessionId;
    const isValidUuid = sid
      ? /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
          sid,
        )
      : false;

    if (isValidUuid && !opts.resume) {
      cmd.push("--session-id", sid!);
    } else if (opts.resume && isValidUuid) {
      cmd.push("--resume", sid!);
    } else if (opts.resume) {
      cmd.push("--resume");
    }

    cmd.push("--print");
    cmd.push("--output-format", "json");

    return cmd;
  }

  // -----------------------------------------------------------------------
  // Execution (async — does NOT block the Node.js event loop)
  // -----------------------------------------------------------------------

  private exec(
    cmd: string[],
    prompt: string,
    opts: {
      timeout: number;
      cwd?: string;
      env?: NodeJS.ProcessEnv;
      model: string;
    },
  ): Promise<EngineResult> {
    const [binary, ...args] = cmd;

    return new Promise<EngineResult>((resolve) => {
      const stdoutChunks: string[] = [];
      const stderrChunks: string[] = [];

      const child = spawn(binary, args, {
        cwd: opts.cwd,
        env: opts.env ?? process.env,
        stdio: ["pipe", "pipe", "pipe"],
      });

      child.stdout.setEncoding("utf-8");
      child.stderr.setEncoding("utf-8");
      child.stdout.on("data", (chunk: string) => stdoutChunks.push(chunk));
      child.stderr.on("data", (chunk: string) => stderrChunks.push(chunk));

      // Write prompt to stdin, then close
      if (prompt) {
        child.stdin.write(prompt);
      }
      child.stdin.end();

      // Enforce timeout
      const timer = setTimeout(() => {
        child.kill("SIGTERM");
        // Give 5s to clean up, then SIGKILL
        setTimeout(() => {
          if (!child.killed) child.kill("SIGKILL");
        }, 5_000);
      }, opts.timeout);

      child.on("close", (code, signal) => {
        clearTimeout(timer);
        const stdout = stdoutChunks.join("");
        const stderr = stderrChunks.join("");
        const returncode = signal ? -1 : (code ?? 1);

        if (returncode === 0) {
          const data = ClaudeCliEngine.parseJsonOutput(stdout);
          resolve(ClaudeCliEngine.buildEngineResult(data, stdout, stderr, 0, opts.model));
        } else if (signal === "SIGTERM" || signal === "SIGKILL") {
          resolve(createEngineResult({
            stdout,
            stderr: stderr || `Timeout after ${opts.timeout / 1000}s`,
            returncode: -1,
            model: opts.model,
            metadata: { errorType: "timeout" },
          }));
        } else {
          const data = ClaudeCliEngine.parseJsonOutput(stdout);
          const result = ClaudeCliEngine.buildEngineResult(data, stdout, stderr, returncode, opts.model);
          result.metadata.errorType = classifyError(stderr, returncode);
          resolve(result);
        }
      });

      child.on("error", (err) => {
        clearTimeout(timer);
        if (isNodeError(err) && err.code === "ENOENT") {
          resolve(createEngineResult({
            stderr: `Binary not found: ${binary}`,
            returncode: -1,
            model: opts.model,
            metadata: { errorType: "unknown" },
          }));
        } else {
          resolve(createEngineResult({
            stderr: err.message,
            returncode: -1,
            model: opts.model,
            metadata: { errorType: "unknown" },
          }));
        }
      });
    });
  }

  // -----------------------------------------------------------------------
  // JSON output parsing
  // -----------------------------------------------------------------------

  /**
   * Parse JSON output from `--output-format json --print`.
   *
   * The CLI emits a single JSON object on stdout:
   * ```json
   * {"type":"result","subtype":"success","cost_usd":0.065,
   *  "duration_ms":2380,"duration_api_ms":2300,"num_turns":1,
   *  "result":"Hello!","session_id":"...","usage":{...}}
   * ```
   *
   * On parse failure, returns `{result: rawStdout}` so the caller
   * still gets the text.
   */
  static parseJsonOutput(rawStdout: string): ClaudeJsonOutput {
    const text = rawStdout.trim();
    if (!text) return { result: "" };
    try {
      const data: unknown = JSON.parse(text);
      if (data !== null && typeof data === "object" && !Array.isArray(data)) {
        return data as ClaudeJsonOutput;
      }
      return { result: text };
    } catch {
      return { result: text };
    }
  }

  /**
   * Build an EngineResult from parsed JSON output dict.
   */
  static buildEngineResult(
    data: ClaudeJsonOutput,
    rawStdout: string,
    stderr: string,
    returncode: number,
    model: string,
  ): EngineResult {
    // Extract text result
    let textResult = data.result ?? rawStdout;
    if (typeof textResult !== "string") {
      textResult = textResult != null ? String(textResult) : "";
    }

    // Extract cost -- prefer JSON field, fall back to stderr regex
    let cost: number | null = data.cost_usd ?? null;
    if (cost == null) {
      cost =
        ClaudeCliEngine.parseCostLegacy(stderr) ??
        ClaudeCliEngine.parseCostLegacy(rawStdout);
    }

    // Extract usage tokens
    const usage = data.usage ?? {};
    const inputTokens = usage.input_tokens ?? null;
    const outputTokens = usage.output_tokens ?? null;
    const cacheReadTokens =
      usage.cache_read_input_tokens ?? usage.cache_read_tokens ?? null;
    const cacheCreationTokens =
      usage.cache_creation_input_tokens ??
      usage.cache_creation_tokens ??
      null;

    return {
      stdout: textResult,
      stderr,
      returncode,
      costUsd: cost != null ? Number(cost) : null,
      model,
      inputTokens: inputTokens != null ? Number(inputTokens) : null,
      outputTokens: outputTokens != null ? Number(outputTokens) : null,
      cacheReadTokens: cacheReadTokens != null ? Number(cacheReadTokens) : null,
      cacheCreationTokens:
        cacheCreationTokens != null ? Number(cacheCreationTokens) : null,
      numTurns: data.num_turns != null ? Number(data.num_turns) : null,
      durationApiMs:
        data.duration_api_ms != null ? Number(data.duration_api_ms) : null,
      sessionId: data.session_id ?? null,
      metadata: {},
    };
  }

  // -----------------------------------------------------------------------
  // Legacy cost parsing (fallback)
  // -----------------------------------------------------------------------

  /**
   * Extract a dollar cost from Claude CLI verbose/stderr output.
   *
   * Legacy fallback for when JSON parsing does not yield a cost_usd field.
   */
  static parseCostLegacy(text: string): number | null {
    for (const line of text.split("\n")) {
      if (!line.toLowerCase().includes("cost")) continue;
      const match = line.match(/\$([0-9,]+(?:\.[0-9]+)?)/);
      if (match?.[1]) {
        const parsed = parseFloat(match[1].replace(/,/g, ""));
        if (!isNaN(parsed)) return parsed;
      }
    }
    return null;
  }
}

// ---------------------------------------------------------------------------
// Type guards for node:child_process errors
// ---------------------------------------------------------------------------

interface NodeError extends Error {
  code?: string;
}

function isNodeError(err: unknown): err is NodeError {
  return err instanceof Error && "code" in err;
}

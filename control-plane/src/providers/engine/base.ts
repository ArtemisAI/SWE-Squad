/**
 * CodingEngine interface -- pluggable coding agent backend.
 *
 * Implement this to swap Claude Code CLI for any other agent
 * (Gemini CLI, OpenCode, OpenHands, GitHub Copilot, Pi, etc.)
 * without changing any core investigator or developer logic.
 *
 * Ported from: src/swe_team/providers/coding_engine/base.py
 */

// ---------------------------------------------------------------------------
// EngineResult
// ---------------------------------------------------------------------------

export interface EngineResult {
  /** Text output from the engine (the "result" field in JSON mode). */
  stdout: string;
  /** Diagnostic / error output from the engine process. */
  stderr: string;
  /** Process exit code. 0 = success. */
  returncode: number;
  /** Total cost in USD for this invocation, if reported. */
  costUsd: number | null;
  /** Model name used for this invocation. */
  model: string | null;
  /** Input tokens consumed (prompt + context). */
  inputTokens: number | null;
  /** Output tokens generated. */
  outputTokens: number | null;
  /** Tokens served from prompt cache. */
  cacheReadTokens: number | null;
  /** Tokens written into prompt cache. */
  cacheCreationTokens: number | null;
  /** Number of agentic turns taken. */
  numTurns: number | null;
  /** Wall-clock time spent in API calls (ms). */
  durationApiMs: number | null;
  /** Session ID for conversation continuity. */
  sessionId: string | null;
  /** Arbitrary provider-specific metadata. */
  metadata: Record<string, unknown>;
}

/**
 * Returns true when the engine exited cleanly (returncode === 0).
 */
export function isSuccess(result: EngineResult): boolean {
  return result.returncode === 0;
}

/**
 * Factory that creates an EngineResult with sensible defaults.
 *
 * All fields default to empty/zero/null. Pass partial overrides to
 * customise specific fields.
 */
export function createEngineResult(
  overrides?: Partial<EngineResult>,
): EngineResult {
  return {
    stdout: "",
    stderr: "",
    returncode: 0,
    costUsd: null,
    model: null,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
    cacheCreationTokens: null,
    numTurns: null,
    durationApiMs: null,
    sessionId: null,
    metadata: {},
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// RunOptions
// ---------------------------------------------------------------------------

export interface RunOptions {
  /** Model override (e.g. "sonnet", "opus", "haiku"). */
  model?: string;
  /** Timeout in seconds. */
  timeout?: number;
  /** Working directory for the engine process. */
  cwd?: string;
  /** Extra environment variables merged into the process env. */
  env?: Record<string, string>;
  /** Session ID for conversation continuity / resume. */
  sessionId?: string;
  /** When true, engine should operate in read-only mode (no file writes). */
  readOnly?: boolean;
}

// ---------------------------------------------------------------------------
// CodingEngine interface
// ---------------------------------------------------------------------------

export interface CodingEngine {
  /** Provider identifier (e.g. "claude-cli", "gemini", "opencode"). */
  readonly name: string;

  /**
   * Run a prompt through the coding agent. Returns structured result.
   *
   * Implementations should catch timeouts and binary-not-found errors
   * and return an EngineResult with a non-zero returncode rather than
   * throwing, unless the error is truly unrecoverable.
   */
  run(prompt: string, options?: RunOptions): Promise<EngineResult>;

  /**
   * Return true if the engine binary/API is reachable and functional.
   */
  healthCheck(): Promise<boolean>;
}

// ---------------------------------------------------------------------------
// Error type classification
// ---------------------------------------------------------------------------

/**
 * Categorised engine error types for programmatic reaction
 * (rate-limiting backoff, fallback routing, alerting, etc.)
 */
export type EngineErrorType =
  | "rate_limit"
  | "overloaded"
  | "server_error"
  | "auth_error"
  | "model_not_found"
  | "timeout"
  | "unknown";

/**
 * Classify a non-zero exit into a named error category.
 *
 * Used to populate `EngineResult.metadata.errorType` so callers and the
 * rate-limiter can react to specific failure modes without parsing raw
 * stderr text.
 *
 * Pattern matching:
 *   - 429 or "rate limit"  -> rate_limit
 *   - 401/403              -> auth_error
 *   - 529 or "overloaded"  -> overloaded
 *   - 500 or "server error"-> server_error
 *   - "model not found"/404-> model_not_found
 *   - timeout / rc -1      -> timeout
 *   - everything else      -> unknown
 */
export function classifyError(
  stderr: string,
  returncode: number,
): EngineErrorType {
  const msg = stderr.toLowerCase();

  if (returncode === -1 || msg.includes("timeout")) {
    return "timeout";
  }
  if (
    msg.includes("rate limit") ||
    msg.includes("rate_limit") ||
    msg.includes("429")
  ) {
    return "rate_limit";
  }
  if (
    msg.includes("overloaded") ||
    msg.includes("529") ||
    msg.includes("capacity")
  ) {
    return "overloaded";
  }
  if (
    msg.includes("500") ||
    msg.includes("internal server error") ||
    msg.includes("server error")
  ) {
    return "server_error";
  }
  if (
    msg.includes("401") ||
    msg.includes("403") ||
    msg.includes("unauthorized") ||
    msg.includes("forbidden")
  ) {
    return "auth_error";
  }
  if (msg.includes("model not found") || msg.includes("404")) {
    return "model_not_found";
  }
  return "unknown";
}

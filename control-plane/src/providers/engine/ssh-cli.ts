/**
 * SSH CLI engine — runs Claude Code CLI on a remote VM via SSH.
 *
 * Enables multi-squad dispatch: the manager can delegate investigations
 * and development to remote worker VMs by running
 * `ssh <alias> claude --print ...` instead of local exec.
 *
 * Registered in the engine registry as "ssh-cli-<alias>" (e.g.
 * "ssh-cli-beta", "ssh-cli-gamma"). Configure SSH aliases in your
 * workers SSH config file (see config/ssh_workers.conf.example).
 */

import { execFileSync, spawn } from "node:child_process";

import type { CodingEngine, EngineResult, RunOptions } from "./base.js";
import { classifyError, createEngineResult } from "./base.js";

// ---------------------------------------------------------------------------
// SSH CLI Engine
// ---------------------------------------------------------------------------

export interface SshCliEngineOptions {
  /** SSH alias (must match a Host entry in ssh config). */
  sshAlias: string;
  /** Remote path to the claude binary. Defaults to "claude". */
  remoteBinary?: string;
  /** Default model name. */
  defaultModel?: string;
  /** Default timeout in seconds. */
  defaultTimeout?: number;
  /** Remote working directory. */
  remoteCwd?: string;
  /** SSH config file path. */
  sshConfig?: string;
}

export class SshCliEngine implements CodingEngine {
  readonly name: string;

  private readonly sshAlias: string;
  private readonly remoteBinary: string;
  private readonly defaultModel: string;
  private readonly defaultTimeout: number;
  private readonly remoteCwd: string | null;
  private readonly sshConfig: string | null;

  constructor(options: SshCliEngineOptions) {
    this.sshAlias = options.sshAlias;
    this.name = `ssh-cli-${options.sshAlias}`;
    this.remoteBinary = options.remoteBinary ?? "claude";
    this.defaultModel = options.defaultModel ?? "sonnet";
    this.defaultTimeout = options.defaultTimeout ?? 600;
    this.remoteCwd = options.remoteCwd ?? null;
    this.sshConfig = options.sshConfig ?? null;
  }

  async run(prompt: string, options?: RunOptions): Promise<EngineResult> {
    const model = options?.model ?? this.defaultModel;
    const timeout = (options?.timeout ?? this.defaultTimeout) * 1_000;
    const cwd = options?.cwd ?? this.remoteCwd;

    // Build remote command
    const remoteCmd = this.buildRemoteCmd(model, {
      cwd,
      readOnly: options?.readOnly,
    });

    // Build SSH args
    const sshArgs: string[] = [];
    if (this.sshConfig) {
      sshArgs.push("-F", this.sshConfig);
    }
    sshArgs.push(this.sshAlias, remoteCmd);

    return new Promise<EngineResult>((resolve) => {
      const stdoutChunks: string[] = [];
      const stderrChunks: string[] = [];

      const child = spawn("ssh", sshArgs, {
        stdio: ["pipe", "pipe", "pipe"],
      });

      child.stdout.setEncoding("utf-8");
      child.stderr.setEncoding("utf-8");
      child.stdout.on("data", (chunk: string) => stdoutChunks.push(chunk));
      child.stderr.on("data", (chunk: string) => stderrChunks.push(chunk));

      if (prompt) child.stdin.write(prompt);
      child.stdin.end();

      const timer = setTimeout(() => {
        child.kill("SIGTERM");
        setTimeout(() => { if (!child.killed) child.kill("SIGKILL"); }, 5_000);
      }, timeout);

      child.on("close", (code, signal) => {
        clearTimeout(timer);
        const stdout = stdoutChunks.join("");
        const stderr = stderrChunks.join("");

        if (signal === "SIGTERM" || signal === "SIGKILL") {
          resolve(createEngineResult({
            stdout, stderr: stderr || `Timeout after ${timeout / 1000}s`,
            returncode: -1, model,
            metadata: { errorType: "timeout", sshAlias: this.sshAlias },
          }));
        } else if ((code ?? 1) === 0) {
          resolve(this.parseOutput(stdout, model));
        } else {
          resolve(createEngineResult({
            stdout, stderr, returncode: code ?? 1, model,
            metadata: { errorType: classifyError(stderr, code ?? 1), sshAlias: this.sshAlias },
          }));
        }
      });

      child.on("error", (err) => {
        clearTimeout(timer);
        resolve(createEngineResult({
          stderr: err.message, returncode: -1, model,
          metadata: { errorType: "unknown", sshAlias: this.sshAlias },
        }));
      });
    });
  }

  async healthCheck(): Promise<boolean> {
    try {
      const sshArgs: string[] = [];
      if (this.sshConfig) {
        sshArgs.push("-F", this.sshConfig);
      }
      sshArgs.push(
        "-o", "ConnectTimeout=2",
        "-o", "BatchMode=yes",
        this.sshAlias,
        "which claude",
      );

      execFileSync("ssh", sshArgs, {
        encoding: "utf-8",
        timeout: 3_000,
        stdio: ["pipe", "pipe", "pipe"],
      });
      return true;
    } catch {
      return false;
    }
  }

  // -----------------------------------------------------------------------
  // Internals
  // -----------------------------------------------------------------------

  private buildRemoteCmd(
    model: string,
    opts: { cwd?: string | null; readOnly?: boolean },
  ): string {
    const parts: string[] = [];

    if (opts.cwd) {
      parts.push(`cd ${this.shellEscape(opts.cwd)} &&`);
    }

    parts.push(this.remoteBinary);
    parts.push("--model", model);
    parts.push("--print");
    parts.push("--output-format", "json");

    return parts.join(" ");
  }

  private parseOutput(raw: string, model: string): EngineResult {
    // Claude CLI --output-format json returns a JSON object
    try {
      const parsed = JSON.parse(raw.trim()) as Record<string, unknown>;
      return createEngineResult({
        stdout: (parsed.result as string) ?? raw,
        returncode: 0,
        model: model,
        costUsd: (parsed.cost_usd as number) ?? null,
        numTurns: (parsed.num_turns as number) ?? null,
        durationApiMs: (parsed.duration_api_ms as number) ?? null,
        sessionId: (parsed.session_id as string) ?? null,
        inputTokens: (parsed.usage as Record<string, number>)?.input_tokens ?? null,
        outputTokens: (parsed.usage as Record<string, number>)?.output_tokens ?? null,
        metadata: { sshAlias: this.sshAlias, raw: parsed },
      });
    } catch {
      // Non-JSON output — return as-is
      return createEngineResult({
        stdout: raw,
        returncode: 0,
        model,
        metadata: { sshAlias: this.sshAlias },
      });
    }
  }

  private shellEscape(s: string): string {
    return `'${s.replace(/'/g, "'\\''")}'`;
  }
}

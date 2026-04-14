/**
 * Git Worktree workspace provider.
 *
 * Creates isolated workspaces via `git worktree add`, each on its own
 * branch rooted from the current HEAD. Worktrees share the object store
 * of the parent repo so they're fast and space-efficient.
 *
 * Handles:
 * - Auto-generated branch names from ticketId
 * - Configurable base directory
 * - Expiry-based cleanup
 * - Branch-already-exists and repo-not-found errors
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";

import type {
  WorkspaceProvider,
  WorkspaceInfo,
  WorkspaceCreateOpts,
} from "./base.js";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface WorktreeProviderConfig {
  /** Base directory for worktrees. Default: /tmp/swe-ws */
  baseDir?: string;
  /** Working directory of the parent git repo. */
  repoCwd?: string;
  /** Default timeout in seconds for workspace expiry. Default: 3600 */
  defaultTimeout?: number;
  /** Maximum concurrent workspaces. Default: 5 */
  maxConcurrent?: number;
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

export class WorktreeProvider implements WorkspaceProvider {
  readonly name = "worktree";

  private readonly baseDir: string;
  private readonly repoCwd: string;
  private readonly defaultTimeout: number;
  private readonly maxConcurrent: number;
  private readonly workspaces = new Map<string, WorkspaceInfo>();

  constructor(config?: WorktreeProviderConfig) {
    this.baseDir = resolve(config?.baseDir ?? "/tmp/swe-ws");
    this.repoCwd = config?.repoCwd ?? process.cwd();
    this.defaultTimeout = config?.defaultTimeout ?? 3600;
    this.maxConcurrent = config?.maxConcurrent ?? 5;
  }

  async create(opts: WorkspaceCreateOpts): Promise<WorkspaceInfo> {
    // Enforce concurrency limit
    const activeCount = Array.from(this.workspaces.values()).filter(
      (ws) => ws.status === "active",
    ).length;
    if (activeCount >= this.maxConcurrent) {
      throw new Error(
        `Maximum concurrent workspaces reached (${this.maxConcurrent}). ` +
          `Clean up existing workspaces first.`,
      );
    }

    const branch = opts.branch ?? `fix/${opts.ticketId}`;
    const safeName = branch.replace(/\//g, "-");
    const wsPath = resolve(opts.baseDir ?? this.baseDir, safeName);
    const id = randomUUID();

    // Ensure base directory exists
    const parentDir = opts.baseDir ?? this.baseDir;
    if (!existsSync(parentDir)) {
      mkdirSync(parentDir, { recursive: true });
    }

    // Check if worktree path already exists
    if (existsSync(wsPath)) {
      throw new Error(
        `Workspace path already exists: ${wsPath}. ` +
          `Clean it up or use a different branch name.`,
      );
    }

    try {
      execFileSync("git", ["worktree", "add", wsPath, "-b", branch], {
        cwd: this.repoCwd,
        timeout: 30_000,
        stdio: "pipe",
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const stderr =
        (err as { stderr?: Buffer })?.stderr?.toString() ?? message;

      // Branch already exists — try adding worktree without -b
      if (
        stderr.includes("already exists") &&
        stderr.includes("branch")
      ) {
        try {
          execFileSync("git", ["worktree", "add", wsPath, branch], {
            cwd: this.repoCwd,
            timeout: 30_000,
            stdio: "pipe",
          });
        } catch (retryErr) {
          const retryMsg =
            retryErr instanceof Error ? retryErr.message : String(retryErr);
          throw new Error(`Failed to create worktree (retry): ${retryMsg}`);
        }
      } else {
        throw new Error(`Failed to create worktree: ${stderr}`);
      }
    }

    const now = new Date();
    const timeout = opts.timeout ?? this.defaultTimeout;

    const workspace: WorkspaceInfo = {
      id,
      ticketId: opts.ticketId,
      path: wsPath,
      branch,
      repo: opts.repo,
      strategy: "worktree",
      createdAt: now.toISOString(),
      expiresAt: new Date(now.getTime() + timeout * 1000).toISOString(),
      status: "active",
    };

    this.workspaces.set(id, workspace);
    return workspace;
  }

  async get(id: string): Promise<WorkspaceInfo | null> {
    return this.workspaces.get(id) ?? null;
  }

  async list(): Promise<WorkspaceInfo[]> {
    return Array.from(this.workspaces.values());
  }

  async cleanup(id: string): Promise<boolean> {
    const ws = this.workspaces.get(id);
    if (!ws) return false;

    ws.status = "cleaning";

    try {
      if (existsSync(ws.path)) {
        try {
          execFileSync("git", ["worktree", "remove", ws.path, "--force"], {
            cwd: this.repoCwd,
            timeout: 30_000,
            stdio: "pipe",
          });
        } catch {
          // Fallback: if git worktree remove fails (e.g. not a valid worktree),
          // remove the directory directly
          rmSync(ws.path, { recursive: true, force: true });
        }
      }

      this.workspaces.delete(id);
      return true;
    } catch {
      ws.status = "error";
      return false;
    }
  }

  async cleanupExpired(): Promise<number> {
    const now = Date.now();
    let cleaned = 0;

    for (const [id, ws] of this.workspaces) {
      if (ws.expiresAt && new Date(ws.expiresAt).getTime() <= now) {
        ws.status = "expired";
        const ok = await this.cleanup(id);
        if (ok) cleaned++;
      }
    }

    return cleaned;
  }

  async healthCheck(): Promise<boolean> {
    try {
      execFileSync("git", ["--version"], {
        timeout: 5_000,
        stdio: "pipe",
      });
      return true;
    } catch {
      return false;
    }
  }
}

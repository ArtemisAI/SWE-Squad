/**
 * Local workspace provider — passthrough for in-place development.
 *
 * Uses the existing working directory as-is, with no isolation.
 * Suitable for single-ticket workflows or when workspace isolation
 * is handled externally (e.g. the repo is already on the right branch).
 *
 * create() still creates a branch (via git checkout -b) for tracking,
 * but does not move files or create a separate worktree.
 */

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { randomUUID } from "node:crypto";

import type {
  WorkspaceProvider,
  WorkspaceInfo,
  WorkspaceCreateOpts,
} from "./base.js";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface LocalProviderConfig {
  /** Default working directory. Falls back to process.cwd(). */
  cwd?: string;
  /** Default timeout in seconds. Default: 3600 */
  defaultTimeout?: number;
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

export class LocalProvider implements WorkspaceProvider {
  readonly name = "local";

  private readonly cwd: string;
  private readonly defaultTimeout: number;
  private readonly workspaces = new Map<string, WorkspaceInfo>();

  constructor(config?: LocalProviderConfig) {
    this.cwd = resolve(config?.cwd ?? process.cwd());
    this.defaultTimeout = config?.defaultTimeout ?? 3600;
  }

  async create(opts: WorkspaceCreateOpts): Promise<WorkspaceInfo> {
    const branch = opts.branch ?? `fix/${opts.ticketId}`;
    const wsPath = resolve(opts.baseDir ?? this.cwd);
    const id = randomUUID();

    // Optionally create the branch (best-effort — may already exist)
    if (existsSync(wsPath)) {
      try {
        execFileSync("git", ["checkout", "-b", branch], {
          cwd: wsPath,
          timeout: 10_000,
          stdio: "pipe",
        });
      } catch {
        // Branch may already exist — try switching to it
        try {
          execFileSync("git", ["checkout", branch], {
            cwd: wsPath,
            timeout: 10_000,
            stdio: "pipe",
          });
        } catch {
          // Not a git repo or other issue — continue without branching.
          // Local provider is a passthrough, so this is acceptable.
        }
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
      strategy: "local",
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

    // Local provider does NOT delete the directory — it's the user's repo.
    // Just remove from tracking.
    this.workspaces.delete(id);
    return true;
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
    // Local provider is always healthy — it just uses the filesystem.
    return true;
  }
}

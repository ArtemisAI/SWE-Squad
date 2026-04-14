/**
 * WorkspaceProvider interface -- pluggable workspace provisioning backend.
 *
 * Implement this to swap git worktrees for any other isolation strategy
 * (clone, Docker, local passthrough) without changing tool or orchestration code.
 */

// ---------------------------------------------------------------------------
// WorkspaceInfo
// ---------------------------------------------------------------------------

export type WorkspaceStrategy = "worktree" | "clone" | "docker" | "local";
export type WorkspaceStatus = "active" | "cleaning" | "expired" | "error";

export interface WorkspaceInfo {
  /** Unique workspace identifier. */
  id: string;
  /** Ticket this workspace was created for. */
  ticketId: string;
  /** Absolute filesystem path to the workspace root. */
  path: string;
  /** Git branch associated with this workspace. */
  branch: string;
  /** Repository identifier (e.g. "owner/repo"). */
  repo: string;
  /** Provisioning strategy used. */
  strategy: WorkspaceStrategy;
  /** ISO-8601 creation timestamp. */
  createdAt: string;
  /** ISO-8601 expiration timestamp, if a timeout was set. */
  expiresAt?: string;
  /** Engine currently using this workspace, if any. */
  engineId?: string;
  /** Current workspace lifecycle status. */
  status: WorkspaceStatus;
}

// ---------------------------------------------------------------------------
// WorkspaceCreateOpts
// ---------------------------------------------------------------------------

export interface WorkspaceCreateOpts {
  /** Ticket ID this workspace is for. */
  ticketId: string;
  /** Repository identifier (e.g. "owner/repo"). */
  repo: string;
  /** Branch name. Auto-generated from ticketId if omitted. */
  branch?: string;
  /** Provisioning strategy override. */
  strategy?: WorkspaceStrategy;
  /** Base directory to create workspace in. */
  baseDir?: string;
  /** Maximum workspace lifetime in seconds. */
  timeout?: number;
}

// ---------------------------------------------------------------------------
// WorkspaceProvider interface
// ---------------------------------------------------------------------------

export interface WorkspaceProvider {
  /** Provider identifier (e.g. "worktree", "local", "docker"). */
  readonly name: string;

  /**
   * Provision a new isolated workspace.
   *
   * Implementations should handle missing repos (clone first), existing
   * branches (error or reuse), and filesystem errors gracefully.
   */
  create(opts: WorkspaceCreateOpts): Promise<WorkspaceInfo>;

  /**
   * Look up a workspace by its ID.
   * Returns null if the workspace doesn't exist or has already been cleaned up.
   */
  get(id: string): Promise<WorkspaceInfo | null>;

  /**
   * List all tracked workspaces (active + expired).
   */
  list(): Promise<WorkspaceInfo[]>;

  /**
   * Clean up a specific workspace by ID.
   * Returns true if the workspace was found and removed, false otherwise.
   */
  cleanup(id: string): Promise<boolean>;

  /**
   * Clean up all workspaces past their expiration time.
   * Returns the number of workspaces cleaned up.
   */
  cleanupExpired(): Promise<number>;

  /**
   * Return true if the provider is functional (e.g. git is available).
   */
  healthCheck(): Promise<boolean>;
}

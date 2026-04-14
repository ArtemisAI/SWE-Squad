/**
 * EnginePlugin interface -- workspace-aware engine extensions.
 *
 * Engine plugins hook into the workspace lifecycle to configure
 * a CodingEngine for a specific workspace before/after execution.
 * This keeps workspace setup logic out of the core engine interface.
 */

import type { WorkspaceInfo } from "../workspace/base.js";

export interface EnginePlugin {
  /** Plugin identifier. */
  readonly name: string;

  /**
   * Called before engine.run() to prepare the workspace.
   *
   * Typical use: set cwd, configure env vars, install dependencies,
   * or apply engine-specific workspace settings.
   */
  beforeRun(
    workspace: WorkspaceInfo,
    engineConfig: Record<string, unknown>,
  ): Promise<void>;

  /**
   * Called after engine.run() to collect artifacts.
   *
   * Typical use: gather diffs, test results, coverage reports,
   * or update workspace metadata with run outcomes.
   */
  afterRun(
    workspace: WorkspaceInfo,
    result: unknown,
  ): Promise<void>;

  /**
   * Plugin-specific health check.
   * Returns true if the plugin's dependencies are available.
   */
  healthCheck(): Promise<boolean>;
}

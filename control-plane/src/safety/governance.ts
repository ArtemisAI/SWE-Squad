/**
 * CI/CD Governance for the Autonomous SWE Team.
 *
 * Provides deployment rules, rollback logic, and integration checks.
 * Works alongside the Ralph-Wiggum stability gate to enforce:
 *
 * - Sandboxed testing before production injection
 * - Automated rollback on post-deploy regressions
 * - Audit trail for every deployment decision
 *
 * Ported from: src/swe_team/governance.py
 */

import crypto from "node:crypto";
import path from "node:path";
import type { StabilityReport } from "../models/ticket.js";
import { GovernanceVerdict } from "../models/ticket.js";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Files that indicate a dependency change. */
export const DEPENDENCY_FILES: ReadonlySet<string> = new Set([
  "requirements.txt",
  "requirements.in",
  "pyproject.toml",
  "poetry.lock",
  "Pipfile",
  "Pipfile.lock",
  "setup.cfg",
  "setup.py",
  "package.json",
  "package-lock.json",
  "pnpm-lock.yaml",
]);

// ---------------------------------------------------------------------------
// Fix complexity checker
// ---------------------------------------------------------------------------

export interface CheckFixComplexityOptions {
  maxFiles?: number;
  maxLines?: number;
  allowedModules?: Set<string>;
  allowDependencyChanges?: boolean;
}

/**
 * Validate fix complexity against SWE team constraints.
 *
 * @param filesChanged  Relative file paths from `git diff --name-only`.
 * @param linesChanged  Total lines changed (added + removed).
 * @param options       Optional validation parameters.
 * @returns `{ valid, reason }` where reason is "ok" on success or explains the failure.
 */
export function checkFixComplexity(
  filesChanged: string[],
  linesChanged: number,
  options?: CheckFixComplexityOptions,
): { valid: boolean; reason: string } {
  const maxFiles = options?.maxFiles ?? 5;
  const maxLines = options?.maxLines ?? 200;
  const allowedModules = options?.allowedModules ?? undefined;
  const allowDependencyChanges = options?.allowDependencyChanges ?? false;

  if (filesChanged.length === 0) {
    return { valid: false, reason: "No files changed" };
  }
  if (filesChanged.length > maxFiles) {
    return {
      valid: false,
      reason: `Too many files changed (${filesChanged.length} > ${maxFiles})`,
    };
  }
  if (linesChanged > maxLines) {
    return {
      valid: false,
      reason: `Too many lines changed (${linesChanged} > ${maxLines})`,
    };
  }

  // Normalize paths to posix and check for dependency file changes
  const normalized = new Set(
    filesChanged.map((f) => f.split(path.sep).join("/")),
  );
  if (!allowDependencyChanges) {
    for (const depFile of DEPENDENCY_FILES) {
      if (normalized.has(depFile)) {
        return {
          valid: false,
          reason: "Dependency changes are not allowed",
        };
      }
    }
  }

  // Module extraction and cross-module check
  const modules = new Set(filesChanged.map(moduleForPath));
  if (allowedModules && allowedModules.size > 0) {
    const allowed = new Set(allowedModules);
    allowed.add("tests");
    const extra = [...modules].filter((m) => !allowed.has(m));
    if (extra.length > 0) {
      return {
        valid: false,
        reason: `Cross-module changes detected: ${extra.sort().join(", ")}`,
      };
    }
  } else {
    const coreModules = [...modules].filter((m) => m !== "tests");
    if (coreModules.length > 1) {
      return { valid: false, reason: "Cross-module changes detected" };
    }
  }

  return { valid: true, reason: "ok" };
}

/**
 * Extract the top-level module name from a file path.
 */
function moduleForPath(filePath: string): string {
  const parts = filePath.split("/").filter(Boolean);
  if (parts.length === 0) {
    return "unknown";
  }
  if (parts[0] === "src" && parts.length > 1) {
    return parts[1];
  }
  if (parts[0] === "tests") {
    return "tests";
  }
  if (parts[0] === "scripts") {
    return "scripts";
  }
  return parts[0];
}

// ---------------------------------------------------------------------------
// Deployment record
// ---------------------------------------------------------------------------

/** Immutable record of a single deployment attempt. */
export interface DeploymentRecord {
  deploymentId: string;
  ticketId: string;
  branch: string;
  status: "pending" | "deploying" | "deployed" | "rolled_back";
  startedAt: string;
  completedAt: string | null;
  rollbackReason: string | null;
  testResults: Record<string, unknown> | null;
  metadata: Record<string, unknown>;
}

function createDeploymentRecord(
  overrides: Partial<DeploymentRecord> = {},
): DeploymentRecord {
  return {
    deploymentId: overrides.deploymentId ?? crypto.randomBytes(6).toString("hex"),
    ticketId: overrides.ticketId ?? "",
    branch: overrides.branch ?? "",
    status: overrides.status ?? "pending",
    startedAt: overrides.startedAt ?? new Date().toISOString(),
    completedAt: overrides.completedAt ?? null,
    rollbackReason: overrides.rollbackReason ?? null,
    testResults: overrides.testResults ?? null,
    metadata: overrides.metadata ?? {},
  };
}

// ---------------------------------------------------------------------------
// Deployment Governor
// ---------------------------------------------------------------------------

/**
 * Decides whether a deployment can proceed and tracks outcomes.
 *
 * Lifecycle:
 * 1. `canDeploy()` -- pre-flight check (stability gate must pass)
 * 2. `startDeployment()` -- record the attempt
 * 3. `completeDeployment()` -- mark success
 * 4. `rollback()` -- revert and record the reason
 */
export class DeploymentGovernor {
  private _records: DeploymentRecord[] = [];

  /** All deployment records. */
  get records(): DeploymentRecord[] {
    return [...this._records];
  }

  /** Return true only if the stability gate did not BLOCK. */
  canDeploy(stability: StabilityReport): boolean {
    if (stability.verdict === GovernanceVerdict.BLOCK) {
      console.warn(
        `Deployment blocked by stability gate: ${stability.details}`,
      );
      return false;
    }
    return true;
  }

  /** Create a new deployment record in "deploying" state. */
  startDeployment(ticketId: string, branch: string = ""): DeploymentRecord {
    const rec = createDeploymentRecord({
      ticketId,
      branch,
      status: "deploying",
    });
    this._records.push(rec);
    return rec;
  }

  /** Mark a deployment as successfully "deployed". */
  completeDeployment(
    deploymentId: string,
    testResults?: Record<string, unknown>,
  ): DeploymentRecord | null {
    const rec = this._find(deploymentId);
    if (rec == null) {
      console.error(`Deployment ${deploymentId} not found`);
      return null;
    }
    rec.status = "deployed";
    rec.completedAt = new Date().toISOString();
    rec.testResults = testResults ?? null;
    return rec;
  }

  /** Revert a deployment and record the reason. */
  rollback(
    deploymentId: string,
    reason: string = "",
  ): DeploymentRecord | null {
    const rec = this._find(deploymentId);
    if (rec == null) {
      console.error(`Deployment ${deploymentId} not found for rollback`);
      return null;
    }
    rec.status = "rolled_back";
    rec.completedAt = new Date().toISOString();
    rec.rollbackReason = reason;
    return rec;
  }

  // -----------------------------------------------------------------------
  // Internals
  // -----------------------------------------------------------------------

  private _find(deploymentId: string): DeploymentRecord | undefined {
    return this._records.find((r) => r.deploymentId === deploymentId);
  }
}

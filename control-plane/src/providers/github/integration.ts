/**
 * GitHub integration using the `gh` CLI subprocess.
 *
 * Creates and manages GitHub issues from SWE tickets.  Assumes `gh` is
 * pre-authenticated via `gh auth login`.
 *
 * Includes a circuit breaker that pauses GitHub operations after repeated
 * failures, preventing noisy retry storms when the CLI or GitHub API is
 * temporarily unavailable.
 *
 * Ported from: src/swe_team/github_integration.py
 */

import { execFileSync } from "node:child_process";

// ---------------------------------------------------------------------------
// Circuit breaker state (module-level, resets on process restart)
// ---------------------------------------------------------------------------

let consecutiveFailures = 0;
let firstFailureAt: number | null = null;
let pausedUntil: number | null = null;
const FAILURE_THRESHOLD = 3;
const RETRY_AFTER_MS = 10 * 60 * 1000; // 10 minutes

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const TITLE_PREFIX = "[SWE-AUTO]";

// ---------------------------------------------------------------------------
// Config interface
// ---------------------------------------------------------------------------

export interface GitHubConfig {
  /** Target repo in "owner/repo" format.  Falls back to SWE_GITHUB_REPO env. */
  repo?: string;
  /** Bot account name.  Falls back to SWE_GITHUB_ACCOUNT env. */
  account?: string;
  /** Label overrides. */
  labels?: {
    team?: string;  // default "swe-team"
    hitl?: string;  // default "needs-human-review"
    auto?: string;  // default "auto-detected"
  };
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function resolveRepo(config?: GitHubConfig): string {
  return config?.repo || process.env.SWE_GITHUB_REPO || "";
}

function resolveLabels(config?: GitHubConfig): {
  team: string;
  hitl: string;
  auto: string;
} {
  return {
    team: config?.labels?.team ?? process.env.SWE_LABEL_TEAM ?? "swe-team",
    hitl: config?.labels?.hitl ?? process.env.SWE_LABEL_HITL ?? "needs-human-review",
    auto: config?.labels?.auto ?? process.env.SWE_LABEL_AUTO ?? "auto-detected",
  };
}

// ---------------------------------------------------------------------------
// Circuit breaker
// ---------------------------------------------------------------------------

/**
 * Returns true when the circuit breaker is open (too many recent failures).
 *
 * If the pause window has expired, the circuit automatically resets to
 * half-open so that the next call can probe availability.
 */
export function isCircuitOpen(): boolean {
  if (pausedUntil !== null) {
    if (Date.now() >= pausedUntil) {
      // Cooldown expired -- allow a retry (half-open)
      pausedUntil = null;
      return false;
    }
    return true;
  }
  return false;
}

/**
 * Record a successful `gh` invocation, resetting the failure counter.
 */
export function recordSuccess(): void {
  if (consecutiveFailures === 0 && firstFailureAt === null) return;
  consecutiveFailures = 0;
  firstFailureAt = null;
  pausedUntil = null;
}

/**
 * Record a failed `gh` invocation.  After {@link FAILURE_THRESHOLD}
 * consecutive failures, the circuit opens for {@link RETRY_AFTER_MS}.
 */
export function recordFailure(reason: string): void {
  const now = Date.now();
  if (firstFailureAt === null) {
    firstFailureAt = now;
  }
  consecutiveFailures += 1;

  if (consecutiveFailures >= FAILURE_THRESHOLD) {
    pausedUntil = now + RETRY_AFTER_MS;
  }
}

/**
 * Forcibly close the circuit breaker (e.g. after a manual recovery).
 */
export function resetCircuit(): void {
  consecutiveFailures = 0;
  firstFailureAt = null;
  pausedUntil = null;
}

// ---------------------------------------------------------------------------
// Low-level gh CLI runner
// ---------------------------------------------------------------------------

/**
 * Run `gh` with the given arguments.
 *
 * Returns `{ stdout, stderr }` on completion (even non-zero exit) or
 * `null` when the circuit breaker is open or an OS-level error occurs
 * (e.g. `gh` not found, signal kill, timeout).
 *
 * Side-effects: calls {@link recordSuccess} / {@link recordFailure}
 * based on the exit code.
 */
export function runGh(
  args: string[],
  options?: { timeout?: number; cwd?: string },
): { stdout: string; stderr: string } | null {
  if (isCircuitOpen()) {
    return null;
  }

  const timeout = options?.timeout ?? 30_000;

  try {
    const stdout = execFileSync("gh", args, {
      encoding: "utf8",
      timeout,
      cwd: options?.cwd,
      maxBuffer: 10 * 1024 * 1024, // 10 MB
      stdio: ["pipe", "pipe", "pipe"],
    });

    recordSuccess();
    return { stdout: stdout ?? "", stderr: "" };
  } catch (err: unknown) {
    // execFileSync throws on non-zero exit AND on signals/timeouts.
    // When the process exited with a non-zero code, the error object
    // carries stdout/stderr from the child.
    if (err && typeof err === "object" && "status" in err) {
      const execErr = err as {
        status: number | null;
        stdout?: string | Buffer | null;
        stderr?: string | Buffer | null;
      };

      const stdout = String(execErr.stdout ?? "");
      const stderr = String(execErr.stderr ?? "");

      const detail =
        stderr.trim() || stdout.trim() || `exit code ${execErr.status}`;
      recordFailure(detail);

      return { stdout, stderr };
    }

    // OS-level error (e.g. ENOENT, ETIMEDOUT)
    const message = err instanceof Error ? err.message : String(err);
    recordFailure(message);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Issue management
// ---------------------------------------------------------------------------

/**
 * Create a GitHub issue from a SWE ticket.
 *
 * Only creates issues for CRITICAL or HIGH severity tickets.  Returns the
 * new issue number on success, or `null` when skipped / on failure.
 */
export async function createGitHubIssue(
  ticket: {
    title: string;
    description: string;
    severity: string;
    ticketId: string;
    sourceModule?: string | null;
    fingerprint?: string | null;
    errorLog?: string | null;
    assignedTo?: string | null;
  },
  config?: GitHubConfig,
): Promise<number | null> {
  // Only HIGH and CRITICAL tickets get GitHub issues
  const sev = ticket.severity.toLowerCase();
  if (sev !== "critical" && sev !== "high") {
    return null;
  }

  const repo = resolveRepo(config);
  if (!repo) {
    return null;
  }

  const labels = resolveLabels(config);
  const title = `${TITLE_PREFIX} ${ticket.title.slice(0, 80)}`;

  const bodyParts: string[] = [
    "## Auto-detected by SWE Team",
    "",
    `**Ticket ID:** \`${ticket.ticketId}\``,
    `**Severity:** ${sev.toUpperCase()}`,
    `**Module:** ${ticket.sourceModule ?? "unknown"}`,
    `**Assigned to:** ${ticket.assignedTo ?? "unassigned"}`,
  ];

  if (ticket.description) {
    bodyParts.push("", "### Description", "", ticket.description.slice(0, 500));
  }
  if (ticket.errorLog) {
    bodyParts.push(
      "",
      "### Error log",
      "",
      `\`\`\`\n${ticket.errorLog.slice(0, 400)}\n\`\`\``,
    );
  }
  if (ticket.fingerprint) {
    bodyParts.push("", `<!-- fingerprint:${ticket.fingerprint} -->`);
  }

  const body = bodyParts.join("\n");
  const severityLabel = `severity: ${sev}`;
  const labelStr = `${labels.team},${labels.auto},${severityLabel}`;

  const result = runGh(
    [
      "issue",
      "create",
      "--repo",
      repo,
      "--title",
      title,
      "--body",
      body,
      "--label",
      labelStr,
    ],
    { timeout: 30_000 },
  );

  if (result === null) {
    return null;
  }

  // gh outputs the URL on stdout: "https://github.com/owner/repo/issues/123"
  const output = result.stdout.trim();
  const match = output.match(/\/issues\/(\d+)/);
  if (match) {
    return parseInt(match[1], 10);
  }

  return null;
}

/**
 * Find the first comment on an issue whose body contains `searchText`.
 *
 * Returns the comment ID (needed for PATCH updates) or `null`.
 */
export async function findCommentByText(
  issueNumber: number,
  searchText: string,
  config?: GitHubConfig,
): Promise<number | null> {
  const repo = resolveRepo(config);
  if (!repo) return null;

  const result = runGh(
    ["api", `repos/${repo}/issues/${issueNumber}/comments`],
    { timeout: 20_000 },
  );
  if (result === null) return null;

  try {
    const comments: Array<{ id?: number; body?: string }> = JSON.parse(
      result.stdout.trim() || "[]",
    );
    for (const comment of comments) {
      if ((comment.body ?? "").includes(searchText)) {
        return comment.id ?? null;
      }
    }
  } catch {
    // JSON parse failure -- treat as "not found"
  }

  return null;
}

/**
 * Edit an existing issue comment in-place via the REST API.
 *
 * Uses `gh api ... -X PATCH` to avoid hitting the `gh issue comment`
 * limitation of only appending.
 */
export async function updateComment(
  commentId: number,
  newText: string,
  config?: GitHubConfig,
): Promise<boolean> {
  const repo = resolveRepo(config);
  if (!repo || !commentId) return false;

  const result = runGh(
    [
      "api",
      `repos/${repo}/issues/comments/${commentId}`,
      "-X",
      "PATCH",
      "-f",
      `body=${newText}`,
    ],
    { timeout: 20_000 },
  );

  if (result === null) return false;

  // Non-zero exit means the stderr was populated; runGh already recorded
  // the failure.  Check stdout for a valid JSON response with an "id".
  try {
    const data = JSON.parse(result.stdout.trim() || "{}");
    return typeof data.id === "number";
  } catch {
    return false;
  }
}

/**
 * Post or update a status comment on an issue.
 *
 * First attempts to find an existing comment containing a "Ticket ID:"
 * marker and updates it in-place.  Falls back to creating a new comment
 * if none exists.
 */
export async function postStatusComment(
  issueNumber: number,
  statusText: string,
  config?: GitHubConfig,
): Promise<boolean> {
  const repo = resolveRepo(config);
  if (!repo) return false;

  // Look for an existing status comment with the "Ticket ID:" marker
  const existingId = await findCommentByText(issueNumber, "Ticket ID:", config);

  if (existingId !== null) {
    return updateComment(existingId, statusText, config);
  }

  // No existing comment -- create a new one
  const result = runGh(
    [
      "issue",
      "comment",
      String(issueNumber),
      "--repo",
      repo,
      "--body",
      statusText,
    ],
    { timeout: 15_000 },
  );

  if (result === null) return false;

  // gh returns 0 on success for issue comment
  return result.stderr.trim() === "" || !result.stderr.includes("error");
}

/**
 * List open issues in the configured repository.
 *
 * Returns an array of `{ number, title, labels }` objects.  Returns an
 * empty array on failure so callers can safely iterate.
 */
export async function listOpenIssues(
  config?: GitHubConfig,
): Promise<Array<{ number: number; title: string; labels: string[]; body: string }>> {
  const repo = resolveRepo(config);
  if (!repo) return [];

  const result = runGh(
    [
      "issue",
      "list",
      "--state",
      "open",
      "--json",
      "number,title,labels,body",
      "-R",
      repo,
    ],
    { timeout: 20_000 },
  );

  if (result === null) return [];

  try {
    const raw: Array<{
      number?: number;
      title?: string;
      labels?: Array<{ name?: string }>;
      body?: string;
    }> = JSON.parse(result.stdout.trim() || "[]");

    return raw.map((issue) => ({
      number: issue.number ?? 0,
      title: issue.title ?? "",
      labels: (issue.labels ?? [])
        .map((l) => l.name ?? "")
        .filter(Boolean),
      body: (issue.body ?? "").slice(0, 2000),
    }));
  } catch {
    return [];
  }
}

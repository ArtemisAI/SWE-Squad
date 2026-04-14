/**
 * Tool: merge_pr -- Merge a pull request after all gates pass.
 *
 * Validates: PR is approved, tests pass, stability gate is not BLOCK.
 * Uses `gh pr merge` with a configurable strategy (squash/merge/rebase).
 * Updates ticket status to "resolved" and sends a notification on success.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { execFileSync } from "node:child_process";
import type { SWEContext } from "../shared/context.js";

/**
 * Extract repo and PR number from a GitHub PR URL.
 */
function parsePrUrl(url: string): { repo: string; prNumber: number } | null {
  const match = url.match(/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/);
  if (!match) return null;
  return { repo: match[1], prNumber: parseInt(match[2], 10) };
}

export function createMergePrTool(ctx: SWEContext) {
  return defineTool({
    name: "merge_pr",
    label: "Merge PR",
    description:
      "Merge a pull request via `gh pr merge`. Validates that the PR is approved, " +
      "tests pass, and the stability gate is not BLOCK. Supports squash, merge, " +
      "and rebase strategies. Updates ticket to resolved and sends notification.",
    parameters: Type.Object({
      ticketId: Type.String({ description: "The ticket ID whose PR to merge" }),
      prNumber: Type.Optional(
        Type.Number({ description: "PR number override. Auto-detected from ticket metadata if omitted." }),
      ),
      repo: Type.Optional(
        Type.String({ description: "Repository (owner/repo). Auto-detected from ticket metadata if omitted." }),
      ),
      strategy: Type.Optional(
        Type.Union(
          [
            Type.Literal("squash"),
            Type.Literal("merge"),
            Type.Literal("rebase"),
          ],
          { description: "Merge strategy (default: squash)", default: "squash" },
        ),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _extCtx) {
      const startMs = Date.now();
      const store = ctx.ticketStore;
      if (!store) {
        return {
          content: [{ type: "text" as const, text: "Error: Supabase ticket store not configured" }],
          details: {},
        };
      }

      try {
        // 1. Get ticket
        const ticket = await store.get(params.ticketId);
        if (!ticket) {
          return {
            content: [{ type: "text" as const, text: `Ticket not found: ${params.ticketId}` }],
            details: {},
          };
        }

        const meta = ticket.metadata as Record<string, unknown>;

        // 2. Resolve PR number and repo
        let prNumber = params.prNumber ?? (meta.prNumber as number | undefined);
        let repo = params.repo ?? (meta.repo as string | undefined);

        // Try to extract from prUrl if direct fields are missing
        if (prNumber == null || repo == null) {
          const prUrl =
            (meta.prUrl as string | undefined) ??
            (meta.pr_url as string | undefined);
          if (prUrl) {
            const parsed = parsePrUrl(prUrl);
            if (parsed) {
              prNumber ??= parsed.prNumber;
              repo ??= parsed.repo;
            }
          }
        }

        if (prNumber == null) {
          return {
            content: [{
              type: "text" as const,
              text: `No PR number found for ${params.ticketId}. ` +
                "Provide prNumber param or set metadata.prNumber / metadata.prUrl on the ticket.",
            }],
            details: {},
          };
        }

        if (!repo) {
          return {
            content: [{
              type: "text" as const,
              text: `No repository found for ${params.ticketId}. ` +
                "Provide repo param or set metadata.repo on the ticket.",
            }],
            details: {},
          };
        }

        // 3. Validate PR is approved
        const prApproved = meta.prApproved === true;
        if (!prApproved) {
          return {
            content: [{
              type: "text" as const,
              text: `Cannot merge: PR #${prNumber} is not approved. Run approve_pr first.`,
            }],
            details: { prNumber, repo, prApproved: false },
          };
        }

        // 4. Validate tests passed
        const testResults = ticket.testResults as Record<string, unknown> | null;
        const testsPassed = testResults?.success === true || meta.testsPassed === true;
        if (!testsPassed) {
          return {
            content: [{
              type: "text" as const,
              text: `Cannot merge: tests have not passed for ${params.ticketId}. ` +
                "Run run_tests and ensure all tests pass before merging.",
            }],
            details: { prNumber, repo, testsPassed: false },
          };
        }

        // 5. Check stability gate (non-blocking if guardrails not configured)
        if (ctx.guardrails) {
          const decision = ctx.guardrails.evaluate();
          if (decision.blocked) {
            return {
              content: [{
                type: "text" as const,
                text: `Cannot merge: stability gate BLOCK -- ${decision.reason} (gate: ${decision.gate})`,
              }],
              details: { prNumber, repo, gate: decision.gate, reason: decision.reason },
            };
          }
        }

        // Also check circuit breaker independently
        if (ctx.circuitBreaker?.isPaused) {
          return {
            content: [{
              type: "text" as const,
              text: `Cannot merge: circuit breaker is tripped ` +
                `(failure rate: ${(ctx.circuitBreaker.failureRate * 100).toFixed(1)}%). ` +
                "Wait for the circuit breaker to reset.",
            }],
            details: { prNumber, repo, circuitBreakerTripped: true },
          };
        }

        // 6. Merge the PR
        const strategy = params.strategy ?? "squash";
        const strategyFlag = `--${strategy}`;

        ctx.logger.info(
          `Merging PR #${prNumber} in ${repo} (strategy: ${strategy}) for ticket ${params.ticketId}`,
        );

        try {
          execFileSync(
            "gh",
            [
              "pr", "merge", String(prNumber),
              "--repo", repo,
              strategyFlag,
              "--delete-branch",
              "--body", `Merged by SWE-Manager. Ticket: ${params.ticketId}`,
            ],
            { cwd: ctx.cwd, timeout: 60_000, encoding: "utf-8", stdio: "pipe" },
          );
        } catch (err: unknown) {
          const execErr = err as { stderr?: string; status?: number };
          return {
            content: [{
              type: "text" as const,
              text: `Failed to merge PR #${prNumber} in ${repo}: ` +
                `${execErr.stderr ?? "unknown error"} (exit ${execErr.status ?? "?"})`,
            }],
            details: { prNumber, repo, strategy },
          };
        }

        // 7. Update ticket to resolved
        ticket.status = "resolved";
        meta.mergedAt = new Date().toISOString();
        meta.mergedBy = ctx.config.teamId;
        meta.mergeStrategy = strategy;
        meta.resolution_note = "fix_succeeded";
        ticket.metadata = meta;
        ticket.updatedAt = new Date().toISOString();
        await store.update(ticket);

        // 7b. Close linked GitHub issue (best-effort)
        const ghIssue = (meta.github_issue ?? meta.issueNumber) as number | undefined;
        if (ghIssue && repo) {
          try {
            execFileSync(
              "gh",
              ["issue", "close", String(ghIssue), "--repo", repo,
               "--comment", `Closed by PR #${prNumber} merge. Ticket: ${params.ticketId}`],
              { cwd: ctx.cwd, timeout: 15_000, encoding: "utf-8", stdio: "pipe" },
            );
          } catch {
            // Non-fatal: issue may already be closed or not exist
          }
        }

        // 8. Send notification (best-effort)
        if (ctx.notifier) {
          try {
            await ctx.notifier.send(
              `PR #${prNumber} merged for ticket ${params.ticketId}: ${ticket.title}\n` +
                `Repo: ${repo}, Strategy: ${strategy}`,
              { alertType: "info" },
            );
          } catch {
            // Non-fatal: notification failure should not block the merge result
          }
        }

        ctx.outcomeTracker?.record({
          tool: "merge_pr",
          ticketId: params.ticketId,
          success: true,
          durationMs: Date.now() - startMs,
          timestamp: new Date(),
        });

        return {
          content: [{
            type: "text" as const,
            text: `PR #${prNumber} merged successfully in ${repo} (strategy: ${strategy}).\n` +
              `Ticket ${params.ticketId} status updated to "resolved".`,
          }],
          details: {
            ticketId: params.ticketId,
            prNumber,
            repo,
            strategy,
            merged: true,
          },
        };
      } catch (err) {
        ctx.outcomeTracker?.record({
          tool: "merge_pr",
          ticketId: params.ticketId,
          success: false,
          error: String(err),
          durationMs: Date.now() - startMs,
          timestamp: new Date(),
        });

        return {
          content: [{ type: "text" as const, text: `Merge failed for ${params.ticketId}: ${err}` }],
          details: {},
        };
      }
    },
  });
}

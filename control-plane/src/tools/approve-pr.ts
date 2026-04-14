/**
 * Tool: approve_pr -- Approve a pull request after review and test gates pass.
 *
 * Validates that the ticket has a PR, review passed, and tests passed
 * before running `gh pr review --approve`.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { execFileSync } from "node:child_process";
import type { SWEContext } from "../shared/context.js";

/**
 * Extract repo and PR number from a GitHub PR URL.
 *
 * Handles: https://github.com/owner/repo/pull/123
 * Returns null if the URL does not match.
 */
function parsePrUrl(url: string): { repo: string; prNumber: number } | null {
  const match = url.match(/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/);
  if (!match) return null;
  return { repo: match[1], prNumber: parseInt(match[2], 10) };
}

export function createApprovePrTool(ctx: SWEContext) {
  return defineTool({
    name: "approve_pr",
    label: "Approve PR",
    description:
      "Approve a pull request via `gh pr review --approve`. " +
      "Validates that the ticket has a PR, code review passed (approved verdict), " +
      "and tests passed before approving.",
    parameters: Type.Object({
      ticketId: Type.String({ description: "The ticket ID whose PR to approve" }),
      prNumber: Type.Optional(
        Type.Number({ description: "PR number override. Auto-detected from ticket metadata if omitted." }),
      ),
      repo: Type.Optional(
        Type.String({ description: "Repository (owner/repo). Auto-detected from ticket metadata if omitted." }),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _extCtx) {
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

        // 3. Validate review passed
        const reviewVerdict = meta.reviewVerdict as string | undefined;
        if (reviewVerdict !== "approved") {
          return {
            content: [{
              type: "text" as const,
              text: `Cannot approve: review verdict is "${reviewVerdict ?? "none"}" -- ` +
                "expected \"approved\". Run delegate_review first.",
            }],
            details: { reviewVerdict: reviewVerdict ?? "none" },
          };
        }

        // 4. Validate tests passed
        const testResults = ticket.testResults as Record<string, unknown> | null;
        const testsPassed = testResults?.success === true || meta.testsPassed === true;
        if (!testsPassed) {
          return {
            content: [{
              type: "text" as const,
              text: `Cannot approve: tests have not passed for ${params.ticketId}. ` +
                "Run run_tests first and ensure all tests pass.",
            }],
            details: { testsPassed: false },
          };
        }

        // 5. Approve the PR
        ctx.logger.info(
          `Approving PR #${prNumber} in ${repo} for ticket ${params.ticketId}`,
        );

        let ghApprovalOk = false;
        let ghApprovalNote = "";
        try {
          execFileSync(
            "gh",
            [
              "pr", "review", String(prNumber),
              "--repo", repo,
              "--approve",
              "--body", `Approved by SWE-Manager. Ticket: ${params.ticketId}`,
            ],
            { cwd: ctx.cwd, timeout: 30_000, encoding: "utf-8", stdio: "pipe" },
          );
          ghApprovalOk = true;
        } catch (err: unknown) {
          const execErr = err as { stderr?: string; status?: number };
          const stderr = execErr.stderr ?? "";
          // GitHub blocks self-approval — this is expected when the same account
          // that created the PR runs the daemon. The internal gates (review + tests)
          // already passed, so we proceed with the pipeline approval.
          if (stderr.includes("own pull request") || stderr.includes("yourself")) {
            ctx.logger.info(
              `Self-approval blocked by GitHub for PR #${prNumber} — proceeding with pipeline approval`,
            );
            ghApprovalNote = " (GitHub self-approval blocked, pipeline approval only)";
          } else {
            return {
              content: [{
                type: "text" as const,
                text: `Failed to approve PR #${prNumber} in ${repo}: ` +
                  `${stderr || "unknown error"} (exit ${execErr.status ?? "?"})`,
              }],
              details: { prNumber, repo },
            };
          }
        }

        // 6. Update ticket
        meta.prApproved = true;
        meta.prApprovedAt = new Date().toISOString();
        meta.prApprovedBy = ctx.config.teamId;
        ticket.metadata = meta;
        ticket.updatedAt = new Date().toISOString();
        await store.update(ticket);

        return {
          content: [{
            type: "text" as const,
            text: `PR #${prNumber} in ${repo} approved for ticket ${params.ticketId}${ghApprovalNote}.`,
          }],
          details: {
            ticketId: params.ticketId,
            prNumber,
            repo,
            approved: true,
            ghApproval: ghApprovalOk,
          },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `PR approval failed for ${params.ticketId}: ${err}` }],
          details: {},
        };
      }
    },
  });
}

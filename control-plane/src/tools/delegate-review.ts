/**
 * Tool: delegate_review -- Delegate code review to a configured engine.
 *
 * Engine-agnostic: reads config.delegation.reviewer.engine to resolve
 * which CodingEngine to use. Never imports an engine directly.
 *
 * The review engine runs read-only -- it reads the PR diff, investigation
 * report, and development notes, then produces a structured review verdict.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { execFileSync } from "node:child_process";
import type { SWEContext } from "../shared/context.js";
import { resolveEngineForRole, getDelegationConfig } from "../shared/engine-resolver.js";
import { buildReviewPrompt } from "../shared/prompt-builder.js";
import { withRetry } from "../utils/retry.js";

/** Structured review result parsed from engine output. */
interface ReviewResult {
  verdict: "approved" | "changes_requested" | "commented";
  issuesFound: number;
  severity: "none" | "low" | "medium" | "high" | "critical";
  summary: string;
}

/**
 * Parse the engine's raw output into a structured ReviewResult.
 *
 * Looks for verdict markers in the text. Falls back to "commented"
 * if no clear signal is found.
 */
function parseReviewOutput(output: string): ReviewResult {
  const lower = output.toLowerCase();

  let verdict: ReviewResult["verdict"] = "commented";
  if (
    lower.includes("approved") &&
    !lower.includes("not approved") &&
    !lower.includes("changes requested")
  ) {
    verdict = "approved";
  } else if (
    lower.includes("changes requested") ||
    lower.includes("changes_requested") ||
    lower.includes("request changes")
  ) {
    verdict = "changes_requested";
  }

  // Count issues: look for numbered items or bullet points after "issues" header
  let issuesFound = 0;
  const issuePatterns = [
    /issue[s]?\s*(?:found)?[:\s]*(\d+)/i,
    /(\d+)\s*issue[s]?\s*found/i,
    /found\s*(\d+)\s*issue/i,
  ];
  for (const pat of issuePatterns) {
    const m = output.match(pat);
    if (m) {
      issuesFound = parseInt(m[1], 10);
      break;
    }
  }
  // Fallback: count lines starting with "- " or numbered items after common headers
  if (issuesFound === 0 && verdict === "changes_requested") {
    const bulletLines = output.match(/^[\s]*[-*]\s+.+/gm);
    issuesFound = bulletLines ? Math.min(bulletLines.length, 50) : 1;
  }

  // Severity classification
  let severity: ReviewResult["severity"] = "none";
  if (verdict === "changes_requested") {
    if (lower.includes("critical") || lower.includes("security")) {
      severity = "critical";
    } else if (lower.includes("high severity") || lower.includes("major")) {
      severity = "high";
    } else if (lower.includes("medium") || lower.includes("moderate")) {
      severity = "medium";
    } else {
      severity = "low";
    }
  }

  // Extract summary: full review text up to 3000 chars (skip preamble)
  const allLines = output.split("\n");
  // Find the start of the actual review (first markdown header or "---")
  let startIdx = 0;
  for (let i = 0; i < allLines.length; i++) {
    if (allLines[i].trim().startsWith("#") || allLines[i].trim() === "---") {
      startIdx = i;
      break;
    }
  }
  const summary = allLines.slice(startIdx).join("\n").trim().slice(0, 3000) || "No summary";

  return { verdict, issuesFound, severity, summary };
}

/**
 * Fetch the PR diff for a given PR URL using `gh pr diff`.
 *
 * Returns the diff text, or null if it cannot be fetched.
 */
function fetchPrDiff(prUrl: string, cwd: string): string | null {
  try {
    // Extract PR number and repo from URL patterns:
    // https://github.com/owner/repo/pull/123
    const match = prUrl.match(/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/);
    if (!match) return null;
    const [, repo, prNum] = match;

    const diff = execFileSync(
      "gh",
      ["pr", "diff", prNum, "--repo", repo],
      { cwd, timeout: 30_000, encoding: "utf-8", stdio: "pipe" },
    );
    return diff;
  } catch {
    return null;
  }
}

export function createDelegateReviewTool(ctx: SWEContext) {
  return defineTool({
    name: "delegate_review",
    label: "Delegate Review",
    description:
      "Delegate code review of a PR to the configured review engine. " +
      "Runs read-only: reads PR diff, investigation report, and development notes, " +
      "then produces a structured review verdict (approved/changes_requested/commented).",
    parameters: Type.Object({
      ticketId: Type.String({ description: "The ticket ID to review" }),
      prUrl: Type.Optional(
        Type.String({ description: "PR URL to review. Auto-detected from ticket metadata if omitted." }),
      ),
      model: Type.Optional(
        Type.String({ description: "Model override (e.g. sonnet, opus)" }),
      ),
      timeout: Type.Optional(
        Type.Number({ description: "Timeout in seconds (default 1200)", default: 1200 }),
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

        // 2. Verify ticket is in a reviewable state
        // Accept "in_review" (normal flow) and "in_development" (rework after changes_requested)
        const reviewableStatuses = ["in_review", "in_development"];
        if (!reviewableStatuses.includes(ticket.status)) {
          return {
            content: [{
              type: "text" as const,
              text: `Ticket ${params.ticketId} is in status "${ticket.status}" -- expected one of: ${reviewableStatuses.join(", ")}.`,
            }],
            details: {},
          };
        }

        // 3. Resolve PR URL from params or ticket metadata
        const meta = ticket.metadata as Record<string, unknown>;
        const prUrl =
          params.prUrl ??
          (meta.prUrl as string | undefined) ??
          (meta.pr_url as string | undefined);

        // 4. Fetch PR diff if we have a URL
        let prDiff: string | null = null;
        if (prUrl) {
          prDiff = fetchPrDiff(prUrl, ctx.cwd);
        }

        // 5. Claim atomically
        const agentId = ctx.config.teamId;
        const claimed = await store.claimTicket(params.ticketId, agentId);
        if (!claimed) {
          return {
            content: [{ type: "text" as const, text: `Already claimed by another agent: ${params.ticketId}` }],
            details: {},
          };
        }

        // 6. Resolve engine from config
        let engine;
        try {
          engine = resolveEngineForRole("reviewer", ctx.config);
        } catch (err) {
          await store.releaseTicket(params.ticketId, ticket.status);
          return {
            content: [{ type: "text" as const, text: `Engine resolution failed: ${err}` }],
            details: {},
          };
        }

        // 7. Health check
        const healthy = await engine.healthCheck();
        if (!healthy) {
          await store.releaseTicket(params.ticketId, ticket.status);
          return {
            content: [{ type: "text" as const, text: `Engine "${engine.name}" health check failed` }],
            details: {},
          };
        }

        // 8. Query memory for past review patterns on similar code
        let memoryContext = "";
        if (ctx.memoryService) {
          try {
            const memories = await ctx.memoryService.query({
              tenantId: ctx.config.teamId,
              projectId: (meta.repo as string) ?? "default",
              types: ["investigation", "fix_pattern", "root_cause"],
              limit: 3,
            });
            if (memories.length > 0) {
              memoryContext = "\n\n## Context from Past Work\n\n" +
                memories.map((m, i) =>
                  `### Memory ${i + 1} (confidence: ${m.confidence}, type: ${m.type})\n${m.content.slice(0, 400)}`
                ).join("\n\n");
              ctx.logger.info(
                `Memory: injected ${memories.length} past memories into review prompt for ${params.ticketId}`,
              );
              for (const m of memories) {
                ctx.memoryService.recordHit(m.id, ctx.config.teamId).catch(() => {});
              }
            } else {
              ctx.logger.debug(`Memory: no relevant memories found for review of ${params.ticketId}`);
            }
          } catch (e) {
            ctx.logger.warn(`Memory query failed (non-fatal): ${e}`);
          }
        }

        // 9. Build prompt and run read-only
        const delegation = getDelegationConfig("reviewer", ctx.config);
        const prompt = buildReviewPrompt(ticket, prDiff) + memoryContext;
        const model = params.model ?? delegation.model;
        const timeout = params.timeout ?? delegation.timeout;

        ctx.logger.info(
          `Reviewing ${params.ticketId} via ${engine.name} (model=${model ?? "default"}, timeout=${timeout}s)`,
        );

        // Resolve workspace from ticket repo → config repos → fallback to cwd
        let reviewCwd = ctx.cwd;
        if (meta.repo) {
          const repoConfig = ctx.config.repos.find(
            (r: Record<string, unknown>) =>
              r.name === meta.repo || (r.name as string)?.endsWith(`/${String(meta.repo).split("/").pop()}`),
          );
          const localPath = repoConfig?.localPath ?? repoConfig?.local_path;
          if (localPath) {
            reviewCwd = String(localPath).replace("~", process.env.HOME ?? "/home/agent");
          }
        }

        const result = await engine.run(prompt, {
          cwd: reviewCwd,
          readOnly: true,
          model,
          timeout,
        });

        // 9b. Check for engine failure
        if (result.returncode !== 0) {
          await withRetry(
            () => store.releaseTicket(params.ticketId, ticket.status),
            { label: "releaseTicket after review failure" },
          );

          ctx.outcomeTracker?.record({
            tool: "delegate_review",
            ticketId: params.ticketId,
            engine: engine.name,
            success: false,
            error: result.stderr || "non-zero exit",
            durationMs: Date.now() - startMs,
            costUsd: result.costUsd ?? undefined,
            timestamp: new Date(),
          });

          return {
            content: [{
              type: "text" as const,
              text: `Review failed for ${params.ticketId}: ${result.stderr || "non-zero exit"}\n` +
                `Engine: ${engine.name}, returncode=${result.returncode}`,
            }],
            details: { ticketId: params.ticketId, returncode: result.returncode },
          };
        }

        // 10. Parse review output
        const review = parseReviewOutput(result.stdout || "");

        // 11. Update ticket with review result
        const updatedMeta = meta;
        updatedMeta.reviewVerdict = review.verdict;
        updatedMeta.reviewIssuesFound = review.issuesFound;
        updatedMeta.reviewSeverity = review.severity;
        updatedMeta.reviewSummary = review.summary;
        updatedMeta.reviewRawOutput = (result.stdout || "").slice(0, 8000);
        updatedMeta.reviewEngine = engine.name;
        updatedMeta.reviewCost = result.costUsd;
        updatedMeta.reviewedAt = new Date().toISOString();

        if (review.verdict === "approved") {
          ticket.status = "testing";
        } else if (review.verdict === "changes_requested") {
          // Map to "in_development" — Supabase CHECK constraint doesn't include
          // "rework_requested". Metadata has reviewVerdict for full context.
          ticket.status = "in_development";
        }
        // "commented" leaves status as-is (still in_review)

        ticket.metadata = updatedMeta;
        ticket.updatedAt = new Date().toISOString();
        await withRetry(() => store.update(ticket), { label: "store.update after review" });

        // 12. Release claim (retry — connection may be stale after long engine.run)
        await withRetry(
          () => store.releaseTicket(params.ticketId, ticket.status),
          { label: "releaseTicket after review" },
        );

        // 13. Record outcome
        ctx.outcomeTracker?.record({
          tool: "delegate_review",
          ticketId: params.ticketId,
          engine: engine.name,
          success: true,
          durationMs: Date.now() - startMs,
          costUsd: result.costUsd ?? undefined,
          timestamp: new Date(),
        });

        return {
          content: [{
            type: "text" as const,
            text: `Review complete for ${params.ticketId}.\n\n` +
              `Verdict: ${review.verdict.toUpperCase()}\n` +
              `Issues found: ${review.issuesFound}, Severity: ${review.severity}\n` +
              `Engine: ${engine.name}, Cost: $${result.costUsd?.toFixed(4) ?? "unknown"}\n\n` +
              `Summary:\n${review.summary}`,
          }],
          details: {
            ticketId: params.ticketId,
            verdict: review.verdict,
            issuesFound: review.issuesFound,
            severity: review.severity,
            engine: engine.name,
            cost: result.costUsd,
            model: result.model,
          },
        };
      } catch (err) {
        ctx.outcomeTracker?.record({
          tool: "delegate_review",
          ticketId: params.ticketId,
          success: false,
          error: String(err),
          durationMs: Date.now() - startMs,
          timestamp: new Date(),
        });

        // Store error in ticket metadata for visibility
        try {
          const ticket = await store.get(params.ticketId);
          if (ticket) {
            const meta = ticket.metadata as Record<string, unknown>;
            meta.lastReviewError = String(err).slice(0, 500);
            ticket.metadata = meta;
            ticket.updatedAt = new Date().toISOString();
            await store.update(ticket);
          }
        } catch { /* non-fatal — best effort error recording */ }

        try {
          await store.releaseTicket(params.ticketId);
        } catch { /* non-fatal */ }

        return {
          content: [{ type: "text" as const, text: `Review failed for ${params.ticketId}: ${err}` }],
          details: {},
        };
      }
    },
  });
}

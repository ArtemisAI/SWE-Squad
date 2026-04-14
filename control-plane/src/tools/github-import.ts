/**
 * Tool: github_import — Import GitHub issues as SWE tickets with dedup.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";
import { listOpenIssues } from "../providers/github/integration.js";
import { createTicket } from "../models/ticket.js";

export function createGithubImportTool(ctx: SWEContext) {
  return defineTool({
    name: "github_import",
    label: "Import GitHub Issues",
    description:
      "Import GitHub issues as SWE tickets. Deduplicates by fingerprint (gh-issue-{repo}-{number}).",
    parameters: Type.Object({
      repo: Type.String({ description: "Repository to import from (owner/repo)" }),
      issueNumber: Type.Optional(
        Type.Number({ description: "Import a specific issue number. Omit to import all new." }),
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
        const issues = await listOpenIssues({ repo: params.repo });
        if (issues.length === 0) {
          return {
            content: [{ type: "text" as const, text: "No open issues found." }],
            details: { imported: 0 },
          };
        }

        // Filter to specific issue if requested
        const toImport = params.issueNumber
          ? issues.filter((i) => i.number === params.issueNumber)
          : issues;

        // Load known fingerprints for dedup
        const knownFps = await store.knownFingerprints;
        const imported: string[] = [];
        const skipped: string[] = [];

        for (const issue of toImport) {
          const fingerprint = `gh-issue-${params.repo}-${issue.number}`;

          if (knownFps.has(fingerprint)) {
            skipped.push(`#${issue.number} (duplicate)`);
            continue;
          }

          // Infer severity from labels
          const severity = inferSeverity(issue.labels);

          const description = issue.body
            ? `GitHub issue #${issue.number} from ${params.repo}\n\n${issue.body}`
            : `GitHub issue #${issue.number} from ${params.repo}`;
          const ticket = createTicket(issue.title, description, {
            severity,
            metadata: {
              fingerprint,
              source: "github",
              repo: params.repo,
              issueNumber: issue.number,
              github_issue: issue.number,
              labels: issue.labels,
            },
          });

          await store.add(ticket);
          imported.push(`${ticket.ticketId} (${params.repo}#${issue.number})`);
        }

        const parts: string[] = [];
        if (imported.length > 0) {
          parts.push(`Imported ${imported.length} issues as tickets: ${imported.join(", ")}`);
        }
        if (skipped.length > 0) {
          parts.push(`Skipped ${skipped.length} duplicates: ${skipped.join(", ")}`);
        }
        if (parts.length === 0) {
          parts.push("No new issues to import.");
        }

        return {
          content: [{ type: "text" as const, text: parts.join("\n") }],
          details: { imported: imported.length, skipped: skipped.length },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `Error importing GitHub issues: ${err}` }],
          details: {},
        };
      }
    },
  });
}

/** Infer ticket severity from GitHub issue labels. */
function inferSeverity(labels: string[]): "critical" | "high" | "medium" | "low" {
  const lower = labels.map((l) => l.toLowerCase());
  if (lower.some((l) => l.includes("critical") || l.includes("p0"))) return "critical";
  if (lower.some((l) => l.includes("high") || l.includes("p1") || l.includes("urgent"))) return "high";
  if (lower.some((l) => l.includes("low") || l.includes("p3"))) return "low";
  return "medium";
}

/**
 * Tool: github_issues — List open issues from configured GitHub repos.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";
import { listOpenIssues, runGh } from "../providers/github/integration.js";

export function createGithubIssuesTool(ctx: SWEContext) {
  return defineTool({
    name: "github_issues",
    label: "GitHub Issues",
    description:
      "List GitHub issues from configured repositories. Filter by repo, labels, or state.",
    parameters: Type.Object({
      repo: Type.Optional(
        Type.String({ description: "Repository (owner/repo). Omit for all configured repos." }),
      ),
      labels: Type.Optional(
        Type.String({ description: "Comma-separated label filter" }),
      ),
      state: Type.Optional(
        Type.String({ description: "Issue state: open (default) or closed", default: "open" }),
      ),
      limit: Type.Optional(
        Type.Number({ description: "Max issues to return (default 20)", default: 20 }),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _extCtx) {
      try {
        const repos = params.repo
          ? [params.repo]
          : ctx.config.githubRepos;

        if (repos.length === 0) {
          return {
            content: [{ type: "text" as const, text: "No GitHub repos configured. Provide a repo parameter or set githubRepos in config." }],
            details: {},
          };
        }

        const allIssues: Array<{
          repo: string;
          number: number;
          title: string;
          labels: string[];
        }> = [];

        for (const repo of repos) {
          // Use the label filter if provided via gh CLI directly
          if (params.labels) {
            const args = [
              "issue", "list",
              "--state", params.state ?? "open",
              "--label", params.labels,
              "--json", "number,title,labels",
              "-R", repo,
              "--limit", String(params.limit ?? 20),
            ];
            const result = runGh(args, { timeout: 20_000 });
            if (result) {
              try {
                const raw = JSON.parse(result.stdout.trim() || "[]") as Array<{
                  number?: number;
                  title?: string;
                  labels?: Array<{ name?: string }>;
                }>;
                for (const issue of raw) {
                  allIssues.push({
                    repo,
                    number: issue.number ?? 0,
                    title: issue.title ?? "",
                    labels: (issue.labels ?? []).map((l) => l.name ?? "").filter(Boolean),
                  });
                }
              } catch { /* parse error — skip */ }
            }
          } else {
            const issues = await listOpenIssues({ repo });
            for (const issue of issues) {
              allIssues.push({ repo, ...issue });
            }
          }
        }

        const limited = allIssues.slice(0, params.limit ?? 20);

        return {
          content: [{
            type: "text" as const,
            text: limited.length === 0
              ? "No issues found."
              : JSON.stringify(limited, null, 2),
          }],
          details: { count: limited.length },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `Error listing GitHub issues: ${err}` }],
          details: {},
        };
      }
    },
  });
}

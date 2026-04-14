/**
 * Tool: manage_workspace -- Create, cleanup, or list development workspaces.
 *
 * Delegates to a WorkspaceProvider rather than running git commands directly.
 * The provider is resolved from config (worktree, local, clone, docker).
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";
import type { WorkspaceProvider } from "../providers/workspace/base.js";
import { WorktreeProvider } from "../providers/workspace/worktree-provider.js";
import { LocalProvider } from "../providers/workspace/local-provider.js";

/**
 * Resolve a WorkspaceProvider from the SWEContext config.
 *
 * Falls back to WorktreeProvider if the config doesn't specify a provider,
 * or to LocalProvider for "local" / "existing" strategies.
 */
function resolveWorkspaceProvider(ctx: SWEContext): WorkspaceProvider {
  const cfg = ctx.config.workspace;
  // Prefer the new `provider` field; fall back to the deprecated `strategy`
  // field for backwards compatibility. Cast to string because the two enums
  // have different literal unions.
  const providerName: string = cfg.provider ?? cfg.strategy ?? "worktree";

  if (providerName === "local" || providerName === "existing") {
    return new LocalProvider({
      cwd: ctx.cwd,
      defaultTimeout: cfg.defaultTimeout ?? cfg.cleanupAfterSeconds,
    });
  }

  // Default: worktree (also covers "clone" / "docker" as future expansions
  // that still fall back to worktree until those providers are implemented).
  return new WorktreeProvider({
    baseDir: cfg.worktreeDir,
    repoCwd: ctx.cwd,
    defaultTimeout: cfg.defaultTimeout ?? cfg.cleanupAfterSeconds,
    maxConcurrent: cfg.maxConcurrent,
  });
}

export function createManageWorkspaceTool(ctx: SWEContext) {
  return defineTool({
    name: "manage_workspace",
    label: "Manage Workspace",
    description:
      "Create, cleanup, or list isolated workspaces for development. " +
      "Supports worktree, local, clone, and docker strategies via providers.",
    parameters: Type.Object({
      action: Type.Union(
        [
          Type.Literal("create"),
          Type.Literal("cleanup"),
          Type.Literal("list"),
          Type.Literal("get"),
        ],
        { description: "Action to perform: create, cleanup, list, or get" },
      ),
      repo: Type.Optional(
        Type.String({
          description: "Repository for create action (owner/repo)",
        }),
      ),
      ticketId: Type.Optional(
        Type.String({ description: "Ticket ID for create action" }),
      ),
      branch: Type.Optional(
        Type.String({ description: "Branch name for create action" }),
      ),
      workspaceId: Type.Optional(
        Type.String({
          description: "Workspace ID for cleanup or get action",
        }),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _extCtx) {
      const provider = resolveWorkspaceProvider(ctx);

      try {
        switch (params.action) {
          case "create": {
            const ticketId = params.ticketId ?? `manual-${Date.now()}`;
            const repo = params.repo ?? "unknown/repo";

            const workspace = await provider.create({
              ticketId,
              repo,
              branch: params.branch,
            });

            return {
              content: [
                {
                  type: "text" as const,
                  text:
                    `Workspace created: ${workspace.path}\n` +
                    `  ID: ${workspace.id}\n` +
                    `  Branch: ${workspace.branch}\n` +
                    `  Strategy: ${workspace.strategy}\n` +
                    `  Expires: ${workspace.expiresAt ?? "never"}`,
                },
              ],
              details: workspace,
            };
          }

          case "cleanup": {
            if (!params.workspaceId) {
              return {
                content: [
                  {
                    type: "text" as const,
                    text: "Error: workspaceId is required for cleanup",
                  },
                ],
                details: {},
              };
            }

            const ok = await provider.cleanup(params.workspaceId);
            return {
              content: [
                {
                  type: "text" as const,
                  text: ok
                    ? `Workspace ${params.workspaceId} cleaned up.`
                    : `Workspace ${params.workspaceId} not found.`,
                },
              ],
              details: { removed: ok },
            };
          }

          case "get": {
            if (!params.workspaceId) {
              return {
                content: [
                  {
                    type: "text" as const,
                    text: "Error: workspaceId is required for get",
                  },
                ],
                details: {},
              };
            }

            const ws = await provider.get(params.workspaceId);
            if (!ws) {
              return {
                content: [
                  {
                    type: "text" as const,
                    text: `Workspace ${params.workspaceId} not found.`,
                  },
                ],
                details: {},
              };
            }

            return {
              content: [
                {
                  type: "text" as const,
                  text: JSON.stringify(ws, null, 2),
                },
              ],
              details: ws,
            };
          }

          case "list": {
            const workspaces = await provider.list();

            if (workspaces.length === 0) {
              return {
                content: [
                  { type: "text" as const, text: "No active workspaces." },
                ],
                details: { workspaces: [] },
              };
            }

            const summary = workspaces.map((ws) => ({
              id: ws.id,
              ticketId: ws.ticketId,
              branch: ws.branch,
              path: ws.path,
              strategy: ws.strategy,
              status: ws.status,
              expiresAt: ws.expiresAt,
            }));

            return {
              content: [
                {
                  type: "text" as const,
                  text: JSON.stringify(summary, null, 2),
                },
              ],
              details: { workspaces: summary },
            };
          }
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        return {
          content: [
            { type: "text" as const, text: `Workspace error: ${message}` },
          ],
          details: {},
        };
      }
    },
  });
}

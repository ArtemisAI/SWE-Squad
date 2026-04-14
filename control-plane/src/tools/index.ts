/**
 * Tool barrel — creates all 15 SWE-Manager custom tools.
 *
 * Each tool receives the shared SWEContext and returns a ToolDefinition
 * that pi-agent registers with the LLM session.
 *
 * Pipeline coverage:
 *   triage -> investigate -> develop -> review -> test -> approve -> merge
 */

import type { SWEContext } from "../shared/context.js";
import { createTicketListTool } from "./ticket-list.js";
import { createTicketUpdateTool } from "./ticket-update.js";
import { createTicketCreateTool } from "./ticket-create.js";
import { createGithubIssuesTool } from "./github-issues.js";
import { createGithubImportTool } from "./github-import.js";
import { createDelegateInvestigationTool } from "./delegate-investigation.js";
import { createDelegateDevelopmentTool } from "./delegate-development.js";
import { createDelegateReviewTool } from "./delegate-review.js";
import { createRunTestsTool } from "./run-tests.js";
import { createApprovePrTool } from "./approve-pr.js";
import { createMergePrTool } from "./merge-pr.js";
import { createCheckStabilityTool } from "./check-stability.js";
import { createCheckHealthTool } from "./check-health.js";
import { createCheckMetricsTool } from "./check-metrics.js";
import { createSendNotificationTool } from "./send-notification.js";
import { createManageWorkspaceTool } from "./manage-workspace.js";

/**
 * Create all SWE-Manager tools, wired to the given context.
 *
 * Returns an array of ToolDefinitions ready for `createAgentSession({ customTools })`.
 */
export function createAllTools(ctx: SWEContext) {
  return [
    // Ticket management
    createTicketListTool(ctx),
    createTicketUpdateTool(ctx),
    createTicketCreateTool(ctx),
    // GitHub integration
    createGithubIssuesTool(ctx),
    createGithubImportTool(ctx),
    // Pipeline: investigate -> develop -> review -> test -> approve -> merge
    createDelegateInvestigationTool(ctx),
    createDelegateDevelopmentTool(ctx),
    createDelegateReviewTool(ctx),
    createRunTestsTool(ctx),
    createApprovePrTool(ctx),
    createMergePrTool(ctx),
    // Safety & operations
    createCheckStabilityTool(ctx),
    createCheckHealthTool(ctx),
    createCheckMetricsTool(ctx),
    createSendNotificationTool(ctx),
    createManageWorkspaceTool(ctx),
  ];
}

/** Expected tool count for architecture harness tests. */
export const EXPECTED_TOOL_COUNT = 16;

/**
 * Data models for the Autonomous SWE Team — TypeScript / Zod port.
 *
 * Defines tickets, agent roles, severity levels, and governance structures
 * used to coordinate the autonomous development lifecycle:
 *   detect -> triage -> investigate -> develop -> test -> deploy -> monitor
 *
 * Ported from: src/swe_team/models.py
 */

import { z } from "zod";
import crypto from "node:crypto";

// ---------------------------------------------------------------------------
// Enums (const objects + type unions for zero-runtime overhead)
// ---------------------------------------------------------------------------

export const TicketSeverity = {
  CRITICAL: "critical",
  HIGH: "high",
  MEDIUM: "medium",
  LOW: "low",
} as const;
export type TicketSeverity = (typeof TicketSeverity)[keyof typeof TicketSeverity];

export const TicketSeveritySchema = z.enum(["critical", "high", "medium", "low"]);

export const TicketStatus = {
  OPEN: "open",
  TRIAGED: "triaged",
  NEEDS_INFO: "needs_info",
  BLOCKED: "blocked",
  ACKNOWLEDGED: "acknowledged",
  INVESTIGATING: "investigating",
  INVESTIGATION_COMPLETE: "investigation_complete",
  IN_DEVELOPMENT: "in_development",
  IN_REVIEW: "in_review",
  REWORK_REQUESTED: "rework_requested",
  TESTING: "testing",
  DEPLOYING: "deploying",
  MONITORING: "monitoring",
  RESOLVED: "resolved",
  ROLLED_BACK: "rolled_back",
  CLOSED: "closed",
  VERIFYING: "verifying",
  FAILED: "failed",
} as const;
export type TicketStatus = (typeof TicketStatus)[keyof typeof TicketStatus];

export const TicketStatusSchema = z.enum([
  "open",
  "triaged",
  "needs_info",
  "blocked",
  "acknowledged",
  "investigating",
  "investigation_complete",
  "in_development",
  "in_review",
  "rework_requested",
  "testing",
  "deploying",
  "monitoring",
  "resolved",
  "rolled_back",
  "closed",
  "verifying",
  "failed",
]);

export const TicketType = {
  BUG: "bug",
  FEATURE: "feature",
  ENHANCEMENT: "enhancement",
  INFRASTRUCTURE: "infrastructure",
  DOCUMENTATION: "documentation",
  QUESTION: "question",
  SECURITY: "security",
  REGRESSION: "regression",
  UNKNOWN: "unknown",
} as const;
export type TicketType = (typeof TicketType)[keyof typeof TicketType];

export const TicketTypeSchema = z.enum([
  "bug",
  "feature",
  "enhancement",
  "infrastructure",
  "documentation",
  "question",
  "security",
  "regression",
  "unknown",
]);

export const AgentRole = {
  MONITOR: "monitor",
  TRIAGE: "triage",
  INVESTIGATOR: "investigator",
  DEVELOPER: "developer",
  REVIEWER: "reviewer",
  QA: "qa",
  TESTER: "tester",
  DEPLOYER: "deployer",
  DOCUMENTER: "documenter",
  CREATIVE: "creative",
} as const;
export type AgentRole = (typeof AgentRole)[keyof typeof AgentRole];

export const AgentRoleSchema = z.enum([
  "monitor",
  "triage",
  "investigator",
  "developer",
  "reviewer",
  "qa",
  "tester",
  "deployer",
  "documenter",
  "creative",
]);

export const GovernanceVerdict = {
  PASS: "pass",
  BLOCK: "block",
  WARN: "warn",
} as const;
export type GovernanceVerdict = (typeof GovernanceVerdict)[keyof typeof GovernanceVerdict];

export const GovernanceVerdictSchema = z.enum(["pass", "block", "warn"]);

export const EdgeType = {
  SIMILAR: "similar",
  TOUCHES_MODULE: "touches_module",
  BLOCKS: "blocks",
  RESOLVES: "resolves",
  CONFLICTS_WITH: "conflicts_with",
  CAUSED_REGRESSION: "caused_regression",
} as const;
export type EdgeType = (typeof EdgeType)[keyof typeof EdgeType];

export const EdgeTypeSchema = z.enum([
  "similar",
  "touches_module",
  "blocks",
  "resolves",
  "conflicts_with",
  "caused_regression",
]);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function randomHex(bytes: number): string {
  return crypto.randomBytes(bytes).toString("hex");
}

function nowISO(): string {
  return new Date().toISOString();
}

// ---------------------------------------------------------------------------
// Schemas + Types
// ---------------------------------------------------------------------------

export const SWETicketSchema = z.object({
  ticketId: z.string().default(() => randomHex(6)),
  title: z.string(),
  description: z.string(),
  severity: TicketSeveritySchema.default("medium"),
  status: TicketStatusSchema.default("open"),
  createdAt: z.string().default(nowISO),
  updatedAt: z.string().default(nowISO),
  assignedTo: z.string().nullable().default(null),
  labels: z.array(z.string()).default([]),
  ticketType: TicketTypeSchema.default("unknown"),
  sourceModule: z.string().nullable().default(null),
  errorLog: z.string().nullable().default(null),
  relatedTickets: z.array(z.string()).default([]),
  blockedBy: z.array(z.string()).default([]),
  blocking: z.array(z.string()).default([]),
  metadata: z.record(z.string(), z.unknown()).default({}),
  investigationReport: z.string().nullable().default(null),
  proposedFix: z.string().nullable().default(null),
  testResults: z.record(z.string(), z.unknown()).nullable().default(null),
  deploymentId: z.string().nullable().default(null),
  rollbackReason: z.string().nullable().default(null),
  investigationSessionId: z.string().nullable().default(null),
  developmentSessionId: z.string().nullable().default(null),
  projectId: z.string().nullable().default(null),
  parentTicketId: z.string().nullable().default(null),
  goal: z.string().nullable().default(null),
});

export type SWETicket = z.infer<typeof SWETicketSchema>;

export const HandoverConstraintsSchema = z.object({
  budgetRemainingUsd: z.number(),
  timeLimitSeconds: z.number().int(),
  modelTier: z.string(),
  retryCount: z.number().int(),
  maxRetries: z.number().int(),
});

export type HandoverConstraints = z.infer<typeof HandoverConstraintsSchema>;

export const InvestigationPhaseOutputSchema = z.object({
  rootCause: z.string(),
  affectedFiles: z.array(z.string()).default([]),
  suggestedFix: z.string().default(""),
  confidence: z.number().default(0),
});

export type InvestigationPhaseOutput = z.infer<typeof InvestigationPhaseOutputSchema>;

export const DevelopmentPhaseOutputSchema = z.object({
  branch: z.string(),
  diff: z.string().default(""),
  testResults: z.record(z.string(), z.unknown()).default({}),
  commitMessage: z.string().default(""),
});

export type DevelopmentPhaseOutput = z.infer<typeof DevelopmentPhaseOutputSchema>;

export const VerificationPhaseOutputSchema = z.object({
  verdict: z.string(),
  testOutput: z.string().default(""),
  regressionCheck: z.record(z.string(), z.unknown()).default({}),
});

export type VerificationPhaseOutput = z.infer<typeof VerificationPhaseOutputSchema>;

export const StabilityReportSchema = z.object({
  verdict: GovernanceVerdictSchema,
  openCritical: z.number().int().default(0),
  openHigh: z.number().int().default(0),
  failingTests: z.number().int().default(0),
  ciStatus: z.string().default("unknown"),
  details: z.string().default(""),
  checkedAt: z.string().default(nowISO),
});

export type StabilityReport = z.infer<typeof StabilityReportSchema>;

export const KnowledgeEdgeSchema = z.object({
  sourceId: z.string(),
  targetId: z.string(),
  edgeType: EdgeTypeSchema,
  confidence: z.number().default(0),
  discoveredAt: z.string().default(nowISO),
  discoveredBy: z.string().default(""),
  metadata: z.record(z.string(), z.unknown()).default({}),
});

export type KnowledgeEdge = z.infer<typeof KnowledgeEdgeSchema>;

export const SWEAgentConfigSchema = z.object({
  name: z.string(),
  role: AgentRoleSchema,
  description: z.string().default(""),
  model: z.string().default("sonnet"),
  tools: z.array(z.string()).default([]),
  maxConcurrentTasks: z.number().int().default(1),
  enabled: z.boolean().default(false),
  node: z.string().default("primary"),
});

export type SWEAgentConfig = z.infer<typeof SWEAgentConfigSchema>;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Resolution bypass reasons that satisfy the audit gate without a full report.
 * Any ticket resolved with one of these notes is considered legitimately closed.
 */
export const RESOLUTION_BYPASS_REASONS: ReadonlySet<string> = new Set([
  "false_regression",
  "duplicate",
  "already_fixed_externally",
  "not_reproducible",
  "wont_fix_approved",
  "manual_override",
  "fix_succeeded",
]);

/** Statuses considered "open" (ticket is not yet resolved/closed/failed). */
export const OPEN_STATUSES: ReadonlySet<TicketStatus> = new Set<TicketStatus>([
  TicketStatus.OPEN,
  TicketStatus.TRIAGED,
  TicketStatus.NEEDS_INFO,
  TicketStatus.BLOCKED,
  TicketStatus.ACKNOWLEDGED,
  TicketStatus.INVESTIGATING,
  TicketStatus.INVESTIGATION_COMPLETE,
  TicketStatus.IN_DEVELOPMENT,
  TicketStatus.IN_REVIEW,
  TicketStatus.REWORK_REQUESTED,
  TicketStatus.TESTING,
  TicketStatus.DEPLOYING,
  TicketStatus.MONITORING,
  TicketStatus.VERIFYING,
]);

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

/**
 * Create a new SWETicket with defaults applied.
 * Only `title` and `description` are required; everything else uses schema defaults.
 */
export function createTicket(
  title: string,
  description: string,
  overrides?: Partial<SWETicket>,
): SWETicket {
  return SWETicketSchema.parse({
    title,
    description,
    ...overrides,
  });
}

/** Returns true if the ticket has unresolved blockers. */
export function isBlocked(ticket: SWETicket): boolean {
  return ticket.blockedBy.length > 0;
}

/**
 * Check whether a ticket may legitimately be closed as RESOLVED.
 *
 * Returns `[ok, reason]`. `ok === false` means the transition should be blocked;
 * `reason` is a human-readable explanation.
 *
 * Rules:
 * 1. A recognised bypass note in `metadata.resolution_note` always permits closure.
 * 2. Otherwise the ticket must have an investigation report of at least 200 characters.
 * 3. HIGH / CRITICAL tickets additionally need at least one fix attempt OR an
 *    explicit bypass note.
 */
export function resolutionAudit(ticket: SWETicket): [ok: boolean, reason: string] {
  const note = String(
    (ticket.metadata as Record<string, unknown>)?.resolution_note ?? "",
  ).toLowerCase();

  for (const bypass of RESOLUTION_BYPASS_REASONS) {
    if (note.includes(bypass)) {
      return [true, `bypass: ${note.slice(0, 80)}`];
    }
  }

  const report = ticket.investigationReport ?? "";
  if (report.length < 200) {
    return [
      false,
      `investigation_report too short (${report.length} chars, need >=200). ` +
        "Investigate first or set metadata.resolution_note to a bypass reason: " +
        [...RESOLUTION_BYPASS_REASONS].sort().join(", "),
    ];
  }

  if (
    ticket.severity === TicketSeverity.HIGH ||
    ticket.severity === TicketSeverity.CRITICAL
  ) {
    const attempts = (ticket.metadata as Record<string, unknown>)?.attempts;
    if (!Array.isArray(attempts) || attempts.length === 0) {
      return [
        false,
        `${ticket.severity.toUpperCase()} ticket requires >=1 fix attempt before RESOLVED. ` +
          "Attempts list is empty. Run developer agent or set resolution_note bypass.",
      ];
    }
  }

  return [true, "audit passed"];
}

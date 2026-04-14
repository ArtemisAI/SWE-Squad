/**
 * Zod schemas for SWE-Squad configuration.
 *
 * Direct port of the Python dataclasses in src/swe_team/config.py and
 * src/swe_team/throttle.py. Every schema has a matching inferred type
 * exported alongside it.
 */

import { z } from "zod";

// ---------------------------------------------------------------------------
// GovernanceConfig
// ---------------------------------------------------------------------------

export const GovernanceConfigSchema = z.object({
  maxOpenCritical: z.number().int().default(0),
  maxOpenHigh: z.number().int().default(3),
  maxFailingTests: z.number().int().default(0),
  requireCiGreen: z.boolean().default(true),
  checkIntervalHours: z.number().int().default(6),
  enabled: z.boolean().default(false),
});

export type GovernanceConfig = z.infer<typeof GovernanceConfigSchema>;

// ---------------------------------------------------------------------------
// MonitorConfig
// ---------------------------------------------------------------------------

export const MonitorConfigSchema = z.object({
  logDirectories: z.array(z.string()).default(["logs/", "data/a2a/"]),
  logPatterns: z
    .array(z.string())
    .default(["ERROR", "CRITICAL", "Traceback", "FAILED"]),
  excludePatterns: z
    .array(z.string())
    .default(["swe_team", "swe_team_runner"]),
  scanIntervalMinutes: z.number().int().default(30),
  dedupWindowHours: z.number().int().default(24),
  enabled: z.boolean().default(false),
  remoteWorkers: z.array(z.record(z.string())).default([]),
  workerModuleMap: z.record(z.array(z.string())).default({}),
});

export type MonitorConfig = z.infer<typeof MonitorConfigSchema>;

// ---------------------------------------------------------------------------
// ModelConfig
// ---------------------------------------------------------------------------

export const ModelConfigSchema = z.object({
  t1Heavy: z.string().default("opus"),
  t2Standard: z.string().default("sonnet"),
  t3Fast: z.string().default("haiku"),
});

export type ModelConfig = z.infer<typeof ModelConfigSchema>;

// ---------------------------------------------------------------------------
// RateLimitConfig
// ---------------------------------------------------------------------------

export const RateLimitConfigSchema = z.object({
  maxRetriesOn429: z.number().int().default(5),
  initialBackoffSeconds: z.number().default(60),
  maxBackoffSeconds: z.number().default(900),
});

export type RateLimitConfig = z.infer<typeof RateLimitConfigSchema>;

// ---------------------------------------------------------------------------
// CycleConfig
// ---------------------------------------------------------------------------

export const CycleConfigSchema = z.object({
  maxNewTicketsPerCycle: z.number().int().default(20),
  maxInvestigationsPerCycle: z.number().int().default(5),
  maxDevelopmentsPerCycle: z.number().int().default(2),
  maxOpenInvestigating: z.number().int().default(3),
  severityFilter: z.string().default("high"),
  maxInvestigationWorkers: z.number().int().default(8),
  maxReinvestigations: z.number().int().default(1),
  blockedTicketTimeoutHours: z.number().int().default(4),
  blockedTicketEscalationHours: z.number().int().default(24),
});

export type CycleConfig = z.infer<typeof CycleConfigSchema>;

// ---------------------------------------------------------------------------
// MemoryConfig
// ---------------------------------------------------------------------------

export const MemoryConfigSchema = z.object({
  embeddingModel: z.string().default("bge-m3"),
  embeddingDimensions: z.number().int().default(1024),
  topK: z.number().int().default(5),
  similarityFloor: z.number().default(0.75),
  storeOnInvestigationComplete: z.boolean().default(true),
  autoResolveThreshold: z.number().default(0.9),
  clusterThreshold: z.number().default(0.85),
  dedupThreshold: z.number().default(0.92),
  similarityEdgeThreshold: z.number().default(0.8),

  // Multi-tenant memory service settings
  provider: z
    .enum(["in-memory", "supabase", "qdrant", "chroma", "weaviate"])
    .default("in-memory"),
  defaultProjectId: z.string().default("default"),
  ttlDays: z.number().int().default(180),
  maxEntriesPerProject: z.number().int().default(10000),
  confidenceIncrement: z.number().default(0.1),
  maxConfidence: z.number().default(2.0),
});

export type MemoryConfig = z.infer<typeof MemoryConfigSchema>;

// ---------------------------------------------------------------------------
// ThrottleConfig
// ---------------------------------------------------------------------------

export const TimeBandSchema = z.object({
  startHour: z.number().int(),
  endHour: z.number().int(),
  multiplier: z.number(),
  timezone: z.string().optional(),
});

export type TimeBand = z.infer<typeof TimeBandSchema>;

export const ThrottleConfigSchema = z.object({
  enabled: z.boolean().default(false),
  weeklyBudgetUsd: z.number().default(500.0),
  backlogSurgeThreshold: z.number().int().default(200),
  criticalSurgeThreshold: z.number().int().default(20),
  timeBands: z
    .record(TimeBandSchema)
    .default({}),
  capacityWarningPct: z.number().default(0.8),
  capacityWarningDaysRemaining: z.number().default(2.0),
  capacityWarningMultiplier: z.number().default(0.5),
  capacityCriticalPct: z.number().default(0.95),
  capacityCriticalMultiplier: z.number().default(0.1),
  backlogSurgeMultiplier: z.number().default(1.5),
  criticalSurgeMultiplier: z.number().default(2.0),
});

export type ThrottleConfig = z.infer<typeof ThrottleConfigSchema>;

// ---------------------------------------------------------------------------
// FallbackAgentConfig
// ---------------------------------------------------------------------------

export const FallbackAgentConfigSchema = z.object({
  name: z.string().default(""),
  command: z.string().default(""),
  argsTemplate: z.array(z.string()).default([]),
  defaultModel: z.string().default(""),
  enabled: z.boolean().default(false),
  priority: z.number().int().default(100),
  timeout: z.number().int().default(120),
  promptViaStdin: z.boolean().default(false),
  skills: z.array(z.string()).default([]),
});

export type FallbackAgentConfig = z.infer<typeof FallbackAgentConfigSchema>;

// ---------------------------------------------------------------------------
// TeamConfig
// ---------------------------------------------------------------------------

export const TeamConfigSchema = z.object({
  name: z.string(),
  vm: z.string().default(""),
  githubAccount: z.string().default(""),
  role: z.enum(["developer", "investigator", "full"]).default("full"),
  maxConcurrent: z.number().int().positive().default(3),
  costBudgetDaily: z.number().positive().default(50.0),
  specialization: z.array(z.string()).default([]),
});

export type TeamConfig = z.infer<typeof TeamConfigSchema>;

// ---------------------------------------------------------------------------
// StaleTicketTimeoutsConfig
// ---------------------------------------------------------------------------

export const StaleTicketTimeoutsConfigSchema = z.object({
  investigatingHours: z.number().int().default(4),
  inDevelopmentHours: z.number().int().default(2),
  inReviewHours: z.number().int().default(24),
});

export type StaleTicketTimeoutsConfig = z.infer<
  typeof StaleTicketTimeoutsConfigSchema
>;

// ---------------------------------------------------------------------------
// AgentDelegationConfig (V2 — per-role engine binding)
// ---------------------------------------------------------------------------

export const AgentDelegationEntrySchema = z.object({
  engine: z.string().default("claude-cli"),
  model: z.string().optional(),
  readOnly: z.boolean().default(false),
  timeout: z.number().int().default(1800),
  fallbackEngine: z.string().optional(),
});

export type AgentDelegationEntry = z.infer<typeof AgentDelegationEntrySchema>;

export const DelegationConfigSchema = z
  .record(AgentDelegationEntrySchema)
  .default({});

export type DelegationConfig = z.infer<typeof DelegationConfigSchema>;

// ---------------------------------------------------------------------------
// WorkspaceConfig (V2 — workspace provisioning)
// ---------------------------------------------------------------------------

export const WorkspaceConfigSchema = z.object({
  provider: z
    .enum(["worktree", "local", "clone", "docker"])
    .default("worktree"),
  baseDir: z.string().default("~/Projects"),
  worktreeDir: z.string().default("/tmp/swe-ws"),
  /** @deprecated Use `provider` instead. Kept for backwards compat. */
  strategy: z.enum(["existing", "worktree", "clone"]).default("worktree"),
  cleanupAfterSeconds: z.number().int().default(3600),
  maxConcurrent: z.number().int().default(5),
  defaultTimeout: z.number().int().default(3600),
  cleanupInterval: z.number().int().default(300),
});

export type WorkspaceConfig = z.infer<typeof WorkspaceConfigSchema>;

// ---------------------------------------------------------------------------
// DaemonConfig (V2 — heartbeat / session lifecycle)
// ---------------------------------------------------------------------------

export const DaemonConfigSchema = z.object({
  heartbeatIntervalSeconds: z.number().int().default(300),
  initialPrompt: z
    .string()
    .default(
      "Management cycle. Steps: " +
      "1) call check_health + check_stability. " +
      "2) call github_import for each configured repo to import any new GitHub issues as tickets. " +
      "3) call ticket_list with status=pipeline to see the full pipeline in ONE call. " +
      "4) Advance ONE ticket through the pipeline — pick by priority (flush right-to-left, complete nearest-done first): " +
      "testing with review approved → run_tests then approve_pr + merge_pr, " +
      "in_review → delegate_review, " +
      "investigation_complete → delegate_development, " +
      "in_development with metadata.reviewVerdict=changes_requested → delegate_development (rework), " +
      "in_development with no assigned_to and no reviewVerdict → ticket_update status=investigation_complete (orphaned, reset for dev retry), " +
      "investigating with no assigned_to → ticket_update status=open (orphaned, reset for retry), " +
      "open/triaged → delegate_investigation (skip tickets with investigationAttempts >= 3). " +
      "5) If stability=BLOCK or all engines unhealthy, send_notification and stop. " +
      "Take action — call one delegation tool. Do NOT write reports or use bash. CALL THE TOOL.",
    ),
  maxSessionAge: z.number().int().default(86400),
  gracefulShutdownTimeout: z.number().int().default(30),
});

export type DaemonConfig = z.infer<typeof DaemonConfigSchema>;

// ---------------------------------------------------------------------------
// NotificationConfig (V2 — provider-agnostic notifications)
// ---------------------------------------------------------------------------

export const NotificationConfigSchema = z.object({
  provider: z
    .enum(["telegram", "slack", "webhook", "none"])
    .default("telegram"),
  telegram: z
    .object({
      botToken: z.string().optional(),
      chatId: z.string().optional(),
    })
    .default({}),
  slack: z
    .object({
      webhookUrl: z.string().optional(),
      channel: z.string().optional(),
    })
    .default({}),
  webhook: z
    .object({
      url: z.string().optional(),
    })
    .default({}),
});

export type NotificationConfig = z.infer<typeof NotificationConfigSchema>;

// ---------------------------------------------------------------------------
// SWETeamConfig (top-level)
// ---------------------------------------------------------------------------

export const SWETeamConfigSchema = z.object({
  governance: GovernanceConfigSchema.default({}),
  monitor: MonitorConfigSchema.default({}),
  models: ModelConfigSchema.default({}),
  rateLimits: RateLimitConfigSchema.default({}),
  cycle: CycleConfigSchema.default({}),
  memory: MemoryConfigSchema.default({}),
  throttle: ThrottleConfigSchema.default({}),
  fallbackAgents: z.array(FallbackAgentConfigSchema).default([]),
  teams: z.record(TeamConfigSchema).default({}),
  staleTicketTimeouts: StaleTicketTimeoutsConfigSchema.default({}),
  repos: z.array(z.record(z.unknown())).default([]),
  githubRepos: z.array(z.string()).default([]),
  ticketStorePath: z.string().default("data/swe_team/tickets.json"),
  a2aHubUrl: z.string().default("http://localhost:18790"),
  enabled: z.boolean().default(false),
  teamId: z.string().default("default"),
  githubAccount: z.string().default(""),
  regressionWindowHours: z.number().int().default(24),
  autoAcceptInvites: z.boolean().default(false),
  inviteAllowlist: z.array(z.string()).default([]),

  // V2: Engine-agnostic delegation, workspace, daemon, notification
  delegation: DelegationConfigSchema.default({}),
  workspace: WorkspaceConfigSchema.default({}),
  daemon: DaemonConfigSchema.default({}),
  notification: NotificationConfigSchema.default({}),
});

export type SWETeamConfig = z.infer<typeof SWETeamConfigSchema>;

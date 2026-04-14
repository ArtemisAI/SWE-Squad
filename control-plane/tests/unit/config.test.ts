/**
 * Unit tests for config schemas (schemas.ts) and config loader (loader.ts).
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import {
  GovernanceConfigSchema,
  MonitorConfigSchema,
  ModelConfigSchema,
  RateLimitConfigSchema,
  CycleConfigSchema,
  MemoryConfigSchema,
  ThrottleConfigSchema,
  FallbackAgentConfigSchema,
  TeamConfigSchema,
  StaleTicketTimeoutsConfigSchema,
  SWETeamConfigSchema,
} from "../../src/config/schemas.js";

import { snakeToCamel, loadConfig } from "../../src/config/loader.js";

// -------------------------------------------------------------------------
// Schema defaults
// -------------------------------------------------------------------------

describe("GovernanceConfigSchema", () => {
  it("parses empty object with correct defaults", () => {
    const result = GovernanceConfigSchema.parse({});
    expect(result.maxOpenCritical).toBe(0);
    expect(result.maxOpenHigh).toBe(3);
    expect(result.maxFailingTests).toBe(0);
    expect(result.requireCiGreen).toBe(true);
    expect(result.checkIntervalHours).toBe(6);
    expect(result.enabled).toBe(false);
  });

  it("overrides defaults with provided values", () => {
    const result = GovernanceConfigSchema.parse({
      maxOpenCritical: 5,
      maxOpenHigh: 10,
      requireCiGreen: false,
    });
    expect(result.maxOpenCritical).toBe(5);
    expect(result.maxOpenHigh).toBe(10);
    expect(result.requireCiGreen).toBe(false);
    // Remaining fields still get defaults
    expect(result.maxFailingTests).toBe(0);
    expect(result.enabled).toBe(false);
  });
});

describe("ModelConfigSchema", () => {
  it("parses empty object with correct defaults", () => {
    const result = ModelConfigSchema.parse({});
    expect(result.t1Heavy).toBe("opus");
    expect(result.t2Standard).toBe("sonnet");
    expect(result.t3Fast).toBe("haiku");
  });

  it("accepts custom model names", () => {
    const result = ModelConfigSchema.parse({
      t1Heavy: "gpt-4o",
      t2Standard: "claude-3-sonnet",
      t3Fast: "gemini-flash",
    });
    expect(result.t1Heavy).toBe("gpt-4o");
    expect(result.t2Standard).toBe("claude-3-sonnet");
    expect(result.t3Fast).toBe("gemini-flash");
  });
});

describe("CycleConfigSchema", () => {
  it("parses empty object with correct defaults", () => {
    const result = CycleConfigSchema.parse({});
    expect(result.maxNewTicketsPerCycle).toBe(20);
    expect(result.maxInvestigationsPerCycle).toBe(5);
    expect(result.maxDevelopmentsPerCycle).toBe(2);
    expect(result.maxOpenInvestigating).toBe(3);
    expect(result.severityFilter).toBe("high");
    expect(result.maxInvestigationWorkers).toBe(8);
    expect(result.maxReinvestigations).toBe(1);
    expect(result.blockedTicketTimeoutHours).toBe(4);
    expect(result.blockedTicketEscalationHours).toBe(24);
  });
});

describe("MonitorConfigSchema", () => {
  it("parses empty object with correct defaults", () => {
    const result = MonitorConfigSchema.parse({});
    expect(result.logDirectories).toEqual(["logs/", "data/a2a/"]);
    expect(result.logPatterns).toEqual([
      "ERROR",
      "CRITICAL",
      "Traceback",
      "FAILED",
    ]);
    expect(result.excludePatterns).toEqual(["swe_team", "swe_team_runner"]);
    expect(result.scanIntervalMinutes).toBe(30);
    expect(result.dedupWindowHours).toBe(24);
    expect(result.enabled).toBe(false);
    expect(result.remoteWorkers).toEqual([]);
    expect(result.workerModuleMap).toEqual({});
  });
});

describe("RateLimitConfigSchema", () => {
  it("parses empty object with correct defaults", () => {
    const result = RateLimitConfigSchema.parse({});
    expect(result.maxRetriesOn429).toBe(5);
    expect(result.initialBackoffSeconds).toBe(60);
    expect(result.maxBackoffSeconds).toBe(900);
  });
});

describe("MemoryConfigSchema", () => {
  it("parses empty object with correct defaults", () => {
    const result = MemoryConfigSchema.parse({});
    expect(result.embeddingModel).toBe("bge-m3");
    expect(result.embeddingDimensions).toBe(1024);
    expect(result.topK).toBe(5);
    expect(result.similarityFloor).toBe(0.75);
    expect(result.storeOnInvestigationComplete).toBe(true);
    expect(result.autoResolveThreshold).toBe(0.9);
    expect(result.clusterThreshold).toBe(0.85);
    expect(result.dedupThreshold).toBe(0.92);
    expect(result.similarityEdgeThreshold).toBe(0.8);
  });
});

describe("ThrottleConfigSchema", () => {
  it("parses empty object with correct defaults", () => {
    const result = ThrottleConfigSchema.parse({});
    expect(result.enabled).toBe(false);
    expect(result.weeklyBudgetUsd).toBe(500.0);
    expect(result.backlogSurgeThreshold).toBe(200);
    expect(result.criticalSurgeThreshold).toBe(20);
    expect(result.timeBands).toEqual({});
    expect(result.capacityWarningPct).toBe(0.8);
    expect(result.capacityCriticalPct).toBe(0.95);
  });
});

describe("FallbackAgentConfigSchema", () => {
  it("parses empty object with correct defaults", () => {
    const result = FallbackAgentConfigSchema.parse({});
    expect(result.name).toBe("");
    expect(result.command).toBe("");
    expect(result.argsTemplate).toEqual([]);
    expect(result.defaultModel).toBe("");
    expect(result.enabled).toBe(false);
    expect(result.priority).toBe(100);
    expect(result.timeout).toBe(120);
    expect(result.promptViaStdin).toBe(false);
    expect(result.skills).toEqual([]);
  });
});

describe("StaleTicketTimeoutsConfigSchema", () => {
  it("parses empty object with correct defaults", () => {
    const result = StaleTicketTimeoutsConfigSchema.parse({});
    expect(result.investigatingHours).toBe(4);
    expect(result.inDevelopmentHours).toBe(2);
    expect(result.inReviewHours).toBe(24);
  });
});

// -------------------------------------------------------------------------
// TeamConfigSchema — role validation
// -------------------------------------------------------------------------

describe("TeamConfigSchema", () => {
  it("accepts role 'developer'", () => {
    const result = TeamConfigSchema.parse({ name: "alpha", role: "developer" });
    expect(result.role).toBe("developer");
  });

  it("accepts role 'investigator'", () => {
    const result = TeamConfigSchema.parse({
      name: "beta",
      role: "investigator",
    });
    expect(result.role).toBe("investigator");
  });

  it("accepts role 'full'", () => {
    const result = TeamConfigSchema.parse({ name: "gamma", role: "full" });
    expect(result.role).toBe("full");
  });

  it("defaults role to 'full' when not provided", () => {
    const result = TeamConfigSchema.parse({ name: "delta" });
    expect(result.role).toBe("full");
  });

  it("rejects invalid role", () => {
    const result = TeamConfigSchema.safeParse({
      name: "bad",
      role: "admin",
    });
    expect(result.success).toBe(false);
  });

  it("requires name field", () => {
    const result = TeamConfigSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("applies numeric defaults", () => {
    const result = TeamConfigSchema.parse({ name: "t1" });
    expect(result.maxConcurrent).toBe(3);
    expect(result.costBudgetDaily).toBe(50.0);
    expect(result.specialization).toEqual([]);
  });
});

// -------------------------------------------------------------------------
// SWETeamConfigSchema — full parse
// -------------------------------------------------------------------------

describe("SWETeamConfigSchema", () => {
  it("parses empty object with all nested defaults", () => {
    const config = SWETeamConfigSchema.parse({});

    // Top-level scalar defaults
    expect(config.enabled).toBe(false);
    expect(config.teamId).toBe("default");
    expect(config.githubAccount).toBe("");
    expect(config.ticketStorePath).toBe("data/swe_team/tickets.json");
    expect(config.a2aHubUrl).toBe("http://localhost:18790");
    expect(config.regressionWindowHours).toBe(24);
    expect(config.autoAcceptInvites).toBe(false);
    expect(config.inviteAllowlist).toEqual([]);
    expect(config.githubRepos).toEqual([]);
    expect(config.repos).toEqual([]);
    expect(config.fallbackAgents).toEqual([]);
    expect(config.teams).toEqual({});

    // Nested sub-config defaults
    expect(config.governance.maxOpenCritical).toBe(0);
    expect(config.governance.maxOpenHigh).toBe(3);
    expect(config.governance.requireCiGreen).toBe(true);
    expect(config.models.t1Heavy).toBe("opus");
    expect(config.models.t2Standard).toBe("sonnet");
    expect(config.models.t3Fast).toBe("haiku");
    expect(config.cycle.maxNewTicketsPerCycle).toBe(20);
    expect(config.cycle.maxInvestigationsPerCycle).toBe(5);
    expect(config.monitor.logDirectories).toEqual(["logs/", "data/a2a/"]);
    expect(config.memory.embeddingModel).toBe("bge-m3");
    expect(config.throttle.weeklyBudgetUsd).toBe(500.0);
    expect(config.rateLimits.maxRetriesOn429).toBe(5);
    expect(config.staleTicketTimeouts.investigatingHours).toBe(4);
  });

  it("parses a fully populated config", () => {
    const config = SWETeamConfigSchema.parse({
      enabled: true,
      teamId: "alpha",
      githubAccount: "your-bot-alpha",
      governance: { maxOpenCritical: 2, maxOpenHigh: 5 },
      models: { t1Heavy: "gpt-4o" },
      cycle: { maxNewTicketsPerCycle: 50 },
      teams: {
        alpha: { name: "alpha", role: "full", maxConcurrent: 5 },
      },
      githubRepos: ["owner/repo1", "owner/repo2"],
    });

    expect(config.enabled).toBe(true);
    expect(config.teamId).toBe("alpha");
    expect(config.governance.maxOpenCritical).toBe(2);
    expect(config.governance.maxOpenHigh).toBe(5);
    expect(config.governance.requireCiGreen).toBe(true); // still default
    expect(config.models.t1Heavy).toBe("gpt-4o");
    expect(config.models.t2Standard).toBe("sonnet"); // default preserved
    expect(config.cycle.maxNewTicketsPerCycle).toBe(50);
    expect(config.teams.alpha.maxConcurrent).toBe(5);
    expect(config.githubRepos).toEqual(["owner/repo1", "owner/repo2"]);
  });
});

// -------------------------------------------------------------------------
// snakeToCamel
// -------------------------------------------------------------------------

describe("snakeToCamel", () => {
  it("converts a simple snake_case key", () => {
    const result = snakeToCamel({ hello_world: 1 });
    expect(result).toEqual({ helloWorld: 1 });
  });

  it("converts nested objects", () => {
    const result = snakeToCamel({
      outer_key: {
        inner_key: "value",
      },
    });
    expect(result).toEqual({
      outerKey: {
        innerKey: "value",
      },
    });
  });

  it("converts arrays of objects", () => {
    const result = snakeToCamel([
      { first_name: "Alice" },
      { last_name: "Bob" },
    ]);
    expect(result).toEqual([{ firstName: "Alice" }, { lastName: "Bob" }]);
  });

  it("handles keys with numbers like a2a_hub_url", () => {
    const result = snakeToCamel({ a2a_hub_url: "http://example.com" });
    expect(result).toEqual({ a2aHubUrl: "http://example.com" });
  });

  it("passes through primitives unchanged", () => {
    expect(snakeToCamel("hello")).toBe("hello");
    expect(snakeToCamel(42)).toBe(42);
    expect(snakeToCamel(true)).toBe(true);
    expect(snakeToCamel(null)).toBe(null);
  });

  it("handles keys already in camelCase", () => {
    const result = snakeToCamel({ alreadyCamel: "ok" });
    expect(result).toEqual({ alreadyCamel: "ok" });
  });

  it("handles deeply nested mixed structures", () => {
    const result = snakeToCamel({
      top_level: {
        mid_level: [
          { deep_key: "v1" },
          { another_deep: "v2" },
        ],
      },
    });
    expect(result).toEqual({
      topLevel: {
        midLevel: [
          { deepKey: "v1" },
          { anotherDeep: "v2" },
        ],
      },
    });
  });

  it("handles empty objects and arrays", () => {
    expect(snakeToCamel({})).toEqual({});
    expect(snakeToCamel([])).toEqual([]);
  });

  it("converts multi-segment snake_case", () => {
    const result = snakeToCamel({
      max_new_tickets_per_cycle: 20,
    });
    expect(result).toEqual({ maxNewTicketsPerCycle: 20 });
  });
});

// -------------------------------------------------------------------------
// loadConfig
// -------------------------------------------------------------------------

describe("loadConfig", () => {
  let tmpDir: string;
  const savedEnv: Record<string, string | undefined> = {};

  // Env vars that loadConfig reads -- save and restore around each test
  const envKeys = [
    "SWE_TEAM_CONFIG",
    "SWE_TEAM_ENABLED",
    "SWE_TEAM_ID",
    "SWE_GITHUB_ACCOUNT",
    "SWE_MODEL_T1",
    "SWE_MODEL_T2",
    "SWE_MODEL_T3",
    "T1_MODEL",
    "T2_MODEL",
    "T3_MODEL",
  ];

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), "swe-config-test-"));
    for (const key of envKeys) {
      savedEnv[key] = process.env[key];
      delete process.env[key];
    }
  });

  afterEach(() => {
    for (const key of envKeys) {
      if (savedEnv[key] === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = savedEnv[key];
      }
    }
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("returns all defaults when file does not exist", () => {
    const config = loadConfig(join(tmpDir, "nonexistent.yaml"));
    expect(config.enabled).toBe(false);
    expect(config.teamId).toBe("default");
    expect(config.models.t1Heavy).toBe("opus");
    expect(config.governance.maxOpenCritical).toBe(0);
    expect(config.cycle.maxNewTicketsPerCycle).toBe(20);
  });

  it("loads a valid YAML file and parses it", () => {
    const yamlContent = `
enabled: true
team_id: my-team
governance:
  max_open_critical: 5
  max_open_high: 10
models:
  t1_heavy: gpt-4o
cycle:
  max_new_tickets_per_cycle: 100
`;
    const filePath = join(tmpDir, "config.yaml");
    writeFileSync(filePath, yamlContent, "utf-8");

    const config = loadConfig(filePath);
    expect(config.enabled).toBe(true);
    expect(config.teamId).toBe("my-team");
    expect(config.governance.maxOpenCritical).toBe(5);
    expect(config.governance.maxOpenHigh).toBe(10);
    expect(config.governance.requireCiGreen).toBe(true); // default preserved
    expect(config.models.t1Heavy).toBe("gpt-4o");
    expect(config.models.t2Standard).toBe("sonnet"); // default preserved
    expect(config.cycle.maxNewTicketsPerCycle).toBe(100);
  });

  it("applies SWE_TEAM_ENABLED env var override (true)", () => {
    const filePath = join(tmpDir, "config.yaml");
    writeFileSync(filePath, "enabled: false\n", "utf-8");

    process.env.SWE_TEAM_ENABLED = "true";
    const config = loadConfig(filePath);
    expect(config.enabled).toBe(true);
  });

  it("applies SWE_TEAM_ENABLED env var override (1)", () => {
    process.env.SWE_TEAM_ENABLED = "1";
    const config = loadConfig(join(tmpDir, "nonexistent.yaml"));
    expect(config.enabled).toBe(true);
  });

  it("applies SWE_TEAM_ENABLED=false override", () => {
    const filePath = join(tmpDir, "config.yaml");
    writeFileSync(filePath, "enabled: true\n", "utf-8");

    process.env.SWE_TEAM_ENABLED = "false";
    const config = loadConfig(filePath);
    expect(config.enabled).toBe(false);
  });

  it("applies SWE_TEAM_ID env var override", () => {
    process.env.SWE_TEAM_ID = "gamma-squad";
    const config = loadConfig(join(tmpDir, "nonexistent.yaml"));
    expect(config.teamId).toBe("gamma-squad");
  });

  it("applies SWE_GITHUB_ACCOUNT env var override", () => {
    process.env.SWE_GITHUB_ACCOUNT = "my-bot";
    const config = loadConfig(join(tmpDir, "nonexistent.yaml"));
    expect(config.githubAccount).toBe("my-bot");
  });

  it("applies SWE_MODEL_T1 env var override", () => {
    process.env.SWE_MODEL_T1 = "custom-opus";
    const config = loadConfig(join(tmpDir, "nonexistent.yaml"));
    expect(config.models.t1Heavy).toBe("custom-opus");
  });

  it("applies SWE_MODEL_T2 env var override", () => {
    process.env.SWE_MODEL_T2 = "custom-sonnet";
    const config = loadConfig(join(tmpDir, "nonexistent.yaml"));
    expect(config.models.t2Standard).toBe("custom-sonnet");
  });

  it("applies SWE_MODEL_T3 env var override", () => {
    process.env.SWE_MODEL_T3 = "custom-haiku";
    const config = loadConfig(join(tmpDir, "nonexistent.yaml"));
    expect(config.models.t3Fast).toBe("custom-haiku");
  });

  it("SWE_MODEL_T* takes precedence over T*_MODEL", () => {
    process.env.T1_MODEL = "old-opus";
    process.env.SWE_MODEL_T1 = "new-opus";
    const config = loadConfig(join(tmpDir, "nonexistent.yaml"));
    expect(config.models.t1Heavy).toBe("new-opus");
  });

  it("falls back to T*_MODEL when SWE_MODEL_T* is not set", () => {
    process.env.T2_MODEL = "fallback-sonnet";
    const config = loadConfig(join(tmpDir, "nonexistent.yaml"));
    expect(config.models.t2Standard).toBe("fallback-sonnet");
  });

  it("uses SWE_TEAM_CONFIG env var for path when no argument given", () => {
    const filePath = join(tmpDir, "env-config.yaml");
    writeFileSync(filePath, "team_id: from-env-path\n", "utf-8");
    process.env.SWE_TEAM_CONFIG = filePath;

    const config = loadConfig();
    expect(config.teamId).toBe("from-env-path");
  });

  it("explicit path argument takes precedence over SWE_TEAM_CONFIG", () => {
    const envPath = join(tmpDir, "env-config.yaml");
    const argPath = join(tmpDir, "arg-config.yaml");
    writeFileSync(envPath, "team_id: from-env\n", "utf-8");
    writeFileSync(argPath, "team_id: from-arg\n", "utf-8");
    process.env.SWE_TEAM_CONFIG = envPath;

    const config = loadConfig(argPath);
    expect(config.teamId).toBe("from-arg");
  });

  it("env var overrides take precedence over YAML values", () => {
    const filePath = join(tmpDir, "config.yaml");
    writeFileSync(
      filePath,
      "team_id: yaml-team\ngithub_account: yaml-bot\n",
      "utf-8",
    );
    process.env.SWE_TEAM_ID = "env-team";
    process.env.SWE_GITHUB_ACCOUNT = "env-bot";

    const config = loadConfig(filePath);
    expect(config.teamId).toBe("env-team");
    expect(config.githubAccount).toBe("env-bot");
  });

  it("throws on invalid YAML structure", () => {
    const filePath = join(tmpDir, "bad.yaml");
    // governance.maxOpenCritical expects a number, not a string
    writeFileSync(
      filePath,
      "governance:\n  max_open_critical: not-a-number\n",
      "utf-8",
    );

    expect(() => loadConfig(filePath)).toThrow(/Invalid SWE team config/);
  });

  it("converts snake_case keys in YAML to camelCase before validation", () => {
    const filePath = join(tmpDir, "snake.yaml");
    writeFileSync(
      filePath,
      `
stale_ticket_timeouts:
  investigating_hours: 8
  in_development_hours: 6
  in_review_hours: 48
rate_limits:
  max_retries_on_429: 10
  initial_backoff_seconds: 30
`,
      "utf-8",
    );

    const config = loadConfig(filePath);
    expect(config.staleTicketTimeouts.investigatingHours).toBe(8);
    expect(config.staleTicketTimeouts.inDevelopmentHours).toBe(6);
    expect(config.staleTicketTimeouts.inReviewHours).toBe(48);
    expect(config.rateLimits.maxRetriesOn429).toBe(10);
    expect(config.rateLimits.initialBackoffSeconds).toBe(30);
  });
});

/**
 * Architecture harness tests for SWE-Manager V2.
 *
 * These tests enforce structural invariants. Breaking them is a build failure.
 * See docs/pi-dev/11-swe-manager-v2-architecture.md for the design rationale.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { resolve, join } from "node:path";

const SRC_DIR = resolve(__dirname, "../../src");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Recursively collect all .ts files under a directory. */
function walkTs(dir: string): string[] {
  const results: string[] = [];
  if (!existsSync(dir)) return results;

  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const fullPath = join(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...walkTs(fullPath));
    } else if (entry.name.endsWith(".ts") && !entry.name.endsWith(".d.ts")) {
      results.push(fullPath);
    }
  }
  return results;
}

// ---------------------------------------------------------------------------
// 1. No agent reimplementations
// ---------------------------------------------------------------------------

describe("V2 Architecture: No agent reimplementations", () => {
  it("src/agents/ directory must not exist", () => {
    const agentsDir = join(SRC_DIR, "agents");
    expect(
      existsSync(agentsDir),
      "src/agents/ directory exists — V2 uses pi-agent tools, not standalone agents",
    ).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 2. No hardcoded phase ordering
// ---------------------------------------------------------------------------

describe("V2 Architecture: No hardcoded phase ordering", () => {
  it("no source file references V1 phase names or CycleRunner", () => {
    const srcFiles = walkTs(SRC_DIR);
    const violations: string[] = [];

    for (const f of srcFiles) {
      const content = readFileSync(f, "utf-8");
      // Match V1 phase patterns: phase_monitor, phase-investigate, etc.
      if (
        /phase[_-]?(monitor|triage|investigate|develop|verify|creative|distill)/i.test(
          content,
        )
      ) {
        violations.push(`${f}: references a V1 phase name`);
      }
      if (/\brunOnce\b/.test(content)) {
        violations.push(`${f}: references runOnce (V1 CycleRunner)`);
      }
      if (/\bCycleRunner\b/.test(content)) {
        violations.push(`${f}: references CycleRunner (V1 orchestrator)`);
      }
      if (/\bphaseOrder\b/.test(content)) {
        violations.push(`${f}: references phaseOrder (V1 sequencing)`);
      }
    }

    expect(violations).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 3. Tools must not import engine implementations directly
// ---------------------------------------------------------------------------

describe("V2 Architecture: Tools are engine-agnostic", () => {
  it("tool files do not import engine implementations directly", () => {
    const toolsDir = join(SRC_DIR, "tools");
    if (!existsSync(toolsDir)) {
      // Tools directory not created yet (Phase 1) — pass vacuously
      return;
    }

    const toolFiles = walkTs(toolsDir);
    const violations: string[] = [];

    for (const f of toolFiles) {
      const content = readFileSync(f, "utf-8");
      if (/from\s+["'].*claude-cli/.test(content)) {
        violations.push(`${f}: imports claude-cli engine directly`);
      }
      if (/from\s+["'].*pi-sdk/.test(content)) {
        violations.push(`${f}: imports pi-sdk engine directly`);
      }
      if (/\bClaudeCliEngine\b/.test(content)) {
        violations.push(`${f}: references ClaudeCliEngine class directly`);
      }
      if (/\bPiSdkEngine\b/.test(content)) {
        violations.push(`${f}: references PiSdkEngine class directly`);
      }
    }

    expect(violations).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 4. No hardcoded notification provider in tool code
// ---------------------------------------------------------------------------

describe("V2 Architecture: Notification is provider-agnostic", () => {
  it("tool files do not import notification providers directly", () => {
    const toolsDir = join(SRC_DIR, "tools");
    if (!existsSync(toolsDir)) {
      return; // Phase 1 — pass vacuously
    }

    const toolFiles = walkTs(toolsDir);
    const violations: string[] = [];

    for (const f of toolFiles) {
      // send-notification.ts IS the notification tool — it may import the
      // provider interface, but must not hardcode a concrete provider.
      const content = readFileSync(f, "utf-8");
      if (f.includes("send-notification")) continue;

      if (/from\s+["'].*telegram/.test(content)) {
        violations.push(`${f}: imports telegram provider directly`);
      }
      if (/from\s+["'].*slack/.test(content)) {
        violations.push(`${f}: imports slack provider directly`);
      }
    }

    expect(violations).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 5. V2 config schemas are wired into top-level config
// ---------------------------------------------------------------------------

describe("V2 Architecture: Config schemas present", () => {
  it("SWETeamConfig includes delegation, workspace, daemon, notification", async () => {
    const { SWETeamConfigSchema } = await import("../../src/config/schemas.js");
    const defaults = SWETeamConfigSchema.parse({});

    expect(defaults).toHaveProperty("delegation");
    expect(defaults).toHaveProperty("workspace");
    expect(defaults).toHaveProperty("daemon");
    expect(defaults).toHaveProperty("notification");
  });

  it("delegation config defaults engine to claude-cli", async () => {
    const { AgentDelegationEntrySchema } = await import(
      "../../src/config/schemas.js"
    );
    const entry = AgentDelegationEntrySchema.parse({});
    expect(entry.engine).toBe("claude-cli");
    expect(entry.readOnly).toBe(false);
    expect(entry.timeout).toBe(1800);
  });

  it("workspace config defaults strategy to worktree", async () => {
    const { WorkspaceConfigSchema } = await import(
      "../../src/config/schemas.js"
    );
    const ws = WorkspaceConfigSchema.parse({});
    expect(ws.strategy).toBe("worktree");
    expect(ws.maxConcurrent).toBe(5);
  });

  it("daemon config defaults heartbeat to 300s", async () => {
    const { DaemonConfigSchema } = await import("../../src/config/schemas.js");
    const d = DaemonConfigSchema.parse({});
    expect(d.heartbeatIntervalSeconds).toBe(300);
    expect(d.maxSessionAge).toBe(86400);
  });

  it("notification config defaults to telegram provider", async () => {
    const { NotificationConfigSchema } = await import(
      "../../src/config/schemas.js"
    );
    const n = NotificationConfigSchema.parse({});
    expect(n.provider).toBe("telegram");
  });
});

// ---------------------------------------------------------------------------
// 6. Tool count matches expected
// ---------------------------------------------------------------------------

describe("V2 Architecture: Tool registration", () => {
  it("createAllTools returns exactly 16 tools", async () => {
    const { createAllTools, EXPECTED_TOOL_COUNT } = await import(
      "../../src/tools/index.js"
    );
    const { loadConfig } = await import("../../src/config/loader.js");
    const { createLogger } = await import("../../src/utils/logger.js");

    const config = loadConfig();
    const logger = createLogger({ level: "error" });
    const ctx = { config, logger, cwd: process.cwd() };
    const tools = createAllTools(ctx);

    expect(tools).toHaveLength(EXPECTED_TOOL_COUNT);
    expect(EXPECTED_TOOL_COUNT).toBe(16);
  });

  it("each tool has a unique name", async () => {
    const { createAllTools } = await import("../../src/tools/index.js");
    const { loadConfig } = await import("../../src/config/loader.js");
    const { createLogger } = await import("../../src/utils/logger.js");

    const config = loadConfig();
    const logger = createLogger({ level: "error" });
    const ctx = { config, logger, cwd: process.cwd() };
    const tools = createAllTools(ctx);

    const names = tools.map((t: { name: string }) => t.name);
    expect(new Set(names).size).toBe(names.length);
  });
});

// ---------------------------------------------------------------------------
// 7. V1 garbage must stay deleted
// ---------------------------------------------------------------------------

describe("V2 Architecture: V1 garbage deleted", () => {
  it("orchestrator/runner.ts does not exist", () => {
    expect(existsSync(join(SRC_DIR, "orchestrator", "runner.ts"))).toBe(false);
  });

  it("cli.ts does not exist", () => {
    expect(existsSync(join(SRC_DIR, "cli.ts"))).toBe(false);
  });

  it("smoke-test.ts does not exist", () => {
    expect(existsSync(join(SRC_DIR, "smoke-test.ts"))).toBe(false);
  });

  it("providers/engine/pi-sdk.ts does not exist", () => {
    expect(existsSync(join(SRC_DIR, "providers", "engine", "pi-sdk.ts"))).toBe(
      false,
    );
  });

  it("validate/ directory does not exist", () => {
    expect(existsSync(join(SRC_DIR, "validate"))).toBe(false);
  });
});

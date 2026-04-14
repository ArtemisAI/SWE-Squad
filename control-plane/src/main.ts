/**
 * SWE-Manager V2 — Daemon entry point.
 *
 * Creates a persistent pi-agent session with 16 custom tools. The LLM
 * decides what to do (scan issues, delegate investigation, etc.) based
 * on its SKILL.md persona and tool results. No hardcoded phases.
 *
 * Usage:
 *   npx tsx control-plane/src/main.ts [--daemon] [--interval <seconds>] [--verbose]
 */

import { parseArgs } from "node:util";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  AuthStorage,
  createAgentSession,
  SessionManager,
  ModelRegistry,
} from "@mariozechner/pi-coding-agent";

import { loadConfig } from "./config/loader.js";
import { createAllTools } from "./tools/index.js";
import { createLogger } from "./utils/logger.js";
import { SupabaseClient } from "./providers/supabase/client.js";
import { SupabaseTicketStore } from "./providers/supabase/store.js";
import { CircuitBreaker } from "./safety/circuit-breaker.js";
import { OutcomeTracker } from "./safety/outcome-tracker.js";
import { sendMessage as telegramSend } from "./providers/notification/telegram.js";
import { MemoryService } from "./services/memory-service.js";
import { SupabaseMemoryProvider } from "./providers/memory/supabase-provider.js";
import { InMemoryMemoryProvider } from "./providers/memory/memory-provider.js";
import type { SWEContext, NotificationProvider } from "./shared/context.js";

// ---------------------------------------------------------------------------
// Load .env file (dotenv-free — just parse KEY=VALUE lines)
// ---------------------------------------------------------------------------

function loadDotEnv(path: string): void {
  try {
    const content = readFileSync(path, "utf-8");
    for (const line of content.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const eqIdx = trimmed.indexOf("=");
      if (eqIdx < 1) continue;
      const key = trimmed.slice(0, eqIdx).trim();
      let val = trimmed.slice(eqIdx + 1).trim();
      // Strip surrounding quotes
      if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
        val = val.slice(1, -1);
      }
      if (!process.env[key]) {
        process.env[key] = val;
      }
    }
  } catch { /* .env not found — that's fine */ }
}

// Load from project root .env
const projectRoot = resolve(import.meta.dirname ?? ".", "../..");
loadDotEnv(resolve(projectRoot, ".env"));

// ---------------------------------------------------------------------------
// CLI args
// ---------------------------------------------------------------------------

const { values: args } = parseArgs({
  options: {
    daemon: { type: "boolean", default: false },
    fresh: { type: "boolean", default: false },
    interval: { type: "string", default: "" },
    verbose: { type: "boolean", default: false },
    config: { type: "string", default: "" },
    "dry-run": { type: "boolean", default: false },
  },
  strict: false,
});

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

const logger = createLogger({
  prefix: "swe-manager",
  level: args.verbose ? "debug" : "info",
  logFile: resolve(process.cwd(), "..", "logs", "swe_manager.log"),
});
const config = loadConfig(
  typeof args.config === "string" && args.config ? args.config : undefined,
);

const heartbeatSeconds =
  (typeof args.interval === "string" && args.interval
    ? parseInt(args.interval, 10)
    : 0) || config.daemon.heartbeatIntervalSeconds;

logger.info(
  `SWE-Manager V2 starting (team=${config.teamId}, heartbeat=${heartbeatSeconds}s, daemon=${args.daemon})`,
);

// ---------------------------------------------------------------------------
// Build SWEContext — wires real providers from env
// ---------------------------------------------------------------------------

// Supabase
let ticketStore: SupabaseTicketStore | undefined;
let supabaseClient: SupabaseClient | undefined;

const supabaseUrl = process.env.SUPABASE_URL;
const supabaseKey = process.env.SUPABASE_ANON_KEY;
if (supabaseUrl && supabaseKey) {
  supabaseClient = new SupabaseClient({ url: supabaseUrl, key: supabaseKey });
  ticketStore = new SupabaseTicketStore({
    client: supabaseClient,
    teamId: config.teamId,
  });
  logger.info(`Supabase connected: ${supabaseUrl} (team=${config.teamId})`);
} else {
  logger.warn("Supabase not configured — ticket tools will return errors");
}

// Circuit breaker
const circuitBreaker = new CircuitBreaker();
logger.info(`Circuit breaker initialized (window=10, threshold=80%)`);

// Outcome tracker — monitors tool execution metrics for code-level enforcement
const outcomeTracker = new OutcomeTracker({
  windowMinutes: 60,
  maxConsecutiveFailures: 3,
  maxTicketAttempts: 3,
  failureRateThreshold: 0.5,
});
logger.info("Outcome tracker initialized (window=60min, maxRetries=3)");

// Telegram notifier (wraps the existing function-based API)
let notifier: NotificationProvider | undefined;
const telegramToken = process.env.TELEGRAM_BOT_TOKEN;
const telegramChat = process.env.TELEGRAM_CHAT_ID;
if (telegramToken && telegramChat) {
  notifier = {
    async send(message, options) {
      return telegramSend(message, {
        chatId: options?.chatId ?? telegramChat,
        config: { botToken: telegramToken, chatId: telegramChat },
      });
    },
  };
  logger.info("Telegram notifier configured");
} else {
  logger.warn("Telegram not configured — notifications will be skipped");
}

// Memory service — uses Supabase when available, falls back to in-memory
let memoryService: MemoryService | undefined;
if (supabaseClient) {
  const memoryProvider = new SupabaseMemoryProvider({ client: supabaseClient });
  memoryService = new MemoryService({
    provider: memoryProvider,
    dedupThreshold: 0.92,
    ttlDays: config.memory?.ttlDays ?? 180,
    maxEntriesPerProject: config.memory?.maxEntriesPerProject ?? 10_000,
  });
  logger.info("Memory service initialized (Supabase pgvector backend)");
} else {
  const memoryProvider = new InMemoryMemoryProvider();
  memoryService = new MemoryService({ provider: memoryProvider });
  logger.info("Memory service initialized (in-memory fallback — not persistent)");
}

const sweContext: SWEContext = {
  config,
  logger,
  cwd: process.cwd(),
  ticketStore,
  supabaseClient,
  circuitBreaker,
  outcomeTracker,
  notifier,
  memoryService,
};

// ---------------------------------------------------------------------------
// Create all custom tools
// ---------------------------------------------------------------------------

const customTools = createAllTools(sweContext);
logger.info(`Registered ${customTools.length} custom tools`);

// ---------------------------------------------------------------------------
// Create or resume pi-agent session
// ---------------------------------------------------------------------------

async function createSession() {
  const cwd = process.cwd();

  // Resume or create session (--fresh forces a new one)
  let sessionManager: SessionManager;
  if (args.fresh) {
    sessionManager = SessionManager.create(cwd);
    logger.info(`Created fresh session: ${sessionManager.getSessionId()}`);
  } else {
    try {
      sessionManager = SessionManager.continueRecent(cwd);
      logger.info(`Resuming session: ${sessionManager.getSessionId()}`);
    } catch {
      sessionManager = SessionManager.create(cwd);
      logger.info(`Created new session: ${sessionManager.getSessionId()}`);
    }
  }

  // Resolve model from the pi-agent model registry.
  // Custom providers from ~/.pi/agent/models.json are auto-loaded.
  // When a proxy provider is configured, "swe-proxy/claude-sonnet" routes
  // through the LiteLLM proxy to the Claude API.
  const agentDir = resolve(process.env.HOME ?? "~", ".pi/agent");
  const authStorage = AuthStorage.create(resolve(agentDir, "auth.json"));
  const modelRegistry = ModelRegistry.create(
    authStorage,
    resolve(agentDir, "models.json"),
  );

  // Try to find the best available model from the swe-proxy provider.
  // Falls back to haiku (gemma4 local) when sonnet (GLM-5.1) is at capacity.
  const preferredModel = process.env.SWE_DAEMON_MODEL ?? "claude-sonnet";
  const model = modelRegistry.find("swe-proxy", preferredModel)
    ?? modelRegistry.find("swe-proxy", "claude-sonnet")
    ?? modelRegistry.find("anthropic", "claude-sonnet-4-6");
  if (model) {
    logger.info(`Model selected: ${model.provider}/${model.id}`);
  } else {
    logger.warn("Could not find claude-sonnet — will use pi-agent default (may be slow)");
  }

  const { session, extensionsResult, modelFallbackMessage } =
    await createAgentSession({
      cwd,
      customTools,
      sessionManager,
      modelRegistry,
      ...(model ? { model } : {}),
      thinkingLevel: "medium",
    });

  if (modelFallbackMessage) {
    logger.warn(`Model fallback: ${modelFallbackMessage}`);
  }

  if (extensionsResult.errors.length > 0) {
    for (const err of extensionsResult.errors) {
      const errMsg = err instanceof Error ? err.message : JSON.stringify(err);
      logger.warn(`Extension error: ${errMsg}`);
    }
  }

  logger.info(
    `Session ready: model=${session.model?.name ?? "default"}, ` +
      `tools=${session.getActiveToolNames().length}, ` +
      `sessionId=${session.sessionId}`,
  );

  return session;
}

// ---------------------------------------------------------------------------
// Heartbeat loop
// ---------------------------------------------------------------------------

let shuttingDown = false;

async function heartbeat(session: Awaited<ReturnType<typeof createSession>>) {
  const prompt = config.daemon.initialPrompt;
  const startMs = Date.now();
  logger.info(`Heartbeat: sending prompt to session`);

  try {
    await session.prompt(prompt);
    const elapsedSec = ((Date.now() - startMs) / 1000).toFixed(1);
    logger.info(`Heartbeat: session turn complete (${elapsedSec}s)`);
  } catch (err) {
    const elapsedSec = ((Date.now() - startMs) / 1000).toFixed(1);
    logger.error(`Heartbeat error after ${elapsedSec}s: ${err}`);
  }
}

async function releaseStaleClaimsOnStartup(): Promise<void> {
  if (!ticketStore) return;
  try {
    // Find tickets claimed by us that are stuck in transient states
    const staleStatuses = ["investigating", "in_development"];
    for (const status of staleStatuses) {
      const tickets = await ticketStore.listByStatus(status);
      for (const t of tickets) {
        if (t.assignedTo === config.teamId) {
          logger.warn(
            `Releasing stale claim on ${t.ticketId} (was ${status}, assigned to us from previous session)`,
          );
          // If it has an investigation report, set to investigation_complete
          // Otherwise reset to open
          const resetStatus = (status === "investigating" && t.investigationReport)
            ? "investigation_complete"
            : (status === "investigating" ? "open" : status);
          await ticketStore.releaseTicket(t.ticketId, resetStatus);
        }
      }
    }
  } catch (err) {
    logger.warn(`Stale claim cleanup failed (non-fatal): ${err}`);
  }
}

async function runDaemon() {
  // Release any stale claims from previous sessions before starting
  if (args.fresh) {
    await releaseStaleClaimsOnStartup();
  }

  const session = await createSession();

  // Initial heartbeat
  await heartbeat(session);

  if (!args.daemon) {
    logger.info("Single-shot mode: exiting after one heartbeat");
    return;
  }

  // Daemon loop
  logger.info(
    `Daemon mode: heartbeat every ${heartbeatSeconds}s (Ctrl+C to stop)`,
  );

  while (!shuttingDown) {
    await sleep(heartbeatSeconds * 1000);
    if (shuttingDown) break;
    await heartbeat(session);
  }

  logger.info("Daemon stopped gracefully");
}

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------

function onShutdown(signal: string) {
  logger.info(`Received ${signal}, shutting down...`);
  shuttingDown = true;

  // Force exit after grace period
  setTimeout(() => {
    logger.warn("Graceful shutdown timeout, forcing exit");
    process.exit(1);
  }, config.daemon.gracefulShutdownTimeout * 1000);
}

process.on("SIGTERM", () => onShutdown("SIGTERM"));
process.on("SIGINT", () => onShutdown("SIGINT"));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------

if (args["dry-run"]) {
  logger.info("Dry run: session bootstrap only (no LLM calls)");
  createSession()
    .then((session) => {
      logger.info(
        `Dry run complete: ${session.getActiveToolNames().length} tools active`,
      );
      const toolNames = session.getActiveToolNames();
      logger.info(`Tools: ${toolNames.join(", ")}`);
    })
    .catch((err) => {
      logger.error(`Dry run failed: ${err}`);
      process.exit(1);
    });
} else {
  runDaemon().catch((err) => {
    logger.error(`Fatal: ${err}`);
    process.exit(1);
  });
}

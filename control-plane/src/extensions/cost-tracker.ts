/**
 * Cost tracking extension for pi-agent sessions.
 *
 * Accumulates per-turn cost from assistant message usage data and
 * enforces a daily budget. When the budget is exceeded, the optional
 * callback fires and subsequent prompts are blocked.
 *
 * Uses the pi-coding-agent ExtensionFactory contract:
 *   pi.on("message_end", ...) to accumulate cost after each LLM response.
 *   pi.on("before_agent_start", ...) to block prompts when over budget.
 */

import type {
  AssistantMessage,
} from "@mariozechner/pi-ai";
import type {
  ExtensionFactory,
} from "@mariozechner/pi-coding-agent";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

export interface CostTrackerConfig {
  /** Team identifier for cost attribution. */
  teamId: string;
  /** Maximum daily spend in USD. 0 = unlimited. */
  dailyBudgetUsd: number;
  /** Called when accumulated cost exceeds the budget. */
  onBudgetExceeded?: (spent: number, budget: number) => void;
  /** Called after each message with updated totals. */
  onCostUpdate?: (snapshot: CostSnapshot) => void;
}

/** Real-time cost snapshot emitted via onCostUpdate callback. */
export interface CostSnapshot {
  teamId: string;
  totalCostUsd: number;
  dailyBudgetUsd: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  messageCount: number;
  budgetExceeded: boolean;
  /** ISO timestamp of first cost event in this tracking window. */
  windowStart: string;
}

// ---------------------------------------------------------------------------
// Extension factory
// ---------------------------------------------------------------------------

/**
 * Create a cost tracking extension.
 *
 * @example
 * ```typescript
 * const costExt = createCostTrackerExtension({
 *   teamId: "alpha",
 *   dailyBudgetUsd: 25.0,
 *   onBudgetExceeded: (spent, budget) => {
 *     console.error(`Budget exceeded: $${spent.toFixed(4)} / $${budget}`);
 *   },
 * });
 * ```
 */
export function createCostTrackerExtension(
  config: CostTrackerConfig,
): ExtensionFactory {
  return (pi) => {
    let totalCostUsd = 0;
    let inputTokens = 0;
    let outputTokens = 0;
    let cacheReadTokens = 0;
    let cacheWriteTokens = 0;
    let messageCount = 0;
    let budgetExceeded = false;
    const windowStart = new Date().toISOString();

    /**
     * Build a snapshot of current cost state.
     */
    function snapshot(): CostSnapshot {
      return {
        teamId: config.teamId,
        totalCostUsd,
        dailyBudgetUsd: config.dailyBudgetUsd,
        inputTokens,
        outputTokens,
        cacheReadTokens,
        cacheWriteTokens,
        messageCount,
        budgetExceeded,
        windowStart,
      };
    }

    // -- Accumulate cost from each assistant message ----------------------
    pi.on("message_end", (event) => {
      const msg = event.message;

      // Only track assistant messages with usage data
      if (!msg || !("role" in msg) || msg.role !== "assistant") return;
      const assistantMsg = msg as AssistantMessage;
      if (!assistantMsg.usage) return;

      const usage = assistantMsg.usage;

      // Accumulate tokens
      inputTokens += usage.input ?? 0;
      outputTokens += usage.output ?? 0;
      cacheReadTokens += usage.cacheRead ?? 0;
      cacheWriteTokens += usage.cacheWrite ?? 0;
      messageCount++;

      // Accumulate cost
      if (usage.cost) {
        totalCostUsd += usage.cost.total ?? 0;
      }

      // Emit update callback
      config.onCostUpdate?.(snapshot());

      // Check budget
      if (
        config.dailyBudgetUsd > 0 &&
        totalCostUsd >= config.dailyBudgetUsd &&
        !budgetExceeded
      ) {
        budgetExceeded = true;
        config.onBudgetExceeded?.(totalCostUsd, config.dailyBudgetUsd);
      }
    });

    // -- Block new prompts when over budget -------------------------------
    // Use the "input" event to intercept prompts before they reach the agent.
    // When budget is exceeded, we transform the input to a warning message
    // and rely on the agent to relay it rather than silently blocking.
    pi.on("input", (_event) => {
      if (budgetExceeded) {
        return {
          action: "transform" as const,
          text: `[Cost Guard] Daily budget of $${config.dailyBudgetUsd.toFixed(2)} exceeded ($${totalCostUsd.toFixed(4)} spent) for team "${config.teamId}". This prompt has been blocked. Please wait for the budget to reset.`,
        };
      }
      return { action: "continue" as const };
    });
  };
}

// ---------------------------------------------------------------------------
// Utility: get current cost from an extension (for external queries)
// ---------------------------------------------------------------------------

/**
 * Create a cost tracker with externally queryable state.
 *
 * Returns both the ExtensionFactory and a getter for the current snapshot.
 * Useful when the caller needs to query cost outside of callbacks.
 */
export function createCostTrackerWithQuery(config: CostTrackerConfig): {
  extension: ExtensionFactory;
  getSnapshot: () => CostSnapshot;
} {
  let latestSnapshot: CostSnapshot = {
    teamId: config.teamId,
    totalCostUsd: 0,
    dailyBudgetUsd: config.dailyBudgetUsd,
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    messageCount: 0,
    budgetExceeded: false,
    windowStart: new Date().toISOString(),
  };

  const wrappedConfig: CostTrackerConfig = {
    ...config,
    onCostUpdate: (snap) => {
      latestSnapshot = snap;
      config.onCostUpdate?.(snap);
    },
  };

  return {
    extension: createCostTrackerExtension(wrappedConfig),
    getSnapshot: () => latestSnapshot,
  };
}

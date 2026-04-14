/**
 * Live smoke test — verifies tools work against real Supabase.
 * Run: set -a && source .env && set +a && npx tsx control-plane/src/smoke-live.ts
 */

import { loadConfig } from "./config/loader.js";
import { createLogger } from "./utils/logger.js";
import { SupabaseClient } from "./providers/supabase/client.js";
import { SupabaseTicketStore } from "./providers/supabase/store.js";
import { CircuitBreaker } from "./safety/circuit-breaker.js";
import { createAllTools } from "./tools/index.js";
import { sendMessage as telegramSend } from "./providers/notification/telegram.js";
import type { NotificationProvider } from "./shared/context.js";

const config = loadConfig();
const logger = createLogger({ level: "info" });
const client = new SupabaseClient({
  url: process.env.SUPABASE_URL!,
  key: process.env.SUPABASE_ANON_KEY!,
});
const ticketStore = new SupabaseTicketStore({ client, teamId: config.teamId });
const circuitBreaker = new CircuitBreaker();

// Wire Telegram notifier (same as main.ts)
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
}

const ctx = { config, logger, cwd: process.cwd(), ticketStore, circuitBreaker, notifier };
const tools = createAllTools(ctx);

async function callTool(name: string, params: Record<string, unknown>) {
  const tool = tools.find((t) => t.name === name)!;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const result = await (tool as any).execute("smoke", params, undefined, undefined, {});
  return result.content[0].text as string;
}

async function main() {
  console.log("=== Live Smoke Test ===\n");

  // 1. ticket_list
  console.log("--- ticket_list (open) ---");
  const listResult = await callTool("ticket_list", {});
  console.log(listResult.slice(0, 600));
  console.log();

  // 2. check_health
  console.log("--- check_health ---");
  const healthResult = await callTool("check_health", {});
  console.log(healthResult.slice(0, 600));
  console.log();

  // 3. check_stability
  console.log("--- check_stability ---");
  const stabilityResult = await callTool("check_stability", {});
  console.log(stabilityResult);
  console.log();

  // 4. github_issues (if repo configured)
  if (config.githubRepos.length > 0 || process.env.SWE_GITHUB_REPO) {
    const repo = config.githubRepos[0] ?? process.env.SWE_GITHUB_REPO;
    console.log(`--- github_issues (${repo}) ---`);
    const ghResult = await callTool("github_issues", { repo });
    console.log(ghResult.slice(0, 600));
    console.log();
  }

  // 5. send_notification (skip if no notifier)
  console.log("--- send_notification (dry) ---");
  const notifResult = await callTool("send_notification", { message: "SWE-Manager V2 smoke test" });
  console.log(notifResult);
  console.log();

  // 6. manage_workspace list
  console.log("--- manage_workspace (list) ---");
  const wsResult = await callTool("manage_workspace", { action: "list" });
  console.log(wsResult.slice(0, 300));

  console.log("\n=== Smoke Test Complete ===");
}

main().catch((err) => {
  console.error("Smoke test failed:", err);
  process.exit(1);
});

/**
 * Quick health check for SWE-Manager V2 monitoring loop.
 * Run: set -a && source .env && set +a && npx tsx control-plane/src/health-check.ts
 */

import { loadConfig } from "./config/loader.js";
import { SupabaseClient } from "./providers/supabase/client.js";
import { SupabaseTicketStore } from "./providers/supabase/store.js";
import { execSync } from "node:child_process";
import { readdirSync, statSync, readFileSync } from "node:fs";

async function main() {
  const config = loadConfig();
  console.log("=== SWE-Manager V2 Health Check ===");
  console.log(`Time: ${new Date().toISOString()}`);
  console.log(`Team: ${config.teamId}`);
  console.log();

  // 1. Daemon process
  console.log("--- Daemon Process ---");
  try {
    const ps = execSync("pgrep -af 'main.ts' 2>/dev/null || true", { encoding: "utf-8" }).trim();
    console.log(`Active: ${ps ? "YES" : "NO"}`);
    if (ps) console.log(ps);
  } catch {
    console.log("Active: NO (no daemon process found)");
  }
  console.log();

  // 2. Session file
  console.log("--- Pi-Agent Session ---");
  try {
    const cwd = process.cwd().replace(/\//g, "-").replace(/^-/, "--") + "--";
    const sessionDir = `${process.env.HOME}/.pi/agent/sessions/${cwd}/`;
    const files = readdirSync(sessionDir).filter(f => f.endsWith(".jsonl")).sort();
    if (files.length > 0) {
      const latest = files[files.length - 1];
      const path = `${sessionDir}${latest}`;
      const stat = statSync(path);
      const content = readFileSync(path, "utf-8");
      const lines = content.split("\n").filter(l => l.trim());
      console.log(`Session: ${latest.slice(0, 40)}...`);
      console.log(`Lines: ${lines.length}`);
      console.log(`Last modified: ${stat.mtime.toISOString()}`);

      // Last tool call
      for (let i = lines.length - 1; i >= 0; i--) {
        try {
          const d = JSON.parse(lines[i]);
          if (d.type === "message" && d.message?.role === "assistant") {
            for (const c of d.message.content || []) {
              if (c.type === "toolCall") {
                console.log(`Last tool: ${c.name}(${JSON.stringify(c.arguments || {}).slice(0, 80)})`);
                break;
              }
            }
            break;
          }
        } catch { /* skip */ }
      }
    } else {
      console.log("No session files found");
    }
  } catch (err) {
    console.log(`Error reading sessions: ${err}`);
  }
  console.log();

  // 3. Supabase tickets
  console.log("--- Supabase Tickets ---");
  try {
    const client = new SupabaseClient({ url: process.env.SUPABASE_URL!, key: process.env.SUPABASE_ANON_KEY! });
    const store = new SupabaseTicketStore({ client, teamId: config.teamId });
    const open = await store.listOpen();
    const byStatus: Record<string, number> = {};
    const bySeverity: Record<string, number> = {};
    for (const t of open) {
      byStatus[t.status] = (byStatus[t.status] ?? 0) + 1;
      bySeverity[t.severity] = (bySeverity[t.severity] ?? 0) + 1;
    }
    console.log(`Total open: ${open.length}`);
    console.log(`By status: ${JSON.stringify(byStatus)}`);
    console.log(`By severity: ${JSON.stringify(bySeverity)}`);
  } catch (err) {
    console.log(`Supabase error: ${err}`);
  }
  console.log();

  // 4. Log errors
  console.log("--- Recent Errors ---");
  const logFiles = [
    "logs/swe-manager-v2-e2e.log",
    "logs/swe-manager-v2-fresh.log",
    "logs/swe-manager-v2.log",
  ];
  for (const logFile of logFiles) {
    try {
      const content = readFileSync(logFile, "utf-8");
      const errors = content.split("\n").filter(l => l.includes("ERROR") || l.includes("error:"));
      if (errors.length > 0) {
        console.log(`${logFile}: ${errors.length} errors`);
        errors.slice(-3).forEach(e => console.log(`  ${e.slice(0, 150)}`));
      }
    } catch { /* file not found, skip */ }
  }
  console.log();

  // 5. Tests
  console.log("--- Test Status ---");
  try {
    const result = execSync("cd control-plane && pnpm test 2>&1 | grep -E '(Test Files|Tests )' | head -2", {
      encoding: "utf-8",
      timeout: 60000,
    });
    console.log(result.trim());
  } catch {
    // pnpm test returns non-zero when tests fail — check output anyway
    try {
      const fallback = execSync("cd control-plane && pnpm test 2>&1 | grep -E '(passed|failed)' | head -2", {
        encoding: "utf-8",
        timeout: 60000,
      });
      console.log(fallback.trim());
    } catch (err2) {
      console.log(`Tests could not run: ${err2}`);
    }
  }

  console.log("\n=== Health Check Complete ===");
}

main().catch(console.error);

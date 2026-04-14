/**
 * SWEContext — shared context passed to all tools.
 *
 * Aggregates provider references so tools resolve dependencies from config
 * rather than importing concrete implementations.
 */

import type { SWETeamConfig } from "../config/schemas.js";
import type { SupabaseTicketStore } from "../providers/supabase/store.js";
import type { SupabaseClient } from "../providers/supabase/client.js";
import type { CircuitBreaker } from "../safety/circuit-breaker.js";
import type { GuardrailsCoordinator } from "../safety/guardrails.js";
import type { OutcomeTracker } from "../safety/outcome-tracker.js";
import type { MemoryService } from "../services/memory-service.js";
import type { Logger } from "../utils/logger.js";

/**
 * Notification provider interface — provider-agnostic.
 *
 * Implementations: TelegramNotifier, SlackNotifier, WebhookNotifier, etc.
 */
export interface NotificationProvider {
  send(message: string, options?: { alertType?: string; chatId?: string }): Promise<boolean>;
}

/**
 * Context bag passed to every tool's execute() function.
 *
 * Tools access providers through this interface — never via direct imports.
 * All fields are optional to support incremental wiring (tools that don't
 * need Supabase won't fail if it's not configured).
 */
export interface SWEContext {
  readonly config: SWETeamConfig;
  readonly logger: Logger;
  readonly ticketStore?: SupabaseTicketStore;
  readonly supabaseClient?: SupabaseClient;
  readonly circuitBreaker?: CircuitBreaker;
  readonly guardrails?: GuardrailsCoordinator;
  readonly outcomeTracker?: OutcomeTracker;
  readonly notifier?: NotificationProvider;
  readonly memoryService?: MemoryService;
  readonly cwd: string;
}

/**
 * Parallel ticket processing with bounded concurrency.
 *
 * Provides `parallelInvestigate` and `parallelDevelop` which run
 * investigation / development on multiple tickets in parallel while
 * respecting a configurable concurrency limit.
 */

import type { SWETicket } from "../models/ticket.js";
import type { CodingEngine, EngineResult } from "../providers/engine/base.js";
import type { Logger } from "../utils/logger.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ParallelResult<T> {
  succeeded: { ticket: SWETicket; result: T }[];
  failed: { ticket: SWETicket; error: Error }[];
}

export interface ParallelOptions {
  concurrency?: number;
  timeoutMs?: number;
  onComplete?: (ticket: SWETicket, ok: boolean) => void;
}

export type TicketProcessor<T> = (
  ticket: SWETicket,
  engine: CodingEngine,
) => Promise<T>;

// ---------------------------------------------------------------------------
// Core: bounded parallel execution
// ---------------------------------------------------------------------------

/**
 * Run a processor function over a list of tickets with bounded concurrency.
 *
 * - At most `concurrency` tickets are processed at the same time.
 * - Individual failures are caught and recorded — they do not abort the batch.
 * - An optional `onComplete` callback fires after each ticket finishes.
 * - An optional `timeoutMs` rejects individual items that exceed the limit.
 */
export async function runParallel<T>(
  tickets: SWETicket[],
  engine: CodingEngine,
  processor: TicketProcessor<T>,
  options?: ParallelOptions,
): Promise<ParallelResult<T>> {
  const concurrency = options?.concurrency ?? 4;
  const timeoutMs = options?.timeoutMs;
  const onComplete = options?.onComplete;

  const succeeded: ParallelResult<T>["succeeded"] = [];
  const failed: ParallelResult<T>["failed"] = [];

  if (tickets.length === 0) {
    return { succeeded, failed };
  }

  // Semaphore: at most `concurrency` items in flight
  let running = 0;
  let idx = 0;

  await new Promise<void>((resolveAll) => {
    function next() {
      // All dispatched and nothing running => done
      if (idx >= tickets.length && running === 0) {
        resolveAll();
        return;
      }

      // Dispatch as many as concurrency allows
      while (running < concurrency && idx < tickets.length) {
        const ticket = tickets[idx++];
        running++;

        const work = processor(ticket, engine);

        const wrapped = timeoutMs
          ? Promise.race([
              work,
              new Promise<never>((_, reject) =>
                setTimeout(
                  () => reject(new Error(`Timeout after ${timeoutMs}ms`)),
                  timeoutMs,
                ),
              ),
            ])
          : work;

        wrapped
          .then((result) => {
            succeeded.push({ ticket, result });
            onComplete?.(ticket, true);
          })
          .catch((error) => {
            failed.push({
              ticket,
              error: error instanceof Error ? error : new Error(String(error)),
            });
            onComplete?.(ticket, false);
          })
          .finally(() => {
            running--;
            next();
          });
      }
    }
    next();
  });

  return { succeeded, failed };
}

// ---------------------------------------------------------------------------
// Convenience wrappers
// ---------------------------------------------------------------------------

/**
 * Run investigation on multiple tickets in parallel.
 * The `investigator` function is called for each ticket.
 */
export async function parallelInvestigate(
  tickets: SWETicket[],
  engine: CodingEngine,
  investigator: TicketProcessor<string>,
  options?: ParallelOptions,
): Promise<ParallelResult<string>> {
  return runParallel(tickets, engine, investigator, options);
}

/**
 * Run development on multiple tickets in parallel.
 * The `developer` function is called for each ticket.
 */
export async function parallelDevelop(
  tickets: SWETicket[],
  engine: CodingEngine,
  developer: TicketProcessor<string>,
  options?: ParallelOptions,
): Promise<ParallelResult<string>> {
  return runParallel(tickets, engine, developer, options);
}

/**
 * Unit tests for the parallel ticket processing module.
 *
 * Tests cover bounded concurrency, error isolation, empty inputs,
 * timeout handling, callback firing, and the convenience wrappers
 * (parallelInvestigate, parallelDevelop).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  runParallel,
  parallelInvestigate,
  parallelDevelop,
  type ParallelResult,
  type TicketProcessor,
} from "../../../src/orchestration/parallel.js";
import {
  createTicket,
  type SWETicket,
} from "../../../src/models/ticket.js";
import type { CodingEngine } from "../../../src/providers/engine/base.js";
import { createEngineResult } from "../../../src/providers/engine/base.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function mkEngine(): CodingEngine {
  return {
    name: "mock-engine",
    run: vi.fn().mockResolvedValue(createEngineResult({ stdout: "ok" })),
    healthCheck: vi.fn().mockResolvedValue(true),
  };
}

function mkTickets(n: number): SWETicket[] {
  return Array.from({ length: n }, (_, i) =>
    createTicket(`Bug ${i + 1}`, `Description ${i + 1}`, {
      severity: "high",
      status: "triaged",
    }),
  );
}

/** Processor that resolves after a configurable delay. */
function delayProcessor(
  delayMs: number,
  result: string = "done",
): TicketProcessor<string> {
  return () => new Promise((resolve) => setTimeout(() => resolve(result), delayMs));
}

/** Processor that always fails. */
function failProcessor(msg: string = "boom"): TicketProcessor<string> {
  return () => Promise.reject(new Error(msg));
}

/** Processor that succeeds instantly. */
function okProcessor(result: string = "ok"): TicketProcessor<string> {
  return () => Promise.resolve(result);
}

let engine: CodingEngine;

beforeEach(() => {
  vi.useFakeTimers();
  engine = mkEngine();
});

afterEach(() => {
  vi.useRealTimers();
});

// ===========================================================================
// 1. runParallel basic
// ===========================================================================

describe("runParallel basic", () => {
  it("returns empty results for empty ticket array", async () => {
    const result = await runParallel([], engine, okProcessor());
    expect(result.succeeded).toEqual([]);
    expect(result.failed).toEqual([]);
  });

  it("processes a single ticket successfully", async () => {
    const tickets = mkTickets(1);
    const result = await runParallel(tickets, engine, okProcessor("result-1"));
    expect(result.succeeded.length).toBe(1);
    expect(result.succeeded[0].result).toBe("result-1");
    expect(result.failed.length).toBe(0);
  });

  it("processes multiple tickets", async () => {
    const tickets = mkTickets(5);
    const result = await runParallel(tickets, engine, okProcessor());
    expect(result.succeeded.length).toBe(5);
    expect(result.failed.length).toBe(0);
  });

  it("succeeded array includes the original ticket", async () => {
    const tickets = mkTickets(1);
    const result = await runParallel(tickets, engine, okProcessor());
    expect(result.succeeded[0].ticket.title).toBe("Bug 1");
  });
});

// ===========================================================================
// 2. Error isolation
// ===========================================================================

describe("Error isolation", () => {
  it("individual ticket failure does not abort batch", async () => {
    const tickets = mkTickets(3);
    let callIdx = 0;
    const processor: TicketProcessor<string> = async () => {
      const i = callIdx++;
      if (i === 1) throw new Error("middle fails");
      return "ok";
    };
    const result = await runParallel(tickets, engine, processor);
    expect(result.succeeded.length).toBe(2);
    expect(result.failed.length).toBe(1);
  });

  it("failed array includes the Error object", async () => {
    const tickets = mkTickets(1);
    const result = await runParallel(tickets, engine, failProcessor("test-error"));
    expect(result.failed.length).toBe(1);
    expect(result.failed[0].error).toBeInstanceOf(Error);
    expect(result.failed[0].error.message).toBe("test-error");
  });

  it("failed array includes the ticket", async () => {
    const tickets = mkTickets(1);
    const result = await runParallel(tickets, engine, failProcessor());
    expect(result.failed[0].ticket.title).toBe("Bug 1");
  });

  it("handles all tickets failing", async () => {
    const tickets = mkTickets(3);
    const result = await runParallel(tickets, engine, failProcessor());
    expect(result.succeeded.length).toBe(0);
    expect(result.failed.length).toBe(3);
  });

  it("non-Error rejection is wrapped in Error", async () => {
    const tickets = mkTickets(1);
    const processor: TicketProcessor<string> = () => Promise.reject("string-err");
    const result = await runParallel(tickets, engine, processor);
    expect(result.failed[0].error).toBeInstanceOf(Error);
  });
});

// ===========================================================================
// 3. Concurrency limits
// ===========================================================================

describe("Concurrency limits", () => {
  it("respects concurrency=1 (sequential processing)", async () => {
    const tickets = mkTickets(3);
    let maxConcurrent = 0;
    let current = 0;

    const processor: TicketProcessor<string> = async () => {
      current++;
      maxConcurrent = Math.max(maxConcurrent, current);
      await new Promise((r) => setTimeout(r, 10));
      current--;
      return "ok";
    };

    // Use real timers for this test since we need actual async scheduling
    vi.useRealTimers();
    await runParallel(tickets, engine, processor, { concurrency: 1 });
    expect(maxConcurrent).toBe(1);
  });

  it("respects concurrency=2 with 5 tickets", async () => {
    const tickets = mkTickets(5);
    let maxConcurrent = 0;
    let current = 0;

    const processor: TicketProcessor<string> = async () => {
      current++;
      maxConcurrent = Math.max(maxConcurrent, current);
      await new Promise((r) => setTimeout(r, 10));
      current--;
      return "ok";
    };

    vi.useRealTimers();
    await runParallel(tickets, engine, processor, { concurrency: 2 });
    expect(maxConcurrent).toBeLessThanOrEqual(2);
  });

  it("concurrency=10 with 3 tickets runs all 3 at once", async () => {
    const tickets = mkTickets(3);
    let maxConcurrent = 0;
    let current = 0;

    const processor: TicketProcessor<string> = async () => {
      current++;
      maxConcurrent = Math.max(maxConcurrent, current);
      await new Promise((r) => setTimeout(r, 10));
      current--;
      return "ok";
    };

    vi.useRealTimers();
    await runParallel(tickets, engine, processor, { concurrency: 10 });
    expect(maxConcurrent).toBe(3);
  });

  it("defaults to concurrency=4 when not specified", async () => {
    const tickets = mkTickets(8);
    let maxConcurrent = 0;
    let current = 0;

    const processor: TicketProcessor<string> = async () => {
      current++;
      maxConcurrent = Math.max(maxConcurrent, current);
      await new Promise((r) => setTimeout(r, 20));
      current--;
      return "ok";
    };

    vi.useRealTimers();
    await runParallel(tickets, engine, processor);
    expect(maxConcurrent).toBeLessThanOrEqual(4);
  });
});

// ===========================================================================
// 4. onComplete callback
// ===========================================================================

describe("onComplete callback", () => {
  it("fires for each completed ticket", async () => {
    const tickets = mkTickets(3);
    const onComplete = vi.fn();
    vi.useRealTimers();
    await runParallel(tickets, engine, okProcessor(), { onComplete });
    expect(onComplete).toHaveBeenCalledTimes(3);
  });

  it("fires with (ticket, true) on success", async () => {
    const tickets = mkTickets(1);
    const onComplete = vi.fn();
    vi.useRealTimers();
    await runParallel(tickets, engine, okProcessor(), { onComplete });
    expect(onComplete).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Bug 1" }),
      true,
    );
  });

  it("fires with (ticket, false) on failure", async () => {
    const tickets = mkTickets(1);
    const onComplete = vi.fn();
    vi.useRealTimers();
    await runParallel(tickets, engine, failProcessor(), { onComplete });
    expect(onComplete).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Bug 1" }),
      false,
    );
  });

  it("fires for both succeeded and failed tickets", async () => {
    const tickets = mkTickets(2);
    const onComplete = vi.fn();
    let idx = 0;
    const processor: TicketProcessor<string> = async () => {
      if (idx++ === 0) throw new Error("fail");
      return "ok";
    };
    vi.useRealTimers();
    await runParallel(tickets, engine, processor, { onComplete });
    expect(onComplete).toHaveBeenCalledTimes(2);
    const calls = onComplete.mock.calls;
    const okCalls = calls.filter((c: unknown[]) => c[1] === true);
    const failCalls = calls.filter((c: unknown[]) => c[1] === false);
    expect(okCalls.length).toBe(1);
    expect(failCalls.length).toBe(1);
  });
});

// ===========================================================================
// 5. Timeout handling
// ===========================================================================

describe("Timeout handling", () => {
  it("ticket exceeding timeout is marked as failed", async () => {
    const tickets = mkTickets(1);
    const slowProcessor: TicketProcessor<string> = () =>
      new Promise((resolve) => setTimeout(() => resolve("late"), 5000));

    vi.useRealTimers();
    const result = await runParallel(tickets, engine, slowProcessor, {
      timeoutMs: 50,
    });
    expect(result.failed.length).toBe(1);
    expect(result.failed[0].error.message).toContain("Timeout");
  });

  it("fast tickets succeed even with tight timeout", async () => {
    const tickets = mkTickets(3);
    vi.useRealTimers();
    const result = await runParallel(tickets, engine, okProcessor(), {
      timeoutMs: 5000,
    });
    expect(result.succeeded.length).toBe(3);
    expect(result.failed.length).toBe(0);
  });

  it("mix of fast and slow tickets with timeout", async () => {
    const tickets = mkTickets(3);
    let idx = 0;
    const mixProcessor: TicketProcessor<string> = () => {
      const i = idx++;
      if (i === 1) {
        return new Promise((resolve) => setTimeout(() => resolve("slow"), 5000));
      }
      return Promise.resolve("fast");
    };

    vi.useRealTimers();
    const result = await runParallel(tickets, engine, mixProcessor, {
      timeoutMs: 100,
    });
    expect(result.succeeded.length).toBe(2);
    expect(result.failed.length).toBe(1);
  });
});

// ===========================================================================
// 6. parallelInvestigate wrapper
// ===========================================================================

describe("parallelInvestigate", () => {
  it("processes investigation on multiple tickets", async () => {
    const tickets = mkTickets(3);
    const investigator: TicketProcessor<string> = async (ticket) =>
      `Investigated: ${ticket.title}`;

    vi.useRealTimers();
    const result = await parallelInvestigate(tickets, engine, investigator);
    expect(result.succeeded.length).toBe(3);
    expect(result.succeeded[0].result).toContain("Investigated:");
  });

  it("returns succeeded + failed arrays", async () => {
    const tickets = mkTickets(2);
    let idx = 0;
    const investigator: TicketProcessor<string> = async () => {
      if (idx++ === 0) throw new Error("fail");
      return "report";
    };

    vi.useRealTimers();
    const result = await parallelInvestigate(tickets, engine, investigator);
    expect(result.succeeded.length).toBe(1);
    expect(result.failed.length).toBe(1);
  });

  it("respects concurrency option", async () => {
    const tickets = mkTickets(4);
    let maxConcurrent = 0;
    let current = 0;

    const investigator: TicketProcessor<string> = async () => {
      current++;
      maxConcurrent = Math.max(maxConcurrent, current);
      await new Promise((r) => setTimeout(r, 10));
      current--;
      return "report";
    };

    vi.useRealTimers();
    await parallelInvestigate(tickets, engine, investigator, {
      concurrency: 2,
    });
    expect(maxConcurrent).toBeLessThanOrEqual(2);
  });

  it("handles empty ticket array", async () => {
    const result = await parallelInvestigate(
      [],
      engine,
      okProcessor(),
    );
    expect(result.succeeded).toEqual([]);
    expect(result.failed).toEqual([]);
  });
});

// ===========================================================================
// 7. parallelDevelop wrapper
// ===========================================================================

describe("parallelDevelop", () => {
  it("processes development on multiple tickets", async () => {
    const tickets = mkTickets(3);
    const developer: TicketProcessor<string> = async (ticket) =>
      `Fixed: ${ticket.title}`;

    vi.useRealTimers();
    const result = await parallelDevelop(tickets, engine, developer);
    expect(result.succeeded.length).toBe(3);
    expect(result.succeeded[0].result).toContain("Fixed:");
  });

  it("returns succeeded + failed arrays", async () => {
    const tickets = mkTickets(2);
    let idx = 0;
    const developer: TicketProcessor<string> = async () => {
      if (idx++ === 0) throw new Error("build fail");
      return "branch";
    };

    vi.useRealTimers();
    const result = await parallelDevelop(tickets, engine, developer);
    expect(result.succeeded.length).toBe(1);
    expect(result.failed.length).toBe(1);
  });

  it("respects concurrency option", async () => {
    const tickets = mkTickets(6);
    let maxConcurrent = 0;
    let current = 0;

    const developer: TicketProcessor<string> = async () => {
      current++;
      maxConcurrent = Math.max(maxConcurrent, current);
      await new Promise((r) => setTimeout(r, 10));
      current--;
      return "branch";
    };

    vi.useRealTimers();
    await parallelDevelop(tickets, engine, developer, { concurrency: 3 });
    expect(maxConcurrent).toBeLessThanOrEqual(3);
  });

  it("handles empty ticket array", async () => {
    const result = await parallelDevelop([], engine, okProcessor());
    expect(result.succeeded).toEqual([]);
    expect(result.failed).toEqual([]);
  });

  it("fires onComplete for each ticket", async () => {
    const tickets = mkTickets(3);
    const onComplete = vi.fn();

    vi.useRealTimers();
    await parallelDevelop(tickets, engine, okProcessor("branch"), {
      onComplete,
    });
    expect(onComplete).toHaveBeenCalledTimes(3);
  });
});

// ===========================================================================
// 8. Engine passed to processor
// ===========================================================================

describe("Engine passed to processor", () => {
  it("processor receives the engine argument", async () => {
    const tickets = mkTickets(1);
    const processor: TicketProcessor<string> = async (_ticket, eng) => {
      return eng.name;
    };

    vi.useRealTimers();
    const result = await runParallel(tickets, engine, processor);
    expect(result.succeeded[0].result).toBe("mock-engine");
  });
});

// ===========================================================================
// 9. Result order
// ===========================================================================

describe("Result ordering", () => {
  it("all tickets appear exactly once across succeeded + failed", async () => {
    const tickets = mkTickets(5);
    let idx = 0;
    const processor: TicketProcessor<string> = async () => {
      if (idx++ % 2 === 0) throw new Error("even fails");
      return "ok";
    };

    vi.useRealTimers();
    const result = await runParallel(tickets, engine, processor);
    const total = result.succeeded.length + result.failed.length;
    expect(total).toBe(5);
    // Verify no ticket ID duplication
    const ids = [
      ...result.succeeded.map((s) => s.ticket.ticketId),
      ...result.failed.map((f) => f.ticket.ticketId),
    ];
    expect(new Set(ids).size).toBe(5);
  });
});

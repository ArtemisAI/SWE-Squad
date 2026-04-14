/**
 * Retry an async operation with exponential backoff.
 *
 * Designed for transient failures like "TypeError: fetch failed"
 * that occur when HTTP connections go stale during long operations
 * (e.g. Supabase calls after a 60+ second engine.run() subprocess).
 */
export async function withRetry<T>(
  fn: () => Promise<T>,
  opts?: { retries?: number; delayMs?: number; label?: string },
): Promise<T> {
  const retries = opts?.retries ?? 3;
  const baseDelay = opts?.delayMs ?? 1000;

  let lastError: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      const msg = String(err);
      const isTransient =
        msg.includes("fetch failed") ||
        msg.includes("ECONNRESET") ||
        msg.includes("ECONNREFUSED") ||
        msg.includes("ETIMEDOUT") ||
        msg.includes("socket hang up");
      if (!isTransient || attempt === retries) {
        throw err;
      }
      const delay = baseDelay * Math.pow(2, attempt);
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }
  throw lastError;
}

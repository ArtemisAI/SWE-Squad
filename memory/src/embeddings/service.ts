/**
 * EmbeddingService — calls bge-m3 via BASE_LLM proxy (OpenAI-compatible API).
 *
 * Config from environment:
 *   BASE_LLM_API_URL  — default: http://127.0.0.1:8082/v1
 *   BASE_LLM_API_KEY  — default: empty string
 *   EMBEDDING_MODEL   — default: bge-m3
 *   EMBEDDING_DIM     — default: 1024
 *
 * Circuit breaker: if a 401 or 403 is received, the service disables itself
 * for 15 minutes to avoid burning tokens against a bad key.  All other errors
 * are logged and produce a null return — callers must treat null as "embedding
 * unavailable" and degrade gracefully.
 */

// ---------------------------------------------------------------------------
// Module-level circuit breaker state
// ---------------------------------------------------------------------------
let _disabled = false;
let _disabledUntil = 0;

const DISABLE_DURATION_MS = 15 * 60 * 1000; // 15 minutes

function _isDisabled(): boolean {
  if (!_disabled) return false;
  if (Date.now() >= _disabledUntil) {
    _disabled = false;
    _disabledUntil = 0;
    console.info('[EmbeddingService] circuit breaker reset — re-enabling');
    return false;
  }
  return true;
}

function _disable(reason: string): void {
  _disabled = true;
  _disabledUntil = Date.now() + DISABLE_DURATION_MS;
  const until = new Date(_disabledUntil).toISOString();
  console.error(`[EmbeddingService] circuit breaker OPEN — ${reason}. Disabled until ${until}`);
}

// ---------------------------------------------------------------------------
// Response shape expected from the OpenAI-compatible embeddings endpoint
// ---------------------------------------------------------------------------
interface EmbeddingResponse {
  data: Array<{
    embedding: number[];
    index: number;
    object: string;
  }>;
  model?: string;
  usage?: { prompt_tokens: number; total_tokens: number };
}

// ---------------------------------------------------------------------------
// EmbeddingService
// ---------------------------------------------------------------------------
export class EmbeddingService {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly model: string;
  private readonly dim: number;

  constructor() {
    this.baseUrl = (
      process.env['BASE_LLM_API_URL'] ?? 'http://127.0.0.1:8082/v1'
    ).replace(/\/$/, ''); // strip trailing slash
    this.apiKey = process.env['BASE_LLM_API_KEY'] ?? '';
    this.model = process.env['EMBEDDING_MODEL'] ?? 'bge-m3';
    this.dim = parseInt(process.env['EMBEDDING_DIM'] ?? '1024', 10);
  }

  /**
   * Embed a single text string.
   * Returns a float array of length `this.dim`, or null on any failure.
   */
  async embed(text: string): Promise<number[] | null> {
    const results = await this.embedBatch([text]);
    return results[0] ?? null;
  }

  /**
   * Embed multiple texts in a single API call.
   * Returns an array parallel to `texts`; failed slots are null.
   */
  async embedBatch(texts: string[]): Promise<(number[] | null)[]> {
    if (texts.length === 0) return [];

    if (_isDisabled()) {
      console.warn('[EmbeddingService] skipping embed — circuit breaker is OPEN');
      return texts.map(() => null);
    }

    const url = `${this.baseUrl}/embeddings`;
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (this.apiKey) {
      headers['Authorization'] = `Bearer ${this.apiKey}`;
    }

    const body = JSON.stringify({
      model: this.model,
      input: texts.length === 1 ? texts[0] : texts,
    });

    let response: Response;
    try {
      response = await fetch(url, { method: 'POST', headers, body });
    } catch (networkErr) {
      console.error('[EmbeddingService] network error calling embeddings API:', networkErr);
      return texts.map(() => null);
    }

    // Circuit-breaker trigger: auth failures
    if (response.status === 401 || response.status === 403) {
      _disable(`HTTP ${response.status} from embeddings API`);
      return texts.map(() => null);
    }

    if (!response.ok) {
      console.error(
        `[EmbeddingService] embeddings API returned HTTP ${response.status} — skipping`
      );
      return texts.map(() => null);
    }

    let payload: EmbeddingResponse;
    try {
      payload = (await response.json()) as EmbeddingResponse;
    } catch (parseErr) {
      console.error('[EmbeddingService] failed to parse embeddings response:', parseErr);
      return texts.map(() => null);
    }

    if (!Array.isArray(payload?.data)) {
      console.error('[EmbeddingService] unexpected response shape — missing data array');
      return texts.map(() => null);
    }

    // Map results back by index; fill missing slots with null
    const resultMap = new Map<number, number[]>();
    for (const item of payload.data) {
      if (Array.isArray(item.embedding)) {
        resultMap.set(item.index, item.embedding);
      }
    }

    return texts.map((_, i) => {
      const vec = resultMap.get(i) ?? null;
      if (vec !== null && vec.length !== this.dim) {
        console.warn(
          `[EmbeddingService] embedding dim mismatch at index ${i}: ` +
            `expected ${this.dim}, got ${vec.length}`
        );
      }
      return vec;
    });
  }

  /** Expose current circuit-breaker state for health checks. */
  get isDisabled(): boolean {
    return _isDisabled();
  }
}

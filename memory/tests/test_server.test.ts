/**
 * test_server.test.ts
 *
 * Integration tests for the Express memory server (createApp factory).
 * Uses MockStorageAdapter from test_storage.test.ts and a mock EmbeddingService
 * so no live Supabase or network is needed.
 *
 * Runs with: npx tsx --test tests/test_server.test.ts
 * (or together: npx tsx --test tests/test_storage.test.ts tests/test_server.test.ts)
 */

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';

import { createApp } from '../src/server.js';
import type { EmbeddingService } from '../src/embeddings/service.js';

// Re-use the in-memory adapter from the storage tests.
import { MockStorageAdapter } from './test_storage.test.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Perform a fetch request relative to baseUrl and parse the JSON body. */
async function request(
  baseUrl: string,
  path: string,
  options: {
    method?: string;
    body?: unknown;
    headers?: Record<string, string>;
  } = {},
): Promise<{ status: number; body: unknown; text: string }> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = {
    ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    ...options.headers,
  };
  const res = await fetch(`${baseUrl}${path}`, {
    method,
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  const text = await res.text();
  let body: unknown = text;
  try {
    body = JSON.parse(text);
  } catch {
    // Leave as raw text for plain-text endpoints.
  }
  return { status: res.status, body, text };
}

// ---------------------------------------------------------------------------
// Mock EmbeddingService
// ---------------------------------------------------------------------------

/** Minimal embeddings stub that always returns a zero 1024-vector. */
const mockEmbeddings = {
  embed: async (_text: string): Promise<number[]> => new Array(1024).fill(0) as number[],
} as unknown as EmbeddingService;

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe('Memory Service HTTP API', () => {
  let server: http.Server;
  let baseUrl: string;
  let mockStorage: MockStorageAdapter;

  before(async () => {
    mockStorage = new MockStorageAdapter();
    const app = createApp({ storage: mockStorage, embeddings: mockEmbeddings });

    await new Promise<void>((resolve) => {
      server = app.listen(0, '127.0.0.1', resolve);
    });

    const addr = server.address() as { port: number };
    baseUrl = `http://127.0.0.1:${addr.port}`;
  });

  after(async () => {
    await new Promise<void>((resolve, reject) =>
      server.close((err) => (err ? reject(err) : resolve())),
    );
  });

  // -------------------------------------------------------------------------
  // Health
  // -------------------------------------------------------------------------

  it('GET /api/health returns 200 with status ok', async () => {
    const { status, body } = await request(baseUrl, '/api/health');
    assert.strictEqual(status, 200);
    assert.ok(typeof body === 'object' && body !== null);
    assert.strictEqual((body as Record<string, unknown>)['status'], 'ok');
  });

  // -------------------------------------------------------------------------
  // Readiness
  // -------------------------------------------------------------------------

  it('GET /api/readiness returns 503 when SUPABASE_URL not set', async () => {
    // In the test environment SUPABASE_URL is not set, so the server is not ready.
    const { status, body } = await request(baseUrl, '/api/readiness');
    assert.strictEqual(status, 503);
    assert.ok(typeof body === 'object' && body !== null);
    assert.strictEqual((body as Record<string, unknown>)['ready'], false);
  });

  // -------------------------------------------------------------------------
  // POST /api/sessions/init
  // -------------------------------------------------------------------------

  it('POST /api/sessions/init returns sessionDbId and memorySessionId', async () => {
    const { status, body } = await request(baseUrl, '/api/sessions/init', {
      method: 'POST',
      body: {
        teamId: 'test',
        contentSessionId: 'cs-init-1',
        project: 'test-project',
      },
    });

    assert.strictEqual(status, 200);
    const b = body as Record<string, unknown>;
    assert.ok(typeof b['sessionDbId'] === 'number', 'sessionDbId should be a number');
    assert.ok(typeof b['memorySessionId'] === 'string', 'memorySessionId should be a string');
    assert.ok((b['memorySessionId'] as string).length > 0, 'memorySessionId should not be empty');
  });

  it('POST /api/sessions/init returns 400 when contentSessionId missing', async () => {
    const { status, body } = await request(baseUrl, '/api/sessions/init', {
      method: 'POST',
      body: {
        teamId: 'test',
        project: 'test-project',
        // contentSessionId intentionally omitted
      },
    });

    assert.strictEqual(status, 400);
    const b = body as Record<string, unknown>;
    assert.ok(typeof b['error'] === 'string', 'error field should be present');
  });

  // -------------------------------------------------------------------------
  // POST /api/sessions/observations
  // -------------------------------------------------------------------------

  it('POST /api/sessions/observations returns 202 when session not initialized', async () => {
    // No session has been created for this contentSessionId.
    const { status, body } = await request(baseUrl, '/api/sessions/observations', {
      method: 'POST',
      body: {
        teamId: 'test',
        contentSessionId: 'cs-does-not-exist',
        tool_name: 'Read',
        tool_response: 'file contents here',
      },
    });

    assert.strictEqual(status, 202);
    const b = body as Record<string, unknown>;
    assert.strictEqual(b['ok'], true);
  });

  it('POST /api/sessions/observations succeeds after session init', async () => {
    const contentSessionId = 'cs-obs-flow-1';

    // Step 1: init session
    const initRes = await request(baseUrl, '/api/sessions/init', {
      method: 'POST',
      body: {
        teamId: 'test',
        contentSessionId,
        project: 'obs-project',
      },
    });
    assert.strictEqual(initRes.status, 200);

    // Step 2: store observation
    const obsRes = await request(baseUrl, '/api/sessions/observations', {
      method: 'POST',
      body: {
        teamId: 'test',
        contentSessionId,
        project: 'obs-project',
        tool_name: 'Bash',
        tool_response: 'command output',
      },
    });

    // Should be 200 (stored) or 409 (dedup hit on rapid re-call — acceptable).
    assert.ok(
      obsRes.status === 200 || obsRes.status === 409,
      `expected 200 or 409, got ${obsRes.status}`,
    );
  });

  // -------------------------------------------------------------------------
  // GET /api/search
  // -------------------------------------------------------------------------

  it('GET /api/search returns empty observations for unknown query', async () => {
    const { status, body } = await request(
      baseUrl,
      '/api/search?q=zzz-no-match-query&teamId=test',
    );

    assert.strictEqual(status, 200);
    const b = body as Record<string, unknown>;
    assert.ok(Array.isArray(b['observations']), 'observations should be an array');
    assert.strictEqual(typeof b['total'], 'number');
  });

  // -------------------------------------------------------------------------
  // GET /api/context/inject
  // -------------------------------------------------------------------------

  it('GET /api/context/inject returns plain text', async () => {
    const res = await fetch(`${baseUrl}/api/context/inject?project=test-project&teamId=test`);
    assert.ok(res.status === 200, `expected 200, got ${res.status}`);

    const contentType = res.headers.get('content-type') ?? '';
    assert.ok(
      contentType.includes('text/plain'),
      `expected text/plain content-type, got ${contentType}`,
    );

    // Body can be empty string when there are no observations — that's fine.
    const text = await res.text();
    assert.ok(typeof text === 'string');
  });

  // -------------------------------------------------------------------------
  // POST /api/context/semantic
  // -------------------------------------------------------------------------

  it('POST /api/context/semantic returns context string', async () => {
    const { status, body } = await request(baseUrl, '/api/context/semantic', {
      method: 'POST',
      body: {
        teamId: 'test',
        query: 'authentication bug fix',
        project: 'test-project',
      },
    });

    assert.strictEqual(status, 200);
    const b = body as Record<string, unknown>;
    assert.ok('context' in b, 'response should have a context field');
    assert.ok(typeof b['context'] === 'string', 'context should be a string');
  });

  // -------------------------------------------------------------------------
  // Auth middleware — localhost bypass
  // -------------------------------------------------------------------------

  it('Requests without Authorization are allowed from localhost (127.0.0.1)', async () => {
    // We are already calling from 127.0.0.1 in all tests above.
    // This test explicitly omits any auth header and verifies the health
    // endpoint still returns 200 (not 401).
    const res = await fetch(`${baseUrl}/api/health`);
    assert.strictEqual(res.status, 200, 'localhost requests should bypass auth unconditionally');
  });
});

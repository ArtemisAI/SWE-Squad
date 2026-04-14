/**
 * test_storage.test.ts
 *
 * Tests for the StorageAdapter contract via an in-memory MockStorageAdapter.
 * Runs with: npx tsx --test tests/test_storage.test.ts
 *
 * No live Supabase or network required.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import type {
  StorageAdapter,
  SessionRow,
  ObservationRow,
  SearchResult,
  CreateSessionParams,
  StoreObservationParams,
  StoreSummaryParams,
} from '../src/storage/types.js';

// ---------------------------------------------------------------------------
// MockStorageAdapter
// ---------------------------------------------------------------------------

/** 30-second dedup window in milliseconds — mirrors the real adapter. */
const DEDUP_WINDOW_MS = 30_000;

/**
 * Thread-safe (single-process) in-memory implementation of StorageAdapter.
 * Uses Maps keyed by stable composite strings.
 *
 * Exported so server tests can import without duplicating the implementation.
 */
export class MockStorageAdapter implements StorageAdapter {
  private readonly _sessions = new Map<string, SessionRow>();
  private readonly _observations = new Map<number, ObservationRow>();
  private _nextSessionId = 1;
  private _nextObsId = 1;

  // --------------------------------------------------------------------------
  // Private helpers
  // --------------------------------------------------------------------------

  private static _assertTeamId(teamId: string | undefined | null): asserts teamId is string {
    if (!teamId || teamId.trim() === '') {
      throw new Error('[MockStorageAdapter] team_id is required');
    }
  }

  // --------------------------------------------------------------------------
  // createSession
  // --------------------------------------------------------------------------

  async createSession(
    params: CreateSessionParams,
  ): Promise<{ sessionDbId: number; memorySessionId: string }> {
    MockStorageAdapter._assertTeamId(params.team_id);

    const key = `${params.team_id}:${params.content_session_id}`;
    const existing = this._sessions.get(key);
    if (existing) {
      return { sessionDbId: existing.id, memorySessionId: existing.memory_session_id };
    }

    const id = this._nextSessionId++;
    const row: SessionRow = {
      id,
      team_id: params.team_id,
      content_session_id: params.content_session_id,
      memory_session_id: params.memory_session_id,
      project: params.project,
      platform_source: params.platform_source,
      agent_id: params.agent_id ?? null,
      user_prompt: params.user_prompt ?? null,
      status: 'active',
      started_at_epoch: Date.now(),
      completed_at_epoch: null,
    };
    this._sessions.set(key, row);
    return { sessionDbId: id, memorySessionId: row.memory_session_id };
  }

  // --------------------------------------------------------------------------
  // getSessionByContentId
  // --------------------------------------------------------------------------

  async getSessionByContentId(
    teamId: string,
    contentSessionId: string,
  ): Promise<SessionRow | null> {
    MockStorageAdapter._assertTeamId(teamId);
    const key = `${teamId}:${contentSessionId}`;
    return this._sessions.get(key) ?? null;
  }

  // --------------------------------------------------------------------------
  // completeSession
  // --------------------------------------------------------------------------

  async completeSession(teamId: string, contentSessionId: string): Promise<void> {
    MockStorageAdapter._assertTeamId(teamId);
    const key = `${teamId}:${contentSessionId}`;
    const row = this._sessions.get(key);
    if (row) {
      row.status = 'completed';
      row.completed_at_epoch = Date.now();
    }
  }

  // --------------------------------------------------------------------------
  // storeObservation
  // --------------------------------------------------------------------------

  async storeObservation(
    params: StoreObservationParams,
  ): Promise<{ id: number; createdAtEpoch: number } | null> {
    MockStorageAdapter._assertTeamId(params.team_id);

    // Dedup: if same content_hash was stored within 30 s, drop it.
    if (params.content_hash) {
      const cutoff = Date.now() - DEDUP_WINDOW_MS;
      for (const row of this._observations.values()) {
        if (
          row.content_hash === params.content_hash &&
          row.team_id === params.team_id &&
          row.created_at_epoch >= cutoff
        ) {
          return null;
        }
      }
    }

    const id = this._nextObsId++;
    const createdAtEpoch = Date.now();
    const row: ObservationRow = {
      id,
      team_id: params.team_id,
      memory_session_id: params.memory_session_id,
      project: params.project,
      type: params.type ?? null,
      title: params.title ?? null,
      narrative: params.narrative ?? null,
      text: params.text ?? null,
      facts: params.facts ?? null,
      concepts: params.concepts ?? null,
      files_read: params.files_read ?? null,
      files_modified: params.files_modified ?? null,
      content_hash: params.content_hash ?? null,
      embedding: params.embedding ?? null,
      fts_vector: null,
      created_at_epoch: createdAtEpoch,
    };
    this._observations.set(id, row);
    return { id, createdAtEpoch };
  }

  // --------------------------------------------------------------------------
  // searchObservations
  // --------------------------------------------------------------------------

  async searchObservations(
    teamId: string,
    _query: string,
    project?: string,
    type?: string,
    limit: number = 50,
  ): Promise<SearchResult[]> {
    MockStorageAdapter._assertTeamId(teamId);

    const rows = [...this._observations.values()]
      .filter((r) => r.team_id === teamId)
      .filter((r) => (project ? r.project === project : true))
      .filter((r) => (type ? r.type === type : true))
      .slice(0, limit);

    return rows.map(MockStorageAdapter._toSearchResult);
  }

  // --------------------------------------------------------------------------
  // semanticSearch
  // --------------------------------------------------------------------------

  async semanticSearch(
    teamId: string,
    _embedding: number[],
    project?: string,
    topK: number = 10,
    _threshold: number = 0.70,
  ): Promise<SearchResult[]> {
    MockStorageAdapter._assertTeamId(teamId);

    // No real vector arithmetic — return whatever matches the project filter.
    const rows = [...this._observations.values()]
      .filter((r) => r.team_id === teamId)
      .filter((r) => (project ? r.project === project : true))
      .slice(0, topK);

    return rows.map(MockStorageAdapter._toSearchResult);
  }

  // --------------------------------------------------------------------------
  // getRecentObservations
  // --------------------------------------------------------------------------

  async getRecentObservations(
    teamId: string,
    project: string,
    limit: number = 20,
  ): Promise<SearchResult[]> {
    MockStorageAdapter._assertTeamId(teamId);

    const rows = [...this._observations.values()]
      .filter((r) => r.team_id === teamId && r.project === project)
      .sort((a, b) => b.created_at_epoch - a.created_at_epoch)
      .slice(0, limit);

    return rows.map(MockStorageAdapter._toSearchResult);
  }

  // --------------------------------------------------------------------------
  // storeSummary
  // --------------------------------------------------------------------------

  async storeSummary(_params: StoreSummaryParams): Promise<{ id: number }> {
    // Minimal implementation — just return a synthetic id.
    return { id: 1 };
  }

  // --------------------------------------------------------------------------
  // Internal row → SearchResult mapping
  // --------------------------------------------------------------------------

  private static _toSearchResult(row: ObservationRow): SearchResult {
    return {
      id: row.id,
      project: row.project,
      type: row.type,
      title: row.title,
      narrative: row.narrative,
      facts: row.facts,
      concepts: row.concepts,
      files_read: row.files_read,
      files_modified: row.files_modified,
      similarity: null,
      created_at_epoch: row.created_at_epoch,
    };
  }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('MockStorageAdapter — StorageAdapter contract', () => {
  // -------------------------------------------------------------------------
  // createSession
  // -------------------------------------------------------------------------

  it('createSession returns sessionDbId and memorySessionId', async () => {
    const adapter = new MockStorageAdapter();
    const result = await adapter.createSession({
      team_id: 'team-a',
      content_session_id: 'session-1',
      memory_session_id: 'uuid-abc-123',
      project: 'proj-x',
      platform_source: 'test',
    });

    assert.ok(typeof result.sessionDbId === 'number', 'sessionDbId must be a number');
    assert.ok(result.sessionDbId >= 1, 'sessionDbId must be >= 1');
    assert.strictEqual(result.memorySessionId, 'uuid-abc-123');
  });

  it('createSession is idempotent — same contentSessionId returns same session', async () => {
    const adapter = new MockStorageAdapter();
    const params: CreateSessionParams = {
      team_id: 'team-b',
      content_session_id: 'session-dup',
      memory_session_id: 'uuid-dup',
      project: 'proj-y',
      platform_source: 'test',
    };

    const first = await adapter.createSession(params);
    const second = await adapter.createSession({
      ...params,
      memory_session_id: 'uuid-different', // different UUID supplied — must be ignored
    });

    assert.strictEqual(second.sessionDbId, first.sessionDbId);
    assert.strictEqual(second.memorySessionId, first.memorySessionId);
  });

  // -------------------------------------------------------------------------
  // storeObservation
  // -------------------------------------------------------------------------

  it('storeObservation returns id and createdAtEpoch', async () => {
    const adapter = new MockStorageAdapter();
    const beforeMs = Date.now();

    const result = await adapter.storeObservation({
      team_id: 'team-c',
      memory_session_id: 'ms-001',
      project: 'proj-z',
      title: 'Test observation',
      content_hash: 'hash-unique-001',
    });

    const afterMs = Date.now();

    assert.notEqual(result, null, 'should return a result, not null');
    assert.ok(result !== null);
    assert.ok(typeof result.id === 'number' && result.id >= 1, 'id must be a positive number');
    assert.ok(
      result.createdAtEpoch >= beforeMs && result.createdAtEpoch <= afterMs,
      'createdAtEpoch should be within the test window',
    );
  });

  it('storeObservation dedup returns null for same content_hash within 30 s', async () => {
    const adapter = new MockStorageAdapter();
    const sharedParams: StoreObservationParams = {
      team_id: 'team-d',
      memory_session_id: 'ms-002',
      project: 'proj-dedup',
      content_hash: 'hash-dedup-abc',
    };

    const first = await adapter.storeObservation(sharedParams);
    assert.notEqual(first, null, 'first write should succeed');

    const second = await adapter.storeObservation(sharedParams);
    assert.strictEqual(second, null, 'duplicate within 30 s should return null');
  });

  it('storeObservation throws on empty team_id', async () => {
    const adapter = new MockStorageAdapter();
    await assert.rejects(
      () =>
        adapter.storeObservation({
          team_id: '',
          memory_session_id: 'ms-003',
          project: 'proj',
        }),
      /team_id is required/i,
    );
  });

  // -------------------------------------------------------------------------
  // searchObservations
  // -------------------------------------------------------------------------

  it('searchObservations throws on empty team_id', async () => {
    const adapter = new MockStorageAdapter();
    await assert.rejects(
      () => adapter.searchObservations('', 'some query'),
      /team_id is required/i,
    );
  });

  // -------------------------------------------------------------------------
  // getRecentObservations
  // -------------------------------------------------------------------------

  it('getRecentObservations returns empty array when no observations exist', async () => {
    const adapter = new MockStorageAdapter();
    const results = await adapter.getRecentObservations('team-e', 'proj-empty');
    assert.deepEqual(results, []);
  });
});

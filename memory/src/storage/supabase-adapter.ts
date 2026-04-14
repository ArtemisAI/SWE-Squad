/**
 * Supabase PostgREST implementation of the StorageAdapter interface.
 *
 * Uses raw fetch() with no SDK — mirrors the pattern in
 * src/swe_team/supabase_store.py. Every request carries both the
 * PostgREST apikey header and an Authorization Bearer header so the
 * service works with both anon and service-role keys.
 *
 * All methods guard against empty teamId — a missing tenant scope is a
 * security error, not a graceful degradation case.
 *
 * Non-critical paths (observation writes, search) catch errors and return
 * null / [] rather than throwing so that agent operations never fail due
 * to memory service issues.
 */

import type {
  CreateSessionParams,
  ObservationRow,
  SearchResult,
  SessionRow,
  StorageAdapter,
  StoreObservationParams,
  StoreSummaryParams,
} from './types.js';

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/** 30-second dedup window for content_hash checks (in epoch milliseconds). */
const DEDUP_WINDOW_MS = 30_000;

/** Shared header set for all PostgREST requests that expect a response body. */
function makeHeaders(supabaseKey: string): Record<string, string> {
  return {
    apikey: supabaseKey,
    Authorization: `Bearer ${supabaseKey}`,
    'Content-Type': 'application/json',
    Prefer: 'return=representation',
  };
}

/** Header set for PATCH / write operations where we don't need the body back. */
function makeMinimalHeaders(supabaseKey: string): Record<string, string> {
  return {
    apikey: supabaseKey,
    Authorization: `Bearer ${supabaseKey}`,
    'Content-Type': 'application/json',
    Prefer: 'return=minimal',
  };
}

/** Strip undefined / null values from an object before sending as JSON body. */
function compact(obj: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(obj).filter(([, v]) => v !== undefined && v !== null),
  );
}

/** Throw a consistent error when team_id is missing. */
function assertTeamId(teamId: string | undefined | null): asserts teamId is string {
  if (!teamId) {
    throw new Error(
      '[SupabaseAdapter] team_id is required — refusing to execute a cross-tenant query.',
    );
  }
}

// ---------------------------------------------------------------------------
// SupabaseAdapter
// ---------------------------------------------------------------------------

export class SupabaseAdapter implements StorageAdapter {
  private readonly baseUrl: string;
  private readonly supabaseKey: string;

  constructor(supabaseUrl: string, supabaseKey: string) {
    if (!supabaseUrl) throw new Error('[SupabaseAdapter] supabaseUrl is required');
    if (!supabaseKey) throw new Error('[SupabaseAdapter] supabaseKey is required');
    this.baseUrl = supabaseUrl.replace(/\/$/, '');
    this.supabaseKey = supabaseKey;
  }

  // -------------------------------------------------------------------------
  // Session lifecycle
  // -------------------------------------------------------------------------

  async createSession(
    params: CreateSessionParams,
  ): Promise<{ sessionDbId: number; memorySessionId: string }> {
    assertTeamId(params.team_id);

    const memorySessionId =
      params.memory_session_id ?? (crypto.randomUUID() as string);

    const body = compact({
      team_id: params.team_id,
      content_session_id: params.content_session_id,
      memory_session_id: memorySessionId,
      project: params.project,
      platform_source: params.platform_source,
      agent_id: params.agent_id,
      user_prompt: params.user_prompt,
      status: 'active',
      started_at_epoch: Date.now(),
    });

    let response = await fetch(`${this.baseUrl}/rest/v1/memory_sessions`, {
      method: 'POST',
      headers: makeHeaders(this.supabaseKey),
      body: JSON.stringify(body),
    });

    // Handle UNIQUE constraint violation — upsert semantics via GET
    if (response.status === 409) {
      const existing = await this.getSessionByContentId(
        params.team_id,
        params.content_session_id,
      );
      if (existing) {
        return {
          sessionDbId: existing.id,
          memorySessionId: existing.memory_session_id,
        };
      }
      throw new Error(
        `[SupabaseAdapter] createSession: 409 conflict but no existing row found ` +
          `for team=${params.team_id} content_session_id=${params.content_session_id}`,
      );
    }

    if (!response.ok) {
      const text = await response.text().catch(() => '');
      // Check JSON body for Postgres error code 23505 (unique_violation)
      try {
        const json = JSON.parse(text) as { code?: string };
        if (json.code === '23505') {
          const existing = await this.getSessionByContentId(
            params.team_id,
            params.content_session_id,
          );
          if (existing) {
            return {
              sessionDbId: existing.id,
              memorySessionId: existing.memory_session_id,
            };
          }
        }
      } catch {
        // not JSON — fall through to error
      }
      throw new Error(
        `[SupabaseAdapter] createSession: HTTP ${response.status} — ${text}`,
      );
    }

    const rows = (await response.json()) as SessionRow[];
    if (!rows.length) {
      throw new Error('[SupabaseAdapter] createSession: empty response from Supabase');
    }
    return { sessionDbId: rows[0].id, memorySessionId: rows[0].memory_session_id };
  }

  async getSessionByContentId(
    teamId: string,
    contentSessionId: string,
  ): Promise<SessionRow | null> {
    assertTeamId(teamId);

    const url =
      `${this.baseUrl}/rest/v1/memory_sessions` +
      `?team_id=eq.${encodeURIComponent(teamId)}` +
      `&content_session_id=eq.${encodeURIComponent(contentSessionId)}` +
      `&limit=1`;

    let response: Response;
    try {
      response = await fetch(url, {
        method: 'GET',
        headers: makeHeaders(this.supabaseKey),
      });
    } catch (err) {
      console.error('[SupabaseAdapter] getSessionByContentId fetch error:', err);
      return null;
    }

    if (!response.ok) {
      console.error(
        `[SupabaseAdapter] getSessionByContentId: HTTP ${response.status}`,
      );
      return null;
    }

    const rows = (await response.json()) as SessionRow[];
    return rows.length > 0 ? rows[0] : null;
  }

  async completeSession(teamId: string, contentSessionId: string): Promise<void> {
    assertTeamId(teamId);

    const url =
      `${this.baseUrl}/rest/v1/memory_sessions` +
      `?team_id=eq.${encodeURIComponent(teamId)}` +
      `&content_session_id=eq.${encodeURIComponent(contentSessionId)}`;

    try {
      const response = await fetch(url, {
        method: 'PATCH',
        headers: makeMinimalHeaders(this.supabaseKey),
        body: JSON.stringify({
          status: 'completed',
          completed_at_epoch: Date.now(),
        }),
      });

      if (!response.ok) {
        const text = await response.text().catch(() => '');
        console.error(
          `[SupabaseAdapter] completeSession: HTTP ${response.status} — ${text}`,
        );
      }
    } catch (err) {
      console.error('[SupabaseAdapter] completeSession error:', err);
    }
  }

  // -------------------------------------------------------------------------
  // Observation storage
  // -------------------------------------------------------------------------

  async storeObservation(
    params: StoreObservationParams,
  ): Promise<{ id: number; createdAtEpoch: number } | null> {
    assertTeamId(params.team_id);

    // --- dedup check ---
    if (params.content_hash) {
      const isDuplicate = await this._isDuplicateObservation(
        params.team_id,
        params.content_hash,
      );
      if (isDuplicate) {
        return null;
      }
    }

    const now = Date.now();
    const body = compact({
      team_id: params.team_id,
      memory_session_id: params.memory_session_id,
      project: params.project,
      type: params.type,
      title: params.title,
      narrative: params.narrative,
      text: params.text,
      facts: params.facts,
      concepts: params.concepts,
      files_read: params.files_read,
      files_modified: params.files_modified,
      content_hash: params.content_hash,
      // pgvector accepts a JSON number array via PostgREST
      embedding: params.embedding,
      // Do NOT include fts_vector — DB trigger generates it automatically
      created_at_epoch: now,
    });

    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}/rest/v1/memory_observations`, {
        method: 'POST',
        headers: makeHeaders(this.supabaseKey),
        body: JSON.stringify(body),
      });
    } catch (err) {
      console.error('[SupabaseAdapter] storeObservation fetch error:', err);
      return null;
    }

    if (!response.ok) {
      const text = await response.text().catch(() => '');
      console.error(
        `[SupabaseAdapter] storeObservation: HTTP ${response.status} — ${text}`,
      );
      return null;
    }

    const rows = (await response.json()) as ObservationRow[];
    if (!rows.length) {
      console.error('[SupabaseAdapter] storeObservation: empty response');
      return null;
    }

    return { id: rows[0].id, createdAtEpoch: rows[0].created_at_epoch };
  }

  /** Returns true if a matching content_hash was seen within the dedup window. */
  private async _isDuplicateObservation(
    teamId: string,
    contentHash: string,
  ): Promise<boolean> {
    const cutoff = Date.now() - DEDUP_WINDOW_MS;
    const url =
      `${this.baseUrl}/rest/v1/memory_observations` +
      `?team_id=eq.${encodeURIComponent(teamId)}` +
      `&content_hash=eq.${encodeURIComponent(contentHash)}` +
      `&created_at_epoch=gte.${cutoff}` +
      `&limit=1`;

    try {
      const response = await fetch(url, {
        method: 'GET',
        headers: makeHeaders(this.supabaseKey),
      });
      if (!response.ok) return false;
      const rows = (await response.json()) as ObservationRow[];
      return rows.length > 0;
    } catch {
      // On network error, assume no duplicate — best-effort dedup.
      return false;
    }
  }

  // -------------------------------------------------------------------------
  // Search
  // -------------------------------------------------------------------------

  async searchObservations(
    teamId: string,
    query: string,
    project?: string,
    type?: string,
    limit = 50,
  ): Promise<SearchResult[]> {
    assertTeamId(teamId);

    const body = compact({
      p_team_id: teamId,
      p_query: query,
      p_limit: limit,
      p_project: project,
      p_type: type,
    });

    try {
      const response = await fetch(
        `${this.baseUrl}/rest/v1/rpc/search_memory_observations`,
        {
          method: 'POST',
          headers: makeHeaders(this.supabaseKey),
          body: JSON.stringify(body),
        },
      );

      if (!response.ok) {
        const text = await response.text().catch(() => '');
        console.error(
          `[SupabaseAdapter] searchObservations: HTTP ${response.status} — ${text}`,
        );
        return [];
      }

      const rows = (await response.json()) as Array<SearchResult & { rank?: number }>;
      // Normalise FTS rank onto the `similarity` field for a uniform shape
      return rows.map((r) => ({
        ...r,
        similarity: r.similarity ?? r.rank ?? null,
      }));
    } catch (err) {
      console.error('[SupabaseAdapter] searchObservations error:', err);
      return [];
    }
  }

  async semanticSearch(
    teamId: string,
    embedding: number[],
    project?: string,
    topK = 10,
    threshold = 0.7,
  ): Promise<SearchResult[]> {
    assertTeamId(teamId);

    const body = compact({
      p_team_id: teamId,
      p_embedding: embedding,
      p_top_k: topK,
      p_threshold: threshold,
      p_project: project,
    });

    try {
      const response = await fetch(
        `${this.baseUrl}/rest/v1/rpc/match_memory_observations`,
        {
          method: 'POST',
          headers: makeHeaders(this.supabaseKey),
          body: JSON.stringify(body),
        },
      );

      if (!response.ok) {
        const text = await response.text().catch(() => '');
        console.error(
          `[SupabaseAdapter] semanticSearch: HTTP ${response.status} — ${text}`,
        );
        return [];
      }

      return (await response.json()) as SearchResult[];
    } catch (err) {
      console.error('[SupabaseAdapter] semanticSearch error:', err);
      return [];
    }
  }

  async getRecentObservations(
    teamId: string,
    project: string,
    limit = 20,
  ): Promise<SearchResult[]> {
    assertTeamId(teamId);

    const url =
      `${this.baseUrl}/rest/v1/memory_observations` +
      `?team_id=eq.${encodeURIComponent(teamId)}` +
      `&project=eq.${encodeURIComponent(project)}` +
      `&order=created_at_epoch.desc` +
      `&limit=${limit}`;

    try {
      const response = await fetch(url, {
        method: 'GET',
        headers: makeHeaders(this.supabaseKey),
      });

      if (!response.ok) {
        const text = await response.text().catch(() => '');
        console.error(
          `[SupabaseAdapter] getRecentObservations: HTTP ${response.status} — ${text}`,
        );
        return [];
      }

      const rows = (await response.json()) as ObservationRow[];
      // Cast ObservationRow[] → SearchResult[] (subset of fields)
      return rows.map(
        (r): SearchResult => ({
          id: r.id,
          project: r.project,
          type: r.type,
          title: r.title,
          narrative: r.narrative,
          facts: r.facts,
          concepts: r.concepts,
          files_read: r.files_read,
          files_modified: r.files_modified,
          similarity: null,
          created_at_epoch: r.created_at_epoch,
        }),
      );
    } catch (err) {
      console.error('[SupabaseAdapter] getRecentObservations error:', err);
      return [];
    }
  }

  // -------------------------------------------------------------------------
  // Summary storage
  // -------------------------------------------------------------------------

  async storeSummary(params: StoreSummaryParams): Promise<{ id: number }> {
    assertTeamId(params.team_id);

    const body = compact({
      team_id: params.team_id,
      memory_session_id: params.memory_session_id,
      project: params.project,
      request: params.request,
      investigated: params.investigated,
      learned: params.learned,
      completed: params.completed,
      next_steps: params.next_steps,
      created_at_epoch: Date.now(),
    });

    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}/rest/v1/memory_summaries`, {
        method: 'POST',
        headers: makeHeaders(this.supabaseKey),
        body: JSON.stringify(body),
      });
    } catch (err) {
      console.error('[SupabaseAdapter] storeSummary fetch error:', err);
      throw err;
    }

    if (!response.ok) {
      const text = await response.text().catch(() => '');
      throw new Error(
        `[SupabaseAdapter] storeSummary: HTTP ${response.status} — ${text}`,
      );
    }

    const rows = (await response.json()) as Array<{ id: number }>;
    if (!rows.length) {
      throw new Error('[SupabaseAdapter] storeSummary: empty response from Supabase');
    }

    return { id: rows[0].id };
  }
}

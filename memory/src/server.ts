/**
 * SWE-Squad Memory Worker Service
 *
 * Minimal Express server implementing the claude-mem HTTP API surface,
 * backed by Supabase PostgreSQL (pgvector + FTS) instead of SQLite/Chroma.
 *
 * Endpoints (all used by memory/src/client.py):
 *   POST /api/sessions/init
 *   POST /api/sessions/complete
 *   POST /api/sessions/observations
 *   POST /api/sessions/summarize
 *   GET  /api/search
 *   GET  /api/context/inject
 *   POST /api/context/semantic
 *   GET  /api/health
 *   GET  /api/readiness
 */

import express, { type Request, type Response } from 'express';
import { createHash } from 'crypto';

import { createStorageAdapter, type StorageAdapter } from './storage/index.js';
import { EmbeddingService } from './embeddings/service.js';
import { authMiddleware } from './middleware/auth.js';
import {
  formatObservationsAsContext,
  formatTimelineContext,
  buildContextText,
} from './context/observation-compiler.js';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const PORT = parseInt(process.env.MEMORY_WORKER_PORT ?? '37777', 10);
const HOST = process.env.MEMORY_WORKER_HOST ?? '127.0.0.1';

const SUPABASE_URL = process.env.SUPABASE_URL ?? '';
const SUPABASE_KEY =
  process.env.SUPABASE_KEY ??
  process.env.SUPABASE_ANON_KEY ??
  process.env.SUPABASE_SERVICE_KEY ??
  '';

// ---------------------------------------------------------------------------
// Server factory (exported for testing with injected adapter)
// ---------------------------------------------------------------------------

export interface ServerDeps {
  storage: StorageAdapter;
  embeddings?: EmbeddingService;
}

export function createApp(deps?: ServerDeps): express.Application {
  const storage: StorageAdapter =
    deps?.storage ??
    createStorageAdapter({ supabaseUrl: SUPABASE_URL, supabaseKey: SUPABASE_KEY });

  const embeddings: EmbeddingService = deps?.embeddings ?? new EmbeddingService();

  const app = express();
  app.use(express.json({ limit: '1mb' }));
  app.set('trust proxy', 'loopback');
  app.use(authMiddleware);

  // -------------------------------------------------------------------------
  // Health / readiness
  // -------------------------------------------------------------------------

  app.get('/api/health', (_req: Request, res: Response) => {
    res.json({
      status: 'ok',
      service: 'swe-squad-memory',
      timestamp: new Date().toISOString(),
    });
  });

  app.get('/api/readiness', (_req: Request, res: Response) => {
    const ready = SUPABASE_URL !== '' && SUPABASE_KEY !== '';
    if (ready) {
      res.json({ ready: true });
    } else {
      res.status(503).json({ ready: false, reason: 'SUPABASE_URL or SUPABASE_KEY not set' });
    }
  });

  // -------------------------------------------------------------------------
  // POST /api/sessions/init
  //
  // Body: { contentSessionId, project, prompt?, platformSource?, agentId?, teamId? }
  // Returns: { sessionDbId, memorySessionId }
  // -------------------------------------------------------------------------

  app.post('/api/sessions/init', async (req: Request, res: Response) => {
    const teamId = req.teamId;
    const { contentSessionId, project, prompt, platformSource, agentId } = req.body as {
      contentSessionId?: string;
      project?: string;
      prompt?: string;
      platformSource?: string;
      agentId?: string;
    };

    if (!contentSessionId || !project) {
      res.status(400).json({ error: 'contentSessionId and project are required' });
      return;
    }

    try {
      const result = await storage.createSession({
        team_id: teamId,
        content_session_id: contentSessionId,
        memory_session_id: crypto.randomUUID(),
        project,
        platform_source: platformSource ?? 'swe-squad',
        agent_id: agentId,
        user_prompt: prompt,
      });
      res.json(result);
    } catch (err) {
      console.error('[memory/sessions/init]', err);
      res.status(500).json({ error: 'Failed to initialize session' });
    }
  });

  // -------------------------------------------------------------------------
  // POST /api/sessions/complete
  //
  // Body: { contentSessionId, teamId? }
  // -------------------------------------------------------------------------

  app.post('/api/sessions/complete', async (req: Request, res: Response) => {
    const teamId = req.teamId;
    const { contentSessionId } = req.body as { contentSessionId?: string };

    if (!contentSessionId) {
      res.status(400).json({ error: 'contentSessionId is required' });
      return;
    }

    try {
      await storage.completeSession(teamId, contentSessionId);
      res.json({ ok: true });
    } catch (err) {
      console.error('[memory/sessions/complete]', err);
      res.status(500).json({ error: 'Failed to complete session' });
    }
  });

  // -------------------------------------------------------------------------
  // POST /api/sessions/observations  (fire-and-forget from client)
  //
  // Body: { contentSessionId, tool_name, tool_input?, tool_response?,
  //         cwd?, platformSource?, teamId? }
  // Returns: { id, createdAtEpoch } or 409 on dedup hit
  // -------------------------------------------------------------------------

  app.post('/api/sessions/observations', async (req: Request, res: Response) => {
    const teamId = req.teamId;
    const {
      contentSessionId,
      tool_name,
      tool_response,
      cwd,
      platformSource,
    } = req.body as {
      contentSessionId?: string;
      tool_name?: string;
      tool_response?: string;
      cwd?: string;
      platformSource?: string;
    };

    if (!contentSessionId) {
      res.status(400).json({ error: 'contentSessionId is required' });
      return;
    }

    // Resolve the memory_session_id from the session
    let memorySessionId: string | null = null;
    try {
      const session = await storage.getSessionByContentId(teamId, contentSessionId);
      if (!session) {
        // Session not initialised — silently accept (fire-and-forget; don't fail the agent)
        res.status(202).json({ ok: true, note: 'session not found, observation dropped' });
        return;
      }
      memorySessionId = session.memory_session_id;
    } catch {
      res.status(202).json({ ok: true, note: 'session lookup failed, observation dropped' });
      return;
    }

    // Build a concise title + narrative from the tool use
    const title = tool_name ? `${tool_name}` : 'observation';
    const narrative = tool_response ?? '';
    const project = (req.body as { project?: string }).project ?? '';

    // Dedup hash: sessionId|title|narrative
    const contentHash = createHash('sha256')
      .update(`${memorySessionId}|${title}|${narrative}`)
      .digest('hex');

    // Async embedding (non-blocking — we respond immediately, embed in background)
    const embeddingPromise = embeddings.embed(`${title} ${narrative}`).catch(() => null);

    try {
      const result = await storage.storeObservation({
        team_id: teamId,
        memory_session_id: memorySessionId,
        project,
        title,
        narrative,
        platform_source: platformSource ?? 'swe-squad',
        content_hash: contentHash,
        type: inferObservationType(tool_name),
        files_read: extractPaths(req.body as Record<string, unknown>, 'read'),
        files_modified: extractPaths(req.body as Record<string, unknown>, 'write'),
      });

      if (result === null) {
        // Dedup hit
        res.status(409).json({ ok: true, note: 'duplicate observation within 30s' });
      } else {
        res.json(result);
        // Update the stored observation with the embedding once ready
        embeddingPromise.then(async (emb) => {
          if (emb && result.id) {
            // Best-effort: update the embedding column via PATCH
            await patchEmbedding(SUPABASE_URL, SUPABASE_KEY, result.id, emb).catch(() => null);
          }
        });
      }
    } catch (err) {
      console.error('[memory/sessions/observations]', err);
      // Still 202 — fire-and-forget; don't crash the agent
      res.status(202).json({ ok: true, note: 'storage error, observation may be dropped' });
    }
  });

  // -------------------------------------------------------------------------
  // POST /api/sessions/summarize
  //
  // Body: { contentSessionId, last_assistant_message, teamId? }
  // -------------------------------------------------------------------------

  app.post('/api/sessions/summarize', async (req: Request, res: Response) => {
    const teamId = req.teamId;
    const { contentSessionId, last_assistant_message } = req.body as {
      contentSessionId?: string;
      last_assistant_message?: string;
    };

    if (!contentSessionId) {
      res.status(400).json({ error: 'contentSessionId is required' });
      return;
    }

    try {
      const session = await storage.getSessionByContentId(teamId, contentSessionId);
      if (!session) {
        res.status(202).json({ ok: true, note: 'session not found' });
        return;
      }

      await storage.storeSummary({
        team_id: teamId,
        memory_session_id: session.memory_session_id,
        project: session.project,
        learned: last_assistant_message?.slice(0, 4000),
      });

      res.json({ ok: true });
    } catch (err) {
      console.error('[memory/sessions/summarize]', err);
      res.status(202).json({ ok: true, note: 'summary storage failed' });
    }
  });

  // -------------------------------------------------------------------------
  // GET /api/search?q=...&project=...&teamId=...&limit=...&type=...
  //
  // Returns: { observations: [...], total: N }
  // -------------------------------------------------------------------------

  app.get('/api/search', async (req: Request, res: Response) => {
    const teamId = req.teamId;
    const q = (req.query['q'] as string | undefined) ?? '';
    const project = req.query['project'] as string | undefined;
    const type = req.query['type'] as string | undefined;
    const limit = Math.min(parseInt((req.query['limit'] as string) ?? '20', 10), 100);

    if (!q) {
      res.json({ observations: [], total: 0 });
      return;
    }

    try {
      // Run FTS and semantic search in parallel
      const [ftsResults, semanticResults] = await Promise.all([
        storage.searchObservations(teamId, q, project, type, limit),
        (async () => {
          const emb = await embeddings.embed(q);
          if (!emb) return [];
          return storage.semanticSearch(teamId, emb, project, limit, 0.65);
        })(),
      ]);

      // Merge by id, keeping the higher similarity score
      const seen = new Map<number, (typeof ftsResults)[0]>();
      for (const r of ftsResults) seen.set(r.id, r);
      for (const r of semanticResults) {
        const existing = seen.get(r.id);
        if (!existing || (r.similarity ?? 0) > (existing.similarity ?? 0)) {
          seen.set(r.id, r);
        }
      }

      const merged = [...seen.values()]
        .sort((a, b) => (b.similarity ?? 0) - (a.similarity ?? 0))
        .slice(0, limit);

      res.json({ observations: merged, total: merged.length });
    } catch (err) {
      console.error('[memory/search]', err);
      res.json({ observations: [], total: 0 });
    }
  });

  // -------------------------------------------------------------------------
  // GET /api/context/inject?project=...&teamId=...&platformSource=...&full=...
  //
  // Returns: plain text context string
  // -------------------------------------------------------------------------

  app.get('/api/context/inject', async (req: Request, res: Response) => {
    const teamId = req.teamId;
    const project = (req.query['project'] as string | undefined) ?? '';
    const limit = req.query['full'] === 'true' ? 50 : 20;

    if (!project) {
      res.type('text/plain').send('');
      return;
    }

    try {
      const recent = await storage.getRecentObservations(teamId, project, limit);
      const context = formatTimelineContext(recent);
      res.type('text/plain').send(context);
    } catch (err) {
      console.error('[memory/context/inject]', err);
      res.type('text/plain').send('');
    }
  });

  // -------------------------------------------------------------------------
  // POST /api/context/semantic
  //
  // Body: { query, project, teamId?, limit? }
  // Returns: { context: "..." }
  // -------------------------------------------------------------------------

  app.post('/api/context/semantic', async (req: Request, res: Response) => {
    const teamId = req.teamId;
    const { query, project, limit: rawLimit } = req.body as {
      query?: string;
      project?: string;
      limit?: number;
    };

    if (!query || !project) {
      res.json({ context: '' });
      return;
    }

    const limit = Math.min(rawLimit ?? 10, 50);

    try {
      const emb = await embeddings.embed(query);
      if (!emb) {
        res.json({ context: '' });
        return;
      }

      const results = await storage.semanticSearch(teamId, emb, project, limit, 0.70);
      const context = formatObservationsAsContext(results, {
        title: '## Relevant Past Work',
        maxObservations: limit,
      });

      res.json({ context });
    } catch (err) {
      console.error('[memory/context/semantic]', err);
      res.json({ context: '' });
    }
  });

  return app;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Infer an observation type from the Claude Code tool name. */
function inferObservationType(toolName?: string): string | undefined {
  if (!toolName) return undefined;
  const t = toolName.toLowerCase();
  if (t === 'investigation_complete') return 'discovery';
  if (t.includes('fix') || t.includes('patch') || t.includes('repair')) return 'bugfix';
  if (t.includes('feature') || t.includes('implement')) return 'feature';
  if (t === 'bash' || t === 'edit' || t === 'write') return 'change';
  return 'discovery';
}

/** Extract file paths from tool_input for read/write tracking. */
function extractPaths(body: Record<string, unknown>, mode: 'read' | 'write'): string | undefined {
  const input = body['tool_input'];
  if (!input || typeof input !== 'object') return undefined;
  const obj = input as Record<string, unknown>;
  const path = obj['file_path'] ?? obj['path'];
  if (!path || typeof path !== 'string') return undefined;
  // Heuristic: Edit/Write are writes, Read is read
  const toolName = (body['tool_name'] as string | undefined)?.toLowerCase() ?? '';
  if (mode === 'write' && (toolName === 'edit' || toolName === 'write')) return path;
  if (mode === 'read' && toolName === 'read') return path;
  return undefined;
}

/** Best-effort PATCH to update the embedding column after INSERT. */
async function patchEmbedding(
  supabaseUrl: string,
  supabaseKey: string,
  id: number,
  embedding: number[],
): Promise<void> {
  if (!supabaseUrl || !supabaseKey) return;
  await fetch(`${supabaseUrl}/rest/v1/memory_observations?id=eq.${id}`, {
    method: 'PATCH',
    headers: {
      apikey: supabaseKey,
      Authorization: `Bearer ${supabaseKey}`,
      'Content-Type': 'application/json',
      Prefer: 'return=minimal',
    },
    body: JSON.stringify({ embedding }),
  });
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

// Only start the server when this module is run directly (not during tests)
const isMain =
  process.argv[1] != null &&
  new URL(import.meta.url).pathname === new URL(process.argv[1], import.meta.url).pathname;

if (isMain) {
  if (!SUPABASE_URL || !SUPABASE_KEY) {
    console.error('[memory] SUPABASE_URL and SUPABASE_KEY must be set');
    process.exit(1);
  }

  const app = createApp();
  app.listen(PORT, HOST, () => {
    console.info(`[memory] Worker started — http://${HOST}:${PORT}`);
    console.info(`[memory] Team ID default: ${process.env.SWE_TEAM_ID ?? 'default'}`);
    console.info(`[memory] Auth: ${process.env.MEMORY_API_KEY ? 'enabled' : 'open (no API key)'}`);
  });
}

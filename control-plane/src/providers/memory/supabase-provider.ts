/**
 * Supabase pgvector MemoryProvider implementation.
 *
 * Uses the existing SupabaseClient (PostgREST) for CRUD and the
 * `match_swarm_memory` RPC for vector similarity search.
 *
 * Tenant isolation is enforced at two levels:
 *   1. PostgREST RLS policies (database-level, using app.tenant_id setting)
 *   2. Application-level tenant_id filtering on every query (defense in depth)
 *
 * Table: swarm_memory (see scripts/ops/migrations/008_memory_service.sql)
 */

import type { SupabaseClient } from "../supabase/client.js";
import { vectorLiteral } from "../supabase/embeddings.js";
import type {
  MemoryEntry,
  MemoryQuery,
  MemoryACL,
  MemoryProvider,
  MemoryEntryType,
} from "./base.js";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface SupabaseMemoryConfig {
  /** Supabase PostgREST client. */
  client: SupabaseClient;
  /** Default timeout for RPC calls (ms). */
  timeout?: number;
}

// ---------------------------------------------------------------------------
// Row <-> MemoryEntry mapping
// ---------------------------------------------------------------------------

interface SwarmMemoryRow {
  id: string;
  tenant_id: string;
  project_id: string;
  agent_id: string | null;
  engine: string | null;
  type: string;
  content: string;
  embedding: string | null;
  tags: string[];
  confidence: number;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

function rowToEntry(row: SwarmMemoryRow): MemoryEntry {
  return {
    id: row.id,
    tenantId: row.tenant_id,
    projectId: row.project_id,
    agentId: row.agent_id ?? undefined,
    engine: row.engine ?? undefined,
    type: row.type as MemoryEntryType,
    content: row.content,
    embedding: row.embedding ? parseVector(row.embedding) : undefined,
    tags: row.tags ?? [],
    confidence: Number(row.confidence),
    expiresAt: row.expires_at ?? undefined,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

/**
 * Parse a pgvector text literal "[0.1,0.2,...]" back into a number array.
 */
function parseVector(v: string): number[] {
  if (!v || v === "null") return [];
  const inner = v.replace(/^\[/, "").replace(/\]$/, "");
  if (!inner) return [];
  return inner.split(",").map(Number);
}

// ---------------------------------------------------------------------------
// SupabaseMemoryProvider
// ---------------------------------------------------------------------------

const TABLE = "swarm_memory";
const ACL_TABLE = "swarm_memory_acl";

export class SupabaseMemoryProvider implements MemoryProvider {
  private readonly client: SupabaseClient;

  constructor(config: SupabaseMemoryConfig) {
    this.client = config.client;
  }

  // -----------------------------------------------------------------------
  // store
  // -----------------------------------------------------------------------

  async store(
    entry: Omit<MemoryEntry, "id" | "createdAt" | "updatedAt">,
  ): Promise<MemoryEntry> {
    const data: Record<string, unknown> = {
      tenant_id: entry.tenantId,
      project_id: entry.projectId,
      agent_id: entry.agentId ?? null,
      engine: entry.engine ?? null,
      type: entry.type,
      content: entry.content,
      tags: entry.tags,
      confidence: entry.confidence,
      expires_at: entry.expiresAt ?? null,
    };

    if (entry.embedding && entry.embedding.length > 0) {
      data["embedding"] = vectorLiteral(entry.embedding);
    }

    const rows = (await this.client.insert(TABLE, data)) as unknown as SwarmMemoryRow[];
    if (!rows || rows.length === 0) {
      throw new Error("Supabase insert returned no rows");
    }

    return rowToEntry(rows[0]);
  }

  // -----------------------------------------------------------------------
  // query
  // -----------------------------------------------------------------------

  async query(query: MemoryQuery): Promise<MemoryEntry[]> {
    // If the caller provided an embedding, use the RPC for vector search.
    if (query.embedding && query.embedding.length > 0) {
      return this.vectorQuery(query);
    }

    // Otherwise, use PostgREST filtering.
    return this.filterQuery(query);
  }

  private async vectorQuery(query: MemoryQuery): Promise<MemoryEntry[]> {
    const limit = query.limit ?? 10;
    const threshold = query.similarityThreshold ?? 0.75;
    const maxAgeDays = query.maxAgeDays ?? 180;

    const params: Record<string, unknown> = {
      query_embedding: vectorLiteral(query.embedding!),
      query_tenant_id: query.tenantId,
      match_count: limit,
      similarity_floor: threshold,
      max_age_days: maxAgeDays,
    };

    if (query.projectId) {
      params["query_project_id"] = query.projectId;
    }

    const rows = await this.client.rpc<SwarmMemoryRow[]>(
      "match_swarm_memory",
      params,
    );

    if (!Array.isArray(rows)) return [];

    let entries = rows.map(rowToEntry);

    // Apply additional filters not handled by the RPC.
    if (query.types && query.types.length > 0) {
      entries = entries.filter((e) => query.types!.includes(e.type));
    }
    if (query.tags && query.tags.length > 0) {
      entries = entries.filter((e) =>
        query.tags!.every((tag) => e.tags.includes(tag)),
      );
    }

    return entries;
  }

  private async filterQuery(query: MemoryQuery): Promise<MemoryEntry[]> {
    const limit = query.limit ?? 10;
    const maxAgeDays = query.maxAgeDays ?? 180;

    const params: Record<string, string> = {
      tenant_id: `eq.${query.tenantId}`,
      order: "updated_at.desc",
      limit: String(limit),
    };

    if (query.projectId) {
      params["project_id"] = `eq.${query.projectId}`;
    }

    if (query.types && query.types.length > 0) {
      params["type"] = `in.(${query.types.join(",")})`;
    }

    if (query.tags && query.tags.length > 0) {
      params["tags"] = `cs.{${query.tags.join(",")}}`;
    }

    // Age filter: created_at must be within maxAgeDays.
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - maxAgeDays);
    params["created_at"] = `gte.${cutoff.toISOString()}`;

    const rows = (await this.client.query<SwarmMemoryRow>(TABLE, params));

    return rows.map(rowToEntry);
  }

  // -----------------------------------------------------------------------
  // get
  // -----------------------------------------------------------------------

  async get(id: string, tenantId: string): Promise<MemoryEntry | null> {
    const rows = await this.client.query<SwarmMemoryRow>(TABLE, {
      id: `eq.${id}`,
      tenant_id: `eq.${tenantId}`,
    });

    if (!rows || rows.length === 0) return null;
    return rowToEntry(rows[0]);
  }

  // -----------------------------------------------------------------------
  // update
  // -----------------------------------------------------------------------

  async update(
    id: string,
    tenantId: string,
    updates: Partial<
      Pick<MemoryEntry, "content" | "confidence" | "tags" | "embedding">
    >,
  ): Promise<MemoryEntry | null> {
    const data: Record<string, unknown> = {
      updated_at: new Date().toISOString(),
    };

    if (updates.content !== undefined) data["content"] = updates.content;
    if (updates.confidence !== undefined)
      data["confidence"] = updates.confidence;
    if (updates.tags !== undefined) data["tags"] = updates.tags;
    if (updates.embedding !== undefined) {
      data["embedding"] = vectorLiteral(updates.embedding);
    }

    const rows = (await this.client.update(
      TABLE,
      { id: `eq.${id}`, tenant_id: `eq.${tenantId}` },
      data,
    )) as unknown as SwarmMemoryRow[];

    if (!rows || rows.length === 0) return null;
    return rowToEntry(rows[0]);
  }

  // -----------------------------------------------------------------------
  // delete
  // -----------------------------------------------------------------------

  async delete(id: string, tenantId: string): Promise<boolean> {
    // Check existence first (PostgREST DELETE does not return rows by default).
    const existing = await this.get(id, tenantId);
    if (!existing) return false;

    await this.client.delete(TABLE, {
      id: `eq.${id}`,
      tenant_id: `eq.${tenantId}`,
    });

    return true;
  }

  // -----------------------------------------------------------------------
  // checkAccess
  // -----------------------------------------------------------------------

  async checkAccess(acl: MemoryACL): Promise<boolean> {
    const rows = await this.client.query<{
      can_read: boolean;
      can_write: boolean;
    }>(ACL_TABLE, {
      tenant_id: `eq.${acl.tenantId}`,
      project_id: `eq.${acl.projectId}`,
      agent_id: `eq.${acl.agentId}`,
    });

    // No explicit ACL = allow (permissive default, matching Python behavior).
    if (!rows || rows.length === 0) return true;

    const rule = rows[0];
    if (acl.canRead && !rule.can_read) return false;
    if (acl.canWrite && !rule.can_write) return false;
    return true;
  }

  // -----------------------------------------------------------------------
  // findDuplicate
  // -----------------------------------------------------------------------

  async findDuplicate(
    tenantId: string,
    projectId: string,
    embedding: number[],
    threshold = 0.92,
  ): Promise<MemoryEntry | null> {
    const results = await this.query({
      tenantId,
      projectId,
      embedding,
      similarityThreshold: threshold,
      limit: 1,
    });

    return results.length > 0 ? results[0] : null;
  }

  // -----------------------------------------------------------------------
  // pruneExpired
  // -----------------------------------------------------------------------

  async pruneExpired(tenantId: string): Promise<number> {
    // Use RPC for atomic count + delete (avoids race conditions).
    const result = await this.client.rpc<{ deleted_count: number }>(
      "prune_expired_swarm_memory",
      { query_tenant_id: tenantId },
    );

    // If the RPC is not available, fall back to manual query + delete.
    if (result && typeof result === "object" && "deleted_count" in result) {
      return (result as { deleted_count: number }).deleted_count;
    }

    // Fallback: query expired entries and delete them one by one.
    const now = new Date().toISOString();
    const expired = await this.client.query<SwarmMemoryRow>(TABLE, {
      tenant_id: `eq.${tenantId}`,
      expires_at: `lt.${now}`,
    });

    let count = 0;
    for (const row of expired) {
      await this.client.delete(TABLE, {
        id: `eq.${row.id}`,
        tenant_id: `eq.${tenantId}`,
      });
      count++;
    }

    return count;
  }

  // -----------------------------------------------------------------------
  // exportProject
  // -----------------------------------------------------------------------

  async exportProject(
    tenantId: string,
    projectId: string,
  ): Promise<MemoryEntry[]> {
    const rows = await this.client.query<SwarmMemoryRow>(TABLE, {
      tenant_id: `eq.${tenantId}`,
      project_id: `eq.${projectId}`,
      order: "created_at.asc",
    });

    return rows.map(rowToEntry);
  }
}

/**
 * MemoryService -- high-level multi-tenant memory layer.
 *
 * Wraps a MemoryProvider with:
 *   - Tenant isolation enforcement (defense-in-depth, even if provider has RLS)
 *   - Semantic deduplication (cosine > 0.92 = merge instead of duplicate)
 *   - Confidence tracking (increment on reuse)
 *   - TTL enforcement
 *   - ACL checks before read/write
 *
 * All public methods require tenantId. There are zero code paths that can
 * skip the tenant filter.
 */

import type {
  MemoryEntry,
  MemoryQuery,
  MemoryACL,
  MemoryProvider,
  MemoryEntryType,
} from "../providers/memory/base.js";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface MemoryServiceConfig {
  /** The underlying storage provider. */
  provider: MemoryProvider;
  /** Cosine similarity threshold for dedup (default 0.92). */
  dedupThreshold?: number;
  /** Default TTL in days for new entries (default 180). */
  ttlDays?: number;
  /** Maximum entries per project (default 10000). */
  maxEntriesPerProject?: number;
  /** Confidence increment on each successful reuse (default 0.1). */
  confidenceIncrement?: number;
  /** Maximum confidence value (default 2.0). */
  maxConfidence?: number;
}

// ---------------------------------------------------------------------------
// Store result
// ---------------------------------------------------------------------------

export type StoreResult =
  | { action: "stored"; entry: MemoryEntry }
  | { action: "merged"; entry: MemoryEntry }
  | { action: "skipped"; existing: MemoryEntry };

// ---------------------------------------------------------------------------
// MemoryService
// ---------------------------------------------------------------------------

export class MemoryService {
  private readonly provider: MemoryProvider;
  private readonly dedupThreshold: number;
  private readonly ttlDays: number;
  private readonly maxEntriesPerProject: number;
  private readonly confidenceIncrement: number;
  private readonly maxConfidence: number;

  constructor(config: MemoryServiceConfig) {
    this.provider = config.provider;
    this.dedupThreshold = config.dedupThreshold ?? 0.92;
    this.ttlDays = config.ttlDays ?? 180;
    this.maxEntriesPerProject = config.maxEntriesPerProject ?? 10_000;
    this.confidenceIncrement = config.confidenceIncrement ?? 0.1;
    this.maxConfidence = config.maxConfidence ?? 2.0;
  }

  // -----------------------------------------------------------------------
  // store -- dedup-aware entry creation
  // -----------------------------------------------------------------------

  /**
   * Store a memory entry with semantic deduplication.
   *
   * 1. Check ACL (canWrite).
   * 2. If embedding provided, check for near-duplicates.
   * 3. If duplicate found above threshold: merge (update content, bump confidence).
   * 4. Otherwise: store new entry.
   *
   * Returns the action taken and the resulting entry.
   */
  async store(
    tenantId: string,
    projectId: string,
    input: {
      agentId?: string;
      engine?: string;
      type: MemoryEntryType;
      content: string;
      embedding?: number[];
      tags?: string[];
      confidence?: number;
      expiresAt?: string;
    },
  ): Promise<StoreResult> {
    // ACL check
    if (input.agentId) {
      const allowed = await this.provider.checkAccess({
        tenantId,
        projectId,
        agentId: input.agentId,
        canRead: false,
        canWrite: true,
      });
      if (!allowed) {
        throw new MemoryAccessError(
          `Agent "${input.agentId}" does not have write access to project "${projectId}" in tenant "${tenantId}"`,
        );
      }
    }

    // Dedup check
    if (input.embedding && input.embedding.length > 0) {
      const duplicate = await this.provider.findDuplicate(
        tenantId,
        projectId,
        input.embedding,
        this.dedupThreshold,
      );

      if (duplicate) {
        // Merge: update the existing entry with richer content.
        const newConfidence = Math.min(
          duplicate.confidence + this.confidenceIncrement,
          this.maxConfidence,
        );

        // Keep the longer/richer content.
        const mergedContent =
          input.content.length > duplicate.content.length
            ? input.content
            : duplicate.content;

        // Merge tags.
        const mergedTags = [
          ...new Set([...duplicate.tags, ...(input.tags ?? [])]),
        ];

        const updated = await this.provider.update(
          duplicate.id,
          tenantId,
          {
            content: mergedContent,
            confidence: newConfidence,
            tags: mergedTags,
            embedding: input.embedding,
          },
        );

        if (updated) {
          return { action: "merged", entry: updated };
        }

        // If update failed (race condition), fall through to store.
      }
    }

    // Compute expiry if not provided.
    const expiresAt =
      input.expiresAt ?? this.computeExpiry(this.ttlDays);

    const entry = await this.provider.store({
      tenantId,
      projectId,
      agentId: input.agentId,
      engine: input.engine,
      type: input.type,
      content: input.content,
      embedding: input.embedding,
      tags: input.tags ?? [],
      confidence: input.confidence ?? 1.0,
      expiresAt,
    });

    return { action: "stored", entry };
  }

  // -----------------------------------------------------------------------
  // query -- tenant-scoped search
  // -----------------------------------------------------------------------

  /**
   * Query memory entries. Always scoped to tenantId.
   * Optionally checks read ACL if agentId is provided.
   */
  async query(
    query: MemoryQuery & { agentId?: string },
  ): Promise<MemoryEntry[]> {
    // ACL check
    if (query.agentId && query.projectId) {
      const allowed = await this.provider.checkAccess({
        tenantId: query.tenantId,
        projectId: query.projectId,
        agentId: query.agentId,
        canRead: true,
        canWrite: false,
      });
      if (!allowed) {
        throw new MemoryAccessError(
          `Agent "${query.agentId}" does not have read access to project "${query.projectId}" in tenant "${query.tenantId}"`,
        );
      }
    }

    return this.provider.query(query);
  }

  // -----------------------------------------------------------------------
  // get -- tenant-scoped single entry
  // -----------------------------------------------------------------------

  async get(id: string, tenantId: string): Promise<MemoryEntry | null> {
    return this.provider.get(id, tenantId);
  }

  // -----------------------------------------------------------------------
  // recordHit -- increment confidence on successful reuse
  // -----------------------------------------------------------------------

  /**
   * Record that a memory entry was successfully used (hit).
   * Increments confidence by confidenceIncrement, capped at maxConfidence.
   */
  async recordHit(
    id: string,
    tenantId: string,
  ): Promise<MemoryEntry | null> {
    const entry = await this.provider.get(id, tenantId);
    if (!entry) return null;

    const newConfidence = Math.min(
      entry.confidence + this.confidenceIncrement,
      this.maxConfidence,
    );

    return this.provider.update(id, tenantId, {
      confidence: newConfidence,
    });
  }

  // -----------------------------------------------------------------------
  // delete -- tenant-scoped deletion
  // -----------------------------------------------------------------------

  async delete(id: string, tenantId: string): Promise<boolean> {
    return this.provider.delete(id, tenantId);
  }

  // -----------------------------------------------------------------------
  // pruneExpired -- clean up expired entries
  // -----------------------------------------------------------------------

  async pruneExpired(tenantId: string): Promise<number> {
    return this.provider.pruneExpired(tenantId);
  }

  // -----------------------------------------------------------------------
  // exportProject -- full project export
  // -----------------------------------------------------------------------

  async exportProject(
    tenantId: string,
    projectId: string,
  ): Promise<MemoryEntry[]> {
    return this.provider.exportProject(tenantId, projectId);
  }

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------

  private computeExpiry(days: number): string {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return d.toISOString();
  }
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export class MemoryAccessError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MemoryAccessError";
  }
}

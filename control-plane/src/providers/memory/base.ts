/**
 * MemoryProvider interface -- pluggable multi-tenant memory backend.
 *
 * Supports tenant isolation, per-project scoping, cross-agent sharing,
 * and engine-agnostic storage. All queries MUST include tenantId.
 *
 * Default implementations:
 *   - InMemoryMemoryProvider  (testing / development)
 *   - SupabaseMemoryProvider  (production, pgvector)
 *
 * Swappable alternatives: Qdrant, Chroma, Weaviate, Pinecone, etc.
 */

// ---------------------------------------------------------------------------
// MemoryEntry
// ---------------------------------------------------------------------------

export type MemoryEntryType =
  | "investigation"
  | "fix_pattern"
  | "root_cause"
  | "knowledge"
  | "config";

export interface MemoryEntry {
  /** Unique entry identifier. */
  id: string;
  /** Organization / team -- hard isolation boundary. */
  tenantId: string;
  /** Project-level scoping within a tenant. */
  projectId: string;
  /** Agent that created this entry (optional). */
  agentId?: string;
  /** CodingEngine that produced this entry (optional). */
  engine?: string;
  /** Content category. */
  type: MemoryEntryType;
  /** The actual memory content (investigation report, fix pattern, etc.). */
  content: string;
  /** Vector embedding for similarity search. */
  embedding?: number[];
  /** Searchable tags. */
  tags: string[];
  /** Confidence score: 0-2.0, incremented on successful reuse. */
  confidence: number;
  /** ISO-8601 expiration timestamp (optional TTL). */
  expiresAt?: string;
  /** ISO-8601 creation timestamp. */
  createdAt: string;
  /** ISO-8601 last-update timestamp. */
  updatedAt: string;
}

// ---------------------------------------------------------------------------
// MemoryQuery
// ---------------------------------------------------------------------------

export interface MemoryQuery {
  /** Required: tenant isolation boundary. */
  tenantId: string;
  /** If omitted, searches all accessible projects within the tenant. */
  projectId?: string;
  /** Filter by entry type(s). */
  types?: MemoryEntryType[];
  /** Filter by tags (entries must contain ALL specified tags). */
  tags?: string[];
  /** Vector embedding for similarity search. */
  embedding?: number[];
  /** Minimum cosine similarity threshold (default 0.75). */
  similarityThreshold?: number;
  /** Maximum results to return (default 10). */
  limit?: number;
  /** Maximum entry age in days (default 180). */
  maxAgeDays?: number;
}

// ---------------------------------------------------------------------------
// MemoryACL
// ---------------------------------------------------------------------------

export interface MemoryACL {
  tenantId: string;
  projectId: string;
  agentId: string;
  canRead: boolean;
  canWrite: boolean;
}

// ---------------------------------------------------------------------------
// MemoryProvider interface
// ---------------------------------------------------------------------------

export interface MemoryProvider {
  /**
   * Store a new memory entry. Returns the created entry with generated id
   * and timestamps.
   */
  store(
    entry: Omit<MemoryEntry, "id" | "createdAt" | "updatedAt">,
  ): Promise<MemoryEntry>;

  /**
   * Query memory entries. All results are scoped to the given tenantId.
   * When embedding is provided, results are sorted by descending similarity.
   */
  query(query: MemoryQuery): Promise<MemoryEntry[]>;

  /**
   * Get a single entry by id. Returns null if not found or if tenantId
   * does not match (no cross-tenant reads).
   */
  get(id: string, tenantId: string): Promise<MemoryEntry | null>;

  /**
   * Partial update of an existing entry. Returns the updated entry, or null
   * if not found / tenant mismatch.
   */
  update(
    id: string,
    tenantId: string,
    updates: Partial<
      Pick<MemoryEntry, "content" | "confidence" | "tags" | "embedding">
    >,
  ): Promise<MemoryEntry | null>;

  /**
   * Delete an entry. Returns true if deleted, false if not found / tenant
   * mismatch.
   */
  delete(id: string, tenantId: string): Promise<boolean>;

  /**
   * Check whether an agent has the requested access level for a project.
   */
  checkAccess(acl: MemoryACL): Promise<boolean>;

  /**
   * Find a near-duplicate entry before storing (semantic dedup).
   * Returns the closest match above the threshold, or null.
   */
  findDuplicate(
    tenantId: string,
    projectId: string,
    embedding: number[],
    threshold?: number,
  ): Promise<MemoryEntry | null>;

  /**
   * Prune expired entries for a tenant. Returns the count of deleted entries.
   */
  pruneExpired(tenantId: string): Promise<number>;

  /**
   * Export all entries for a project (for migration / backup).
   */
  exportProject(
    tenantId: string,
    projectId: string,
  ): Promise<MemoryEntry[]>;
}

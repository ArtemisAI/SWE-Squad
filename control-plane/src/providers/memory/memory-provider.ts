/**
 * In-memory MemoryProvider implementation.
 *
 * Thread-safe (single-threaded JS), uses cosine similarity for vector search.
 * Suitable for testing and local development. Not for production use.
 */

import type {
  MemoryEntry,
  MemoryQuery,
  MemoryACL,
  MemoryProvider,
} from "./base.js";

// ---------------------------------------------------------------------------
// Cosine similarity
// ---------------------------------------------------------------------------

/**
 * Compute cosine similarity between two vectors.
 * Returns 0 if either vector is zero-length or they have different dimensions.
 */
export function cosineSimilarity(a: number[], b: number[]): number {
  if (a.length !== b.length || a.length === 0) return 0;

  let dotProduct = 0;
  let normA = 0;
  let normB = 0;

  for (let i = 0; i < a.length; i++) {
    dotProduct += a[i] * b[i];
    normA += a[i] * a[i];
    normB += b[i] * b[i];
  }

  const denominator = Math.sqrt(normA) * Math.sqrt(normB);
  if (denominator === 0) return 0;

  return dotProduct / denominator;
}

// ---------------------------------------------------------------------------
// UUID generation (no external deps)
// ---------------------------------------------------------------------------

function generateId(): string {
  return crypto.randomUUID();
}

// ---------------------------------------------------------------------------
// InMemoryMemoryProvider
// ---------------------------------------------------------------------------

export class InMemoryMemoryProvider implements MemoryProvider {
  /** All entries keyed by id. */
  private readonly entries = new Map<string, MemoryEntry>();

  /** ACL rules keyed by "tenantId:projectId:agentId". */
  private readonly acls = new Map<string, MemoryACL>();

  // -----------------------------------------------------------------------
  // store
  // -----------------------------------------------------------------------

  async store(
    entry: Omit<MemoryEntry, "id" | "createdAt" | "updatedAt">,
  ): Promise<MemoryEntry> {
    const now = new Date().toISOString();
    const full: MemoryEntry = {
      ...entry,
      id: generateId(),
      createdAt: now,
      updatedAt: now,
    };
    this.entries.set(full.id, full);
    return full;
  }

  // -----------------------------------------------------------------------
  // query
  // -----------------------------------------------------------------------

  async query(query: MemoryQuery): Promise<MemoryEntry[]> {
    const now = new Date();
    const maxAgeDays = query.maxAgeDays ?? 180;
    const limit = query.limit ?? 10;
    const threshold = query.similarityThreshold ?? 0.75;

    let results: Array<{ entry: MemoryEntry; similarity: number }> = [];

    for (const entry of this.entries.values()) {
      // --- Tenant isolation (mandatory) ---
      if (entry.tenantId !== query.tenantId) continue;

      // --- Project filter ---
      if (query.projectId && entry.projectId !== query.projectId) continue;

      // --- Type filter ---
      if (query.types && query.types.length > 0) {
        if (!query.types.includes(entry.type)) continue;
      }

      // --- Tag filter (must contain ALL requested tags) ---
      if (query.tags && query.tags.length > 0) {
        const hasAll = query.tags.every((tag) => entry.tags.includes(tag));
        if (!hasAll) continue;
      }

      // --- Age filter ---
      const createdAt = new Date(entry.createdAt);
      const ageDays =
        (now.getTime() - createdAt.getTime()) / (1000 * 60 * 60 * 24);
      if (ageDays > maxAgeDays) continue;

      // --- Expiry filter ---
      if (entry.expiresAt && new Date(entry.expiresAt) < now) continue;

      // --- Similarity filter ---
      let similarity = 1.0;
      if (query.embedding && query.embedding.length > 0) {
        if (!entry.embedding || entry.embedding.length === 0) continue;
        similarity = cosineSimilarity(query.embedding, entry.embedding);
        if (similarity < threshold) continue;
      }

      results.push({ entry, similarity });
    }

    // Sort by similarity descending (vector queries), then by updatedAt descending.
    results.sort((a, b) => {
      if (a.similarity !== b.similarity) return b.similarity - a.similarity;
      return (
        new Date(b.entry.updatedAt).getTime() -
        new Date(a.entry.updatedAt).getTime()
      );
    });

    return results.slice(0, limit).map((r) => r.entry);
  }

  // -----------------------------------------------------------------------
  // get
  // -----------------------------------------------------------------------

  async get(id: string, tenantId: string): Promise<MemoryEntry | null> {
    const entry = this.entries.get(id);
    if (!entry || entry.tenantId !== tenantId) return null;
    return entry;
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
    const entry = this.entries.get(id);
    if (!entry || entry.tenantId !== tenantId) return null;

    const updated: MemoryEntry = {
      ...entry,
      ...updates,
      updatedAt: new Date().toISOString(),
    };
    this.entries.set(id, updated);
    return updated;
  }

  // -----------------------------------------------------------------------
  // delete
  // -----------------------------------------------------------------------

  async delete(id: string, tenantId: string): Promise<boolean> {
    const entry = this.entries.get(id);
    if (!entry || entry.tenantId !== tenantId) return false;
    return this.entries.delete(id);
  }

  // -----------------------------------------------------------------------
  // checkAccess
  // -----------------------------------------------------------------------

  async checkAccess(acl: MemoryACL): Promise<boolean> {
    const key = `${acl.tenantId}:${acl.projectId}:${acl.agentId}`;
    const rule = this.acls.get(key);

    // No explicit rule = allow (permissive default for dev/testing).
    if (!rule) return true;

    if (acl.canRead && !rule.canRead) return false;
    if (acl.canWrite && !rule.canWrite) return false;
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
    let bestMatch: MemoryEntry | null = null;
    let bestSim = 0;

    for (const entry of this.entries.values()) {
      if (entry.tenantId !== tenantId) continue;
      if (entry.projectId !== projectId) continue;
      if (!entry.embedding || entry.embedding.length === 0) continue;

      const sim = cosineSimilarity(embedding, entry.embedding);
      if (sim >= threshold && sim > bestSim) {
        bestSim = sim;
        bestMatch = entry;
      }
    }

    return bestMatch;
  }

  // -----------------------------------------------------------------------
  // pruneExpired
  // -----------------------------------------------------------------------

  async pruneExpired(tenantId: string): Promise<number> {
    const now = new Date();
    let pruned = 0;

    for (const [id, entry] of this.entries.entries()) {
      if (entry.tenantId !== tenantId) continue;
      if (entry.expiresAt && new Date(entry.expiresAt) < now) {
        this.entries.delete(id);
        pruned++;
      }
    }

    return pruned;
  }

  // -----------------------------------------------------------------------
  // exportProject
  // -----------------------------------------------------------------------

  async exportProject(
    tenantId: string,
    projectId: string,
  ): Promise<MemoryEntry[]> {
    const results: MemoryEntry[] = [];
    for (const entry of this.entries.values()) {
      if (entry.tenantId === tenantId && entry.projectId === projectId) {
        results.push(entry);
      }
    }
    return results;
  }

  // -----------------------------------------------------------------------
  // Test helpers
  // -----------------------------------------------------------------------

  /**
   * Set an explicit ACL rule. For testing only.
   */
  setACL(acl: MemoryACL): void {
    const key = `${acl.tenantId}:${acl.projectId}:${acl.agentId}`;
    this.acls.set(key, acl);
  }

  /**
   * Clear all entries and ACLs. For testing only.
   */
  clear(): void {
    this.entries.clear();
    this.acls.clear();
  }

  /**
   * Return total entry count. For testing only.
   */
  size(): number {
    return this.entries.size;
  }
}

/**
 * Unit tests for InMemoryMemoryProvider.
 *
 * Covers: CRUD, tenant isolation, cosine similarity, dedup, ACL, pruning.
 * No network access or external services required.
 */

import { describe, it, expect, beforeEach } from "vitest";
import {
  InMemoryMemoryProvider,
  cosineSimilarity,
} from "../../../src/providers/memory/memory-provider.js";
import type { MemoryEntry } from "../../../src/providers/memory/base.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Create a simple unit-length embedding for testing. */
function makeEmbedding(seed: number, dims = 4): number[] {
  const vec = Array.from({ length: dims }, (_, i) => Math.sin(seed + i));
  const norm = Math.sqrt(vec.reduce((s, v) => s + v * v, 0));
  return norm > 0 ? vec.map((v) => v / norm) : vec;
}

// ---------------------------------------------------------------------------
// cosineSimilarity
// ---------------------------------------------------------------------------

describe("cosineSimilarity", () => {
  it("returns 1.0 for identical vectors", () => {
    const v = [1, 0, 0, 0];
    expect(cosineSimilarity(v, v)).toBeCloseTo(1.0, 5);
  });

  it("returns 0 for orthogonal vectors", () => {
    expect(cosineSimilarity([1, 0], [0, 1])).toBeCloseTo(0, 5);
  });

  it("returns -1 for opposite vectors", () => {
    expect(cosineSimilarity([1, 0], [-1, 0])).toBeCloseTo(-1.0, 5);
  });

  it("returns 0 for mismatched dimensions", () => {
    expect(cosineSimilarity([1, 0], [1, 0, 0])).toBe(0);
  });

  it("returns 0 for empty vectors", () => {
    expect(cosineSimilarity([], [])).toBe(0);
  });

  it("returns 0 for zero vectors", () => {
    expect(cosineSimilarity([0, 0, 0], [1, 2, 3])).toBe(0);
  });

  it("computes correct similarity for non-trivial vectors", () => {
    const a = [1, 2, 3];
    const b = [4, 5, 6];
    // Manual: dot=32, |a|=sqrt(14), |b|=sqrt(77) -> 32/(sqrt(14)*sqrt(77))
    const expected = 32 / (Math.sqrt(14) * Math.sqrt(77));
    expect(cosineSimilarity(a, b)).toBeCloseTo(expected, 5);
  });
});

// ---------------------------------------------------------------------------
// InMemoryMemoryProvider
// ---------------------------------------------------------------------------

describe("InMemoryMemoryProvider", () => {
  let provider: InMemoryMemoryProvider;

  beforeEach(() => {
    provider = new InMemoryMemoryProvider();
  });

  // -----------------------------------------------------------------------
  // store + get
  // -----------------------------------------------------------------------

  describe("store / get", () => {
    it("stores an entry and retrieves it by id", async () => {
      const entry = await provider.store({
        tenantId: "tenant-1",
        projectId: "project-a",
        type: "investigation",
        content: "Root cause: null pointer in parser",
        tags: ["parser", "crash"],
        confidence: 1.0,
      });

      expect(entry.id).toBeTruthy();
      expect(entry.tenantId).toBe("tenant-1");
      expect(entry.projectId).toBe("project-a");
      expect(entry.createdAt).toBeTruthy();
      expect(entry.updatedAt).toBeTruthy();

      const fetched = await provider.get(entry.id, "tenant-1");
      expect(fetched).not.toBeNull();
      expect(fetched!.content).toBe("Root cause: null pointer in parser");
    });

    it("returns null for wrong tenant", async () => {
      const entry = await provider.store({
        tenantId: "tenant-1",
        projectId: "project-a",
        type: "knowledge",
        content: "secret info",
        tags: [],
        confidence: 1.0,
      });

      const fetched = await provider.get(entry.id, "tenant-2");
      expect(fetched).toBeNull();
    });

    it("returns null for non-existent id", async () => {
      const fetched = await provider.get("non-existent", "tenant-1");
      expect(fetched).toBeNull();
    });
  });

  // -----------------------------------------------------------------------
  // Tenant isolation
  // -----------------------------------------------------------------------

  describe("tenant isolation", () => {
    it("query only returns entries from the specified tenant", async () => {
      await provider.store({
        tenantId: "tenant-1",
        projectId: "project-a",
        type: "investigation",
        content: "Tenant 1 data",
        tags: [],
        confidence: 1.0,
      });

      await provider.store({
        tenantId: "tenant-2",
        projectId: "project-a",
        type: "investigation",
        content: "Tenant 2 data",
        tags: [],
        confidence: 1.0,
      });

      const t1Results = await provider.query({ tenantId: "tenant-1" });
      expect(t1Results).toHaveLength(1);
      expect(t1Results[0].content).toBe("Tenant 1 data");

      const t2Results = await provider.query({ tenantId: "tenant-2" });
      expect(t2Results).toHaveLength(1);
      expect(t2Results[0].content).toBe("Tenant 2 data");
    });

    it("update fails for wrong tenant", async () => {
      const entry = await provider.store({
        tenantId: "tenant-1",
        projectId: "project-a",
        type: "knowledge",
        content: "original",
        tags: [],
        confidence: 1.0,
      });

      const result = await provider.update(entry.id, "tenant-2", {
        content: "hacked",
      });
      expect(result).toBeNull();

      // Original unchanged
      const original = await provider.get(entry.id, "tenant-1");
      expect(original!.content).toBe("original");
    });

    it("delete fails for wrong tenant", async () => {
      const entry = await provider.store({
        tenantId: "tenant-1",
        projectId: "project-a",
        type: "knowledge",
        content: "protected",
        tags: [],
        confidence: 1.0,
      });

      const deleted = await provider.delete(entry.id, "tenant-2");
      expect(deleted).toBe(false);
      expect(provider.size()).toBe(1);
    });
  });

  // -----------------------------------------------------------------------
  // query — filters
  // -----------------------------------------------------------------------

  describe("query filters", () => {
    beforeEach(async () => {
      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "investigation",
        content: "investigation report",
        tags: ["crash", "parser"],
        confidence: 1.0,
      });
      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "fix_pattern",
        content: "fix pattern",
        tags: ["parser"],
        confidence: 1.5,
      });
      await provider.store({
        tenantId: "t1",
        projectId: "p2",
        type: "root_cause",
        content: "root cause",
        tags: ["network"],
        confidence: 1.0,
      });
    });

    it("filters by projectId", async () => {
      const results = await provider.query({
        tenantId: "t1",
        projectId: "p1",
      });
      expect(results).toHaveLength(2);
    });

    it("filters by type", async () => {
      const results = await provider.query({
        tenantId: "t1",
        types: ["investigation"],
      });
      expect(results).toHaveLength(1);
      expect(results[0].type).toBe("investigation");
    });

    it("filters by multiple types", async () => {
      const results = await provider.query({
        tenantId: "t1",
        types: ["investigation", "root_cause"],
      });
      expect(results).toHaveLength(2);
    });

    it("filters by tags (AND logic)", async () => {
      const results = await provider.query({
        tenantId: "t1",
        tags: ["crash", "parser"],
      });
      expect(results).toHaveLength(1);
      expect(results[0].content).toBe("investigation report");
    });

    it("respects limit", async () => {
      const results = await provider.query({
        tenantId: "t1",
        limit: 1,
      });
      expect(results).toHaveLength(1);
    });
  });

  // -----------------------------------------------------------------------
  // query — similarity search
  // -----------------------------------------------------------------------

  describe("similarity search", () => {
    it("returns entries sorted by cosine similarity", async () => {
      const baseEmb = makeEmbedding(1.0);
      const similarEmb = makeEmbedding(1.1); // Close to base
      const differentEmb = makeEmbedding(50.0); // Far from base

      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "investigation",
        content: "similar entry",
        embedding: similarEmb,
        tags: [],
        confidence: 1.0,
      });

      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "investigation",
        content: "different entry",
        embedding: differentEmb,
        tags: [],
        confidence: 1.0,
      });

      const results = await provider.query({
        tenantId: "t1",
        embedding: baseEmb,
        similarityThreshold: 0.5,
      });

      // The similar entry should rank first.
      expect(results.length).toBeGreaterThanOrEqual(1);
      expect(results[0].content).toBe("similar entry");
    });

    it("excludes entries below similarity threshold", async () => {
      const emb1 = [1, 0, 0, 0];
      const emb2 = [0, 1, 0, 0]; // Orthogonal = 0 similarity

      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "investigation",
        content: "orthogonal",
        embedding: emb2,
        tags: [],
        confidence: 1.0,
      });

      const results = await provider.query({
        tenantId: "t1",
        embedding: emb1,
        similarityThreshold: 0.5,
      });

      expect(results).toHaveLength(0);
    });

    it("excludes entries without embeddings from similarity search", async () => {
      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "investigation",
        content: "no embedding",
        tags: [],
        confidence: 1.0,
      });

      const results = await provider.query({
        tenantId: "t1",
        embedding: [1, 0, 0, 0],
        similarityThreshold: 0.5,
      });

      expect(results).toHaveLength(0);
    });
  });

  // -----------------------------------------------------------------------
  // update
  // -----------------------------------------------------------------------

  describe("update", () => {
    it("updates content and bumps updatedAt", async () => {
      const entry = await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "knowledge",
        content: "original",
        tags: ["v1"],
        confidence: 1.0,
      });

      const updated = await provider.update(entry.id, "t1", {
        content: "revised",
        tags: ["v1", "v2"],
        confidence: 1.5,
      });

      expect(updated).not.toBeNull();
      expect(updated!.content).toBe("revised");
      expect(updated!.tags).toEqual(["v1", "v2"]);
      expect(updated!.confidence).toBe(1.5);
      expect(new Date(updated!.updatedAt).getTime()).toBeGreaterThanOrEqual(
        new Date(entry.updatedAt).getTime(),
      );
    });

    it("partial update preserves unmodified fields", async () => {
      const entry = await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "knowledge",
        content: "original",
        tags: ["keep-me"],
        confidence: 1.0,
      });

      const updated = await provider.update(entry.id, "t1", {
        confidence: 1.5,
      });

      expect(updated!.content).toBe("original");
      expect(updated!.tags).toEqual(["keep-me"]);
    });
  });

  // -----------------------------------------------------------------------
  // delete
  // -----------------------------------------------------------------------

  describe("delete", () => {
    it("removes the entry", async () => {
      const entry = await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "knowledge",
        content: "to delete",
        tags: [],
        confidence: 1.0,
      });

      const deleted = await provider.delete(entry.id, "t1");
      expect(deleted).toBe(true);
      expect(provider.size()).toBe(0);

      const fetched = await provider.get(entry.id, "t1");
      expect(fetched).toBeNull();
    });

    it("returns false for non-existent id", async () => {
      const deleted = await provider.delete("non-existent", "t1");
      expect(deleted).toBe(false);
    });
  });

  // -----------------------------------------------------------------------
  // findDuplicate
  // -----------------------------------------------------------------------

  describe("findDuplicate", () => {
    it("finds a near-duplicate above threshold", async () => {
      const emb = makeEmbedding(1.0);

      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "investigation",
        content: "existing report",
        embedding: emb,
        tags: [],
        confidence: 1.0,
      });

      // Query with the exact same embedding should find it.
      const dup = await provider.findDuplicate("t1", "p1", emb, 0.99);
      expect(dup).not.toBeNull();
      expect(dup!.content).toBe("existing report");
    });

    it("returns null when no duplicate above threshold", async () => {
      const emb1 = [1, 0, 0, 0];
      const emb2 = [0, 1, 0, 0];

      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "investigation",
        content: "orthogonal report",
        embedding: emb1,
        tags: [],
        confidence: 1.0,
      });

      const dup = await provider.findDuplicate("t1", "p1", emb2, 0.92);
      expect(dup).toBeNull();
    });

    it("scopes dedup to tenant + project", async () => {
      const emb = makeEmbedding(1.0);

      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "investigation",
        content: "tenant 1 report",
        embedding: emb,
        tags: [],
        confidence: 1.0,
      });

      // Different tenant should not find the duplicate.
      const dup = await provider.findDuplicate("t2", "p1", emb, 0.5);
      expect(dup).toBeNull();

      // Different project should not find the duplicate.
      const dup2 = await provider.findDuplicate("t1", "p2", emb, 0.5);
      expect(dup2).toBeNull();
    });
  });

  // -----------------------------------------------------------------------
  // ACL
  // -----------------------------------------------------------------------

  describe("checkAccess", () => {
    it("allows access when no ACL rules are set (permissive default)", async () => {
      const allowed = await provider.checkAccess({
        tenantId: "t1",
        projectId: "p1",
        agentId: "agent-1",
        canRead: true,
        canWrite: true,
      });
      expect(allowed).toBe(true);
    });

    it("denies read when ACL disallows it", async () => {
      provider.setACL({
        tenantId: "t1",
        projectId: "p1",
        agentId: "agent-1",
        canRead: false,
        canWrite: true,
      });

      const allowed = await provider.checkAccess({
        tenantId: "t1",
        projectId: "p1",
        agentId: "agent-1",
        canRead: true,
        canWrite: false,
      });
      expect(allowed).toBe(false);
    });

    it("denies write when ACL disallows it", async () => {
      provider.setACL({
        tenantId: "t1",
        projectId: "p1",
        agentId: "agent-1",
        canRead: true,
        canWrite: false,
      });

      const allowed = await provider.checkAccess({
        tenantId: "t1",
        projectId: "p1",
        agentId: "agent-1",
        canRead: false,
        canWrite: true,
      });
      expect(allowed).toBe(false);
    });

    it("allows when ACL permits the requested access", async () => {
      provider.setACL({
        tenantId: "t1",
        projectId: "p1",
        agentId: "agent-1",
        canRead: true,
        canWrite: false,
      });

      const allowed = await provider.checkAccess({
        tenantId: "t1",
        projectId: "p1",
        agentId: "agent-1",
        canRead: true,
        canWrite: false,
      });
      expect(allowed).toBe(true);
    });
  });

  // -----------------------------------------------------------------------
  // pruneExpired
  // -----------------------------------------------------------------------

  describe("pruneExpired", () => {
    it("removes entries past their expires_at", async () => {
      const past = new Date(Date.now() - 86400000).toISOString(); // 1 day ago

      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "knowledge",
        content: "expired entry",
        tags: [],
        confidence: 1.0,
        expiresAt: past,
      });

      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "knowledge",
        content: "fresh entry",
        tags: [],
        confidence: 1.0,
      });

      const pruned = await provider.pruneExpired("t1");
      expect(pruned).toBe(1);
      expect(provider.size()).toBe(1);
    });

    it("does not prune entries from other tenants", async () => {
      const past = new Date(Date.now() - 86400000).toISOString();

      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "knowledge",
        content: "expired t1",
        tags: [],
        confidence: 1.0,
        expiresAt: past,
      });

      await provider.store({
        tenantId: "t2",
        projectId: "p1",
        type: "knowledge",
        content: "expired t2",
        tags: [],
        confidence: 1.0,
        expiresAt: past,
      });

      const pruned = await provider.pruneExpired("t1");
      expect(pruned).toBe(1);
      expect(provider.size()).toBe(1); // t2 entry survives
    });

    it("returns 0 when nothing to prune", async () => {
      const pruned = await provider.pruneExpired("t1");
      expect(pruned).toBe(0);
    });
  });

  // -----------------------------------------------------------------------
  // exportProject
  // -----------------------------------------------------------------------

  describe("exportProject", () => {
    it("returns all entries for the specified tenant + project", async () => {
      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "investigation",
        content: "entry 1",
        tags: [],
        confidence: 1.0,
      });
      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "fix_pattern",
        content: "entry 2",
        tags: [],
        confidence: 1.0,
      });
      await provider.store({
        tenantId: "t1",
        projectId: "p2",
        type: "knowledge",
        content: "different project",
        tags: [],
        confidence: 1.0,
      });

      const exported = await provider.exportProject("t1", "p1");
      expect(exported).toHaveLength(2);
    });

    it("does not leak entries from other tenants", async () => {
      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "investigation",
        content: "t1 data",
        tags: [],
        confidence: 1.0,
      });
      await provider.store({
        tenantId: "t2",
        projectId: "p1",
        type: "investigation",
        content: "t2 data",
        tags: [],
        confidence: 1.0,
      });

      const exported = await provider.exportProject("t1", "p1");
      expect(exported).toHaveLength(1);
      expect(exported[0].content).toBe("t1 data");
    });
  });

  // -----------------------------------------------------------------------
  // query — age filter
  // -----------------------------------------------------------------------

  describe("age filtering", () => {
    it("excludes entries older than maxAgeDays", async () => {
      // Store an entry with a backdated createdAt.
      const entry = await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "knowledge",
        content: "old entry",
        tags: [],
        confidence: 1.0,
      });

      // Manually backdate the entry in the store.
      const old = new Date();
      old.setDate(old.getDate() - 200);
      await provider.update(entry.id, "t1", {}); // trigger updatedAt change

      // Hack: modify the entry directly via store + get + re-store trick.
      // The in-memory provider stores by id, so we can retrieve and modify.
      const fetched = await provider.get(entry.id, "t1");
      if (fetched) {
        // Override createdAt by deleting and re-storing a backdated entry.
        await provider.delete(entry.id, "t1");
        const backdated = await provider.store({
          tenantId: "t1",
          projectId: "p1",
          type: "knowledge",
          content: "old entry",
          tags: [],
          confidence: 1.0,
        });
        // Manually set the createdAt by accessing internal state.
        // Since this is testing the in-memory provider, this is acceptable.
        const internal = await provider.get(backdated.id, "t1");
        if (internal) {
          (internal as { createdAt: string }).createdAt = old.toISOString();
        }
      }

      const results = await provider.query({
        tenantId: "t1",
        maxAgeDays: 180,
      });

      // The backdated entry should be excluded.
      expect(results).toHaveLength(0);
    });

    it("includes entries within maxAgeDays", async () => {
      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        type: "knowledge",
        content: "recent entry",
        tags: [],
        confidence: 1.0,
      });

      const results = await provider.query({
        tenantId: "t1",
        maxAgeDays: 180,
      });

      expect(results).toHaveLength(1);
    });
  });

  // -----------------------------------------------------------------------
  // Cross-engine / cross-agent sharing
  // -----------------------------------------------------------------------

  describe("cross-agent and cross-engine sharing", () => {
    it("agents within same tenant+project can see each others entries", async () => {
      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        agentId: "agent-claude",
        engine: "claude-cli",
        type: "investigation",
        content: "Claude found a bug",
        tags: [],
        confidence: 1.0,
      });

      await provider.store({
        tenantId: "t1",
        projectId: "p1",
        agentId: "agent-gemini",
        engine: "gemini-cli",
        type: "fix_pattern",
        content: "Gemini fixed it",
        tags: [],
        confidence: 1.0,
      });

      const results = await provider.query({
        tenantId: "t1",
        projectId: "p1",
      });

      expect(results).toHaveLength(2);
      const engines = results.map((r) => r.engine).sort();
      expect(engines).toEqual(["claude-cli", "gemini-cli"]);
    });
  });
});

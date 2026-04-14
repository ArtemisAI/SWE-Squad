/**
 * Unit tests for MemoryService.
 *
 * Tests the high-level service layer: dedup, confidence tracking, ACL
 * enforcement, TTL, and tenant isolation. Uses InMemoryMemoryProvider.
 */

import { describe, it, expect, beforeEach } from "vitest";
import {
  MemoryService,
  MemoryAccessError,
} from "../../../src/services/memory-service.js";
import { InMemoryMemoryProvider } from "../../../src/providers/memory/memory-provider.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeEmbedding(seed: number, dims = 4): number[] {
  const vec = Array.from({ length: dims }, (_, i) => Math.sin(seed + i));
  const norm = Math.sqrt(vec.reduce((s, v) => s + v * v, 0));
  return norm > 0 ? vec.map((v) => v / norm) : vec;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("MemoryService", () => {
  let provider: InMemoryMemoryProvider;
  let service: MemoryService;

  beforeEach(() => {
    provider = new InMemoryMemoryProvider();
    service = new MemoryService({
      provider,
      dedupThreshold: 0.92,
      ttlDays: 180,
      maxEntriesPerProject: 10_000,
      confidenceIncrement: 0.1,
      maxConfidence: 2.0,
    });
  });

  // -----------------------------------------------------------------------
  // Basic store + query
  // -----------------------------------------------------------------------

  describe("store", () => {
    it("stores a new entry and returns action=stored", async () => {
      const result = await service.store("t1", "p1", {
        type: "investigation",
        content: "Found a null pointer bug in parser module",
        tags: ["parser", "crash"],
      });

      expect(result.action).toBe("stored");
      expect(result.entry.tenantId).toBe("t1");
      expect(result.entry.projectId).toBe("p1");
      expect(result.entry.type).toBe("investigation");
      expect(result.entry.confidence).toBe(1.0);
      expect(result.entry.expiresAt).toBeTruthy();
    });

    it("sets default confidence to 1.0", async () => {
      const result = await service.store("t1", "p1", {
        type: "knowledge",
        content: "API docs link",
      });

      expect(result.entry.confidence).toBe(1.0);
    });

    it("preserves custom confidence", async () => {
      const result = await service.store("t1", "p1", {
        type: "knowledge",
        content: "high confidence info",
        confidence: 1.8,
      });

      expect(result.entry.confidence).toBe(1.8);
    });

    it("sets expiry based on ttlDays config", async () => {
      const result = await service.store("t1", "p1", {
        type: "knowledge",
        content: "will expire",
      });

      const expiresAt = new Date(result.entry.expiresAt!);
      const now = new Date();
      const diffDays = (expiresAt.getTime() - now.getTime()) / (1000 * 60 * 60 * 24);
      // Should be approximately 180 days in the future.
      expect(diffDays).toBeGreaterThan(179);
      expect(diffDays).toBeLessThan(181);
    });

    it("preserves custom expiresAt", async () => {
      const custom = "2027-01-01T00:00:00.000Z";
      const result = await service.store("t1", "p1", {
        type: "knowledge",
        content: "custom ttl",
        expiresAt: custom,
      });

      expect(result.entry.expiresAt).toBe(custom);
    });
  });

  // -----------------------------------------------------------------------
  // Dedup
  // -----------------------------------------------------------------------

  describe("dedup", () => {
    it("merges when near-duplicate is found above threshold", async () => {
      const emb = makeEmbedding(1.0);

      // Store the first entry.
      const first = await service.store("t1", "p1", {
        type: "investigation",
        content: "short report",
        embedding: emb,
        tags: ["bug"],
      });
      expect(first.action).toBe("stored");

      // Store a second entry with the same embedding but richer content.
      const second = await service.store("t1", "p1", {
        type: "investigation",
        content: "much longer and more detailed investigation report about the bug",
        embedding: emb,
        tags: ["detailed"],
      });

      expect(second.action).toBe("merged");
      // The merged entry should have the longer content.
      expect(second.entry.content).toBe(
        "much longer and more detailed investigation report about the bug",
      );
      // Tags should be merged.
      expect(second.entry.tags).toContain("bug");
      expect(second.entry.tags).toContain("detailed");
      // Confidence should be bumped.
      expect(second.entry.confidence).toBe(1.1);
    });

    it("stores separately when embeddings are dissimilar", async () => {
      const emb1 = [1, 0, 0, 0];
      const emb2 = [0, 1, 0, 0]; // Orthogonal

      await service.store("t1", "p1", {
        type: "investigation",
        content: "report A",
        embedding: emb1,
      });

      const result = await service.store("t1", "p1", {
        type: "investigation",
        content: "report B",
        embedding: emb2,
      });

      expect(result.action).toBe("stored");
      expect(provider.size()).toBe(2);
    });

    it("stores separately when no embedding provided", async () => {
      await service.store("t1", "p1", {
        type: "investigation",
        content: "no embedding 1",
      });

      const result = await service.store("t1", "p1", {
        type: "investigation",
        content: "no embedding 2",
      });

      expect(result.action).toBe("stored");
      expect(provider.size()).toBe(2);
    });

    it("dedup is scoped to tenant", async () => {
      const emb = makeEmbedding(1.0);

      await service.store("t1", "p1", {
        type: "investigation",
        content: "tenant 1 report",
        embedding: emb,
      });

      // Same embedding, different tenant: should NOT merge.
      const result = await service.store("t2", "p1", {
        type: "investigation",
        content: "tenant 2 report",
        embedding: emb,
      });

      expect(result.action).toBe("stored");
      expect(provider.size()).toBe(2);
    });
  });

  // -----------------------------------------------------------------------
  // Confidence tracking
  // -----------------------------------------------------------------------

  describe("recordHit", () => {
    it("increments confidence by configured amount", async () => {
      const result = await service.store("t1", "p1", {
        type: "fix_pattern",
        content: "retry with backoff",
      });

      const hit = await service.recordHit(result.entry.id, "t1");
      expect(hit).not.toBeNull();
      expect(hit!.confidence).toBe(1.1);
    });

    it("caps confidence at maxConfidence", async () => {
      const result = await service.store("t1", "p1", {
        type: "fix_pattern",
        content: "well-known pattern",
        confidence: 1.95,
      });

      const hit = await service.recordHit(result.entry.id, "t1");
      expect(hit!.confidence).toBe(2.0);

      // Another hit should not exceed 2.0.
      const hit2 = await service.recordHit(result.entry.id, "t1");
      expect(hit2!.confidence).toBe(2.0);
    });

    it("returns null for wrong tenant", async () => {
      const result = await service.store("t1", "p1", {
        type: "knowledge",
        content: "something",
      });

      const hit = await service.recordHit(result.entry.id, "t2");
      expect(hit).toBeNull();
    });

    it("returns null for non-existent id", async () => {
      const hit = await service.recordHit("non-existent", "t1");
      expect(hit).toBeNull();
    });
  });

  // -----------------------------------------------------------------------
  // ACL enforcement
  // -----------------------------------------------------------------------

  describe("ACL enforcement", () => {
    it("blocks write when agent lacks write permission", async () => {
      provider.setACL({
        tenantId: "t1",
        projectId: "p1",
        agentId: "reader-agent",
        canRead: true,
        canWrite: false,
      });

      await expect(
        service.store("t1", "p1", {
          agentId: "reader-agent",
          type: "investigation",
          content: "should fail",
        }),
      ).rejects.toThrow(MemoryAccessError);
    });

    it("blocks read when agent lacks read permission", async () => {
      provider.setACL({
        tenantId: "t1",
        projectId: "p1",
        agentId: "writer-agent",
        canRead: false,
        canWrite: true,
      });

      await expect(
        service.query({
          tenantId: "t1",
          projectId: "p1",
          agentId: "writer-agent",
        }),
      ).rejects.toThrow(MemoryAccessError);
    });

    it("allows store when agent has write permission", async () => {
      provider.setACL({
        tenantId: "t1",
        projectId: "p1",
        agentId: "full-agent",
        canRead: true,
        canWrite: true,
      });

      const result = await service.store("t1", "p1", {
        agentId: "full-agent",
        type: "knowledge",
        content: "allowed",
      });

      expect(result.action).toBe("stored");
    });

    it("skips ACL check when agentId is not provided", async () => {
      // No agent context = system-level operation, always allowed.
      const result = await service.store("t1", "p1", {
        type: "config",
        content: "system config",
      });

      expect(result.action).toBe("stored");
    });
  });

  // -----------------------------------------------------------------------
  // Tenant isolation (via service layer)
  // -----------------------------------------------------------------------

  describe("tenant isolation", () => {
    it("query only returns entries from specified tenant", async () => {
      await service.store("t1", "p1", {
        type: "investigation",
        content: "t1 data",
      });
      await service.store("t2", "p1", {
        type: "investigation",
        content: "t2 data",
      });

      const results = await service.query({ tenantId: "t1" });
      expect(results).toHaveLength(1);
      expect(results[0].content).toBe("t1 data");
    });

    it("get returns null for wrong tenant", async () => {
      const result = await service.store("t1", "p1", {
        type: "knowledge",
        content: "tenant 1 only",
      });

      const fetched = await service.get(result.entry.id, "t2");
      expect(fetched).toBeNull();
    });

    it("delete returns false for wrong tenant", async () => {
      const result = await service.store("t1", "p1", {
        type: "knowledge",
        content: "protected",
      });

      const deleted = await service.delete(result.entry.id, "t2");
      expect(deleted).toBe(false);
    });
  });

  // -----------------------------------------------------------------------
  // TTL / expiry
  // -----------------------------------------------------------------------

  describe("pruneExpired", () => {
    it("prunes expired entries for the specified tenant", async () => {
      const past = new Date(Date.now() - 86400000).toISOString();

      await service.store("t1", "p1", {
        type: "knowledge",
        content: "expired",
        expiresAt: past,
      });

      await service.store("t1", "p1", {
        type: "knowledge",
        content: "still valid",
      });

      const pruned = await service.pruneExpired("t1");
      expect(pruned).toBe(1);
    });
  });

  // -----------------------------------------------------------------------
  // exportProject
  // -----------------------------------------------------------------------

  describe("exportProject", () => {
    it("exports all entries for a tenant + project", async () => {
      await service.store("t1", "p1", {
        type: "investigation",
        content: "entry 1",
      });
      await service.store("t1", "p1", {
        type: "fix_pattern",
        content: "entry 2",
      });
      await service.store("t1", "p2", {
        type: "knowledge",
        content: "other project",
      });

      const exported = await service.exportProject("t1", "p1");
      expect(exported).toHaveLength(2);
    });
  });

  // -----------------------------------------------------------------------
  // Cross-engine sharing
  // -----------------------------------------------------------------------

  describe("cross-engine sharing", () => {
    it("entries from different engines are queryable together", async () => {
      await service.store("t1", "p1", {
        agentId: "agent-1",
        engine: "claude-cli",
        type: "investigation",
        content: "Claude investigation",
        tags: ["claude"],
      });

      await service.store("t1", "p1", {
        agentId: "agent-2",
        engine: "gemini-cli",
        type: "fix_pattern",
        content: "Gemini fix",
        tags: ["gemini"],
      });

      await service.store("t1", "p1", {
        agentId: "agent-3",
        engine: "copilot",
        type: "root_cause",
        content: "Copilot root cause",
        tags: ["copilot"],
      });

      const all = await service.query({ tenantId: "t1", projectId: "p1" });
      expect(all).toHaveLength(3);

      const engines = all.map((e) => e.engine).sort();
      expect(engines).toEqual(["claude-cli", "copilot", "gemini-cli"]);
    });
  });

  // -----------------------------------------------------------------------
  // Edge cases
  // -----------------------------------------------------------------------

  describe("edge cases", () => {
    it("merge keeps existing content when new content is shorter", async () => {
      const emb = makeEmbedding(1.0);

      await service.store("t1", "p1", {
        type: "investigation",
        content: "this is a very detailed and long investigation report with lots of context",
        embedding: emb,
        tags: ["original"],
      });

      const result = await service.store("t1", "p1", {
        type: "investigation",
        content: "short",
        embedding: emb,
        tags: ["short"],
      });

      expect(result.action).toBe("merged");
      // Should keep the longer content.
      expect(result.entry.content).toContain("very detailed");
    });

    it("multiple confidence bumps on merge", async () => {
      const emb = makeEmbedding(1.0);

      // First store.
      await service.store("t1", "p1", {
        type: "fix_pattern",
        content: "retry with exponential backoff",
        embedding: emb,
      });

      // Second store (merge).
      const result2 = await service.store("t1", "p1", {
        type: "fix_pattern",
        content: "retry with exponential backoff",
        embedding: emb,
      });
      expect(result2.entry.confidence).toBe(1.1);

      // Third store (merge again).
      const result3 = await service.store("t1", "p1", {
        type: "fix_pattern",
        content: "retry with exponential backoff",
        embedding: emb,
      });
      expect(result3.entry.confidence).toBeCloseTo(1.2);
    });

    it("handles empty tags gracefully", async () => {
      const result = await service.store("t1", "p1", {
        type: "knowledge",
        content: "no tags",
      });
      expect(result.entry.tags).toEqual([]);
    });
  });
});

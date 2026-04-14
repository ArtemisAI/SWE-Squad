/**
 * Embedding pipeline for SWE Squad semantic memory — TypeScript port.
 *
 * Uses the BASE_LLM proxy (OpenAI-compatible API) for embeddings and
 * Supabase pgvector for similarity search and storage.
 *
 * Ported from: src/swe_team/embeddings.py + store_embedding_with_dedup
 *              in src/swe_team/supabase_store.py
 */

import { SupabaseClient } from "./client.js";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface EmbeddingConfig {
  /** BASE_LLM_API_URL — OpenAI-compatible embedding endpoint. */
  apiUrl: string;
  /** BASE_LLM_API_KEY — API key for the proxy. */
  apiKey: string;
  /** Embedding model name (default "bge-m3"). */
  model?: string;
  /** Expected embedding dimensions (default 1024). */
  dimensions?: number;
}

// ---------------------------------------------------------------------------
// Similarity search result
// ---------------------------------------------------------------------------

export interface SimilarTicket {
  ticketId: string;
  similarity: number;
  metadata: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Service
// ---------------------------------------------------------------------------

export class EmbeddingService {
  private readonly apiUrl: string;
  private readonly apiKey: string;
  private readonly model: string;
  private readonly dimensions: number;

  constructor(config: EmbeddingConfig) {
    this.apiUrl = config.apiUrl.replace(/\/+$/, "");
    this.apiKey = config.apiKey;
    this.model = config.model ?? "bge-m3";
    this.dimensions = config.dimensions ?? 1024;
  }

  // -----------------------------------------------------------------------
  // Embed text
  // -----------------------------------------------------------------------

  /**
   * Generate an embedding vector for the given text.
   *
   * Calls POST {apiUrl}/embeddings with the configured model.
   * Returns the raw float array (length === dimensions).
   *
   * Throws on API errors (caller decides whether to treat as fatal).
   */
  async embed(text: string): Promise<number[]> {
    const url = `${this.apiUrl}/embeddings`;

    const res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        input: text,
        model: this.model,
      }),
      signal: AbortSignal.timeout(10_000),
    });

    if (!res.ok) {
      const body = await res.text();
      throw new Error(
        `Embedding API error (${res.status}): ${body.slice(0, 500)}`,
      );
    }

    const json = (await res.json()) as {
      data: Array<{ embedding: number[] }>;
    };

    if (!json.data?.[0]?.embedding) {
      throw new Error("Embedding API returned no data");
    }

    return json.data[0].embedding;
  }

  // -----------------------------------------------------------------------
  // Similarity search
  // -----------------------------------------------------------------------

  /**
   * Find similar tickets using the `match_similar_tickets` Supabase RPC.
   *
   * Returns matches sorted by descending similarity, above the floor threshold.
   * The RPC applies confidence-weighted scoring (memory_confidence 1.0-2.0).
   */
  async findSimilar(
    client: SupabaseClient,
    embedding: number[],
    options?: {
      topK?: number;
      floor?: number;
      teamId?: string;
      maxAgeDays?: number;
    },
  ): Promise<SimilarTicket[]> {
    const topK = options?.topK ?? 5;
    const floor = options?.floor ?? 0.75;
    const teamId = options?.teamId ?? "default";
    const maxAgeDays = options?.maxAgeDays ?? 180;

    const rows = await client.rpc<Record<string, unknown>[]>(
      "match_similar_tickets",
      {
        query_embedding: vectorLiteral(embedding),
        team: teamId,
        match_count: topK,
        similarity_floor: floor,
        max_age_days: maxAgeDays,
      },
    );

    if (!Array.isArray(rows)) return [];

    return rows.map((row) => ({
      ticketId: String(row["ticket_id"] ?? ""),
      similarity: Number(row["similarity"] ?? 0),
      metadata: {
        title: row["title"],
        sourceModule: row["source_module"],
        errorLog: row["error_log"],
        investigationReport: row["investigation_report"],
        proposedFix: row["proposed_fix"],
        rawSimilarity: Number(row["raw_similarity"] ?? 0),
        memoryConfidence: Number(row["memory_confidence"] ?? 1.0),
      },
    }));
  }

  // -----------------------------------------------------------------------
  // Store with semantic dedup
  // -----------------------------------------------------------------------

  /**
   * Store an embedding for a ticket, with semantic deduplication.
   *
   * 1. Checks for existing tickets above the dedup threshold (default 0.92).
   * 2. If no near-duplicate exists: stores the embedding ("stored").
   * 3. If a near-duplicate exists but has less detail: merges ("merged").
   * 4. If a near-duplicate exists and is richer: skips ("skipped").
   *
   * This mirrors `store_embedding_with_dedup()` from the Python store.
   */
  async storeEmbedding(
    client: SupabaseClient,
    ticketId: string,
    embedding: number[],
    metadata: Record<string, unknown>,
    options?: {
      teamId?: string;
      dedupThreshold?: number;
    },
  ): Promise<"stored" | "merged" | "skipped"> {
    const teamId = options?.teamId ?? "default";
    const dedupThreshold = options?.dedupThreshold ?? 0.92;

    // Step 1: Check for near-duplicates.
    const matches = await this.findSimilar(client, embedding, {
      topK: 1,
      floor: dedupThreshold,
      teamId,
    });

    if (matches.length === 0) {
      // No duplicate — store directly.
      await this.patchEmbedding(client, ticketId, embedding, teamId);
      return "stored";
    }

    const candidate = matches[0];

    // Same ticket — just store.
    if (!candidate.ticketId || candidate.ticketId === ticketId) {
      await this.patchEmbedding(client, ticketId, embedding, teamId);
      return "stored";
    }

    // Compare detail richness: the ticket with more investigation data wins.
    const existingScore = memoryDetailScore(candidate.metadata);
    const newScore = memoryDetailScore(metadata);

    if (existingScore >= newScore) {
      return "skipped";
    }

    // New ticket is richer — merge into the existing slot.
    await client.update(
      "swe_tickets",
      {
        ticket_id: `eq.${candidate.ticketId}`,
        team_id: `eq.${teamId}`,
      },
      {
        investigation_report: metadata["investigationReport"] ?? null,
        proposed_fix: metadata["proposedFix"] ?? null,
        embedding: vectorLiteral(embedding),
      },
    );

    return "merged";
  }

  // -----------------------------------------------------------------------
  // Internal helpers
  // -----------------------------------------------------------------------

  /**
   * PATCH the embedding column on an existing ticket row.
   */
  private async patchEmbedding(
    client: SupabaseClient,
    ticketId: string,
    embedding: number[],
    teamId: string,
  ): Promise<void> {
    await client.update(
      "swe_tickets",
      {
        ticket_id: `eq.${ticketId}`,
        team_id: `eq.${teamId}`,
      },
      { embedding: vectorLiteral(embedding) },
    );
  }
}

// ---------------------------------------------------------------------------
// Utility functions
// ---------------------------------------------------------------------------

/**
 * Convert a float array to pgvector text literal format:
 * `[0.1,0.2,0.3,...]`
 *
 * Matches the Python `_vector_literal()` helper.
 */
export function vectorLiteral(embedding: number[]): string {
  return "[" + embedding.map(Number).join(",") + "]";
}

/**
 * Score the "detail richness" of a ticket/metadata for merge decisions.
 *
 * Returns [populatedFields, totalLength] as a comparable tuple.
 * Matches the Python `_memory_detail_score()` helper.
 */
function memoryDetailScore(
  data: Record<string, unknown>,
): number {
  const report = String(data["investigationReport"] ?? data["investigation_report"] ?? "").trim();
  const fix = String(data["proposedFix"] ?? data["proposed_fix"] ?? "").trim();
  const populated = (report ? 1 : 0) + (fix ? 1 : 0);
  // Combine into a single comparable number: populated fields weighted heavily,
  // plus text length as tiebreaker.
  return populated * 1_000_000 + report.length + fix.length;
}

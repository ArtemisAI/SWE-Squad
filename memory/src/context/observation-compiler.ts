/**
 * observation-compiler.ts
 *
 * Formats memory observations into markdown context blocks suitable for
 * injection into AI agent system prompts.
 */

// Local equivalent of the storage-layer SearchResult to avoid circular deps.
export interface SearchResult {
  id: number;
  project: string;
  type?: string | null;
  title?: string | null;
  narrative?: string | null;
  facts?: string | null;
  concepts?: string | null;
  files_read?: string | null;
  files_modified?: string | null;
  similarity?: number | null;
  rank?: number | null;
  created_at_epoch?: number | null;
  platform_source?: string | null;
  agent_id?: string | null;
}

export interface FormatOptions {
  /** Maximum number of observations to include. Default: 10 */
  maxObservations?: number;
  /** Whether to include files_read / files_modified lines. Default: true */
  includeFiles?: boolean;
  /** Whether to include the facts line. Default: true */
  includeFacts?: boolean;
  /** Section header rendered at the top of the block. Default: "## Memory Context" */
  title?: string;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Convert a millisecond epoch timestamp to a human-readable relative string
 * such as "2h ago", "3d ago", "just now", etc.
 */
function relativeTime(epochMs: number | null | undefined): string {
  if (epochMs == null) return "unknown time";

  const nowMs = Date.now();
  const diffMs = nowMs - epochMs;

  if (diffMs < 0) return "just now"; // future timestamps treated as just now

  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 60) return `${seconds}s ago`;

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;

  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;

  const years = Math.floor(months / 12);
  return `${years}y ago`;
}

/**
 * Format an epoch timestamp (milliseconds) as "YYYY-MM-DD HH:MM".
 */
function formatTimestamp(epochMs: number | null | undefined): string {
  if (epochMs == null) return "unknown";

  const d = new Date(epochMs);
  const yyyy = d.getUTCFullYear();
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const min = String(d.getUTCMinutes()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd} ${hh}:${min}`;
}

/**
 * Returns true if the observation carries enough content to be worth showing.
 */
function hasContent(obs: SearchResult): boolean {
  return Boolean(obs.title?.trim() || obs.narrative?.trim());
}

/**
 * Sort comparator: descending similarity when present, otherwise descending
 * created_at_epoch.
 */
function bySimilarityThenRecency(a: SearchResult, b: SearchResult): number {
  // Both have similarity — sort descending.
  if (a.similarity != null && b.similarity != null) {
    if (b.similarity !== a.similarity) return b.similarity - a.similarity;
  } else if (a.similarity != null) {
    return -1; // a ranks higher
  } else if (b.similarity != null) {
    return 1; // b ranks higher
  }

  // Fall back to recency (descending).
  const aEpoch = a.created_at_epoch ?? 0;
  const bEpoch = b.created_at_epoch ?? 0;
  return bEpoch - aEpoch;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Formats a list of observations into markdown suitable for agent system
 * prompt injection.
 *
 * Output shape:
 * ```
 * ## Memory Context
 * *N relevant observations from past agent sessions*
 *
 * ### {type}: {title}
 * *{relative_time} ago · {project} · {similarity_pct}% match*
 *
 * {narrative}
 *
 * **Facts:** {facts}
 * **Files touched:** {files_read}, {files_modified}
 * ---
 * ```
 */
export function formatObservationsAsContext(
  observations: SearchResult[],
  options?: FormatOptions,
): string {
  const maxObservations = options?.maxObservations ?? 10;
  const includeFiles = options?.includeFiles ?? true;
  const includeFacts = options?.includeFacts ?? true;
  const title = options?.title ?? "## Memory Context";

  // Filter out empty observations, sort, then cap.
  const filtered = observations
    .filter(hasContent)
    .sort(bySimilarityThenRecency)
    .slice(0, maxObservations);

  if (filtered.length === 0) return "";

  const lines: string[] = [];

  lines.push(title);
  lines.push(`*${filtered.length} relevant observation${filtered.length === 1 ? "" : "s"} from past agent sessions*`);

  for (const obs of filtered) {
    lines.push("");

    // --- Heading line ---
    const typeLabel = obs.type ?? "observation";
    const rawTitle = obs.title?.trim() ?? "(untitled)";
    const highRelevance =
      obs.similarity != null && obs.similarity >= 0.9
        ? "(high relevance) "
        : "";
    lines.push(`### ${highRelevance}${typeLabel}: ${rawTitle}`);

    // --- Meta line ---
    const metaParts: string[] = [relativeTime(obs.created_at_epoch)];
    metaParts.push(obs.project);
    if (obs.similarity != null) {
      const pct = Math.round(obs.similarity * 100);
      metaParts.push(`${pct}% match`);
    }
    lines.push(`*${metaParts.join(" · ")}*`);

    // --- Narrative ---
    const narrative = obs.narrative?.trim();
    if (narrative) {
      lines.push("");
      lines.push(narrative);
    }

    // --- Facts ---
    if (includeFacts && obs.facts?.trim()) {
      lines.push("");
      lines.push(`**Facts:** ${obs.facts.trim()}`);
    }

    // --- Files ---
    if (includeFiles) {
      const fileParts: string[] = [];
      if (obs.files_read?.trim()) fileParts.push(obs.files_read.trim());
      if (obs.files_modified?.trim()) fileParts.push(obs.files_modified.trim());
      if (fileParts.length > 0) {
        lines.push(`**Files touched:** ${fileParts.join(", ")}`);
      }
    }

    lines.push("---");
  }

  return lines.join("\n");
}

/**
 * Formats observations as a simple chronological timeline, most recent first.
 *
 * Output shape:
 * ```
 * [2026-04-13 14:23] investigator | SWE-Sandbox | Fixed authentication bug
 * [2026-04-12 09:11] developer    | SWE-Sandbox | Patched null pointer
 * ```
 *
 * Returns empty string if no observations are provided.
 */
export function formatTimelineContext(observations: SearchResult[]): string {
  if (observations.length === 0) return "";

  // Sort most-recent first.
  const sorted = [...observations].sort((a, b) => {
    const aEpoch = a.created_at_epoch ?? 0;
    const bEpoch = b.created_at_epoch ?? 0;
    return bEpoch - aEpoch;
  });

  const lines: string[] = [];

  for (const obs of sorted) {
    const timestamp = formatTimestamp(obs.created_at_epoch);
    const agentId = obs.agent_id ?? obs.type ?? "agent";
    const project = obs.project;
    const summary =
      obs.title?.trim() ?? obs.narrative?.trim()?.split("\n")[0] ?? "(no summary)";

    lines.push(`[${timestamp}] ${agentId} | ${project} | ${summary}`);
  }

  return lines.join("\n");
}

/**
 * Combines a timeline of recent observations with a semantic-relevance block
 * of past observations into a single context string.
 *
 * Output shape:
 * ```
 * ## Recent Project Timeline
 * [timeline entries...]
 *
 * ## Relevant Past Work
 * [formatted observations...]
 * ```
 *
 * Returns empty string if both inputs are empty / produce no output.
 */
export function buildContextText(params: {
  recentObservations: SearchResult[];
  semanticObservations?: SearchResult[];
  project: string;
}): string {
  const { recentObservations, semanticObservations = [], project: _project } =
    params;

  const timelineText = formatTimelineContext(recentObservations);
  const semanticText = formatObservationsAsContext(semanticObservations, {
    title: "## Relevant Past Work",
  });

  if (!timelineText && !semanticText) return "";

  const sections: string[] = [];

  if (timelineText) {
    sections.push("## Recent Project Timeline");
    sections.push(timelineText);
  }

  if (semanticText) {
    sections.push(semanticText);
  }

  return sections.join("\n\n");
}

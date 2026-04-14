/**
 * Prompt builder for delegation tools.
 *
 * Builds structured prompts for investigation, development, and review
 * engines using ticket data, investigation reports, and PR diffs.
 */

import type { SWETicket } from "../models/ticket.js";

/**
 * Build an investigation prompt from ticket data.
 *
 * The investigation engine receives this and performs read-only root-cause
 * analysis using its available tools.
 */
export function buildInvestigationPrompt(ticket: SWETicket): string {
  const meta = ticket.metadata as Record<string, unknown>;
  const parts = [
    `# Investigation Request: ${ticket.title}`,
    "",
    `**Ticket ID:** ${ticket.ticketId}`,
    `**Severity:** ${ticket.severity}`,
    `**Status:** ${ticket.status}`,
  ];

  if (meta.repo) {
    parts.push(`**Repository:** ${meta.repo}`);
  }

  parts.push("", "## Description", "", ticket.description);

  if (ticket.errorLog) {
    parts.push("", "## Error Log", "", "```", ticket.errorLog, "```");
  }

  if (meta.labels && Array.isArray(meta.labels)) {
    parts.push("", `**Labels:** ${(meta.labels as string[]).join(", ")}`);
  }

  parts.push(
    "",
    "## Instructions",
    "",
    "Investigate this issue and produce a structured report with:",
    "1. **Root cause** — What is causing this issue?",
    "2. **Affected files** — Which files need to be changed?",
    "3. **Suggested fix** — What specific changes should be made?",
    "4. **Risk assessment** — What could go wrong with the fix?",
    "5. **Confidence** — How confident are you in this analysis? (0-100)",
    "",
    "Use read-only tools (Read, Grep, Glob, Bash) to investigate.",
    "Do NOT make any changes to files.",
    "Do NOT use the Agent tool — work directly, do not spawn subagents.",
  );

  return parts.join("\n");
}

/**
 * Build a development prompt from ticket data and investigation report.
 */
export function buildDevelopmentPrompt(
  ticket: SWETicket,
  workspace: string,
): string {
  const meta = ticket.metadata as Record<string, unknown>;
  const parts = [
    `# Development Request: ${ticket.title}`,
    "",
    `**Ticket ID:** ${ticket.ticketId}`,
    `**Severity:** ${ticket.severity}`,
    `**Workspace:** ${workspace}`,
  ];

  if (meta.repo) {
    parts.push(`**Repository:** ${meta.repo}`);
  }

  if (ticket.investigationReport) {
    parts.push(
      "",
      "## Investigation Report",
      "",
      ticket.investigationReport,
    );
  }

  parts.push("", "## Description", "", ticket.description);

  // Include review feedback for rework iterations
  const isRework = meta.reviewVerdict === "changes_requested";
  if (isRework && meta.reviewSummary) {
    parts.push(
      "",
      "## REVIEW FEEDBACK (changes_requested — address these issues)",
      "",
      String(meta.reviewSummary),
    );
    if (meta.prUrl) {
      parts.push("", `**Existing PR:** ${meta.prUrl}`);
    }
    if (meta.devBranch) {
      parts.push(`**Existing branch:** ${meta.devBranch} — checkout this branch and push fixes to it.`);
    }
  }

  parts.push(
    "",
    "## Instructions",
    "",
    isRework
      ? "Address the review feedback above. The reviewer requested changes on an existing PR:"
      : "Implement the fix described in the investigation report:",
    isRework ? "1. Checkout the existing branch listed above" : "1. Make the necessary code changes",
    "2. Make the necessary code changes to address all issues",
    "3. Run any existing tests to verify the fix",
    "4. Commit your changes with a descriptive message",
    isRework ? "5. Push to the existing branch (the PR will update automatically)" : "5. Push the branch and create a pull request",
    "",
    "Keep changes minimal and focused on the issue.",
    "Do NOT modify unrelated code.",
    "Prefer direct tool calls (Read, Edit, Write, Bash, Grep, Glob) over Agent subagents for speed.",
    "Do NOT use the Agent tool unless the task clearly requires exploring multiple unrelated areas.",
  );

  return parts.join("\n");
}

/**
 * Build a code review prompt from ticket data and PR diff.
 *
 * The review engine receives this and performs a read-only code review,
 * producing a structured verdict with issues found.
 */
export function buildReviewPrompt(
  ticket: SWETicket,
  prDiff: string | null,
): string {
  const meta = ticket.metadata as Record<string, unknown>;
  const parts = [
    `# Code Review Request: ${ticket.title}`,
    "",
    `**Ticket ID:** ${ticket.ticketId}`,
    `**Severity:** ${ticket.severity}`,
    `**Status:** ${ticket.status}`,
  ];

  if (meta.repo) {
    parts.push(`**Repository:** ${meta.repo}`);
  }

  const prUrl =
    (meta.prUrl as string | undefined) ??
    (meta.pr_url as string | undefined);
  if (prUrl) {
    parts.push(`**PR URL:** ${prUrl}`);
  }

  parts.push("", "## Description", "", ticket.description);

  if (ticket.investigationReport) {
    parts.push(
      "",
      "## Investigation Report",
      "",
      ticket.investigationReport,
    );
  }

  if (meta.lastDevError) {
    parts.push(
      "",
      "## Development Notes",
      "",
      `Previous development error: ${meta.lastDevError}`,
    );
  }

  if (prDiff) {
    // Truncate very large diffs to avoid blowing context
    const maxDiffLength = 50_000;
    const truncatedDiff =
      prDiff.length > maxDiffLength
        ? prDiff.slice(0, maxDiffLength) +
          `\n\n... [diff truncated at ${maxDiffLength} chars, ${prDiff.length} total]`
        : prDiff;
    parts.push("", "## PR Diff", "", "```diff", truncatedDiff, "```");
  } else {
    parts.push(
      "",
      "## PR Diff",
      "",
      "*No diff available. Review the PR directly using git tools.*",
    );
  }

  parts.push(
    "",
    "## Review Instructions",
    "",
    "Review this pull request and produce a structured assessment:",
    "",
    "1. **Verdict** -- State one of: APPROVED, CHANGES_REQUESTED, or COMMENTED",
    "2. **Issues found** -- List specific issues with severity (critical/high/medium/low)",
    "3. **Code quality** -- Assess readability, maintainability, and adherence to project conventions",
    "4. **Test coverage** -- Are the changes adequately tested?",
    "5. **Risk assessment** -- Could this change cause regressions?",
    "6. **Summary** -- Brief overall assessment",
    "",
    "### Verdict Guidelines (IMPORTANT)",
    "",
    "- **APPROVED**: The fix correctly addresses the original issue with no critical or high-severity bugs.",
    "  Medium and low issues should be NOTED but must NOT block approval.",
    "  Approve if the code is correct, safe, and solves the problem — even if minor improvements are possible.",
    "- **CHANGES_REQUESTED**: ONLY for critical bugs, security vulnerabilities, logic errors,",
    "  data loss risks, or high-severity regressions. Do NOT request changes for style, minor",
    "  improvements, missing log rotation, or nice-to-have features.",
    "- **COMMENTED**: For advisory feedback only — no blocking issues.",
    "",
    "Use read-only tools (Read, Grep, Glob, Bash) to understand context.",
    "Do NOT make any changes to files.",
    "Do NOT use the Agent tool — work directly, do not spawn subagents.",
    "Respond concisely with your structured review.",
  );

  return parts.join("\n");
}

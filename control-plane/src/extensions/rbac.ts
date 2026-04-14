/**
 * RBAC extension for pi-agent sessions.
 *
 * Restricts tool access based on the agent's role:
 *   - investigator: read-only (blocks write, edit, bash write commands)
 *   - developer: full tools, scoped to sandbox paths
 *   - reviewer: strict read-only (no bash at all except safe read commands)
 *   - full: no restrictions
 *
 * Uses the pi-coding-agent ExtensionFactory contract:
 *   pi.on("tool_call", ...) to intercept and optionally block tool calls.
 */

import type {
  ExtensionFactory,
  ToolCallEvent,
  ToolCallEventResult,
} from "@mariozechner/pi-coding-agent";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

export interface RBACConfig {
  /** Agent role determining tool access level. */
  role: "investigator" | "developer" | "reviewer" | "full";
  /** Allowed working directories. Writes outside these paths are blocked. */
  sandboxPaths?: string[];
}

// ---------------------------------------------------------------------------
// Role-based tool restrictions
// ---------------------------------------------------------------------------

/** Tools that are always safe (read-only operations). */
const READ_ONLY_TOOLS = new Set(["read", "grep", "find", "ls"]);

/** Tools that modify files. */
const WRITE_TOOLS = new Set(["write", "edit"]);

/**
 * Bash commands that are safe for read-only roles.
 * Matches the command prefix (before any arguments).
 */
const SAFE_BASH_PREFIXES = [
  "cat ",
  "head ",
  "tail ",
  "less ",
  "more ",
  "wc ",
  "file ",
  "stat ",
  "ls ",
  "find ",
  "grep ",
  "rg ",
  "git log",
  "git show",
  "git diff",
  "git status",
  "git branch",
  "git rev-parse",
  "echo ",
  "printf ",
  "pwd",
  "env",
  "whoami",
  "date",
  "which ",
  "type ",
  "test ",
  "[ ",
];

/**
 * Check whether a bash command is read-only safe.
 */
function isSafeBashCommand(command: string): boolean {
  const trimmed = command.trim();
  // Empty commands are safe
  if (!trimmed) return true;
  // Check against safe prefixes
  return SAFE_BASH_PREFIXES.some(
    (prefix) => trimmed === prefix.trim() || trimmed.startsWith(prefix),
  );
}

/**
 * Check whether a file path falls within any of the sandbox paths.
 */
function isWithinSandbox(filePath: string, sandboxPaths: string[]): boolean {
  if (sandboxPaths.length === 0) return true; // No sandbox = allow all
  return sandboxPaths.some((sp) => {
    const normalized = sp.endsWith("/") ? sp : `${sp}/`;
    return filePath === sp || filePath.startsWith(normalized);
  });
}

// ---------------------------------------------------------------------------
// Extension factory
// ---------------------------------------------------------------------------

/**
 * Create an RBAC extension that restricts tool access by role.
 *
 * @example
 * ```typescript
 * import { createAgentSession } from "@mariozechner/pi-coding-agent";
 * import { createRBACExtension } from "./extensions/rbac.js";
 *
 * const rbac = createRBACExtension({
 *   role: "investigator",
 *   sandboxPaths: ["/home/agent/Projects/my-repo"],
 * });
 * // Pass as a session-level extension via pi.on() in session setup
 * ```
 */
export function createRBACExtension(config: RBACConfig): ExtensionFactory {
  return (pi) => {
    // Full role has no restrictions
    if (config.role === "full") return;

    const sandboxPaths = config.sandboxPaths ?? [];

    pi.on("tool_call", (event: ToolCallEvent): ToolCallEventResult | void => {
      const toolName = event.toolName;

      // ------------------------------------------------------------------
      // Reviewer: strictest -- only read tools, no bash
      // ------------------------------------------------------------------
      if (config.role === "reviewer") {
        if (READ_ONLY_TOOLS.has(toolName)) {
          return checkSandboxForRead(event, sandboxPaths);
        }
        // Block everything else including bash
        return {
          block: true,
          reason: `RBAC: reviewer role cannot use tool "${toolName}". Only read, grep, find, ls are allowed.`,
        };
      }

      // ------------------------------------------------------------------
      // Investigator: read tools + safe bash, no writes
      // ------------------------------------------------------------------
      if (config.role === "investigator") {
        if (READ_ONLY_TOOLS.has(toolName)) {
          return checkSandboxForRead(event, sandboxPaths);
        }
        if (WRITE_TOOLS.has(toolName)) {
          return {
            block: true,
            reason: `RBAC: investigator role cannot use tool "${toolName}". File modifications are not allowed.`,
          };
        }
        if (toolName === "bash") {
          const command = extractBashCommand(event);
          if (!isSafeBashCommand(command)) {
            return {
              block: true,
              reason: `RBAC: investigator role cannot run bash command "${truncate(command, 80)}". Only read-only commands are allowed.`,
            };
          }
          return undefined; // Allow safe bash
        }
        // Unknown tools -- block by default for safety
        return {
          block: true,
          reason: `RBAC: investigator role cannot use unknown tool "${toolName}".`,
        };
      }

      // ------------------------------------------------------------------
      // Developer: all tools, but scoped to sandbox paths
      // ------------------------------------------------------------------
      if (config.role === "developer") {
        if (WRITE_TOOLS.has(toolName)) {
          const filePath = extractFilePath(event);
          if (filePath && !isWithinSandbox(filePath, sandboxPaths)) {
            return {
              block: true,
              reason: `RBAC: developer role cannot write to "${filePath}". Allowed paths: ${sandboxPaths.join(", ") || "(none)"}`,
            };
          }
          return undefined; // Allow within sandbox
        }
        // bash -- check for writes outside sandbox via output redirection
        if (toolName === "bash") {
          const command = extractBashCommand(event);
          // Check for obvious writes outside sandbox
          if (sandboxPaths.length > 0 && containsOutOfSandboxWrite(command, sandboxPaths)) {
            return {
              block: true,
              reason: `RBAC: developer role bash command writes outside sandbox. Allowed paths: ${sandboxPaths.join(", ")}`,
            };
          }
          return undefined; // Allow
        }
        // All other tools (read, grep, find, ls) -- allow
        return undefined;
      }
    });
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function extractBashCommand(event: ToolCallEvent): string {
  if (event.toolName === "bash" && "input" in event) {
    const input = event.input as Record<string, unknown>;
    return (input.command as string) ?? "";
  }
  return "";
}

function extractFilePath(event: ToolCallEvent): string | undefined {
  if ("input" in event) {
    const input = event.input as Record<string, unknown>;
    return (input.file_path as string) ?? (input.path as string) ?? undefined;
  }
  return undefined;
}

function checkSandboxForRead(
  event: ToolCallEvent,
  sandboxPaths: string[],
): ToolCallEventResult | undefined {
  if (sandboxPaths.length === 0) return undefined; // No sandbox restriction

  const filePath = extractFilePath(event);
  if (filePath && !isWithinSandbox(filePath, sandboxPaths)) {
    return {
      block: true,
      reason: `RBAC: cannot read "${filePath}" -- outside sandbox. Allowed paths: ${sandboxPaths.join(", ")}`,
    };
  }
  return undefined;
}

/**
 * Basic heuristic: check if a bash command writes to paths outside sandbox.
 * Catches common patterns like redirects (> /etc/foo) and tee/cp/mv to
 * absolute paths outside sandbox.
 */
function containsOutOfSandboxWrite(
  command: string,
  sandboxPaths: string[],
): boolean {
  // Match output redirections to absolute paths
  const redirectPattern = /(?:>|>>)\s*(\/[^\s;|&]+)/g;
  let match: RegExpExecArray | null;
  while ((match = redirectPattern.exec(command)) !== null) {
    const target = match[1];
    if (!isWithinSandbox(target, sandboxPaths)) return true;
  }

  // Match common write commands targeting absolute paths
  const writeCommands =
    /\b(?:cp|mv|tee|install)\s+(?:-\S+\s+)*\S+\s+(\/[^\s;|&]+)/g;
  while ((match = writeCommands.exec(command)) !== null) {
    const target = match[1];
    if (!isWithinSandbox(target, sandboxPaths)) return true;
  }

  return false;
}

function truncate(s: string, maxLen: number): string {
  return s.length > maxLen ? `${s.slice(0, maxLen)}...` : s;
}

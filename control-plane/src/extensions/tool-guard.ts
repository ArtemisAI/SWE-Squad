/**
 * Tool guard safety extension for pi-agent sessions.
 *
 * Blocks dangerous bash commands (rm -rf, force push, DROP TABLE, etc.)
 * and prevents file writes outside the working directory.
 *
 * Uses the pi-coding-agent ExtensionFactory contract:
 *   pi.on("tool_call", ...) to intercept and block destructive operations.
 */

import type {
  ExtensionFactory,
  ToolCallEvent,
  ToolCallEventResult,
} from "@mariozechner/pi-coding-agent";

// ---------------------------------------------------------------------------
// Destructive command patterns
// ---------------------------------------------------------------------------

/**
 * Patterns that match dangerous bash commands.
 * Each entry includes the regex and a human-readable description.
 */
const DESTRUCTIVE_PATTERNS: Array<{ pattern: RegExp; description: string }> = [
  {
    pattern: /\brm\s+(?:-\S*[rf]\S*\s+|--recursive|--force)/,
    description: "recursive/force file deletion (rm -rf)",
  },
  {
    pattern: /\bgit\s+push\s+(?:.*\s+)?--force(?:-with-lease)?\b/,
    description: "force push (git push --force)",
  },
  {
    pattern: /\bgit\s+reset\s+--hard\b/,
    description: "hard reset (git reset --hard)",
  },
  {
    pattern: /\bgit\s+clean\s+(?:-\S*[fd]\S*)/,
    description: "git clean with force/directory flags",
  },
  {
    pattern: /\bDROP\s+(?:TABLE|DATABASE|SCHEMA|INDEX)\b/i,
    description: "SQL DROP statement",
  },
  {
    pattern: /\bDELETE\s+FROM\b/i,
    description: "SQL DELETE FROM statement",
  },
  {
    pattern: /\bTRUNCATE\s+(?:TABLE\s+)?\w/i,
    description: "SQL TRUNCATE statement",
  },
  {
    pattern: /\bkill\s+-9\b/,
    description: "kill -9 (SIGKILL)",
  },
  {
    pattern: /\bkillall\b/,
    description: "killall command",
  },
  {
    pattern: /\bchmod\s+(?:777|666)\b/,
    description: "overly permissive chmod",
  },
  {
    pattern: /\bdd\s+.*\bof=\/dev\//,
    description: "dd writing to device (of=/dev/...)",
  },
  {
    pattern: /\bmkfs\b/,
    description: "filesystem format command (mkfs)",
  },
  {
    pattern: />\s*\/dev\/sd[a-z]/,
    description: "redirect to block device",
  },
  {
    pattern: /\b:\s*\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:/,
    description: "fork bomb",
  },
  {
    pattern: /\bcurl\b.*\|\s*(?:bash|sh|zsh)\b/,
    description: "pipe curl to shell (curl | bash)",
  },
  {
    pattern: /\bwget\b.*\|\s*(?:bash|sh|zsh)\b/,
    description: "pipe wget to shell (wget | bash)",
  },
];

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

export interface ToolGuardConfig {
  /**
   * Working directory. File writes outside this directory are blocked.
   * Defaults to process.cwd() if not specified.
   */
  cwd?: string;
  /**
   * Additional patterns to block.
   * Each entry should have a `pattern` regex and `description` string.
   */
  extraPatterns?: Array<{ pattern: RegExp; description: string }>;
  /**
   * Patterns to exempt from blocking.
   * Useful for allowing specific commands in controlled environments.
   */
  allowPatterns?: RegExp[];
  /**
   * Called when a command is blocked. Useful for logging/alerting.
   */
  onBlocked?: (toolName: string, reason: string, input: unknown) => void;
}

// ---------------------------------------------------------------------------
// Extension factory
// ---------------------------------------------------------------------------

/**
 * Create a tool guard extension that blocks destructive operations.
 *
 * @example
 * ```typescript
 * const guard = createToolGuardExtension({
 *   cwd: "/home/agent/Projects/my-repo",
 *   onBlocked: (tool, reason) => {
 *     console.warn(`[ToolGuard] Blocked ${tool}: ${reason}`);
 *   },
 * });
 * ```
 */
export function createToolGuardExtension(
  config?: ToolGuardConfig,
): ExtensionFactory {
  return (pi) => {
    const cwd = config?.cwd ?? process.cwd();
    const allPatterns = [
      ...DESTRUCTIVE_PATTERNS,
      ...(config?.extraPatterns ?? []),
    ];
    const allowPatterns = config?.allowPatterns ?? [];

    pi.on("tool_call", (event: ToolCallEvent): ToolCallEventResult | void => {
      // -- Guard bash commands --------------------------------------------
      if (event.toolName === "bash") {
        const command = extractBashCommand(event);
        if (!command) return undefined;

        // Check allow-list first
        if (allowPatterns.some((p) => p.test(command))) {
          return undefined;
        }

        // Check destructive patterns
        for (const { pattern, description } of allPatterns) {
          if (pattern.test(command)) {
            const reason = `Blocked destructive bash command: ${description}`;
            config?.onBlocked?.("bash", reason, event.input);
            return {
              block: true,
              reason: `[ToolGuard] ${reason}. Command: "${truncate(command, 120)}"`,
            };
          }
        }

        return undefined; // Allow
      }

      // -- Guard file writes outside cwd ----------------------------------
      if (event.toolName === "write" || event.toolName === "edit") {
        const filePath = extractFilePath(event);
        if (filePath && !isWithinDirectory(filePath, cwd)) {
          const reason = `File write outside working directory: "${filePath}" is not under "${cwd}"`;
          config?.onBlocked?.(event.toolName, reason, event.input);
          return {
            block: true,
            reason: `[ToolGuard] ${reason}`,
          };
        }
        return undefined;
      }

      // All other tools -- allow
      return undefined;
    });
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function extractBashCommand(event: ToolCallEvent): string {
  if ("input" in event) {
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

/**
 * Check whether a file path is within a directory.
 *
 * Handles both absolute and relative paths. Relative paths are
 * assumed to be within cwd (since the tools resolve them that way).
 */
function isWithinDirectory(filePath: string, directory: string): boolean {
  // Relative paths are resolved against cwd by the tools, so they're safe
  if (!filePath.startsWith("/")) return true;

  const normalizedDir = directory.endsWith("/") ? directory : `${directory}/`;
  return filePath === directory || filePath.startsWith(normalizedDir);
}

function truncate(s: string, maxLen: number): string {
  return s.length > maxLen ? `${s.slice(0, maxLen)}...` : s;
}

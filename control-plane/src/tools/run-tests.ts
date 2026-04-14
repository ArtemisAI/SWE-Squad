/**
 * Tool: run_tests -- Execute tests in a workspace and report results.
 *
 * Auto-detects the test command from package.json scripts, Makefile targets,
 * or pytest convention. Returns structured pass/fail/coverage data.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { SWEContext } from "../shared/context.js";

/** Structured test report parsed from test runner output. */
interface TestReport {
  passed: number;
  failed: number;
  skipped: number;
  total: number;
  coverage: number | null;
  failures: string[];
  success: boolean;
  command: string;
  durationMs: number;
}

/**
 * Auto-detect the test command for a workspace.
 *
 * Checks in order:
 * 1. package.json "test" script -> npm test
 * 2. Makefile with "test" target -> make test
 * 3. pytest.ini / pyproject.toml / tests/ dir -> python3 -m pytest
 * 4. Falls back to "echo 'No test runner detected'"
 */
function detectTestCommand(workspace: string): string {
  // 1. Node.js: package.json
  const pkgPath = join(workspace, "package.json");
  if (existsSync(pkgPath)) {
    try {
      const pkg = JSON.parse(readFileSync(pkgPath, "utf-8"));
      if (pkg.scripts?.test && pkg.scripts.test !== 'echo "Error: no test specified" && exit 1') {
        return "npm test";
      }
      // 1b. Astro/Vite/Next.js: use build as smoke test if no test script
      if (pkg.scripts?.build && !pkg.scripts?.test) {
        return "npm run build";
      }
    } catch { /* fall through */ }
  }

  // 2. Makefile
  const makefilePath = join(workspace, "Makefile");
  if (existsSync(makefilePath)) {
    try {
      const content = readFileSync(makefilePath, "utf-8");
      if (/^test\s*:/m.test(content)) {
        return "make test";
      }
    } catch { /* fall through */ }
  }

  // 3. Python: pytest
  const pytestIndicators = [
    join(workspace, "pytest.ini"),
    join(workspace, "pyproject.toml"),
    join(workspace, "setup.cfg"),
    join(workspace, "tests"),
  ];
  if (pytestIndicators.some((p) => existsSync(p))) {
    return "python3 -m pytest tests/ -v --tb=short";
  }

  return 'echo "No test runner detected"';
}

/**
 * Parse test runner output into a structured TestReport.
 *
 * Handles common formats:
 * - pytest: "X passed, Y failed, Z skipped"
 * - jest/vitest: "Tests: X passed, Y failed, Z total"
 * - tap/mocha: "X passing", "Y failing"
 * - make test: looks for nested test runner output
 */
function parseTestOutput(output: string, exitCode: number): Omit<TestReport, "command" | "durationMs"> {
  let passed = 0;
  let failed = 0;
  let skipped = 0;
  let coverage: number | null = null;
  const failures: string[] = [];

  // pytest format: "5 passed, 2 failed, 1 skipped"
  const pytestMatch = output.match(
    /(\d+)\s+passed(?:.*?(\d+)\s+failed)?(?:.*?(\d+)\s+skipped)?/,
  );
  if (pytestMatch) {
    passed = parseInt(pytestMatch[1], 10);
    failed = pytestMatch[2] ? parseInt(pytestMatch[2], 10) : 0;
    skipped = pytestMatch[3] ? parseInt(pytestMatch[3], 10) : 0;
  }

  // jest/vitest format: "Tests:  5 passed, 2 failed, 7 total"
  const jestMatch = output.match(
    /Tests:\s*(?:(\d+)\s+failed,?\s*)?(?:(\d+)\s+skipped,?\s*)?(?:(\d+)\s+passed,?\s*)?(\d+)\s+total/,
  );
  if (jestMatch && !pytestMatch) {
    failed = jestMatch[1] ? parseInt(jestMatch[1], 10) : 0;
    skipped = jestMatch[2] ? parseInt(jestMatch[2], 10) : 0;
    passed = jestMatch[3] ? parseInt(jestMatch[3], 10) : 0;
  }

  // vitest format: "Tests  916 passed (916)" or "Test Files  21 passed (21)"
  const vitestMatch = output.match(
    /Tests\s+(?:(\d+)\s+failed,?\s*)?(?:(\d+)\s+skipped,?\s*)?(\d+)\s+passed\s+\((\d+)\)/,
  );
  if (vitestMatch && !pytestMatch && !jestMatch) {
    failed = vitestMatch[1] ? parseInt(vitestMatch[1], 10) : 0;
    skipped = vitestMatch[2] ? parseInt(vitestMatch[2], 10) : 0;
    passed = parseInt(vitestMatch[3], 10);
  }

  // mocha/tap format: "X passing", "Y failing"
  const mochaPassMatch = output.match(/(\d+)\s+passing/);
  const mochaFailMatch = output.match(/(\d+)\s+failing/);
  if (mochaPassMatch && !pytestMatch && !jestMatch && !vitestMatch) {
    passed = parseInt(mochaPassMatch[1], 10);
    failed = mochaFailMatch ? parseInt(mochaFailMatch[1], 10) : 0;
  }

  const total = passed + failed + skipped;

  // Coverage: look for percentage patterns
  // pytest-cov: "TOTAL    500    50    90%"
  // jest: "All files | 90.5 |"
  // istanbul: "Statements   : 85.5%"
  const coveragePatterns = [
    /TOTAL\s+\d+\s+\d+\s+(\d+(?:\.\d+)?)%/,
    /All files\s*\|\s*(\d+(?:\.\d+)?)/,
    /Statements\s*:\s*(\d+(?:\.\d+)?)%/,
    /Lines\s*:\s*(\d+(?:\.\d+)?)%/,
    /Coverage:\s*(\d+(?:\.\d+)?)%/i,
  ];
  for (const pat of coveragePatterns) {
    const m = output.match(pat);
    if (m) {
      coverage = parseFloat(m[1]);
      break;
    }
  }

  // Extract failure messages: lines containing FAILED, FAIL, or Error after a test name
  const failLines = output.split("\n").filter((line) => {
    const trimmed = line.trim();
    return (
      /^FAIL(ED)?\s/i.test(trimmed) ||
      /\sFAIL(ED)?$/i.test(trimmed) ||
      /^E\s+/.test(trimmed) ||
      /AssertionError|AssertError|assert.*failed/i.test(trimmed)
    );
  });
  failures.push(...failLines.slice(0, 20).map((l) => l.trim()));

  const success = exitCode === 0 && failed === 0;

  return { passed, failed, skipped, total, coverage, failures, success };
}

export function createRunTestsTool(ctx: SWEContext) {
  return defineTool({
    name: "run_tests",
    label: "Run Tests",
    description:
      "Execute tests in a workspace and return a structured test report. " +
      "Auto-detects the test runner from package.json, Makefile, or pytest convention. " +
      "Updates the ticket with test results.",
    parameters: Type.Object({
      ticketId: Type.String({ description: "The ticket ID to record test results against" }),
      workspace: Type.Optional(
        Type.String({ description: "Workspace path. Falls back to ticket metadata or cwd." }),
      ),
      testCommand: Type.Optional(
        Type.String({ description: "Explicit test command. Auto-detected if omitted." }),
      ),
      timeout: Type.Optional(
        Type.Number({ description: "Timeout in seconds (default 600)", default: 600 }),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _extCtx) {
      const store = ctx.ticketStore;
      if (!store) {
        return {
          content: [{ type: "text" as const, text: "Error: Supabase ticket store not configured" }],
          details: {},
        };
      }

      try {
        // 1. Get ticket
        const ticket = await store.get(params.ticketId);
        if (!ticket) {
          return {
            content: [{ type: "text" as const, text: `Ticket not found: ${params.ticketId}` }],
            details: {},
          };
        }

        // 2. Determine workspace — resolve from ticket repo → config repos → fallback to cwd
        const meta = ticket.metadata as Record<string, unknown>;
        let workspace =
          params.workspace ??
          (meta.workspace as string | undefined) ??
          (meta.workspacePath as string | undefined);

        if (!workspace) {
          const ticketRepo = meta.repo as string | undefined;
          if (ticketRepo) {
            const repoConfig = ctx.config.repos.find(
              (r: Record<string, unknown>) =>
                r.name === ticketRepo || (r.name as string)?.endsWith(`/${ticketRepo.split("/").pop()}`),
            );
            const localPath = repoConfig?.localPath ?? repoConfig?.local_path;
            if (localPath) {
              workspace = String(localPath).replace("~", process.env.HOME ?? "/home/agent");
            }
          }
        }
        workspace ??= ctx.cwd;

        if (!existsSync(workspace)) {
          return {
            content: [{
              type: "text" as const,
              text: `Workspace not found: ${workspace}`,
            }],
            details: {},
          };
        }

        // 3. Determine test command
        const testCommand = params.testCommand ?? detectTestCommand(workspace);
        const timeout = (params.timeout ?? 600) * 1000; // convert to ms

        ctx.logger.info(
          `Running tests for ${params.ticketId} in ${workspace}: ${testCommand}`,
        );

        // 4. Execute tests
        const startMs = Date.now();
        let output = "";
        let exitCode = 0;

        try {
          output = execSync(testCommand, {
            cwd: workspace,
            timeout,
            encoding: "utf-8",
            stdio: ["pipe", "pipe", "pipe"],
            env: { ...process.env, CI: "true", FORCE_COLOR: "0", NO_COLOR: "1" },
          });
        } catch (err: unknown) {
          // Test failures produce non-zero exit codes -- this is expected
          const execErr = err as {
            status?: number;
            stdout?: string;
            stderr?: string;
          };
          exitCode = execErr.status ?? 1;
          output = (execErr.stdout ?? "") + "\n" + (execErr.stderr ?? "");
        }

        const durationMs = Date.now() - startMs;

        // 5. Parse results
        const report: TestReport = {
          ...parseTestOutput(output, exitCode),
          command: testCommand,
          durationMs,
        };

        // 6. Update ticket with test results
        ticket.testResults = {
          passed: report.passed,
          failed: report.failed,
          skipped: report.skipped,
          total: report.total,
          coverage: report.coverage,
          success: report.success,
          command: report.command,
          durationMs: report.durationMs,
          failures: report.failures,
          ranAt: new Date().toISOString(),
        };

        // Update metadata
        const updatedMeta = meta;
        updatedMeta.lastTestRun = new Date().toISOString();
        updatedMeta.testsPassed = report.success;
        ticket.metadata = updatedMeta;

        // Advance status if tests passed and ticket is in testing
        if (report.success && ticket.status === "testing") {
          // Status stays "testing" -- approval tool advances it
        }

        ticket.updatedAt = new Date().toISOString();
        await store.update(ticket);

        // 7. Build result text
        const statusIcon = report.success ? "PASS" : "FAIL";
        const coverageStr =
          report.coverage != null ? `${report.coverage.toFixed(1)}%` : "N/A";
        const failureStr =
          report.failures.length > 0
            ? `\n\nFailures:\n${report.failures.join("\n")}`
            : "";

        return {
          content: [{
            type: "text" as const,
            text: `Tests ${statusIcon} for ${params.ticketId}\n\n` +
              `Command: ${report.command}\n` +
              `Passed: ${report.passed}, Failed: ${report.failed}, Skipped: ${report.skipped}, Total: ${report.total}\n` +
              `Coverage: ${coverageStr}\n` +
              `Duration: ${(report.durationMs / 1000).toFixed(1)}s` +
              failureStr,
          }],
          details: {
            ticketId: params.ticketId,
            success: report.success,
            passed: report.passed,
            failed: report.failed,
            skipped: report.skipped,
            total: report.total,
            coverage: report.coverage,
            durationMs: report.durationMs,
          },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `Test execution failed for ${params.ticketId}: ${err}` }],
          details: {},
        };
      }
    },
  });
}

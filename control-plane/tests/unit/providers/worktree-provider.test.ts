/**
 * Unit tests for WorktreeProvider.
 *
 * All git and filesystem calls are mocked via vi.mock.
 * No actual git commands are executed.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";

// ---------------------------------------------------------------------------
// Mocks — declared before imports that reference them
// ---------------------------------------------------------------------------

vi.mock("node:child_process", () => ({
  execFileSync: vi.fn(),
}));

vi.mock("node:fs", () => ({
  existsSync: vi.fn(() => false),
  mkdirSync: vi.fn(),
  rmSync: vi.fn(),
}));

vi.mock("node:crypto", () => ({
  randomUUID: vi.fn(() => "test-uuid-1234"),
}));

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { randomUUID } from "node:crypto";

import { WorktreeProvider } from "../../../src/providers/workspace/worktree-provider.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const mockExecFileSync = execFileSync as ReturnType<typeof vi.fn>;
const mockExistsSync = existsSync as ReturnType<typeof vi.fn>;
const mockMkdirSync = mkdirSync as ReturnType<typeof vi.fn>;
const mockRmSync = rmSync as ReturnType<typeof vi.fn>;
const mockRandomUUID = randomUUID as ReturnType<typeof vi.fn>;

function createProvider(overrides?: Record<string, unknown>) {
  return new WorktreeProvider({
    baseDir: "/tmp/test-ws",
    repoCwd: "/home/user/repo",
    defaultTimeout: 3600,
    maxConcurrent: 5,
    ...overrides,
  });
}

// =========================================================================
// Tests
// =========================================================================

describe("WorktreeProvider", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockExistsSync.mockReturnValue(false);
    mockExecFileSync.mockReturnValue("");
    mockRandomUUID.mockReturnValue("test-uuid-1234");
  });

  // -----------------------------------------------------------------------
  // Constructor
  // -----------------------------------------------------------------------

  describe("constructor", () => {
    it("has name 'worktree'", () => {
      const provider = createProvider();
      expect(provider.name).toBe("worktree");
    });

    it("uses default config when none provided", () => {
      const provider = new WorktreeProvider();
      expect(provider.name).toBe("worktree");
    });
  });

  // -----------------------------------------------------------------------
  // create()
  // -----------------------------------------------------------------------

  describe("create()", () => {
    it("creates a worktree with auto-generated branch", async () => {
      const provider = createProvider();

      const ws = await provider.create({
        ticketId: "TICKET-42",
        repo: "owner/repo",
      });

      expect(ws.id).toBe("test-uuid-1234");
      expect(ws.ticketId).toBe("TICKET-42");
      expect(ws.branch).toBe("fix/TICKET-42");
      expect(ws.path).toContain("fix-TICKET-42");
      expect(ws.repo).toBe("owner/repo");
      expect(ws.strategy).toBe("worktree");
      expect(ws.status).toBe("active");
      expect(ws.createdAt).toBeDefined();
      expect(ws.expiresAt).toBeDefined();

      // Verify git worktree add was called
      expect(mockExecFileSync).toHaveBeenCalledWith(
        "git",
        ["worktree", "add", expect.stringContaining("fix-TICKET-42"), "-b", "fix/TICKET-42"],
        expect.objectContaining({ cwd: "/home/user/repo" }),
      );
    });

    it("creates a worktree with explicit branch", async () => {
      const provider = createProvider();

      const ws = await provider.create({
        ticketId: "TICKET-42",
        repo: "owner/repo",
        branch: "feat/custom-branch",
      });

      expect(ws.branch).toBe("feat/custom-branch");
      expect(ws.path).toContain("feat-custom-branch");
    });

    it("creates base directory if it does not exist", async () => {
      const provider = createProvider();

      await provider.create({
        ticketId: "TICKET-1",
        repo: "owner/repo",
      });

      expect(mockMkdirSync).toHaveBeenCalledWith(
        "/tmp/test-ws",
        { recursive: true },
      );
    });

    it("sets expiry based on timeout", async () => {
      const provider = createProvider();

      const ws = await provider.create({
        ticketId: "TICKET-1",
        repo: "owner/repo",
        timeout: 7200,
      });

      const created = new Date(ws.createdAt).getTime();
      const expires = new Date(ws.expiresAt!).getTime();
      expect(expires - created).toBe(7200 * 1000);
    });

    it("throws when path already exists", async () => {
      mockExistsSync.mockImplementation((path: string) => {
        if (typeof path === "string" && path.includes("fix-TICKET-1")) return true;
        return false;
      });

      const provider = createProvider();

      await expect(
        provider.create({ ticketId: "TICKET-1", repo: "owner/repo" }),
      ).rejects.toThrow(/already exists/);
    });

    it("throws when max concurrent reached", async () => {
      const provider = createProvider({ maxConcurrent: 2 });

      // Use unique UUIDs for each workspace
      let callCount = 0;
      mockRandomUUID.mockImplementation(() => `uuid-${++callCount}`);

      await provider.create({ ticketId: "T-1", repo: "r", branch: "b1" });
      await provider.create({ ticketId: "T-2", repo: "r", branch: "b2" });

      await expect(
        provider.create({ ticketId: "T-3", repo: "r", branch: "b3" }),
      ).rejects.toThrow(/Maximum concurrent workspaces/);
    });

    it("retries without -b when branch already exists", async () => {
      mockExecFileSync.mockImplementation(
        (cmd: string, args: string[]) => {
          if (cmd === "git" && args.includes("-b")) {
            const err = new Error("fatal: branch already exists") as Error & {
              stderr: Buffer;
            };
            err.stderr = Buffer.from(
              "fatal: a branch named 'fix/T-1' already exists",
            );
            throw err;
          }
          return "";
        },
      );

      const provider = createProvider();
      const ws = await provider.create({
        ticketId: "T-1",
        repo: "owner/repo",
      });

      expect(ws.status).toBe("active");
      // Second call should be without -b
      const calls = mockExecFileSync.mock.calls.filter(
        (c: unknown[]) =>
          c[0] === "git" &&
          (c[1] as string[])[0] === "worktree",
      );
      expect(calls.length).toBe(2);
      expect((calls[1][1] as string[])).not.toContain("-b");
    });

    it("throws on git failure (not branch-exists)", async () => {
      mockExecFileSync.mockImplementation(
        (cmd: string, args: string[]) => {
          if (cmd === "git" && args.includes("worktree")) {
            const err = new Error("fatal: not a git repository") as Error & {
              stderr: Buffer;
            };
            err.stderr = Buffer.from("fatal: not a git repository");
            throw err;
          }
          return "";
        },
      );

      const provider = createProvider();
      await expect(
        provider.create({ ticketId: "T-1", repo: "owner/repo" }),
      ).rejects.toThrow(/not a git repository/);
    });
  });

  // -----------------------------------------------------------------------
  // get()
  // -----------------------------------------------------------------------

  describe("get()", () => {
    it("returns workspace by id", async () => {
      const provider = createProvider();
      const ws = await provider.create({
        ticketId: "T-1",
        repo: "owner/repo",
      });

      const found = await provider.get(ws.id);
      expect(found).not.toBeNull();
      expect(found!.ticketId).toBe("T-1");
    });

    it("returns null for unknown id", async () => {
      const provider = createProvider();
      const found = await provider.get("nonexistent");
      expect(found).toBeNull();
    });
  });

  // -----------------------------------------------------------------------
  // list()
  // -----------------------------------------------------------------------

  describe("list()", () => {
    it("returns empty array initially", async () => {
      const provider = createProvider();
      const list = await provider.list();
      expect(list).toEqual([]);
    });

    it("returns all created workspaces", async () => {
      const provider = createProvider();
      let callCount = 0;
      mockRandomUUID.mockImplementation(() => `uuid-${++callCount}`);

      await provider.create({
        ticketId: "T-1",
        repo: "r",
        branch: "b1",
      });
      await provider.create({
        ticketId: "T-2",
        repo: "r",
        branch: "b2",
      });

      const list = await provider.list();
      expect(list).toHaveLength(2);
      expect(list.map((ws) => ws.ticketId)).toEqual(["T-1", "T-2"]);
    });
  });

  // -----------------------------------------------------------------------
  // cleanup()
  // -----------------------------------------------------------------------

  describe("cleanup()", () => {
    it("removes a tracked workspace", async () => {
      const provider = createProvider();
      const ws = await provider.create({
        ticketId: "T-1",
        repo: "r",
      });

      // Make existsSync return true for the worktree path during cleanup
      mockExistsSync.mockReturnValue(true);

      const result = await provider.cleanup(ws.id);
      expect(result).toBe(true);

      // Verify git worktree remove was called
      expect(mockExecFileSync).toHaveBeenCalledWith(
        "git",
        ["worktree", "remove", ws.path, "--force"],
        expect.objectContaining({ cwd: "/home/user/repo" }),
      );

      // Workspace should no longer be tracked
      const found = await provider.get(ws.id);
      expect(found).toBeNull();
    });

    it("returns false for unknown id", async () => {
      const provider = createProvider();
      const result = await provider.cleanup("nonexistent");
      expect(result).toBe(false);
    });

    it("falls back to rmSync when git worktree remove fails", async () => {
      const provider = createProvider();
      const ws = await provider.create({
        ticketId: "T-1",
        repo: "r",
      });

      mockExistsSync.mockReturnValue(true);
      mockExecFileSync.mockImplementation(
        (cmd: string, args: string[]) => {
          if (
            cmd === "git" &&
            (args as string[]).includes("remove")
          ) {
            throw new Error("not a valid worktree");
          }
          return "";
        },
      );

      const result = await provider.cleanup(ws.id);
      expect(result).toBe(true);
      expect(mockRmSync).toHaveBeenCalledWith(
        ws.path,
        { recursive: true, force: true },
      );
    });

    it("skips filesystem removal when path does not exist", async () => {
      const provider = createProvider();
      const ws = await provider.create({
        ticketId: "T-1",
        repo: "r",
      });

      // Path does not exist during cleanup
      mockExistsSync.mockReturnValue(false);

      const result = await provider.cleanup(ws.id);
      expect(result).toBe(true);

      // Should NOT have called git worktree remove
      const removeCalls = mockExecFileSync.mock.calls.filter(
        (c: unknown[]) =>
          c[0] === "git" &&
          (c[1] as string[]).includes("remove"),
      );
      expect(removeCalls).toHaveLength(0);
    });
  });

  // -----------------------------------------------------------------------
  // cleanupExpired()
  // -----------------------------------------------------------------------

  describe("cleanupExpired()", () => {
    it("cleans up expired workspaces", async () => {
      const provider = createProvider();

      // Create a workspace with 0-second timeout (immediately expired)
      const ws = await provider.create({
        ticketId: "T-1",
        repo: "r",
        timeout: 0,
      });

      const cleaned = await provider.cleanupExpired();
      expect(cleaned).toBe(1);

      const found = await provider.get(ws.id);
      expect(found).toBeNull();
    });

    it("does not clean up non-expired workspaces", async () => {
      const provider = createProvider();
      await provider.create({
        ticketId: "T-1",
        repo: "r",
        timeout: 99999,
      });

      const cleaned = await provider.cleanupExpired();
      expect(cleaned).toBe(0);

      const list = await provider.list();
      expect(list).toHaveLength(1);
    });
  });

  // -----------------------------------------------------------------------
  // healthCheck()
  // -----------------------------------------------------------------------

  describe("healthCheck()", () => {
    it("returns true when git is available", async () => {
      mockExecFileSync.mockReturnValue("git version 2.40.0\n");

      const provider = createProvider();
      const healthy = await provider.healthCheck();
      expect(healthy).toBe(true);
    });

    it("returns false when git is not available", async () => {
      mockExecFileSync.mockImplementation(() => {
        throw new Error("git not found");
      });

      const provider = createProvider();
      const healthy = await provider.healthCheck();
      expect(healthy).toBe(false);
    });
  });
});

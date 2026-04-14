/**
 * Unit tests for LocalProvider.
 *
 * All git and filesystem calls are mocked via vi.mock.
 * No actual commands are executed.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock("node:child_process", () => ({
  execFileSync: vi.fn(),
}));

vi.mock("node:fs", () => ({
  existsSync: vi.fn(() => true),
}));

vi.mock("node:crypto", () => ({
  randomUUID: vi.fn(() => "local-uuid-5678"),
}));

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { randomUUID } from "node:crypto";

import { LocalProvider } from "../../../src/providers/workspace/local-provider.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const mockExecFileSync = execFileSync as ReturnType<typeof vi.fn>;
const mockExistsSync = existsSync as ReturnType<typeof vi.fn>;
const mockRandomUUID = randomUUID as ReturnType<typeof vi.fn>;

function createProvider(overrides?: Record<string, unknown>) {
  return new LocalProvider({
    cwd: "/home/user/project",
    defaultTimeout: 3600,
    ...overrides,
  });
}

// =========================================================================
// Tests
// =========================================================================

describe("LocalProvider", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockExistsSync.mockReturnValue(true);
    mockExecFileSync.mockReturnValue("");
    mockRandomUUID.mockReturnValue("local-uuid-5678");
  });

  // -----------------------------------------------------------------------
  // Constructor
  // -----------------------------------------------------------------------

  describe("constructor", () => {
    it("has name 'local'", () => {
      const provider = createProvider();
      expect(provider.name).toBe("local");
    });

    it("uses default config when none provided", () => {
      const provider = new LocalProvider();
      expect(provider.name).toBe("local");
    });
  });

  // -----------------------------------------------------------------------
  // create()
  // -----------------------------------------------------------------------

  describe("create()", () => {
    it("creates a workspace pointing to cwd", async () => {
      const provider = createProvider();

      const ws = await provider.create({
        ticketId: "TICKET-99",
        repo: "org/project",
      });

      expect(ws.id).toBe("local-uuid-5678");
      expect(ws.ticketId).toBe("TICKET-99");
      expect(ws.path).toBe("/home/user/project");
      expect(ws.branch).toBe("fix/TICKET-99");
      expect(ws.repo).toBe("org/project");
      expect(ws.strategy).toBe("local");
      expect(ws.status).toBe("active");
    });

    it("creates git branch in the working directory", async () => {
      const provider = createProvider();

      await provider.create({
        ticketId: "T-1",
        repo: "r",
        branch: "feat/my-branch",
      });

      expect(mockExecFileSync).toHaveBeenCalledWith(
        "git",
        ["checkout", "-b", "feat/my-branch"],
        expect.objectContaining({ cwd: "/home/user/project" }),
      );
    });

    it("falls back to checkout (no -b) when branch exists", async () => {
      // First call (checkout -b) throws, second (checkout) succeeds
      mockExecFileSync
        .mockImplementationOnce(() => {
          throw new Error("branch already exists");
        })
        .mockReturnValueOnce("");

      const provider = createProvider();
      const ws = await provider.create({
        ticketId: "T-1",
        repo: "r",
        branch: "existing-branch",
      });

      expect(ws.status).toBe("active");
      expect(mockExecFileSync).toHaveBeenCalledWith(
        "git",
        ["checkout", "existing-branch"],
        expect.objectContaining({ cwd: "/home/user/project" }),
      );
    });

    it("succeeds even when not a git repo (no branch created)", async () => {
      mockExecFileSync.mockImplementation(() => {
        throw new Error("not a git repository");
      });

      const provider = createProvider();
      const ws = await provider.create({
        ticketId: "T-1",
        repo: "r",
      });

      // Should still succeed — local provider is a passthrough
      expect(ws.status).toBe("active");
      expect(ws.branch).toBe("fix/T-1");
    });

    it("uses baseDir from opts when provided", async () => {
      const provider = createProvider();

      const ws = await provider.create({
        ticketId: "T-1",
        repo: "r",
        baseDir: "/custom/path",
      });

      expect(ws.path).toBe("/custom/path");
    });

    it("sets expiry based on timeout", async () => {
      const provider = createProvider();

      const ws = await provider.create({
        ticketId: "T-1",
        repo: "r",
        timeout: 1800,
      });

      const created = new Date(ws.createdAt).getTime();
      const expires = new Date(ws.expiresAt!).getTime();
      expect(expires - created).toBe(1800 * 1000);
    });

    it("skips branch creation when path does not exist", async () => {
      mockExistsSync.mockReturnValue(false);
      const provider = createProvider();

      const ws = await provider.create({
        ticketId: "T-1",
        repo: "r",
      });

      expect(ws.status).toBe("active");
      // git checkout should NOT have been called
      expect(mockExecFileSync).not.toHaveBeenCalled();
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
        repo: "r",
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

      await provider.create({ ticketId: "T-1", repo: "r", branch: "b1" });
      await provider.create({ ticketId: "T-2", repo: "r", branch: "b2" });

      const list = await provider.list();
      expect(list).toHaveLength(2);
    });
  });

  // -----------------------------------------------------------------------
  // cleanup()
  // -----------------------------------------------------------------------

  describe("cleanup()", () => {
    it("removes workspace from tracking without deleting directory", async () => {
      const provider = createProvider();
      const ws = await provider.create({
        ticketId: "T-1",
        repo: "r",
      });

      const result = await provider.cleanup(ws.id);
      expect(result).toBe(true);

      // Should NOT call git or rmSync — local provider doesn't delete
      const gitCalls = mockExecFileSync.mock.calls.filter(
        (c: unknown[]) =>
          c[0] === "git" &&
          (c[1] as string[]).includes("remove"),
      );
      expect(gitCalls).toHaveLength(0);

      const found = await provider.get(ws.id);
      expect(found).toBeNull();
    });

    it("returns false for unknown id", async () => {
      const provider = createProvider();
      const result = await provider.cleanup("nonexistent");
      expect(result).toBe(false);
    });
  });

  // -----------------------------------------------------------------------
  // cleanupExpired()
  // -----------------------------------------------------------------------

  describe("cleanupExpired()", () => {
    it("cleans up expired workspaces", async () => {
      const provider = createProvider();

      await provider.create({
        ticketId: "T-1",
        repo: "r",
        timeout: 0,
      });

      const cleaned = await provider.cleanupExpired();
      expect(cleaned).toBe(1);

      const list = await provider.list();
      expect(list).toHaveLength(0);
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
    it("always returns true", async () => {
      const provider = createProvider();
      const healthy = await provider.healthCheck();
      expect(healthy).toBe(true);
    });
  });
});

/**
 * Engine registry with factory pattern.
 *
 * Provides a central registry for CodingEngine factories so engines
 * can be resolved by name from configuration without hardcoded imports
 * in core code.
 *
 * Built-in engines are registered at module load time. Additional engines
 * can be registered at runtime via registerEngine().
 */

import { resolve } from "node:path";

import type { CodingEngine, EngineResult, RunOptions } from "./base.js";
import { ClaudeCliEngine } from "./claude-cli.js";
import { SshCliEngine } from "./ssh-cli.js";

// ---------------------------------------------------------------------------
// Factory type
// ---------------------------------------------------------------------------

export type EngineFactory = (config: Record<string, unknown>) => CodingEngine;

// ---------------------------------------------------------------------------
// Registry (module-private)
// ---------------------------------------------------------------------------

const registry = new Map<string, EngineFactory>();

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Register an engine factory under a given name.
 *
 * Overwrites any existing factory with the same name (allows overriding
 * built-in defaults).
 */
export function registerEngine(name: string, factory: EngineFactory): void {
  registry.set(name, factory);
}

/**
 * Resolve an engine by name, instantiating it via the registered factory.
 *
 * @throws Error if no factory is registered for the given name.
 */
export function resolveEngine(
  name: string,
  config?: Record<string, unknown>,
): CodingEngine {
  const factory = registry.get(name);
  if (!factory) {
    throw new Error(
      `Engine "${name}" is not registered. Available: ${listEngines().join(", ") || "(none)"}`,
    );
  }
  return factory(config ?? {});
}

/**
 * List all registered engine names.
 */
export function listEngines(): string[] {
  return Array.from(registry.keys());
}

/**
 * Check whether an engine name has been registered.
 */
export function hasEngine(name: string): boolean {
  return registry.has(name);
}

// ---------------------------------------------------------------------------
// Built-in engine factories
// ---------------------------------------------------------------------------

/**
 * Factory for ClaudeCliEngine. Passes config through to the constructor.
 */
function claudeCliFactory(config: Record<string, unknown>): CodingEngine {
  return new ClaudeCliEngine({
    binary: config.binary as string | undefined,
    defaultModel: config.defaultModel as string | undefined,
    defaultTimeout: config.defaultTimeout as number | undefined,
    allowedTools: config.allowedTools as string | undefined,
    permissionMode: config.permissionMode as
      | "strict"
      | "auto"
      | "bypass"
      | undefined,
  });
}

/**
 * Placeholder engine that identifies itself but throws on run().
 *
 * Used for engines that are registered for discovery / listing purposes
 * but whose runtimes are not yet available.
 */
class PlaceholderEngine implements CodingEngine {
  readonly name: string;

  constructor(name: string) {
    this.name = name;
  }

  async run(_prompt: string, _options?: RunOptions): Promise<EngineResult> {
    throw new Error(
      `Engine "${this.name}" is registered but not configured. ` +
        `Install the required runtime and provide configuration to use it.`,
    );
  }

  async healthCheck(): Promise<boolean> {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Built-in registrations (executed at module load time)
// ---------------------------------------------------------------------------

registerEngine("claude-cli", claudeCliFactory);

// SSH CLI engines — dispatch to remote VMs via SSH
function sshCliFactory(config: Record<string, unknown>): CodingEngine {
  return new SshCliEngine({
    sshAlias: (config.sshAlias as string) ?? "localhost",
    remoteBinary: config.remoteBinary as string | undefined,
    defaultModel: config.defaultModel as string | undefined,
    defaultTimeout: config.defaultTimeout as number | undefined,
    remoteCwd: config.remoteCwd as string | undefined,
    sshConfig: config.sshConfig as string | undefined,
  });
}

// Register SSH engines for configured squads (use workers SSH config for correct IPs)
const defaultSshConfig = process.env.SWE_SSH_CONFIG
  ?? resolve(process.cwd(), "config/ssh_workers.conf");
registerEngine("ssh-cli-beta", (cfg) =>
  sshCliFactory({ sshAlias: cfg.sshAlias as string ?? "squad-beta", sshConfig: defaultSshConfig, ...cfg }),
);
registerEngine("ssh-cli-gamma", (cfg) =>
  sshCliFactory({ sshAlias: cfg.sshAlias as string ?? "squad-gamma", sshConfig: defaultSshConfig, ...cfg }),
);

// Generic SSH engine — specify sshAlias in delegation config
registerEngine("ssh-cli", sshCliFactory);

// Additional engines can be registered at runtime via registerEngine().
// See docs/pi-dev/11-swe-manager-v2-architecture.md for the engine-agnostic
// delegation model. Engine config lives in swe_team.yaml under `delegation:`.

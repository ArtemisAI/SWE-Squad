/**
 * Engine resolver — reads delegation config to resolve CodingEngine by role.
 *
 * Tools call resolveEngineForRole("investigator", config) and get back
 * whatever engine the config says to use. No hardcoded engine references.
 */

import type { SWETeamConfig } from "../config/schemas.js";
import type { CodingEngine } from "../providers/engine/base.js";
import { resolveEngine } from "../providers/engine/registry.js";

/**
 * Resolve the CodingEngine for a given delegation role.
 *
 * Reads `config.delegation[role].engine` and resolves it from the registry.
 * Falls back to "claude-cli" if no delegation config exists for the role.
 *
 * @throws Error if the engine name is not registered.
 */
export function resolveEngineForRole(
  role: string,
  config: SWETeamConfig,
): CodingEngine {
  const entry = config.delegation[role];
  const engineName = entry?.engine ?? "claude-cli";
  return resolveEngine(engineName);
}

/**
 * Per-role defaults when no delegation config exists.
 *
 * Each role has sensible defaults for engine, readOnly, and timeout.
 * The "reviewer" role defaults to readOnly: true since reviews should
 * never modify files.
 */
const ROLE_DEFAULTS: Record<string, { engine: string; readOnly: boolean; timeout: number }> = {
  investigator: { engine: "claude-cli", readOnly: true, timeout: 1800 },
  developer: { engine: "claude-cli", readOnly: false, timeout: 3600 },
  reviewer: { engine: "claude-cli", readOnly: true, timeout: 1200 },
};

const FALLBACK_DEFAULT = { engine: "claude-cli", readOnly: false, timeout: 1800 };

/**
 * Get the delegation config for a role, with defaults.
 */
export function getDelegationConfig(role: string, config: SWETeamConfig) {
  return config.delegation[role] ?? ROLE_DEFAULTS[role] ?? FALLBACK_DEFAULT;
}

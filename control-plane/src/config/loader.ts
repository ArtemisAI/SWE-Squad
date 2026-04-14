/**
 * Configuration loader for SWE-Squad control plane.
 *
 * Reads YAML, converts snake_case keys to camelCase, validates via Zod,
 * and applies environment variable overrides -- mirroring the Python
 * ``load_config()`` in ``src/swe_team/config.py``.
 */

import { readFileSync } from "node:fs";
import YAML from "yaml";
import { SWETeamConfigSchema, type SWETeamConfig } from "./schemas.js";

// ---------------------------------------------------------------------------
// snake_case -> camelCase key converter
// ---------------------------------------------------------------------------

/**
 * Convert a single snake_case string to camelCase.
 *
 * Examples:
 *   "max_open_critical"  -> "maxOpenCritical"
 *   "a2a_hub_url"        -> "a2aHubUrl"
 *   "already_camel"      -> "alreadyCamel"  (no-op on segments without _)
 */
function snakeToCamelKey(key: string): string {
  return key.replace(/_([a-z0-9])/g, (_match, char: string) =>
    char.toUpperCase(),
  );
}

/**
 * Recursively convert all object keys from snake_case to camelCase.
 *
 * - Arrays are traversed element-by-element.
 * - Primitives (string, number, boolean, null) pass through unchanged.
 * - Preserves Record<string, T> entries (e.g. teams, workerModuleMap)
 *   by converting *their* keys too -- YAML files use snake_case
 *   everywhere, including map keys like team names, but team name keys
 *   (e.g. "alpha", "beta") don't contain underscores so they survive
 *   the transform untouched.
 */
export function snakeToCamel(obj: unknown): unknown {
  if (Array.isArray(obj)) {
    return obj.map(snakeToCamel);
  }

  if (obj !== null && typeof obj === "object") {
    const result: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
      result[snakeToCamelKey(key)] = snakeToCamel(value);
    }
    return result;
  }

  // Primitives
  return obj;
}

// ---------------------------------------------------------------------------
// Truthy-string helper
// ---------------------------------------------------------------------------

function isTruthy(value: string): boolean {
  return ["true", "1", "yes"].includes(value.toLowerCase());
}

// ---------------------------------------------------------------------------
// loadConfig
// ---------------------------------------------------------------------------

/**
 * Load and validate SWE-Squad configuration.
 *
 * Resolution order for the YAML path:
 *   1. Explicit `path` argument
 *   2. `SWE_TEAM_CONFIG` environment variable
 *   3. `config/swe_team.yaml` (default)
 *
 * After parsing the YAML file (or falling back to defaults when the
 * file is missing), the following env-var overrides are applied:
 *
 * | Env var               | Config field           |
 * |-----------------------|------------------------|
 * | SWE_TEAM_ENABLED      | enabled                |
 * | SWE_TEAM_ID           | teamId                 |
 * | SWE_GITHUB_ACCOUNT    | githubAccount          |
 * | T1_MODEL / SWE_MODEL_T1 | models.t1Heavy      |
 * | T2_MODEL / SWE_MODEL_T2 | models.t2Standard   |
 * | T3_MODEL / SWE_MODEL_T3 | models.t3Fast       |
 *
 * @throws {Error} If the YAML file exists but fails Zod validation.
 */
export function loadConfig(path?: string): SWETeamConfig {
  const configPath =
    path ?? process.env.SWE_TEAM_CONFIG ?? "config/swe_team.yaml";

  let raw: unknown = {};

  try {
    const contents = readFileSync(configPath, "utf-8");
    raw = YAML.parse(contents) ?? {};
  } catch (err: unknown) {
    // File not found is fine -- fall back to defaults.
    if (
      err instanceof Error &&
      "code" in err &&
      (err as NodeJS.ErrnoException).code === "ENOENT"
    ) {
      raw = {};
    } else {
      throw err;
    }
  }

  // Convert every key from snake_case -> camelCase before validation
  const camelCased = snakeToCamel(raw) as Record<string, unknown>;

  // Teams in YAML are a map where the key IS the team name:
  //   teams:
  //     alpha:
  //       vm: swe-squad-1
  // The Zod schema requires `name` inside each team object, so inject it.
  if (
    camelCased.teams &&
    typeof camelCased.teams === "object" &&
    !Array.isArray(camelCased.teams)
  ) {
    const teams = camelCased.teams as Record<string, unknown>;
    for (const [key, value] of Object.entries(teams)) {
      if (value && typeof value === "object" && !Array.isArray(value)) {
        (value as Record<string, unknown>).name ??= key;
      }
    }
  }

  const result = SWETeamConfigSchema.safeParse(camelCased);
  if (!result.success) {
    const issues = result.error.issues
      .map((i) => `  ${i.path.join(".")}: ${i.message}`)
      .join("\n");
    throw new Error(`Invalid SWE team config at ${configPath}:\n${issues}`);
  }

  const config = result.data;

  // ----- Environment variable overrides -----

  const envEnabled = process.env.SWE_TEAM_ENABLED;
  if (envEnabled !== undefined) {
    config.enabled = isTruthy(envEnabled);
  }

  const envTeamId = process.env.SWE_TEAM_ID;
  if (envTeamId) {
    config.teamId = envTeamId;
  }

  const envGhAccount = process.env.SWE_GITHUB_ACCOUNT;
  if (envGhAccount) {
    config.githubAccount = envGhAccount;
  }

  // Model tier overrides: SWE_MODEL_T* takes precedence over T*_MODEL
  const envT1 = process.env.SWE_MODEL_T1 ?? process.env.T1_MODEL;
  if (envT1) {
    config.models.t1Heavy = envT1;
  }

  const envT2 = process.env.SWE_MODEL_T2 ?? process.env.T2_MODEL;
  if (envT2) {
    config.models.t2Standard = envT2;
  }

  const envT3 = process.env.SWE_MODEL_T3 ?? process.env.T3_MODEL;
  if (envT3) {
    config.models.t3Fast = envT3;
  }

  // ----- V2: Notification provider env overrides -----

  const envTelegramToken = process.env.TELEGRAM_BOT_TOKEN;
  if (envTelegramToken) {
    config.notification.telegram.botToken = envTelegramToken;
  }

  const envTelegramChat = process.env.TELEGRAM_CHAT_ID;
  if (envTelegramChat) {
    config.notification.telegram.chatId = envTelegramChat;
  }

  // ----- V2: Daemon heartbeat interval override -----

  const envHeartbeat = process.env.SWE_HEARTBEAT_INTERVAL;
  if (envHeartbeat) {
    const parsed = parseInt(envHeartbeat, 10);
    if (!isNaN(parsed) && parsed > 0) {
      config.daemon.heartbeatIntervalSeconds = parsed;
    }
  }

  return config;
}

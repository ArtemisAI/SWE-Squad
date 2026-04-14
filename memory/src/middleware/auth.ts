/**
 * Auth middleware for the SWE-Squad Memory Service.
 *
 * Behaviour:
 *  - Localhost callers (127.0.0.1 / ::1) are always allowed; teamId is
 *    resolved from query string, request body, SWE_TEAM_ID env var, or
 *    falls back to 'default'.
 *  - Non-localhost callers:
 *      • If MEMORY_API_KEY is set: require `Authorization: Bearer <key>`.
 *        Mismatch → 401.
 *      • If MEMORY_API_KEY is NOT set: allow unconditionally (no auth
 *        configured — dev/open deployment).
 *    In both allowed cases teamId is resolved the same way as localhost.
 *
 * The resolved `teamId` is written to `req.teamId` for downstream handlers.
 */

import type { Request, Response, NextFunction } from 'express';

// ---------------------------------------------------------------------------
// Augment Express Request with teamId
// ---------------------------------------------------------------------------
declare global {
  namespace Express {
    interface Request {
      teamId: string;
    }
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Returns true when the request originates from the loopback interface.
 * Express normalises IPv4-mapped IPv6 addresses, but we handle the common
 * forms explicitly to be safe.
 */
function isLocalhost(req: Request): boolean {
  const ip = req.ip ?? '';
  return (
    ip === '127.0.0.1' ||
    ip === '::1' ||
    ip === '::ffff:127.0.0.1'
  );
}

/**
 * Resolve teamId from the request, environment, or default sentinel.
 * Priority: query param → body field → SWE_TEAM_ID env var → 'default'
 */
function resolveTeamId(req: Request): string {
  const fromQuery = req.query['teamId'];
  if (typeof fromQuery === 'string' && fromQuery.trim() !== '') {
    return fromQuery.trim();
  }

  // req.body is populated by express.json() or express.urlencoded() before
  // this middleware runs; guard against it being undefined.
  const fromBody = (req.body as Record<string, unknown> | undefined)?.['teamId'];
  if (typeof fromBody === 'string' && fromBody.trim() !== '') {
    return fromBody.trim();
  }

  const fromEnv = process.env['SWE_TEAM_ID'];
  if (typeof fromEnv === 'string' && fromEnv.trim() !== '') {
    return fromEnv.trim();
  }

  return 'default';
}

// ---------------------------------------------------------------------------
// Middleware
// ---------------------------------------------------------------------------

export function authMiddleware(req: Request, res: Response, next: NextFunction): void {
  // --- Localhost: always pass ---
  if (isLocalhost(req)) {
    req.teamId = resolveTeamId(req);
    next();
    return;
  }

  // --- Non-localhost: optional API key enforcement ---
  const configuredKey = process.env['MEMORY_API_KEY'];

  if (configuredKey) {
    // Auth is required — validate the Bearer token.
    const authHeader = req.headers['authorization'] ?? '';
    const match = /^Bearer\s+(.+)$/i.exec(authHeader);
    const providedKey = match?.[1] ?? '';

    if (providedKey !== configuredKey) {
      res.status(401).json({ error: 'Unauthorized' });
      return;
    }
  }
  // If MEMORY_API_KEY is not set, skip auth entirely (open deployment).

  req.teamId = resolveTeamId(req);
  next();
}

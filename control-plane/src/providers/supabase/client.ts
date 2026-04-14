/**
 * Lightweight PostgREST HTTP client for Supabase — native fetch only.
 *
 * Ported from: src/swe_team/supabase_store.py (the _request() method)
 *
 * Uses native `fetch()` with `AbortSignal.timeout()` for timeouts.
 * Returns parsed JSON. Throws on non-2xx with a descriptive error.
 */

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface SupabaseClientConfig {
  /** Supabase project URL, e.g. "http://your-supabase-host:8000" */
  url: string;
  /** Anon or service-role key */
  key: string;
  /** Request timeout in ms (default 15 000) */
  timeout?: number;
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

export class SupabaseError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: string,
    public readonly method: string,
    public readonly path: string,
  ) {
    super(message);
    this.name = "SupabaseError";
  }
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

export class SupabaseClient {
  private readonly restUrl: string;
  private readonly headers: Record<string, string>;
  private readonly timeout: number;

  constructor(config: SupabaseClientConfig) {
    const baseUrl = config.url.replace(/\/+$/, "");
    this.restUrl = `${baseUrl}/rest/v1`;
    this.timeout = config.timeout ?? 15_000;
    this.headers = {
      apikey: config.key,
      Authorization: `Bearer ${config.key}`,
      "Content-Type": "application/json",
    };
  }

  // -----------------------------------------------------------------------
  // Generic CRUD
  // -----------------------------------------------------------------------

  /**
   * GET /{table}?{params} — query rows.
   *
   * PostgREST filter syntax: `{ status: "eq.open", team_id: "eq.default" }`
   */
  async query<T = Record<string, unknown>>(
    table: string,
    params?: Record<string, string>,
  ): Promise<T[]> {
    const url = this.buildUrl(`/${table}`, params);
    const res = await this.fetch(url, {
      method: "GET",
      headers: { ...this.headers, Prefer: "return=representation" },
    });
    return (await this.parseResponse(res, "GET", `/${table}`)) as T[];
  }

  /**
   * POST /{table} — insert row(s).
   *
   * Sends `Prefer: return=representation,resolution=merge-duplicates`
   * so inserts act as upserts when the table has a unique constraint.
   */
  async insert(
    table: string,
    data: Record<string, unknown>,
  ): Promise<Record<string, unknown>[]> {
    const url = this.buildUrl(`/${table}`);
    const res = await this.fetch(url, {
      method: "POST",
      headers: {
        ...this.headers,
        Prefer: "return=representation,resolution=merge-duplicates",
      },
      body: JSON.stringify(data),
    });
    return (await this.parseResponse(
      res,
      "POST",
      `/${table}`,
    )) as Record<string, unknown>[];
  }

  /**
   * PATCH /{table}?{filter} — update matching rows.
   *
   * Filter uses PostgREST syntax: `{ ticket_id: "eq.abc123" }`.
   */
  async update(
    table: string,
    filter: Record<string, string>,
    data: Record<string, unknown>,
  ): Promise<Record<string, unknown>[]> {
    const url = this.buildUrl(`/${table}`, filter);
    const res = await this.fetch(url, {
      method: "PATCH",
      headers: { ...this.headers, Prefer: "return=representation" },
      body: JSON.stringify(data),
    });
    return (await this.parseResponse(
      res,
      "PATCH",
      `/${table}`,
    )) as Record<string, unknown>[];
  }

  /**
   * DELETE /{table}?{filter} — delete matching rows.
   */
  async delete(
    table: string,
    filter: Record<string, string>,
  ): Promise<void> {
    const url = this.buildUrl(`/${table}`, filter);
    const res = await this.fetch(url, {
      method: "DELETE",
      headers: this.headers,
    });
    if (!res.ok) {
      const body = await res.text();
      throw new SupabaseError(
        `DELETE /${table} failed (${res.status}): ${body.slice(0, 500)}`,
        res.status,
        body,
        "DELETE",
        `/${table}`,
      );
    }
  }

  /**
   * POST /rpc/{fnName} — call a Postgres function.
   */
  async rpc<T = unknown>(
    fnName: string,
    params?: Record<string, unknown>,
  ): Promise<T> {
    const url = this.buildUrl(`/rpc/${fnName}`);
    const res = await this.fetch(url, {
      method: "POST",
      headers: { ...this.headers, Prefer: "return=representation" },
      body: params != null ? JSON.stringify(params) : undefined,
    });
    return (await this.parseResponse(
      res,
      "POST",
      `/rpc/${fnName}`,
    )) as T;
  }

  /**
   * Lightweight health check — GET / with timeout.
   *
   * Returns `true` if the response is 2xx or 401 (auth required but service
   * is alive). Returns `false` on network errors or non-2xx/401 statuses.
   */
  async healthCheck(): Promise<boolean> {
    try {
      const baseUrl = this.restUrl.replace(/\/rest\/v1$/, "");
      const res = await fetch(baseUrl, {
        method: "GET",
        headers: this.headers,
        signal: AbortSignal.timeout(this.timeout),
      });
      // 2xx = healthy, 401 = auth required but service is up
      return res.ok || res.status === 401;
    } catch {
      return false;
    }
  }

  // -----------------------------------------------------------------------
  // Internal helpers
  // -----------------------------------------------------------------------

  /**
   * Build a full URL from a path and optional query parameters.
   *
   * PostgREST filter values may contain characters like `(`, `)`, `.`, and `,`
   * that must NOT be percent-encoded, so we build the query string manually
   * (matching the Python urllib.parse.urlencode safe=".,()!" behaviour).
   */
  private buildUrl(path: string, params?: Record<string, string>): string {
    let url = `${this.restUrl}${path}`;
    if (params && Object.keys(params).length > 0) {
      const qs = Object.entries(params)
        .map(([k, v]) => `${encodeURIComponent(k)}=${this.encodePostgREST(v)}`)
        .join("&");
      url += `?${qs}`;
    }
    return url;
  }

  /**
   * Encode a PostgREST value, preserving the characters `.`, `,`, `(`, `)`, `!`
   * that PostgREST requires to be literal in filter expressions.
   */
  private encodePostgREST(value: string): string {
    return encodeURIComponent(value).replace(
      /%2C|%2E|%28|%29|%21/gi,
      (match) => decodeURIComponent(match),
    );
  }

  /**
   * Wrapper around native fetch with timeout via AbortSignal.
   */
  private fetch(url: string, init: RequestInit): Promise<Response> {
    return fetch(url, {
      ...init,
      signal: AbortSignal.timeout(this.timeout),
    });
  }

  /**
   * Parse a fetch response, throwing a descriptive error on non-2xx.
   */
  private async parseResponse(
    res: Response,
    method: string,
    path: string,
  ): Promise<unknown> {
    if (!res.ok) {
      const body = await res.text();
      throw new SupabaseError(
        `${method} ${path} failed (${res.status}): ${body.slice(0, 500)}`,
        res.status,
        body,
        method,
        path,
      );
    }

    const text = await res.text();
    if (!text) return null;
    return JSON.parse(text);
  }
}

/**
 * Telegram Bot API client -- stdlib-only, zero external dependencies.
 *
 * Uses native `fetch()` for HTTP.  Includes per-type rate limiting
 * (5-minute cooldown) and a global per-minute cap to prevent spam.
 *
 * All public functions are best-effort: they return `true` / `false`
 * (or `null` for data-returning helpers) and never throw.
 *
 * Ported from: src/swe_team/telegram.py
 */

// ---------------------------------------------------------------------------
// Rate limiting state (module-level, resets on process restart)
// ---------------------------------------------------------------------------

const COOLDOWN_SECONDS = 300; // 5-minute per-type cooldown
const MAX_PER_MINUTE = 6; // hard cap regardless of type
const recentSends: number[] = []; // timestamps (Date.now())
const lastSentByType: Map<string, number> = new Map();

// ---------------------------------------------------------------------------
// Config interface
// ---------------------------------------------------------------------------

export interface TelegramConfig {
  /** Telegram bot token.  Falls back to TELEGRAM_BOT_TOKEN env. */
  botToken?: string;
  /** Default chat ID.  Falls back to TELEGRAM_CHAT_ID env. */
  chatId?: string;
}

// ---------------------------------------------------------------------------
// Credentials
// ---------------------------------------------------------------------------

function getCredentials(config?: TelegramConfig): {
  botToken: string | null;
  chatId: string | null;
} {
  return {
    botToken: config?.botToken || process.env.TELEGRAM_BOT_TOKEN || null,
    chatId: config?.chatId || process.env.TELEGRAM_CHAT_ID || null,
  };
}

// ---------------------------------------------------------------------------
// Rate limiting
// ---------------------------------------------------------------------------

/**
 * Check whether a message of the given alert type should be suppressed.
 *
 * Two independent checks:
 *  1. Per-type cooldown -- same `alertType` cannot fire within
 *     {@link COOLDOWN_SECONDS} of the previous send.
 *  2. Global per-minute cap -- at most {@link MAX_PER_MINUTE} messages
 *     in any rolling 60-second window.
 */
function isRateLimited(alertType?: string): boolean {
  const now = Date.now();

  // Per-type cooldown
  if (alertType) {
    const last = lastSentByType.get(alertType);
    if (last !== undefined && now - last < COOLDOWN_SECONDS * 1000) {
      return true;
    }
  }

  // Global per-minute cap
  const cutoff = now - 60_000;
  // Prune old entries in-place
  while (recentSends.length > 0 && recentSends[0] <= cutoff) {
    recentSends.shift();
  }
  if (recentSends.length >= MAX_PER_MINUTE) {
    return true;
  }

  return false;
}

/**
 * Record a successful send, updating both per-type and global trackers.
 */
function recordSend(alertType?: string): void {
  const now = Date.now();
  recentSends.push(now);
  if (alertType) {
    lastSentByType.set(alertType, now);
  }
}

// ---------------------------------------------------------------------------
// Core API helper
// ---------------------------------------------------------------------------

const API_BASE = "https://api.telegram.org";

/**
 * Make a JSON POST request to the Telegram Bot API.
 *
 * @param method  Telegram API method name (e.g. "sendMessage").
 * @param payload JSON-serialisable request body.
 * @param token   Bot token override.  If omitted, reads from env.
 *
 * @returns The parsed `result` field on success, or `null` on failure.
 *          Never throws.
 */
async function apiRequest(
  method: string,
  payload: Record<string, unknown>,
  token?: string,
): Promise<Record<string, unknown> | null> {
  const resolvedToken = token || process.env.TELEGRAM_BOT_TOKEN;
  if (!resolvedToken) {
    return null;
  }

  const url = `${API_BASE}/bot${resolvedToken}/${method}`;

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10_000),
    });

    if (!response.ok) {
      return null;
    }

    const body = (await response.json()) as {
      ok?: boolean;
      result?: Record<string, unknown>;
    };

    if (body.ok) {
      return body.result ?? null;
    }
    return null;
  } catch {
    // Network error, timeout, JSON parse failure, etc.
    return null;
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Send a text message via the Telegram Bot API.
 *
 * @returns `true` if the message was sent, `false` otherwise.
 *          Never throws.
 */
export async function sendMessage(
  text: string,
  options?: {
    parseMode?: string;
    chatId?: string;
    replyToMessageId?: number;
    config?: TelegramConfig;
  },
): Promise<boolean> {
  const { botToken, chatId: defaultChatId } = getCredentials(options?.config);
  const targetChat = options?.chatId || defaultChatId;

  if (!botToken || !targetChat) {
    return false;
  }

  const payload: Record<string, unknown> = {
    chat_id: targetChat,
    text,
    parse_mode: options?.parseMode ?? "HTML",
  };

  if (options?.replyToMessageId !== undefined) {
    payload.reply_to_message_id = options.replyToMessageId;
  }

  const result = await apiRequest("sendMessage", payload, botToken ?? undefined);
  return result !== null;
}

/**
 * Edit an existing message in-place (live status updates).
 *
 * @returns `true` on success, `false` otherwise.
 */
export async function editMessage(
  chatId: string,
  messageId: number,
  newText: string,
  options?: {
    parseMode?: string;
    config?: TelegramConfig;
  },
): Promise<boolean> {
  const { botToken } = getCredentials(options?.config);

  const payload: Record<string, unknown> = {
    chat_id: chatId,
    message_id: messageId,
    text: newText,
    parse_mode: options?.parseMode ?? "HTML",
  };

  const result = await apiRequest("editMessageText", payload, botToken ?? undefined);
  return result !== null;
}

/**
 * Send a rate-limited notification.
 *
 * Wraps {@link sendMessage} with per-type rate limiting so that the
 * same alert type cannot fire more than once per 5 minutes, and the
 * global message rate stays under 6/min.
 *
 * @param alertType  A short key identifying the notification type
 *                   (e.g. "circuit_breaker", "stalled_ticket").
 * @param text       The message body (may contain HTML).
 *
 * @returns `true` if sent, `false` if suppressed or failed.
 */
export async function sendNotification(
  alertType: string,
  text: string,
  options?: { config?: TelegramConfig },
): Promise<boolean> {
  if (isRateLimited(alertType)) {
    return false;
  }

  const sent = await sendMessage(text, { config: options?.config });

  if (sent) {
    recordSend(alertType);
  }

  return sent;
}

// ---------------------------------------------------------------------------
// HTML escaping helper
// ---------------------------------------------------------------------------

/**
 * Escape special characters for Telegram HTML parse mode.
 *
 * Telegram's HTML parser only recognises `<b>`, `<i>`, `<u>`, `<s>`,
 * `<a>`, `<code>`, `<pre>`, and `<tg-spoiler>` tags.  All other `<` / `>`
 * must be escaped to avoid parse errors.
 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

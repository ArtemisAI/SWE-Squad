/**
 * Tool: send_notification — Send alerts via the configured notification provider.
 *
 * Provider-agnostic: resolves the notification provider from context.
 */

import { defineTool } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import type { SWEContext } from "../shared/context.js";

export function createSendNotificationTool(ctx: SWEContext) {
  return defineTool({
    name: "send_notification",
    label: "Send Notification",
    description:
      "Send an alert or status notification via the configured provider " +
      "(telegram, slack, webhook). Respects rate limiting.",
    parameters: Type.Object({
      message: Type.String({ description: "The notification message to send" }),
      alertType: Type.Optional(
        Type.String({ description: "Alert type: info (default), warning, critical", default: "info" }),
      ),
      chatId: Type.Optional(
        Type.String({ description: "Override chat/channel ID" }),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _extCtx) {
      const notifier = ctx.notifier;
      if (!notifier) {
        const provider = ctx.config.notification.provider;
        if (provider === "none") {
          return {
            content: [{ type: "text" as const, text: "Notifications disabled (provider: none)" }],
            details: {},
          };
        }
        return {
          content: [{
            type: "text" as const,
            text: `Notification provider "${provider}" not configured. Set up the provider in SWEContext.`,
          }],
          details: {},
        };
      }

      try {
        const sent = await notifier.send(params.message, {
          alertType: params.alertType,
          chatId: params.chatId,
        });

        return {
          content: [{
            type: "text" as const,
            text: sent ? "Sent" : "Rate limited (cooldown active)",
          }],
          details: { sent },
        };
      } catch (err) {
        return {
          content: [{ type: "text" as const, text: `Send failed: ${err}` }],
          details: {},
        };
      }
    },
  });
}
